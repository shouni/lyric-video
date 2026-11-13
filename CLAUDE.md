# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

**Lyric Video** is an orchestrator that turns an MP3 into an MP4 with karaoke subtitles and uploads it to YouTube. It processes audio and keyframe assets on GCS asynchronously on Cloud Run. See README.md for the full specification, the environment variables and the endpoint list.

**The Cloud Run deployment was shut down on 2026-08-13.** The repository is kept as a reference. The notes below on deployment describe how it ran while it was live.

## Architecture

A Flask app (`app/main.py`) with asynchronous workers driven by Cloud Tasks.

```
app/
├── main.py              # Flask entry point (create_app, blueprint registration, auth middleware)
├── config.py            # Loads Config from environment variables (Config.from_env)
├── auth.py              # Google OAuth2 login (Authlib)
├── align_subtitles.py   # Karaoke timing with Whisper
├── burn_subs.py         # Subtitle burn-in with PIL, and video encoding
├── video_merge.py       # Merge for source_type=video (finished ap-mv video + ap-music song)
├── adapters/            # External services (GCS, job_store, Slack, Cloud Tasks, YouTube, OIDC verification)
├── domain/              # Domain models (Job, Task, YoutubeTask)
├── handlers/
│   ├── web.py           # User-facing endpoints (/, /new, /jobs, ...)
│   └── worker.py        # Cloud Tasks workers (/tasks/generate, /tasks/youtube, OIDC auth)
└── pipeline/runner.py   # Runs align_subtitles.py / burn_subs.py as subprocesses and uploads to GCS
```

The flow is two asynchronous stages:
1. A job is submitted on `/new` → Cloud Tasks → `/tasks/generate` (video generation)
2. A manual trigger on the job detail page → Cloud Tasks → `/tasks/youtube` (YouTube publishing)

The sibling services it reads from are `ap-mv` (keyframes and Veo videos, public) and `ap-music` (songs, private). An earlier version of ap-music is public and archived as [ap-music-poc](https://github.com/shouni/ap-music-poc); it still uses the older flat layout (`<job_id>.mp3`), not the `music/<job_id>/` layout this code reads.

## Development commands

```sh
# Run locally (needs environment variables, see README.md)
python3 -m app.main

# Docker build
docker build -t lyric-video .
```

While the service was live, a push to `main` started the Cloud Build trigger `build-lyric-video` and deployed automatically. There was no need to run `gcloud builds submit` by hand.

**This repository owned only the image.** The two Cloud Run services, their environment variables, the runtime service accounts, the secret references, the queue and the bucket were owned by Terraform in ap-infra (a private repository; its `app_lyric_video.tf` was removed when the service was retired). That is why `cloudbuild.yaml` passes nothing but `--image`. **Running `gcloud run services update --set-env-vars` would be reverted by the next `terraform apply`, and ap-infra's daily drift check would fail until then.** Adding an environment variable meant touching both `config.py` here and the Terraform in ap-infra.

ruff handles lint and format, and pytest runs the tests (`pyproject.toml`, CI: `.github/workflows/ci.yml`). After a change, check with:

```sh
ruff check .
ruff format --check .
SESSION_SECRET=dummy python3 -m pytest -q
```

The tests cover only pure logic that reaches no external service (`domain/`, `adapters/job_store`, `adapters/oidc`, `config`, `align_subtitles`, and the line wrapping in `video_merge`) and the wiring of the auth middleware through the Flask test client. Code that calls GCS, Cloud Tasks, YouTube, Whisper or ffmpeg has no tests, so a change around it needs checking in a real environment. Development dependencies are in `requirements-dev.txt` (`-r requirements.txt` + pytest + ruff).

CI runs on Python 3.14, the same as the Dockerfile. Create the local `.venv` with 3.14 too. With a venv on an older Python, code that passes in CI and production can fail on import.

## Conventions and cautions

- Comments and docstrings are English (moved from Japanese on 2026-11-14). Add one to a function or method only when a design decision needs its reason recorded, not to say what the code does. User-facing text, log messages and error messages stay Japanese: they are program output, and some tests assert on them.
- `README.md` and this file are English.
- Secrets (`SESSION_SECRET`, `GOOGLE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` and so on) come through environment variables. Use Secret Manager in production, and never hard-code them.
- The script that obtained the YouTube refresh token (`scripts/get_youtube_token.py`) was removed on 2026-11-14. The same flow lives in `cmd/youtube-token` of [ap-mcp-youtube](https://github.com/shouni/ap-mcp-youtube); see the README.
- The `/tasks/*` endpoints in `app/handlers/worker.py` are for Cloud Tasks only and are protected by OIDC verification. Do not confuse them with the user-facing endpoints.
- OIDC verification is gathered in `app/adapters/oidc.py`: `verify_task_caller` for Cloud Tasks and `verify_service_account` for M2M share the same token verification.
- Read and write a job's `meta.json` (on GCS) through `app/adapters/job_store.py`. Do not scatter URI building across handlers. `update()` does not raise on failure, so that a failed metadata update does not take the job down with it.
- The user-facing endpoints (`/new`, `/jobs`, ...) accept OIDC M2M authentication through `ALLOWED_M2M_SERVICE_ACCOUNTS` (`app/adapters/oidc.py`) as well as the browser session. A request authenticated by M2M bypasses CSRF verification, and returns JSON when it sends `Accept: application/json` (for calls from the MCP gateway). This is the same design as the `/web/*` pattern in ap-music.
- Defaults such as the GCS input and output paths and the Whisper model are gathered in `Config.from_env` in `app/config.py`. The merge for `source_type=video` aligns the whole song in one pass, so it uses `VIDEO_WHISPER_MODEL` (default `small`), separate from `WHISPER_MODEL`.
- `meta.json` is saved synchronously when `/new` accepts a job. Cloud Run throttles the CPU after the response is returned, so a save on a background thread can be lost.
- **The timeouts form three tiers (`PIPELINE_TIMEOUT` < dispatch deadline <= Cloud Run timeout), and `PIPELINE_TIMEOUT` must always be the shortest.** Only by giving up first can the app write `failed` to `meta.json` and notify Slack. If Cloud Tasks cuts the request first, the whole process gets SIGTERM, nothing is recorded or notified, and the job is left at `running` (there is no retry either, because `max_attempts = 1`). The deadline is held by `Deadline` in `app/pipeline/deadline.py`; `PipelineRunner.run` creates one and hands it to every step. Values such as `_SUBPROCESS_TIMEOUT_SEC` in `video_merge.py` are limits **per step** and do not add up to a limit for the whole job (the shorter of the two applies). `app/config.py` checks the invariant at startup.
- When adding a subprocess, go through `_run_subprocess` in `pipeline/runner.py` or `_run` in `video_merge.py`. Both take the deadline and cap the wait. Calling `subprocess.run` or `Popen.wait()` directly with no limit leaves that call outside the three tiers.
- `align_subtitles.py` adds a 300 ms tail (`TAIL_MS`) to the end of each line, so a line can overlap the next. `burn_subs.py` draws only one line, the earlier one winning, so it is unaffected. `video_merge.py` burns with `-vf ass=` (libass), where the overlap would show two lines stacked. To avoid that, `trim_overlaps` cuts each line at the start of the next before the ASS file is written.
- `app/align_subtitles.py` and `app/burn_subs.py` are near copies of the sibling repository [lyric-video-maker](https://github.com/shouni/lyric-video-maker). The differences are the Linux font paths for Docker, and `--style-file` and line mode, which only the maker has. When one side is fixed, consider porting the fix to the other.
