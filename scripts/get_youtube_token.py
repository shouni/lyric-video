#!/usr/bin/env python3
"""
YouTube OAuth2 refresh token を取得し、Secret Manager に新しいバージョンとして追加する。
ローカルで、トークンを作り直したいときだけ実行する。

事前準備:
  仮想環境で依存を入れる（本番イメージには入らない開発用の依存）。
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements-dev.txt

使い方:
  export GOOGLE_CLIENT_ID=<your_client_id>
  export GOOGLE_CLIENT_SECRET=<your_client_secret>
  python scripts/get_youtube_token.py

ブラウザが開くので、アップロード先の YouTube チャンネルの Google アカウントでログインして承認する。

書き込み先のプロジェクトは ADC の既定（GOOGLE_CLOUD_PROJECT があればそちら）。
シークレット名は 1 つしかないので固定してある。

**トークンは画面に出さない。** 端末の履歴やスクロールバックへ残さずに済ませるためで、
取得した値を手で貼り付けていた頃より安全になる点でもある。
"""

import os
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("Error: google-auth-oauthlib が見つかりません。", file=sys.stderr)
    print("  pip install -r requirements-dev.txt （仮想環境を有効にしてから）", file=sys.stderr)
    sys.exit(1)

_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

# Secret Manager 上のシークレット名。シークレット自体の作成は ap-infra の担当で、
# ここが行うのは新しいバージョンの追加だけ。
_SECRET_NAME = "YOUTUBE_REFRESH_TOKEN"


def _mint_credentials(client_id: str, client_secret: str):
    """同意フローを回してリフレッシュトークン付きの資格情報を返す。"""
    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost:8080"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, scopes=_SCOPES)

    # 認可 URL は run_local_server が作り直すので捨てる。ここで一度呼ぶのは、
    # この呼び出しで redirect_uri が確定するため。Console 側の登録とズレていると
    # 承認後に redirect_uri_mismatch で失敗するので、先に実際の値を出しておく。
    flow.authorization_url(prompt="consent", access_type="offline")
    print(f"\nDEBUG redirect_uri: {flow.redirect_uri}")
    print("ブラウザが開きます。YouTube チャンネルのアカウントでログインして承認してください。")

    return flow.run_local_server(port=8080, prompt="consent", access_type="offline")


def _verify_granted_scopes(credentials) -> None:
    """要求したスコープが実際に付与されたかを確かめる。

    同意画面はスコープごとにチェックを外せる。外したまま承認すると、
    アップロードは通るのにコメント投稿だけ 403 になる、といった分かりにくい
    壊れ方をする。oauthlib も食い違いを検出して例外を投げるが、こちらは
    何が付いたのかを目で確かめられるようにするためのもの。
    """
    granted = set(getattr(credentials, "granted_scopes", None) or [])
    if not granted:
        return

    missing = [scope for scope in _SCOPES if scope not in granted]
    if missing:
        print("\nError: 要求したスコープの一部が付与されていません。", file=sys.stderr)
        for scope in missing:
            print(f"  未付与: {scope}", file=sys.stderr)
        print("同意画面でチェックを外さずにやり直してください。", file=sys.stderr)
        sys.exit(1)

    print("\n付与されたスコープ:")
    for scope in sorted(granted):
        print(f"  {scope}")


def _resolve_project() -> str:
    """書き込み先のプロジェクトを決める。環境変数が無ければ ADC の既定を使う。"""
    env_project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    if env_project:
        return env_project

    try:
        import google.auth

        _, adc_project = google.auth.default()
    except Exception:  # noqa: BLE001 - ADC が無い場合も含めて下のメッセージで案内する
        adc_project = None

    if not adc_project:
        print("Error: プロジェクトを特定できません。", file=sys.stderr)
        print("  gcloud auth application-default login", file=sys.stderr)
        print("  もしくは GOOGLE_CLOUD_PROJECT を設定してください。", file=sys.stderr)
        sys.exit(1)
    return adc_project


def _add_secret_version(project: str, token: str) -> str:
    """新しいバージョンを追加し、そのバージョン番号を返す。

    古いバージョンには触らない。latest は最新のものを指すので放っておいても
    読まれることはなく、整理が要るなら `gcloud secrets versions destroy` で
    別途行う（ap-mcp の rotate_mcp_key.sh も同じ扱い）。
    """
    try:
        from google.api_core import exceptions as api_exceptions
        from google.cloud import secretmanager
    except ImportError:
        print("Error: google-cloud-secret-manager が見つかりません。", file=sys.stderr)
        print("  pip install -r requirements-dev.txt （仮想環境を有効にしてから）", file=sys.stderr)
        sys.exit(1)

    client = secretmanager.SecretManagerServiceClient()
    parent = client.secret_path(project, _SECRET_NAME)

    try:
        version = client.add_secret_version(request={"parent": parent, "payload": {"data": token.encode("utf-8")}})
    except api_exceptions.NotFound:
        print(f"\nError: シークレット {_SECRET_NAME} が {project} にありません。", file=sys.stderr)
        print("シークレット自体の作成は ap-infra の担当です。", file=sys.stderr)
        sys.exit(1)
    except api_exceptions.PermissionDenied:
        print("\nError: シークレットへの書き込み権限がありません。", file=sys.stderr)
        print("roles/secretmanager.secretVersionAdder が要ります。", file=sys.stderr)
        sys.exit(1)

    return version.name.rsplit("/", 1)[-1]


def main() -> None:
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        print("Error: 環境変数 GOOGLE_CLIENT_ID と GOOGLE_CLIENT_SECRET を設定してください。", file=sys.stderr)
        sys.exit(1)

    # 書き込み先は同意フローより先に決める。承認まで済ませてから
    # 「プロジェクトが分かりません」で止まると、取り直しになる。
    project = _resolve_project()

    credentials = _mint_credentials(client_id, client_secret)

    if not credentials.refresh_token:
        print("\nError: refresh_token が取得できませんでした。", file=sys.stderr)
        print("Google Cloud Console でアプリのアクセス権を一度削除してから再実行してください。", file=sys.stderr)
        print("  https://myaccount.google.com/permissions", file=sys.stderr)
        sys.exit(1)

    _verify_granted_scopes(credentials)

    version = _add_secret_version(project, credentials.refresh_token)

    # バージョン番号は残す。あとから `gcloud secrets versions list` の作成時刻と
    # 突き合わせるとき、どの実行がどれを作ったかはこれでしか辿れない。
    print(f"\n{_SECRET_NAME} にバージョン {version} を追加しました（{project}）")


if __name__ == "__main__":
    main()
