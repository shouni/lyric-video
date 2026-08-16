# ✨ Lyric Video

[![CI](https://github.com/shouni/lyric-video/actions/workflows/ci.yml/badge.svg)](https://github.com/shouni/lyric-video/actions/workflows/ci.yml)
[![Language](https://img.shields.io/badge/Language-Python-blue)](https://www.python.org/)
[![Python Version](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Status](https://img.shields.io/badge/Status-Active-brightgreen)](#)

## 🎯 概要

**Lyric Video** は、カラオケ字幕付き MP4 を生成して YouTube へ投稿する **オーケストレーター** です。

ap-mv（キーフレーム / Veo 動画）と ap-music（楽曲）の **Job ID を 2 つ指定するだけ**で、
GCS 上の素材を自動的に参照し、Whisper アライメント・字幕焼き込み・エンコードを
Cloud Run 上で非同期実行します。生成した動画はジョブ詳細画面から YouTube へ
ストリーミングアップロードでき、削除済み動画の再アップロードにも対応しています。

ブラウザ単体で完結しますが、OIDC によるマシン間認証と JSON レスポンスにも対応しており、
MCP ゲートウェイ経由で AI エージェントからも操作できます（「エンドポイント」参照）。

---

## 🏗 アーキテクチャ

**本番は 2 つの Cloud Run サービスに分かれています。** `SERVER_ROLE` が役割を決め、
担当しない面は Blueprint ごと登録されません（`app/main.py`）。

| サービス | `SERVER_ROLE` | 公開 | 役割 |
|---|---|---|---|
| `lyric-video` | `web` | 公開（OAuth / M2M OIDC） | 画面とタスク投入 |
| `lyric-video-worker` | `worker` | 非公開（`ingress=internal`） | 動画生成と YouTube 投稿 |

**イメージも 2 本に分かれています。** `Dockerfile` がマルチステージで `web` / `worker` に
分かれ、worker ステージだけが `requirements-worker.txt`（stable-ts / torch）と Whisper の
モデル重みを焼き込みます。`stable_whisper` を import するのは別プロセスで動く
`align_subtitles.py` だけなので、web には torch もモデルも要りません。
1 イメージにまとめると、公開側がコールドスタートのたびに数 GB を引くことになります。

```
ブラウザ（Google OAuth2 認証済み） / MCP ゲートウェイ（OIDC）
  │  /new に MV Job ID・Music Job ID・素材の種類を渡す
  ▼
lyric-video（web）
  │  job_id を生成 → meta.json を GCS へ保存 → キューに投入
  ▼
Cloud Tasks（lyric-video-queue、max_attempts = 1）
  │  POST /tasks/generate を OIDC 付きで非同期呼び出し
  ▼
lyric-video-worker
  │  Job ID から GCS パスを導出（AP_MV_BUCKET / AP_MUSIC_BUCKET）
  │
  ├─【images】静止画スライドショー
  │   audio.mp3 + keyframes.zip を取得 → Whisper でカラオケタイミング生成
  │   → PIL で字幕を焼き込み MP4 化
  │
  └─【video】ap-mv の完成動画 + ap-music の楽曲を合成
      final_video_url と楽曲を取得 → Whisper で実音声へ強制アライメント
      → ffmpeg(libass) で焼き込み・音声と mux
  │
  └─→ GCS へアップロード → meta.json 更新 → Slack 通知

【オプション】YouTube 公開（動画を確認してから手動でトリガー）
  ジョブ詳細画面 → lyric-video（web）がキューに投入
    → lyric-video-worker が POST /tasks/youtube を受ける
    → 動画の存在確認（削除済みなら meta をリセットして再アップロード）
    → GCS からストリームして YouTube Data API へアップロード
    → meta.json に youtube_url を保存 → Slack 通知
```

---

## 🔌 エンドポイント

| メソッド | パス | 面 | 説明 |
|---|---|---|---|
| `GET` | `/` | web | ホーム（最新 5 件） |
| `GET` `POST` | `/new` | web | 入力フォーム / タスク投入 |
| `GET` | `/jobs` | web | ジョブ履歴一覧 |
| `GET` `DELETE` | `/jobs/<job_id>` | web | 詳細・動画プレーヤー / ジョブと GCS ファイルの削除 |
| `POST` | `/jobs/<job_id>/youtube` | web | YouTube 公開タスクを投入 |
| `DELETE` | `/jobs/<job_id>/youtube` | web | YouTube 動画を削除し、`meta.json` の YouTube 情報をクリア |
| `GET` | `/auth/login` `/auth/callback` `/auth/logout` | web | Google OAuth2 |
| `POST` | `/tasks/generate` | worker | 動画生成（OIDC 検証） |
| `POST` | `/tasks/youtube` | worker | YouTube アップロード（OIDC 検証） |
| `GET` | `/healthz` | 両方 | ヘルスチェック（認証不要） |

web 面のエンドポイントは、ブラウザセッション（Cookie + CSRF）に加えて
**`ALLOWED_M2M_SERVICE_ACCOUNTS` に登録済みの SA が発行した OIDC Bearer トークン**
（`Authorization: Bearer <ID Token>`, audience = `SERVICE_URL`）でも認証できます。
M2M 認証済みリクエストは CSRF 検証をバイパスし、`Accept: application/json` を付けると
HTML の代わりに JSON を返します（MCP ゲートウェイからの呼び出し向け）。
未設定なら M2M 呼び出しは常に拒否されます。

`/tasks/*` は Cloud Tasks 専用で、各ハンドラが `verify_task_caller` で OIDC を個別に検証します。

---

## 🎬 素材の種類（`source_type`）

入力はどちらも **MV Job ID と Music Job ID の 2 つだけ**です。GCS URL を直接入力する
必要はありません（パスは `AP_MV_BUCKET` / `AP_MUSIC_BUCKET` から組み立てられます）。

| `source_type` | 素材 | 説明 |
|---|---|---|
| `images`（既定） | ap-mv の `keyframes.zip` + ap-music の楽曲 | 静止画スライドショー。`subs_url`（省略可）・`whisper_model` を追加指定できる |
| `video` | ap-mv の完成動画（Veo 生成） + ap-music の楽曲 | 生成済み動画にカラオケ字幕を焼き込む。MV Job ID に含まれるカット全体が対象なので、1 セクション分でも曲全体でも同じ仕組みで扱える |

**`video` では ap-mv の `cuts[].start_sec` / `end_sec` を使いません。** これは演出上の
カット割り当てであって実際の歌唱タイミングではないためで、必ず実音声を Whisper で
強制アライメントして求めます（`app/video_merge.py`）。

**チェーンの本数を意識する必要はありません。** ap-mv 側は Veo の累積尺上限（30 秒）の
手前で継続チェーンをリセットすることがありますが、複数チェーンの検出・結合は
ap-mv 側で完結し、結果は `video_music_meta.json` の `final_video_url` に 1 本化されます。
このアプリはそれをダウンロードするだけです（`final_video_url` が無い旧ジョブには、
最後のカットの動画を使う後方互換フォールバックのみ持ちます）。

---

## 📦 入力 ZIP の構成（`images` モード）

ap-mv が生成する `keyframes.zip`（`AP_MV_BUCKET` 配下）の内部構成です。
`video` モードではこの ZIP は使いません。

```
keyframes.zip
├── cut_01.png
├── cut_02.png
├── ...
├── inputs.txt       # 画像ファイル名と表示尺
└── subtitles.ass    # ASS 字幕（カラオケタイミングなしでも可）
```

**`inputs.txt`**

```
file 'cut_01.png'
duration 40.000

file 'cut_02.png'
duration 50.000
```

**`subtitles.ass`**

歌詞行は **1 行ずつ別の `Dialogue` イベント**に分割してください。
`\N` で複数行を 1 イベントにまとめると Whisper アライメントが正しく機能しません。

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
| PrimaryColour | 歌唱済み文字の色（`&HAABBGGRR`）。黄: `&H0000FFFF` |
| SecondaryColour | 未歌唱文字の色。白: `&H00FFFFFF` |
| `\k` タグ | 不要。`align_subtitles.py` が Whisper で自動生成する |

---

## ⏱ タイムアウトの三段

```
PIPELINE_TIMEOUT  <  dispatch deadline  <=  Cloud Run の timeout
      25m                  30m                    1800s
```

| 段 | 置き場 |
|---|---|
| `PIPELINE_TIMEOUT`（既定 `25m`） | このアプリの env。実装は `app/pipeline/deadline.py` |
| dispatch deadline（1800s） | `app/adapters/task_queue.py` の `DISPATCH_DEADLINE_SEC` |
| Cloud Run の timeout（1800s） | ap-infra の `app_lyric_video.tf` |

実効上限を決めるのは**いちばん小さい値**で、他は飾りになります。
`PIPELINE_TIMEOUT` を**いちばん短く**取るのが要点で、**アプリが自分で先に諦める**ことで
`meta.json` に `failed` を書き、Slack へ通知してから終われます。

逆順にすると先に Cloud Tasks が打ち切ります。打ち切りはプロセスごと SIGTERM なので
**失敗ハンドラも Slack 通知も走りません**。`max_attempts = 1` なので再試行も来ず、
`meta.json` は `status: running` のまま取り残されます。
**ログに `Task failed` が出ないまま `running` で止まっているジョブを見たら、まずこれを疑ってください。**

この経路は実際に踏んでいます（`source_type=images` の検証で 1 本打ち切られました。
内訳は Whisper large-v3 のアライメントが約 8 分半、残り 20 分以上が字幕の PNG 描画）。

**dispatch deadline を指定しないと Cloud Tasks の既定 10 分が上限になり、
Cloud Run の timeout をいくら伸ばしてもそこで打ち切られます。**

`app/video_merge.py` の `_SUBPROCESS_TIMEOUT_SEC`（300s）や `_ALIGN_TIMEOUT_SEC`（900s）は
**1 ステップあたり**の上限で、足してもジョブ全体の上限にはなりません
（trim + align + burn だけで 1500s に達します）。`PIPELINE_TIMEOUT` は上から被せる別の層で、
各ステップの待ち時間はジョブ全体の残り時間で頭打ちになります。

`PIPELINE_TIMEOUT` を dispatch deadline 以上にすると、worker 面を持つ役割では
**起動時に落ちます**（`app/config.py`）。両方の値が同時に見えるのはそこだけなので、
不変条件はそこで機械的に確かめています。

---

## ☁️ 設定とデプロイ

### 所有はイメージを境に分かれている

**このリポジトリが所有するのはイメージだけです。** Cloud Run サービス 2 本・環境変数・
実行 SA・シークレット参照・CPU/メモリ・タイムアウト・公開可否・`lyric-video-queue`・
`gs://lyric-video` は、すべて [ap-infra](https://github.com/shouni/ap-infra) の
`app_lyric_video.tf` が所有しています。

| 変えたいもの | どこを触るか |
|---|---|
| アプリのコード | このリポジトリ。`main` へ push すれば自動でデプロイされる |
| 環境変数（`PIPELINE_TIMEOUT`・`WHISPER_MODEL` など） | ap-infra の `app_lyric_video.tf` → `terraform apply` |
| CPU・メモリ・タイムアウト・公開可否 | 同上 |
| キュー・バケット・IAM | 同上 |
| シークレットの**値** | Secret Manager に新しいバージョンを追加（参照は `latest`） |

**`main` へ push すると Cloud Build のトリガー `build-lyric-video` が自動で走ります。**
やることは 3 手だけです（`cloudbuild.yaml`）。`web` / `worker` の 2 ターゲットをビルドして
`ap-repo` へ push し、2 つのサービスを **`--image` だけ**更新します。

> ⚠️ **`gcloud run services update --set-env-vars` を打たないでください。**
> env の所有者は Terraform です。手で書き換えると次の `terraform apply` で元に戻り、
> それまでの間 ap-infra の毎日のドリフト検知が落ち続けます。`cloudbuild.yaml` が
> `--image` しか渡していないのも同じ理由で、ビルドと Terraform が同じフィールドを
> 奪い合わないようにするためです。`gcloud tasks queues create` も不要です
> （キューは Terraform の管理下にあります）。

### 環境変数

アプリが読む変数の一覧です。**本番でこれらを設定しているのは ap-infra の Terraform**で、
このリポジトリでも `gcloud` でもありません。ローカル実行時と、値の意味を確認するときの
参照表として使ってください。

「面」は**本番で実際に渡っているサービス**です（`app_lyric_video.tf` と照合済み）。
**⛔ は欠けていると起動時に落ちる**もので、それ以外は空でも起動します（機能しないだけです）。

| 変数 | 面 | 説明 |
|---|---|---|
| `SERVER_ROLE` | 両方 ⛔ | このプロセスが担う役割。`web` / `worker` / `both`。未設定と未知の値は起動時エラー |
| `SESSION_SECRET` | web ⛔ | セッション署名用のランダム文字列。worker では OAuth もセッションも使わないため不要 |
| `ALLOWED_TASK_SERVICE_ACCOUNTS` | worker ⛔ | worker が受け付ける caller SA の許可リスト（カンマ区切り）。空だと全リクエストを拒否（fail-closed） |
| `PIPELINE_TIMEOUT` | worker | ジョブ 1 件の実行時間の上限。`25m` / `1500s` / `1500`（単位なしは秒）。既定 `25m`、`0` で無制限。dispatch deadline 以上だと worker では起動時エラー |
| `GCP_PROJECT_ID` | 両方 | GCP プロジェクト ID |
| `GCP_LOCATION_ID` | 両方 | リージョン（既定 `asia-northeast1`） |
| `GCS_BUCKET` | 両方 | 成果物の入出力に使う GCS バケット名 |
| `AP_MV_BUCKET` | 両方 | ap-mv の成果物を参照するバケット名（既定 `ap-mv`）。**実際に読むのは worker だけ**で、web は job_id をタスクに載せるだけです |
| `AP_MUSIC_BUCKET` | 両方 | ap-music の楽曲を参照するバケット名（既定 `ap-music`）。同上 |
| `SERVICE_URL` | 両方 | **常に web 側の URL。** worker にも web の URL が入ります（Slack 通知のリンクが公開側を指す必要があるため）。M2M OIDC の audience にもなります |
| `TASK_AUDIENCE_URL` | 両方 | 投入するタスクの audience。**必ず worker の URL。** ズレると Cloud Run が 403 を返し、`max_attempts = 1` なのでタスクが失われます。未設定なら `SERVICE_URL` にフォールバック |
| `WORKER_URL` | web | タスクの配信先（既定 `<SERVICE_URL>/tasks/generate`）。**web が直接呼ぶ先ではありません**。配信するのは Cloud Tasks です |
| `CLOUD_TASKS_QUEUE_ID` | web | Cloud Tasks キュー ID。タスクを積むのは web だけ |
| `TASK_CALLER_SERVICE_ACCOUNT_EMAIL` | web | 投入するタスクに指定する caller SA。トークンを生成して付与するのは Cloud Tasks であって、このサービスではありません |
| `SIGNING_SERVICE_ACCOUNT_EMAIL` | web | GCS 署名付き URL の署名者。IAM SignBlob で自分を借用するため、**このサービス自身の実行 SA** を指定します |
| `WHISPER_MODEL` | web | `/new` フォームの初期値（既定 `large-v3`）。選ばれた値はタスクに載って worker へ渡るので、worker 側には要りません |
| `ALLOWED_EMAILS` | web | ログインを許可するメールアドレス（カンマ区切り） |
| `ALLOWED_M2M_SERVICE_ACCOUNTS` | web | web 面を OIDC Bearer で呼べる SA の一覧（カンマ区切り）。未設定なら M2M 呼び出しは常に拒否 |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | 両方 | Google OAuth2 クライアント。web は OAuth ログインと YouTube、worker は YouTube アップロードに使います |
| `YOUTUBE_REFRESH_TOKEN` | 両方 | YouTube OAuth2 リフレッシュトークン。web は動画の削除、worker はアップロードに使います |
| `SLACK_WEBHOOK_URL` | worker | Slack Incoming Webhook URL。通知するのは worker だけ |

**本番では設定しておらず、既定値のまま動いているもの**です。変えたくなったら
`app_lyric_video.tf` に足してください。

| 変数 | 既定 | 読む面 | 説明 |
|---|---|---|---|
| `GCS_OUTPUT_PREFIX` | `lyric-video/output` | 両方 | 出力先パスプレフィックス |
| `VIDEO_WHISPER_MODEL` | `small` | worker | `video` モードのアライメントに使うモデル。曲全体を 1 本で強制アライメントするため、小さめを既定にしています |
| `ALLOWED_DOMAINS` | 空 | web | ログインを許可するドメイン（カンマ区切り）。`ALLOWED_EMAILS` との**どちらかは必要**で、両方空だと誰もログインできません。本番は `ALLOWED_EMAILS` だけを使っています |
| `PORT` | `8080` | 両方 | 待ち受けポート。Cloud Run が実行時に注入します |

### シークレットと実行 SA

**web と worker で実行 SA を分けており、必要な権限も違います。**
ロール名は環境によって粒度が変わるので、何ができる必要があるかだけ挙げます。

| | web | worker |
|---|---|---|
| `GCS_BUCKET`（成果物） | 読み書き・削除 | 読み書き |
| `AP_MUSIC_BUCKET` / `AP_MV_BUCKET` | **不要**（job_id をタスクに載せるだけ） | **読み取りのみ** |
| Cloud Tasks への投入と ActAs | 必要 | 不要（タスクを積まない） |
| 署名付き URL の生成 | 必要 | 不要 |
| worker サービスの呼び出し | 必要 | 不要 |

シークレットは平文の env ではなく Cloud Run のシークレット参照
（`value_source.secret_key_ref`、`version: latest`）で渡ります。
**Cloud Run は起動時にシークレット env を解決するため、env に参照が残っていれば
使っていなくてもアクセス権が要ります。IAM だけ絞っても分離になりません。**
そのため参照する一覧自体を役割ごとに分けてあります。

| | web | worker |
|---|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | 要 | 要 |
| `YOUTUBE_REFRESH_TOKEN` | 要 | 要 |
| `SESSION_SECRET` | 要 | **不要** |
| `SLACK_WEBHOOK_URL` | **不要** | 要 |

> **`ALLOWED_TASK_SERVICE_ACCOUNTS` には worker 自身ではなく web の SA を書きます。**
> worker が受け付けるべき呼び出し元は、タスクを投入した web 側だからです。
>
> かつては `SERVICE_ACCOUNT_EMAIL` 1 つが「タスクの caller」「受け付ける caller」
> 「署名付き URL の署名者」の 3 つを兼ねていました。本来別の主体なので変数を分けてあります。

> **素材バケットは読み取りだけで足ります。** `storage.objectAdmin` は使わないでください
> （オブジェクト ACL の操作まで許します）。

**worker への到達は三段で守られています。** `ingress=internal` で Cloud Tasks からの
呼び出しだけがネットワーク層を通り（同一プロジェクトかつ既定の `run.app` URL である
ことが条件で、どちらも満たしています）、次に IAM の invoker、最後に
`verify_task_caller` の許可リストが検査されます。

### YouTube リフレッシュトークンの取得

**1. Google Cloud Console**

- **OAuth 同意画面** にスコープを追加: `youtube.upload` / `youtube.readonly` / `youtube.force-ssl`
- **認証情報** の OAuth クライアント（ウェブ アプリケーション）に
  `http://localhost:8080/` をリダイレクト URI として追加

**2. トークン取得と Secret Manager の更新**

```sh
source .venv/bin/activate
pip install -r requirements-dev.txt   # 初回のみ

export GOOGLE_CLIENT_ID=your-client-id
export GOOGLE_CLIENT_SECRET=your-client-secret
python3 scripts/get_youtube_token.py
```

ブラウザが開くので、YouTube チャンネルのアカウントでログインして承認します。
承認後、`YOUTUBE_REFRESH_TOKEN` に新しいバージョンが追加されます。**トークンは画面に出ません**
（端末の履歴やスクロールバックへ残さないため）。

スクリプトが使う `google-auth-oauthlib` と `google-cloud-secret-manager` は `requirements-dev.txt`
にあります。ローカルで動かすだけのものなので、本番イメージ（`requirements.txt` /
`requirements-worker.txt`）には入れていません。

付与されたスコープはその場に表示され、要求した 3 つのどれかが欠けていれば書き込まずに止まります。
同意画面はスコープごとにチェックを外せるため、外したまま承認するとアップロードは通るのに
コメント投稿だけ 403 になります。

書き込み先は ADC の既定プロジェクト（`GOOGLE_CLOUD_PROJECT` があればそちら）です。
古いバージョンには触らないので、整理する場合は `gcloud secrets versions destroy` で別途行ってください。

> 取得後は `http://localhost:8080/` をリダイレクト URI から削除することを推奨します。

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

テストは外部サービス（GCS / Cloud Tasks / YouTube / Whisper / ffmpeg）へ接続しません。
ドメインモデル・設定・OIDC 検証・`meta.json` の読み書き・字幕の折り返しといった純粋ロジックと、
Flask テストクライアントによる認証まわりの配線確認、およびジョブ全体の締切
（サブプロセスの打ち切りと `failed` の記録）を対象にしています。
CI（`.github/workflows/ci.yml`）でも同じコマンドが実行されます。

---

## 📜 ライセンス

このプロジェクトは [MIT License](https://opensource.org/licenses/MIT) の下で公開されています。
