# ✨ Lyric Video

[![Language](https://img.shields.io/badge/Language-Python-blue)](https://www.python.org/)
[![Python Version](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Status](https://img.shields.io/badge/Status-Active-brightgreen)](#)

## 🎯 概要

**Lyric Video** は、MP3 音声ファイルとキーフレーム ZIP ファイルから **カラオケ字幕付き MP4 動画** を生成するツールです。

ローカルで直接実行する **CLI モード** と、Google Cloud 上で動作する **Web アプリモード** の 2 通りで使えます。

---

## 🏗 アーキテクチャ（Web アプリモード）

```
ブラウザ
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
  ├─ align_subtitles.py でカラオケタイミング生成（省略可）
  ├─ burn_subs.py で MP4 生成
  └─ GCS へアップロード → Slack 通知
```

---

## 💎 特徴と設計思想

### 🎤 Whisper による文字レベル自動アライメント（align_subtitles.py）

- [stable-ts](https://github.com/jianfch/stable-ts) を使って Whisper の推論結果を文字レベルに分解します。
- ZIP 内の ASS から歌詞テキストだけを抽出して Whisper にアライメントさせ、句読点・記号は直前の文字の `\k` に吸収させます。
- 出力は pysubs2 互換の ASS ファイルで、スタイル情報は元ファイルから引き継ぎます。
- **歌い出し対応**: 1行目のみ、元 ASS の開始時刻が Whisper の判定より **0〜3秒だけ早い**場合に限り、元 ASS の開始時刻を採用します。
- **繰り返し歌唱対応**: 同じ歌詞を繰り返す場合、各行の終了時刻を次行開始直前まで延長します。

### 🎤 ASS カラオケ字幕の完全再現（burn_subs.py）

- `\k` タグを解析し、文字単位でハイライト色（黄）と待機色（白）を切り替えます。
- **フォント**: macOS 標準の **ヒラギノ角ゴシック W7** を最優先で使用します。Linux (Docker) 環境では **Noto Sans CJK** にフォールバックします。

### ⚡ 差分描画による高速処理

- 字幕状態が変化するタイミングだけ描画するため、3 分の動画でも約 200 枚で済み、数十秒で完了します。

---

## 🚀 クイックスタート（CLI モード）

### 依存パッケージのインストール

```sh
pip install -r requirements.txt
```

> ffmpeg が別途必要です。
> ```sh
> brew install ffmpeg   # macOS
> ```

### 実行

**精度重視（Whisper アライメントあり）:**

```sh
# ① タイミング生成
python3 app/align_subtitles.py input/audio.mp3 input/keyframes.zip input/subtitles_aligned.ass

# ② 動画生成
python3 app/burn_subs.py input/audio.mp3 input/keyframes.zip output/output.mp4 --subs input/subtitles_aligned.ass
```

**簡易版（ZIP 内の ASS タイミングをそのまま使う）:**

```sh
python3 app/burn_subs.py input/audio.mp3 input/keyframes.zip output/output.mp4
```

---

## ☁️ Web アプリモード（Cloud Run）

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
uvicorn app.main:app --reload
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
_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

> **サービスアカウントの必要権限:**
> - `roles/storage.objectAdmin`（GCS 読み書き）
> - `roles/cloudtasks.enqueuer`（タスク投入）

### エンドポイント

| メソッド | パス | 説明 |
|---|---|---|
| `GET` | `/` | 入力フォーム |
| `POST` | `/` | タスクをキューに投入 |
| `POST` | `/tasks/generate` | Cloud Tasks からの worker 呼び出し（OIDC 認証） |
| `GET` | `/healthz` | ヘルスチェック |

---

## ⚙️ CLI 引数

### align_subtitles.py

| 引数 | 必須 | 説明 |
|---|---|---|
| `audio.mp3` | ✅ | アライメント対象の音声ファイル |
| `keyframes.zip` または `subtitles.ass` | ✅ | ZIP を渡すと内部の `subtitles.ass` を自動抽出 |
| `output.ass` | ➖ | 出力 ASS ファイル名（省略時: `subtitles_aligned.ass`） |
| `--model` | ➖ | Whisper モデルサイズ（省略時: `large-v3`） |

### burn_subs.py

| 引数 | 必須 | 説明 |
|---|---|---|
| `audio.mp3` | ✅ | BGM として使用する音声ファイル |
| `keyframes.zip` | ✅ | PNG 画像・`inputs.txt`・`subtitles.ass` を含む ZIP |
| `output.mp4` | ➖ | 出力ファイル名（省略時: `output.mp4`） |
| `--subs <subtitles.ass>` | ➖ | ZIP 内の字幕を上書きする ASS ファイル |

---

## 📦 ZIP ファイルの構成

```
keyframes.zip
├── cut_01.png
├── cut_02.png
├── ...
├── inputs.txt       # 画像ファイル名と表示尺の定義
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
| [google-cloud-tasks](https://cloud.google.com/tasks) | 非同期タスクキュー |
| [google-cloud-storage](https://cloud.google.com/storage) | GCS ファイル入出力 |

---

## 📜 ライセンス

このプロジェクトは [MIT License](https://opensource.org/licenses/MIT) の下で公開されています。
