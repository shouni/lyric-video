# CLAUDE.md

このファイルは、このリポジトリで Claude Code (claude.ai/code) が作業する際のガイダンスを提供する。

## 概要

**Lyric Video** は、MP3 からカラオケ字幕付き MP4 動画を生成し、YouTube へ自動投稿するオーケストレーター。GCS 上の音声・キーフレーム素材を Cloud Run 上で非同期処理する。詳細な仕様・環境変数・エンドポイント一覧は README.md を参照。

## アーキテクチャ

Flask アプリ（`app/main.py`）+ Cloud Tasks による非同期ワーカー構成。

```
app/
├── main.py              # Flaskエントリポイント（create_app, Blueprint登録, 認証ミドルウェア）
├── config.py             # 環境変数からのConfig読込（Config.from_env）
├── auth.py                # Google OAuth2ログイン（Authlib）
├── align_subtitles.py    # Whisperによるカラオケタイミング生成
├── burn_subs.py           # PILによる字幕焼き込み・動画エンコード
├── adapters/               # 外部サービス連携（GCS, Slack, Cloud Tasks, YouTube）
├── domain/                 # ドメインモデル（Job, Task, YoutubeTask）
├── handlers/
│   ├── web.py              # ユーザー向けエンドポイント（/, /new, /jobs, ...)
│   └── worker.py           # Cloud Tasksワーカー（/tasks/generate, /tasks/youtube, OIDC認証）
└── pipeline/runner.py      # align_subtitles.py / burn_subs.py をsubprocess実行しGCSへアップロード
```

フローは2段階の非同期処理:
1. `/new` でジョブ投入 → Cloud Tasks → `/tasks/generate`（動画生成）
2. ジョブ詳細画面から手動トリガー → Cloud Tasks → `/tasks/youtube`（YouTube公開）

## 開発コマンド

```sh
# ローカル起動（要環境変数、README.md参照）
python3 -m app.main

# Dockerビルド
docker build -t lyric-video .

# Cloud Buildデプロイ
gcloud builds submit --config=cloudbuild.yaml
```

lint/format には ruff を導入済み（`pyproject.toml`、CI: `.github/workflows/ci.yml`）。テストコードは存在しない。変更後は以下で確認する。

```sh
ruff check .
ruff format --check .
python3 -c "import app.main"
```

CI は Python 3.11（Dockerfile と同じ）で実行される。ローカルの `.venv` が古い Python バージョンの場合、`datetime.UTC` 等 3.11 以降の構文で import エラーになることがあるが、CI/本番の 3.11 環境では問題ない。

## 規約・注意点

- 関数・メソッドには「何を」ではなく設計判断の理由が要る場合のみ日本語のdocstring/コメントを付ける（既存コードの慣習に合わせる）。
- 秘密情報（`SESSION_SECRET`, `GOOGLE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` 等）は環境変数経由。本番はSecret Manager推奨（コード内にハードコードしない）。
- `app/handlers/worker.py` の `/tasks/*` エンドポイントはOIDC検証で保護されたCloud Tasks専用。ユーザー向けエンドポイントと混同しない。
- ユーザー向けエンドポイント（`/new`, `/jobs`, ...）は、ブラウザセッションに加えて `ALLOWED_M2M_SERVICE_ACCOUNTS` 経由のOIDC M2M認証（`app/adapters/m2m.py`）も受け付ける。M2M認証済みリクエストはCSRF検証をバイパスし、`Accept: application/json` を付けるとJSONを返す（MCPゲートウェイからの呼び出し用）。ap-comp の `/web/*` パターンと同じ設計。
- GCS入出力パスやWhisperモデル等のデフォルト値は `app/config.py` の `Config.from_env` に集約されている。
