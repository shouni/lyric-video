# ✨ Lyric Video

[![CI](https://github.com/shouni/lyric-video/actions/workflows/ci.yml/badge.svg)](https://github.com/shouni/lyric-video/actions/workflows/ci.yml)
[![Language](https://img.shields.io/badge/Language-Python-blue)](https://www.python.org/)
[![Python Version](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Status](https://img.shields.io/badge/Status-Active-brightgreen)](#)

## 🎯 概要

**Lyric Video** は、MP3 からカラオケ字幕付き MP4 動画を生成し、YouTube へ自動投稿する **オーケストレーター** です。ap-mv（キーフレーム/Veo動画）・ap-music（楽曲）の Job ID を指定するだけで、GCS 上の素材を自動的に参照し、Whisper アライメント・字幕焼き込み・エンコードを Cloud Run 上で非同期実行します。生成した動画はジョブ詳細画面から GCS を経由して YouTube へ直接ストリーミングアップロードでき、削除済み動画の再アップロードにも対応しています。

ブラウザ単体でも完結しますが、OIDC によるマシン間認証と JSON API レスポンスに対応しており、MCP (Model Context Protocol) ゲートウェイ経由で AI エージェントから動画生成・YouTube 公開を操作できるように設計されています。

---

## 🏗 アーキテクチャ

```
ブラウザ（Google OAuth2 認証済み）
  │  /new フォームで MV Job ID / Music Job ID（+ 素材の種類）を入力
  ▼
Cloud Run (Flask)
  │  job_id を生成 → meta.json を GCS へ保存 → キューに投入
  ▼
Cloud Tasks
  │  POST /tasks/generate を非同期呼び出し
  ▼
Cloud Run (同一サービス / worker エンドポイント)
  │  Job ID から GCS パスを導出（AP_MV_BUCKET / AP_MUSIC_BUCKET）
  │
  ├─【素材の種類 = images】静止画スライドショー
  │   ├─ GCS から audio.mp3 / keyframes.zip をダウンロード
  │   ├─ Whisper でカラオケタイミング生成（ASS 未指定時）
  │   ├─ PIL で字幕を焼き込み MP4 生成
  │   └─ GCS へアップロード → meta.json 更新 → Slack 通知
  │
  └─【素材の種類 = video】ap-mv動画 + ap-music楽曲の合成
      ├─ ap-mvの完成動画（final_video_url、Veo生成）とap-musicの楽曲をダウンロード
      ├─ Whisperで実音声に強制アライメントしてカラオケ字幕を生成
      ├─ ffmpeg(libass)で動画に字幕を焼き込み・音声とmux
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
| `WHISPER_MODEL` | ➖ | Whisper モデルサイズ（省略時: `large-v3`）。`/new` フォームの初期値に使われる（`images` モード） |
| `VIDEO_WHISPER_MODEL` | ➖ | `video` モードのアライメントに使う Whisper モデル（省略時: `small`）。曲全体を1本で強制アライメントするため、Cloud Run のメモリ・処理時間に収まる小さめのモデルを既定にしている |
| `AP_MV_BUCKET` | ➖ | ap-mvの成果物（keyframes.zip / 完成動画・歌詞タイミング）を参照するGCSバケット名。images/video両モードで使用（省略時: `ap-mv`） |
| `AP_MUSIC_BUCKET` | ➖ | ap-musicの楽曲(mp3)を参照するGCSバケット名。images/video両モードで使用（省略時: `ap-music`） |
| `ALLOWED_EMAILS` | ➖ | アクセス許可メールアドレス（カンマ区切り）`ALLOWED_DOMAINS` との**どちらか必須** |
| `ALLOWED_DOMAINS` | ➖ | アクセス許可ドメイン（カンマ区切り、例: `example.com`）`ALLOWED_EMAILS` との**どちらか必須** |
| `ALLOWED_M2M_SERVICE_ACCOUNTS` | ➖ | ユーザー向けエンドポイントを OIDC Bearer トークン（`Authorization: Bearer <ID Token>`, audience=`SERVICE_URL`）で呼び出せるサービスアカウントのメールアドレス一覧（カンマ区切り）。MCP ゲートウェイなど他サービスからのマシン間呼び出し向け。未設定の場合、M2M呼び出しは常に拒否されます |
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

### サービスアカウントの必要権限

**web と worker で実行 SA を分けており、必要な権限も異なります。**
ロール名は環境によって粒度が変わるので、何ができる必要があるかだけ挙げます。

| | web | worker |
| --- | --- | --- |
| `GCS_BUCKET`（成果物） | 読み書き・削除 | 読み書き |
| `AP_MUSIC_BUCKET` / `AP_MV_BUCKET` | **不要**（job_id をタスクに載せるだけ） | **読み取りのみ**（素材を読むのはこちら） |
| Cloud Tasks への投入と ActAs | 必要 | 不要（タスクを積まない） |
| 署名付き URL の生成 | 必要 | 不要 |
| worker サービスの呼び出し | 必要 | 不要 |

**シークレットも役割ごとに分かれます。** Cloud Run はシークレット env を起動時に解決するため、
env に参照が残っていると使っていなくてもアクセス権が要ります。IAM だけ絞っても分離になりません。

| | web | worker |
| --- | --- | --- |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | 要（OAuth・YouTube） | 要（YouTube アップロード） |
| `YOUTUBE_REFRESH_TOKEN` | 要（動画の削除） | 要（アップロード） |
| `SESSION_SECRET` | 要 | **不要**（`SERVER_ROLE=worker` は OAuth もセッションも使わない） |
| `SLACK_WEBHOOK_URL` | **不要**（通知するのは worker だけ） | 要 |

> **`SERVICE_ACCOUNT_EMAIL` は「呼び出し元」を指します。** `verify_task_caller` の第 3 引数として
> 使われるため、**worker 側には worker 自身ではなく web の SA** を設定してください。

> **素材バケットは読み取りだけで足ります。** `storage.objectAdmin` は使わないでください
> （オブジェクト ACL の操作まで許します）。削除を含まない読み取り専用ロールを選んでください。

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

「要ログイン」のエンドポイントは、ブラウザセッション（Cookie + CSRF）に加えて、`ALLOWED_M2M_SERVICE_ACCOUNTS` に登録済みのサービスアカウントが発行した OIDC Bearer トークン（`Authorization: Bearer <ID Token>`, audience=`SERVICE_URL`）でも認証できます。M2M 認証済みリクエストは CSRF 検証をバイパスします。`POST /new`・`GET /jobs`・`GET /jobs/<job_id>`・`POST /jobs/<job_id>/youtube` は、`Accept: application/json` を付けると HTML の代わりに JSON を返します（MCP ゲートウェイなど他サービスからのマシン間呼び出し向け）。

---

## 🎬 素材の種類（`source_type`）

`/new` フォームでは、生成する MV の素材を2種類から選べます。どちらも入力は共通で **MV Job ID** と **Music Job ID** の2つだけです（GCS URL を直接入力する必要はありません。パスは `AP_MV_BUCKET` / `AP_MUSIC_BUCKET` から自動的に組み立てられます）。

| `source_type` | 素材 | 説明 |
|---|---|---|
| `images`（デフォルト） | ap-mv の `keyframes.zip`（静止画） + ap-music の楽曲 | 既存の静止画スライドショー方式。`subs_url`（省略可）・`whisper_model` を追加指定できる |
| `video` | ap-mv の完成動画（Veo生成） + ap-music の楽曲 | Veo で生成済みの実写アニメーション動画に、Whisper で実音声へ強制アライメントしたカラオケ字幕を焼き込む。指定した MV Job ID に含まれるカット全体が対象になるため、1セクション分のジョブでも曲全体を通しで作るジョブでも同じ仕組みで扱える |

`video` モードでは、ap-mv の `cuts[].start_sec`/`end_sec` は演出上のカット割り当てであり実際の歌唱タイミングと一致しないため使用しない。必ず実音声を Whisper で強制アライメントしてタイミングを求める（`app/video_merge.py`）。

ap-mv 側は Veo の video_extension（継続生成）が受け付けられる累積尺の上限（30秒）に達する手前で継続チェーンを自動的にリセットすることがあるが（`internal/worker/filter/veo_cut_utils.go` の `veoContinuationMaxDurationSec`）、複数チェーンの検出・フレーム引き継ぎ・ハードカット結合は ap-mv 側（`internal/worker/filter/chain_finalize.go`）で完結しており、結果は `video_music_meta.json` の `final_video_url` に1本化された完成動画として記録される。`video` モード（`app/video_merge.py`）はこの `final_video_url` をそのままダウンロードするだけで、チェーンの本数や境目を意識する必要はない（`final_video_url` が無い旧ジョブに対しては、後方互換として最後のカットの動画を使うフォールバックのみ持つ）。

---

## 📦 入力 ZIP の構成（`images` モード）

ap-mv が生成する `keyframes.zip`（`AP_MV_BUCKET` 配下）の内部構成です。`video` モードではこの ZIP は使用しません。

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

## 🧪 開発・テスト

```sh
pip install -r requirements-dev.txt

ruff check . && ruff format --check .
SESSION_SECRET=dummy pytest -q
```

テストは外部サービス（GCS / Cloud Tasks / YouTube / Whisper / ffmpeg）へ接続しません。ドメインモデル・設定・OIDC 検証・`meta.json` の読み書き・字幕の折り返しといった純粋ロジックと、Flask テストクライアントによる認証まわりの配線確認を対象にしています。CI（`.github/workflows/ci.yml`）でも同じコマンドが実行されます。

---

## 📜 ライセンス

このプロジェクトは [MIT License](https://opensource.org/licenses/MIT) の下で公開されています。
