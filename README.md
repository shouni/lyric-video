# ✨ Lyric Video

[![CI](https://github.com/shouni/lyric-video/actions/workflows/ci.yml/badge.svg)](https://github.com/shouni/lyric-video/actions/workflows/ci.yml)
[![Language](https://img.shields.io/badge/Language-Python-blue)](https://www.python.org/)
[![Python Version](https://img.shields.io/badge/Python-3.14%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Status](https://img.shields.io/badge/Status-Retired%20%2F%20Reference-lightgrey)](#)

## 🎯 Overview

**Lyric Video** is an **orchestrator** that generates an MP4 with karaoke subtitles and uploads it to YouTube.

You **specify only two Job IDs**: one from ap-mv (keyframes or a Veo video) and one from ap-music (the song).
The service finds the source files on GCS from those IDs, then runs Whisper alignment, subtitle burn-in
and encoding asynchronously on Cloud Run. A generated video can be uploaded to YouTube from the job detail
page as a streaming upload. If the YouTube video has been deleted, it can be uploaded again.

[ap-mv](https://github.com/shouni/ap-mv) is a public repository. ap-music is private. An earlier version of it
is public and archived as [ap-music-poc](https://github.com/shouni/ap-music-poc); that version still uses the
older flat layout (`<job_id>.mp3`), not the `music/<job_id>/` layout this service reads.

A browser is all you need to use it. The service also supports machine-to-machine authentication with OIDC
and JSON responses, so an AI agent can operate it through an MCP gateway (see "Endpoints").

> **Status:** The Cloud Run deployment of this service was shut down on 2026-08-13.
> The repository is kept as a reference. The sections on deployment describe how the service ran while it was live.

---

## 🏗 Architecture

**Production is split into two Cloud Run services.** `SERVER_ROLE` decides the role, and the Blueprint
for a side that the process does not serve is not registered at all (`app/main.py`).

| Service | `SERVER_ROLE` | Exposure | Role |
|---|---|---|---|
| `lyric-video` | `web` | Public (OAuth / M2M OIDC) | Pages and task submission |
| `lyric-video-worker` | `worker` | Private (`ingress=internal`) | Video generation and YouTube upload |

**There are also two images.** The `Dockerfile` is multi-stage and has a `web` target and a `worker` target.
Only the worker stage installs `requirements-worker.txt` (stable-ts / torch) and bakes in the Whisper
model weights. The only file that imports `stable_whisper` is `align_subtitles.py`, which runs as a separate
process, so the web image needs neither torch nor the model.
With a single image, the public service would pull several GB on every cold start.

```
Browser (authenticated with Google OAuth2) / MCP gateway (OIDC)
  │  Sends the MV Job ID, the Music Job ID and the source type to /new
  ▼
lyric-video (web)
  │  Generates a job_id → saves meta.json to GCS → enqueues the task
  ▼
Cloud Tasks (lyric-video-queue, max_attempts = 1)
  │  Calls POST /tasks/generate asynchronously with an OIDC token
  ▼
lyric-video-worker
  │  Derives the GCS paths from the Job IDs (AP_MV_BUCKET / AP_MUSIC_BUCKET)
  │
  ├─[images] Still-image slideshow
  │   Fetches audio.mp3 + keyframes.zip → generates karaoke timing with Whisper
  │   → burns the subtitles in with PIL and encodes an MP4
  │
  └─[video] Combines the finished ap-mv video with the ap-music song
      Fetches final_video_url and the song → force-aligns to the actual audio with Whisper
      → burns the subtitles in with ffmpeg (libass) and muxes the audio
  │
  └─→ Uploads to GCS → updates meta.json → notifies Slack

[Optional] Publishing to YouTube (triggered by hand after checking the video)
  Job detail page → lyric-video (web) enqueues the task
    → lyric-video-worker receives POST /tasks/youtube
    → Checks whether the video exists (if it was deleted, resets the meta and uploads again)
    → Streams from GCS and uploads to the YouTube Data API
    → Saves youtube_url to meta.json → notifies Slack
```

---

## 🔌 Endpoints

| Method | Path | Served by | Description |
|---|---|---|---|
| `GET` | `/` | web | Home (latest 5 jobs) |
| `GET` `POST` | `/new` | web | Input form / task submission |
| `GET` | `/jobs` | web | Job history list |
| `GET` `DELETE` | `/jobs/<job_id>` | web | Detail page and video player / deletes the job and its GCS files |
| `POST` | `/jobs/<job_id>/youtube` | web | Enqueues a YouTube publishing task |
| `DELETE` | `/jobs/<job_id>/youtube` | web | Deletes the YouTube video and clears the YouTube fields in `meta.json` |
| `GET` | `/auth/login` `/auth/callback` `/auth/logout` | web | Google OAuth2 |
| `POST` | `/tasks/generate` | worker | Video generation (OIDC verified) |
| `POST` | `/tasks/youtube` | worker | YouTube upload (OIDC verified) |
| `GET` | `/healthz` | both | Health check (no authentication) |

The web endpoints accept a browser session (cookie + CSRF). They also accept an
**OIDC Bearer token issued for a service account listed in `ALLOWED_M2M_SERVICE_ACCOUNTS`**
(`Authorization: Bearer <ID Token>`, audience = `SERVICE_URL`).
A request authenticated this way skips CSRF verification. With `Accept: application/json`,
it receives JSON instead of HTML (intended for calls from the MCP gateway).
If the variable is not set, M2M calls are always rejected.

`/tasks/*` is for Cloud Tasks only. Each handler verifies the OIDC token itself with `verify_task_caller`.

---

## 🎬 Source types (`source_type`)

Both types take **only two inputs: the MV Job ID and the Music Job ID**. You do not enter GCS URLs directly.
The paths are built from `AP_MV_BUCKET` / `AP_MUSIC_BUCKET`.

| `source_type` | Source | Description |
|---|---|---|
| `images` (default) | `keyframes.zip` from ap-mv + the song from ap-music | Still-image slideshow. `subs_url` (optional) and `whisper_model` can also be specified |
| `video` | The finished video from ap-mv (generated by Veo) + the song from ap-music | Burns karaoke subtitles into an existing video. All cuts in the MV job are covered, so one section and a whole song are handled the same way |

**`video` does not use `cuts[].start_sec` / `end_sec` from ap-mv.** Those values assign cuts for
direction and are not the actual singing times. The timing always comes from force-aligning the
actual audio with Whisper (`app/video_merge.py`).

**You do not need to care how many chains there are.** ap-mv may reset its continuation chain
before Veo's cumulative length limit (30 seconds). Detecting and joining multiple chains is done entirely
in ap-mv, and the result is a single video at `final_video_url` in `video_music_meta.json`.
This app only downloads it. For older jobs without `final_video_url`, there is one backward-compatible
fallback: the app uses the video of the last cut.

---

## 📦 Input ZIP layout (`images` mode)

This is the layout of `keyframes.zip`, which ap-mv generates (under `AP_MV_BUCKET`).
`video` mode does not use this ZIP.

```
keyframes.zip
├── cut_01.png
├── cut_02.png
├── ...
├── inputs.txt       # Image file names and display durations
└── subtitles.ass    # ASS subtitles (karaoke timing is not required)
```

**`inputs.txt`**

```
file 'cut_01.png'
duration 40.000

file 'cut_02.png'
duration 50.000
```

**`subtitles.ass`**

Put **each lyric line in its own `Dialogue` event**.
If several lines are joined into one event with `\N`, Whisper alignment does not work correctly.
Do not create lines that contain only punctuation or symbols either. Such a line has no characters to
match, so no timing can be assigned to it and the times you entered stay as they are
(a `Warning` appears in the log).

```ass
[V4+ Styles]
Style: Karaoke,Arial,64,&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,...
;                        ↑ yellow (sung)  ↑ white (not yet sung)

[Events]
Dialogue: 0,0:00:10.00,0:00:17.00,Karaoke,,0,0,0,,1行目の歌詞
Dialogue: 0,0:00:17.00,0:00:22.00,Karaoke,,0,0,0,,2行目の歌詞
```

The text of the two `Dialogue` events is example lyric text ("lyrics of line 1", "lyrics of line 2").

| Item | Description |
|---|---|
| Style name | `Karaoke` (required) |
| PrimaryColour | Color of characters already sung (`&HAABBGGRR`). Yellow: `&H0000FFFF` |
| SecondaryColour | Color of characters not yet sung. White: `&H00FFFFFF` |
| `\k` tags | Not needed. `align_subtitles.py` generates them with Whisper |

---

## ⏱ The three timeout layers

```
PIPELINE_TIMEOUT  <  dispatch deadline  <=  Cloud Run timeout
      25m                  30m                    1800s
```

| Layer | Where it is set |
|---|---|
| `PIPELINE_TIMEOUT` (default `25m`) | An environment variable of this app. Implemented in `app/pipeline/deadline.py` |
| dispatch deadline (1800s) | `DISPATCH_DEADLINE_SEC` in `app/adapters/task_queue.py` |
| Cloud Run timeout (1800s) | `app_lyric_video.tf` in ap-infra |

ap-infra is a private Terraform repository. `app_lyric_video.tf` was removed when the service was
retired, so it is no longer in ap-infra.

The effective limit is the **smallest value**. The others have no effect.
The point is to make `PIPELINE_TIMEOUT` the **shortest**. Because **the app gives up first by itself**,
it can write `failed` to `meta.json` and notify Slack before it exits.

In the opposite order, Cloud Tasks cuts the request off first. The cutoff is a SIGTERM to the whole
process, so **neither the failure handler nor the Slack notification runs**. With `max_attempts = 1`
there is no retry, and `meta.json` is left at `status: running`.
**If you see a job stuck at `running` with no `Task failed` in the log, suspect this first.**

This has actually happened. One job was cut off during verification of `source_type=images`:
Whisper large-v3 alignment took about 8.5 minutes, and rendering the subtitle PNGs took the remaining
20 minutes or more.

**If no dispatch deadline is specified, the Cloud Tasks default of 10 minutes is the limit,
and the request is cut off there however long the Cloud Run timeout is.**

`_SUBPROCESS_TIMEOUT_SEC` (300s) and `_ALIGN_TIMEOUT_SEC` (900s) in `app/video_merge.py` are limits
**per step**. Adding them up does not give a limit for the whole job
(trim + align + burn alone reach 1500s). `PIPELINE_TIMEOUT` is a separate layer placed on top of them:
the wait for each step is capped by the time remaining for the whole job.

If `PIPELINE_TIMEOUT` is equal to or greater than the dispatch deadline, a role that includes the worker
side **fails at startup** (`app/config.py`). That is the only place where both values are visible at
once, so the invariant is checked there mechanically.

---

## ☁️ Configuration and deployment

### Ownership is divided at the image

**This repository owns only the images.** The two Cloud Run services, their environment variables,
runtime service accounts, secret references, CPU and memory, timeouts, public or private exposure,
`lyric-video-queue` and `gs://lyric-video` are all owned by `app_lyric_video.tf` in ap-infra.

| What you want to change | Where to change it |
|---|---|
| Application code | This repository. A push to `main` deploys automatically |
| Environment variables (`PIPELINE_TIMEOUT`, `WHISPER_MODEL` and so on) | `app_lyric_video.tf` in ap-infra → `terraform apply` |
| CPU, memory, timeout, public or private exposure | Same as above |
| Queue, bucket, IAM | Same as above |
| The **value** of a secret | Add a new version in Secret Manager (the reference is `latest`) |

**A push to `main` runs the Cloud Build trigger `build-lyric-video` automatically.**
It does only three things (`cloudbuild.yaml`). It builds the two targets `web` and `worker`,
pushes them to `ap-repo`, and updates the two services with **`--image` only**.

> ⚠️ **Do not run `gcloud run services update --set-env-vars`.**
> Terraform owns the environment variables. A manual change is reverted by the next `terraform apply`,
> and until then the daily drift check in ap-infra keeps failing. `cloudbuild.yaml` passes only
> `--image` for the same reason: the build and Terraform must not compete for the same field.
> `gcloud tasks queues create` is not needed either
> (the queue is managed by Terraform).

### Environment variables

This is the list of variables the app reads. **In production they are set by the ap-infra Terraform**,
not by this repository and not with `gcloud`. Use the table for local runs and as a reference
when you need to check what a value means.

"Set on" is the **service that actually receives the variable in production** (checked against `app_lyric_video.tf`).
**⛔ marks a variable whose absence makes startup fail.** The others can be empty and the process still
starts (the related feature just does not work).

| Variable | Set on | Description |
|---|---|---|
| `SERVER_ROLE` | both ⛔ | The role of this process: `web` / `worker` / `both`. An unset or unknown value is a startup error |
| `SESSION_SECRET` | web ⛔ | Random string for signing the session. Not needed on the worker, which uses neither OAuth nor sessions |
| `ALLOWED_TASK_SERVICE_ACCOUNTS` | worker ⛔ | Allowlist of caller service accounts that the worker accepts (comma-separated). If empty, every request is rejected (fail-closed) |
| `PIPELINE_TIMEOUT` | worker | Upper limit on the run time of one job. `25m` / `1500s` / `1500` (no unit means seconds). Default `25m`; `0` means unlimited. A value equal to or greater than the dispatch deadline is a startup error on the worker |
| `GCP_PROJECT_ID` | both | GCP project ID |
| `GCP_LOCATION_ID` | both | Region (default `asia-northeast1`) |
| `GCS_BUCKET` | both | Name of the GCS bucket used to read and write the outputs |
| `AP_MV_BUCKET` | both | Name of the bucket that holds the ap-mv outputs (default `ap-mv`). **Only the worker actually reads it.** The web service only puts the job_id on the task |
| `AP_MUSIC_BUCKET` | both | Name of the bucket that holds the ap-music songs (default `ap-music`). Same as above |
| `SERVICE_URL` | both | **Always the URL of the web service.** The worker also gets the web URL, because the links in Slack notifications must point to the public service. It is also the audience for M2M OIDC |
| `TASK_AUDIENCE_URL` | both | Audience of the tasks that are enqueued. **Must be the worker URL.** If it is wrong, Cloud Run returns 403, and the task is lost because `max_attempts = 1`. Falls back to `SERVICE_URL` when unset |
| `WORKER_URL` | web | Where the task is delivered (default `<SERVICE_URL>/tasks/generate`). **The web service does not call this URL itself.** Cloud Tasks delivers the task |
| `CLOUD_TASKS_QUEUE_ID` | web | Cloud Tasks queue ID. Only the web service enqueues tasks |
| `TASK_CALLER_SERVICE_ACCOUNT_EMAIL` | web | Caller service account specified on the enqueued task. Cloud Tasks generates and attaches the token, not this service |
| `SIGNING_SERVICE_ACCOUNT_EMAIL` | web | Signer of GCS signed URLs. The service impersonates itself through IAM SignBlob, so this must be **the runtime service account of this service itself** |
| `WHISPER_MODEL` | web | Initial value in the `/new` form (default `large-v3`). The selected value travels on the task to the worker, so the worker does not need this variable |
| `ALLOWED_EMAILS` | web | Email addresses allowed to log in (comma-separated) |
| `ALLOWED_M2M_SERVICE_ACCOUNTS` | web | Service accounts that may call the web endpoints with an OIDC Bearer token (comma-separated). If unset, M2M calls are always rejected |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | both | Google OAuth2 client. The web service uses it for OAuth login and YouTube, the worker for YouTube uploads |
| `YOUTUBE_REFRESH_TOKEN` | both | YouTube OAuth2 refresh token. The web service uses it to delete videos, the worker to upload them |
| `SLACK_WEBHOOK_URL` | worker | Slack Incoming Webhook URL. Only the worker sends notifications |

The following are **not set in production and run with their defaults**. To change one,
add it to `app_lyric_video.tf`.

| Variable | Default | Read by | Description |
|---|---|---|---|
| `GCS_OUTPUT_PREFIX` | `lyric-video/output` | both | Path prefix of the output location |
| `VIDEO_WHISPER_MODEL` | `small` | worker | Model used for alignment in `video` mode. The default is a small model because the whole song is force-aligned in one pass |
| `ALLOWED_DOMAINS` | empty | web | Domains allowed to log in (comma-separated). **Either this or `ALLOWED_EMAILS` is required.** If both are empty, nobody can log in. Production uses only `ALLOWED_EMAILS` |
| `PORT` | `8080` | both | Listening port. Cloud Run injects it at run time |

### Secrets and runtime service accounts

**The web service and the worker run as different service accounts and need different permissions.**
Role names vary in granularity from one environment to another, so the table lists only what each
account must be able to do.

| | web | worker |
|---|---|---|
| `GCS_BUCKET` (outputs) | Read, write, delete | Read, write |
| `AP_MUSIC_BUCKET` / `AP_MV_BUCKET` | **Not needed** (it only puts the job_id on the task) | **Read only** |
| Enqueueing to Cloud Tasks and ActAs | Needed | Not needed (it does not enqueue tasks) |
| Generating signed URLs | Needed | Not needed |
| Invoking the worker service | Needed | Not needed |

Secrets are passed as Cloud Run secret references
(`value_source.secret_key_ref`, `version: latest`), not as plain-text environment variables.
**Cloud Run resolves secret environment variables at startup, so as long as a reference remains in the
environment, the account needs access to it even if the process never uses it. Narrowing IAM alone does
not separate the two services.**
For that reason the list of referenced secrets itself is different for each role.

| | web | worker |
|---|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Needed | Needed |
| `YOUTUBE_REFRESH_TOKEN` | Needed | Needed |
| `SESSION_SECRET` | Needed | **Not needed** |
| `SLACK_WEBHOOK_URL` | **Not needed** | Needed |

> **`ALLOWED_TASK_SERVICE_ACCOUNTS` holds the service account of the web service, not of the worker itself.**
> The caller that the worker should accept is the web service, which enqueued the task.
>
> In the past, a single `SERVICE_ACCOUNT_EMAIL` served as three things: the caller of the task,
> the caller to accept, and the signer of signed URLs. These are different principals,
> so the variable was split.

> **Read access is enough for the source buckets.** Do not use `storage.objectAdmin`
> (it also allows changes to object ACLs).

**Access to the worker is protected in three layers.** With `ingress=internal`, only calls from
Cloud Tasks pass the network layer (this requires the same project and the default `run.app` URL,
and both conditions are met). Next comes the IAM invoker check, and last the allowlist in
`verify_task_caller`.

### Getting a YouTube refresh token

The script that obtained the token (`scripts/get_youtube_token.py`) was deleted on 2026-11-14.
The same process now lives in `cmd/youtube-token` of [ap-mcp-youtube](https://github.com/shouni/ap-mcp-youtube).

```sh
# Run in the ap-mcp-youtube directory
MCP_YOUTUBE_CLIENT_ID="$GOOGLE_CLIENT_ID" MCP_YOUTUBE_CLIENT_SECRET="$GOOGLE_CLIENT_SECRET" \
  go run ./cmd/youtube-token -scopes youtube.upload,youtube.readonly,youtube.force-ssl \
  | gcloud secrets versions add YOUTUBE_REFRESH_TOKEN --data-file=-
```

Before you run it, add `http://localhost:8080/` to the redirect URIs of the OAuth client
(type: Web application), and remove it once you have the token. **The token is not shown on screen.**
If any of the requested scopes is not granted, the command stops without writing anything.

The Cloud Run deployment of this service was shut down on 2026-08-13, and ap-music is now the only user of
`YOUTUBE_REFRESH_TOKEN`. The steps for getting a new token for ap-music are in the ap-music README.

---

## 🤝 Dependencies

| Package | Purpose |
|---|---|
| [stable-ts](https://github.com/jianfch/stable-ts) | Character-level audio alignment with Whisper |
| [Pillow](https://pillow.readthedocs.io/) | Drawing subtitles onto PNG images |
| [pysubs2](https://pysubs2.readthedocs.io/) | Parsing ASS subtitle files |
| [ffmpeg](https://ffmpeg.org/) | Video encoding and audio muxing |
| [Flask](https://flask.palletsprojects.com/) | Web server |
| [gunicorn](https://gunicorn.org/) | Production WSGI server |
| [Authlib](https://docs.authlib.org/) | Google OAuth2 authentication |
| [google-cloud-tasks](https://cloud.google.com/tasks) | Asynchronous task queue |
| [google-cloud-storage](https://cloud.google.com/storage) | GCS file input and output |
| [google-api-python-client](https://github.com/googleapis/google-api-python-client) | YouTube Data API v3 |

---

## 🧪 Development and testing

```sh
pip install -r requirements-dev.txt

ruff check . && ruff format --check .
SESSION_SECRET=dummy pytest -q
```

The tests do not connect to external services (GCS / Cloud Tasks / YouTube / Whisper / ffmpeg).
They cover pure logic (the domain models, configuration, OIDC verification, reading and writing
`meta.json`, subtitle line wrapping), the authentication wiring through the Flask test client,
and the deadline for the whole job (terminating a subprocess and recording `failed`).
CI (`.github/workflows/ci.yml`) runs the same commands.

---

## 📜 License

This project is released under the [MIT License](https://opensource.org/licenses/MIT).
