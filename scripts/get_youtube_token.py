#!/usr/bin/env python3
"""
YouTube OAuth2 refresh token を取得するスクリプト。
Cloud Run にデプロイする前に、ローカルで一度だけ実行する。

事前準備:
  pip install google-auth-oauthlib

使い方:
  export GOOGLE_CLIENT_ID=<your_client_id>
  export GOOGLE_CLIENT_SECRET=<your_client_secret>
  python scripts/get_youtube_token.py

ブラウザが開くので、アップロード先の YouTube チャンネルの Google アカウントでログインして承認する。
承認後、YOUTUBE_REFRESH_TOKEN が出力される。この値を Cloud Run の環境変数に設定する。
"""

import os
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("Error: google-auth-oauthlib が見つかりません。", file=sys.stderr)
    print("  pip install google-auth-oauthlib", file=sys.stderr)
    sys.exit(1)

_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def main() -> None:
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        print("Error: 環境変数 GOOGLE_CLIENT_ID と GOOGLE_CLIENT_SECRET を設定してください。", file=sys.stderr)
        sys.exit(1)

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

    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    print(f"\nDEBUG redirect_uri: {flow.redirect_uri}")
    print("ブラウザが開きます。YouTube チャンネルのアカウントでログインして承認してください。")
    credentials = flow.run_local_server(port=8080, prompt="consent", access_type="offline")

    if not credentials.refresh_token:
        print("\nError: refresh_token が取得できませんでした。", file=sys.stderr)
        print("Google Cloud Console でアプリのアクセス権を一度削除してから再実行してください。", file=sys.stderr)
        print("  https://myaccount.google.com/permissions", file=sys.stderr)
        sys.exit(1)

    print("\n" + "=" * 50)
    print("取得成功。以下の値を Cloud Run の環境変数に設定してください。")
    print("=" * 50)
    print(f"YOUTUBE_REFRESH_TOKEN={credentials.refresh_token}")
    print()


if __name__ == "__main__":
    main()
