# ✨ Lyric Video

[![Language](https://img.shields.io/badge/Language-Python-blue)](https://www.python.org/)
[![Python Version](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Status](https://img.shields.io/badge/Status-Active-brightgreen)](#)

## 🎯 概要

**Lyric Video** は、MP3 からカラオケ字幕付き MP4 動画を生成する **オーケストレーター** です。GCS 上の音声・キーフレーム素材を受け取り、Whisper アライメント・字幕焼き込み・エンコードを Cloud Run 上で非同期実行します。

---

## 🏗 アーキテクチャ

```
ブラウザ（Google OAuth2 認証済み）
  │  GCS URL をフォームで入力
  ▼
Cloud Run (FastAPI)
  │  job_id を生成してキューに投入
  ▼
Cloud Tasks
  │  POST /tasks/generate を非同期呼び出し
  ▼
Cloud Run (同一サービス / worker エンドポイント)
  ├─ GCS から audio.mp3 / keyframes.zip をダウンロード
  ├─ Whisper でカラオケタイミング生成（ASS 未指定時）
  ├─ PIL で字幕を焼き込み MP4 生成
  └─ GCS へアップロード → Slack 通知
```

---

## ☁️ セットアップ

### 必要な環境変数

| 変数 | 必須 | 説明 |
|---|---|---|
| `GCP_PROJECT_ID` | ✅ | GCP プロジェクト ID |
| `GCS_BUCKET` | ✅ | 入出力に使う GCS バケット名 |
| `CLOUD_TASKS_QUEUE_ID` | ✅ | Cloud Tasks キュー ID |
| `SERVICE_ACCOUNT_EMAIL` | ✅ | OIDC 認証用サービスアカウント |
| `SERVICE_URL` | ✅ | Cloud Run サービスの URL |
| `GOOGLE_CLIENT_ID` | ✅ | Google OAuth2 クライアント ID |
| `GOOGLE_CLIENT_SECRET` | ✅ | Google OAuth2 クライアントシークレット |
| `SESSION_SECRET` | ✅ | セッション署名用シークレット（ランダム文字列） |
| `SLACK_WEBHOOK_URL` | ➖ | Slack Incoming Webhook URL |
| `GCP_LOCATION_ID` | ➖ | リージョン（省略時: `asia-northeast1`） |
| `GCS_OUTPUT_PREFIX` | ➖ | 出力先パスプレフィックス（省略時: `lyric-video/output`） |
| `WHISPER_MODEL` | ➖ | Whisper モデルサイズ（省略時: `large-v3`） |
| `ALLOWED_EMAILS` | ➖ | アクセス許可メールアドレス（カンマ区切り） |
| `ALLOWED_DOMAINS` | ➖ | アクセス許可ドメイン（カンマ区切り、例: `example.com`） |

### ローカル起動

```sh
pip install -r requirements.txt

export SESSION_SECRET=any-random-string
export GOOGLE_CLIENT_ID=...
export GOOGLE_CLIENT_SECRET=...

uvicorn app.main:app --reload --port 8080
# → http://localhost:8080
```

### Cloud Run へのデプロイ

```sh
# 1. Cloud Tasks キュー作成
gcloud tasks queues create lyric-video-queue --location=asia-northeast1

# 2. Cloud Build でビルド＆デプロイ
gcloud builds submit --config=cloudbuild.yaml \
  --substitutions=\
_GCS_BUCKET=your-bucket,\
_SERVICE_ACCOUNT_EMAIL=sa@project.iam.gserviceaccount.com,\
_GOOGLE_CLIENT_ID=your-client-id,\
_GOOGLE_CLIENT_SECRET=your-client-secret,\
_SESSION_SECRET=your-random-secret,\
_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

> **サービスアカウントの必要権限:**
> - `roles/storage.objectAdmin`（GCS 読み書き）
> - `roles/cloudtasks.enqueuer`（タスク投入）

---

## 🔌 エンドポイント

| メソッド | パス | 説明 |
|---|---|---|
| `GET` | `/` | 入力フォーム（要ログイン） |
| `POST` | `/` | タスクをキューに投入（要ログイン） |
| `GET` | `/auth/login` | Google OAuth2 ログイン |
| `GET` | `/auth/callback` | OAuth2 コールバック |
| `GET` | `/auth/logout` | ログアウト |
| `POST` | `/tasks/generate` | Cloud Tasks worker（OIDC 認証） |
| `GET` | `/healthz` | ヘルスチェック |

---

## 📦 入力 ZIP の構成

```
keyframes.zip
├── cut_01.png
├── cut_02.png
├── ...
├── inputs.txt       # 画像ファイル名と表示尺
└── subtitles.ass    # ASS カラオケ字幕
```

**inputs.txt の形式:**

```
file 'cut_01.png'
duration 40.000

file 'cut_02.png'
duration 50.000
```

---

## 🤝 依存関係

| パッケージ | 用途 |
|---|---|
| [stable-ts](https://github.com/jianfch/stable-ts) | Whisper による文字レベル音声アライメント |
| [Pillow](https://pillow.readthedocs.io/) | PNG 画像への字幕描画 |
| [pysubs2](https://pysubs2.readthedocs.io/) | ASS 字幕ファイルのパース |
| [ffmpeg](https://ffmpeg.org/) | 動画エンコード・音声合成 |
| [FastAPI](https://fastapi.tiangolo.com/) | Web サーバー |
| [Authlib](https://docs.authlib.org/) | Google OAuth2 認証 |
| [google-cloud-tasks](https://cloud.google.com/tasks) | 非同期タスクキュー |
| [google-cloud-storage](https://cloud.google.com/storage) | GCS ファイル入出力 |

---

## 📜 ライセンス

このプロジェクトは [MIT License](https://opensource.org/licenses/MIT) の下で公開されています。
