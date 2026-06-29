# ✨ Lyric Video

[![Language](https://img.shields.io/badge/Language-Python-blue)](https://www.python.org/)
[![Python Version](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Status](https://img.shields.io/badge/Status-Active-brightgreen)](#)

## 🎯 概要

**Lyric Video** は、MP3 からカラオケ字幕付き MP4 動画を生成し、YouTube へ自動投稿する **オーケストレーター** です。GCS 上の音声・キーフレーム素材を受け取り、Whisper アライメント・字幕焼き込み・エンコードを Cloud Run 上で非同期実行します。生成した動画はジョブ詳細画面から GCS を経由して YouTube へ直接ストリーミングアップロードでき、削除済み動画の再アップロードにも対応しています。

---

## 🏗 アーキテクチャ

```
ブラウザ（Google OAuth2 認証済み）
  │  /new フォームで GCS URL を入力
  ▼
Cloud Run (Flask)
  │  job_id を生成 → meta.json を GCS へ保存 → キューに投入
  ▼
Cloud Tasks
  │  POST /tasks/generate を非同期呼び出し
  ▼
Cloud Run (同一サービス / worker エンドポイント)
  ├─ GCS から audio.mp3 / keyframes.zip をダウンロード
  ├─ Whisper でカラオケタイミング生成（ASS 未指定時）
  ├─ PIL で字幕を焼き込み MP4 生成
  └─ GCS へアップロード → meta.json 更新 → Slack 通知

【オプション】YouTube 公開（動画確認後に手動トリガー）
  │  ジョブ詳細画面の「YouTube に公開」フォームを送信
  ▼
Cloud Run (Flask)
  │  meta.json のステータスを queued に更新 → キューに投入
  ▼
Cloud Tasks
  │  POST /tasks/youtube を非同期呼び出し
  ▼
Cloud Run (同一サービス / worker エンドポイント)
  ├─ YouTube API で動画の存在確認（削除済みなら meta をリセットして再アップロード）
  └─ GCS からストリーム → YouTube Data API でアップロード
     → タイトルにサフィックス自動付与・固定タグ追加
     → meta.json に youtube_url 保存 → Slack 通知
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
| `ALLOWED_EMAILS` | ➖ | アクセス許可メールアドレス（カンマ区切り）`ALLOWED_DOMAINS` との**どちらか必須** |
| `ALLOWED_DOMAINS` | ➖ | アクセス許可ドメイン（カンマ区切り、例: `example.com`）`ALLOWED_EMAILS` との**どちらか必須** |
| `YOUTUBE_REFRESH_TOKEN` | ➖ | YouTube OAuth2 リフレッシュトークン（`scripts/get_youtube_token.py` で取得）。`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` を共用。必要スコープ: `youtube.upload` / `youtube.readonly` / `youtube.force-ssl` |

### YouTube リフレッシュトークンの取得

YouTube への投稿機能を使うには、事前にリフレッシュトークンを取得して Cloud Run に設定する必要があります。

**1. Google Cloud Console の設定**

- **OAuth 同意画面** にスコープを追加: `youtube.upload` / `youtube.readonly` / `youtube.force-ssl`
- **認証情報** の OAuth クライアント（ウェブ アプリケーション タイプ）に `http://localhost:8080/` をリダイレクト URI として追加

**2. トークン取得**

```sh
export GOOGLE_CLIENT_ID=your-client-id
export GOOGLE_CLIENT_SECRET=your-client-secret
python3 scripts/get_youtube_token.py
```

ブラウザが開くので YouTube チャンネルのアカウントでログインして承認すると、`YOUTUBE_REFRESH_TOKEN` が出力されます。

**3. Cloud Run に設定**

```sh
gcloud run services update lyric-video \
  --region asia-northeast1 \
  --set-env-vars "YOUTUBE_REFRESH_TOKEN=your-refresh-token"
```

> **注意:** トークン取得後は `http://localhost:8080/` をリダイレクト URI から削除することを推奨します。

> 🔒 **セキュリティ推奨事項:** 本番環境では `YOUTUBE_REFRESH_TOKEN` や `GOOGLE_CLIENT_SECRET` などの機密情報を [Google Cloud Secret Manager](https://cloud.google.com/secret-manager) に保存し、Cloud Run からシークレットとして参照することを推奨します。

---

### Cloud Run へのデプロイ

```sh
# 1. Cloud Tasks キュー作成
gcloud tasks queues create lyric-video-queue --location=asia-northeast1

# 2. Cloud Run に環境変数を設定（初回のみ）
gcloud run services update lyric-video \
  --region asia-northeast1 \
  --set-env-vars "\
GCS_BUCKET=your-bucket,\
CLOUD_TASKS_QUEUE_ID=lyric-video-queue,\
SERVICE_ACCOUNT_EMAIL=sa@project.iam.gserviceaccount.com,\
SERVICE_URL=https://your-service-url,\
GOOGLE_CLIENT_ID=your-client-id,\
GOOGLE_CLIENT_SECRET=your-client-secret,\
SESSION_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))'),\
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...,\
ALLOWED_EMAILS=you@example.com,\
YOUTUBE_REFRESH_TOKEN=your-refresh-token"

# 3. Cloud Build でビルド＆デプロイ
gcloud builds submit --config=cloudbuild.yaml
```

> **注意:** 環境変数は Cloud Run に直接設定します。`cloudbuild.yaml` はビルド・デプロイのみを行い、秘密情報は上書きしません。

> 🔒 **セキュリティ推奨事項:** `GOOGLE_CLIENT_SECRET`・`SESSION_SECRET`・`YOUTUBE_REFRESH_TOKEN` などの機密情報は、本番環境では [Google Cloud Secret Manager](https://cloud.google.com/secret-manager) で管理することを推奨します。

> **サービスアカウントの必要権限:**
> - `roles/storage.objectAdmin`（GCS 読み書き）
> - `roles/cloudtasks.enqueuer`（タスク投入）

---

## 🔌 エンドポイント

| メソッド | パス | 説明 |
|---|---|---|
| `GET` | `/` | ホーム（最新5件表示、要ログイン） |
| `GET` | `/new` | 入力フォーム（要ログイン） |
| `POST` | `/new` | タスクをキューに投入（要ログイン） |
| `GET` | `/jobs` | ジョブ履歴一覧（要ログイン） |
| `GET` | `/jobs/<job_id>` | ジョブ詳細・動画プレーヤー（要ログイン） |
| `DELETE` | `/jobs/<job_id>` | ジョブ削除・GCS ファイル削除（要ログイン） |
| `POST` | `/jobs/<job_id>/youtube` | YouTube 公開タスクをキューに投入（要ログイン） |
| `GET` | `/auth/login` | Google OAuth2 ログイン |
| `GET` | `/auth/callback` | OAuth2 コールバック |
| `GET` | `/auth/logout` | ログアウト |
| `POST` | `/tasks/generate` | Cloud Tasks worker — 動画生成（OIDC 認証） |
| `POST` | `/tasks/youtube` | Cloud Tasks worker — YouTube アップロード（OIDC 認証） |
| `GET` | `/healthz` | ヘルスチェック |

---

## 📦 入力 ZIP の構成

```
keyframes.zip
├── cut_01.png
├── cut_02.png
├── ...
├── inputs.txt       # 画像ファイル名と表示尺
└── subtitles.ass    # ASS 字幕（カラオケタイミングなしでも可）
```

**inputs.txt の形式:**

```
file 'cut_01.png'
duration 40.000

file 'cut_02.png'
duration 50.000
```

**subtitles.ass の形式:**

歌詞行は **1行ずつ別の `Dialogue` イベント** に分割する必要があります。`\N` で複数行を1イベントにまとめると、Whisper アライメントが正しく機能しません。

```ass
[V4+ Styles]
Style: Karaoke,Arial,64,&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,...
;                        ↑黄（歌唱済み）  ↑白（未歌唱）

[Events]
Dialogue: 0,0:00:10.00,0:00:17.00,Karaoke,,0,0,0,,1行目の歌詞
Dialogue: 0,0:00:17.00,0:00:22.00,Karaoke,,0,0,0,,2行目の歌詞
```

| 項目 | 説明 |
|---|---|
| スタイル名 | `Karaoke`（必須） |
| PrimaryColour | 歌唱済み文字の色（ASS 形式 `&HAABBGGRR`）。黄色: `&H0000FFFF` |
| SecondaryColour | 未歌唱文字の色。白: `&H00FFFFFF` |
| `\k` タグ | 不要。`align_subtitles.py` が Whisper で自動生成する |

---

## 🤝 依存関係

| パッケージ | 用途 |
|---|---|
| [stable-ts](https://github.com/jianfch/stable-ts) | Whisper による文字レベル音声アライメント |
| [Pillow](https://pillow.readthedocs.io/) | PNG 画像への字幕描画 |
| [pysubs2](https://pysubs2.readthedocs.io/) | ASS 字幕ファイルのパース |
| [ffmpeg](https://ffmpeg.org/) | 動画エンコード・音声合成 |
| [Flask](https://flask.palletsprojects.com/) | Web サーバー |
| [gunicorn](https://gunicorn.org/) | 本番 WSGI サーバー |
| [Authlib](https://docs.authlib.org/) | Google OAuth2 認証 |
| [google-cloud-tasks](https://cloud.google.com/tasks) | 非同期タスクキュー |
| [google-cloud-storage](https://cloud.google.com/storage) | GCS ファイル入出力 |
| [google-api-python-client](https://github.com/googleapis/google-api-python-client) | YouTube Data API v3 |

---

## 📜 ライセンス

このプロジェクトは [MIT License](https://opensource.org/licenses/MIT) の下で公開されています。
