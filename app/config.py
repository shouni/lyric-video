from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from app.adapters.task_queue import DISPATCH_DEADLINE_SEC

# Default upper limit on the run time of one job. It matches the 25m that the fleet's Go
# implementations (ap-music / ap-mv / ap-story) set explicitly in production. It is the value with
# which **the app gives up by itself** before dispatch_deadline (30 minutes). The reason is in
# app/pipeline/deadline.py.
_DEFAULT_PIPELINE_TIMEOUT = "25m"

# The values SERVER_ROLE can take. It must be set explicitly, and unset is a startup error.
# If unset were treated as "both", a single missing environment variable in production would bring
# /tasks/* back on the public web side. both is chosen only for local development.
SERVER_ROLE_BOTH = "both"
SERVER_ROLE_WEB = "web"
SERVER_ROLE_WORKER = "worker"
_SERVER_ROLES = (SERVER_ROLE_BOTH, SERVER_ROLE_WEB, SERVER_ROLE_WORKER)


@dataclass
class Config:
    service_url: str
    # server_role is the role this process takes on: one of SERVER_ROLE_*. from_env rejects an unset value.
    server_role: str
    port: str
    project_id: str
    location_id: str
    queue_id: str
    worker_url: str
    task_audience_url: str
    # The three roles that a single SERVICE_ACCOUNT_EMAIL once covered. They are really different
    # principals, so they are separated.
    # task_caller: the caller SA set in oidcToken.serviceAccountEmail of the tasks that are enqueued (web)
    task_caller_service_account_email: str
    # allowed_task: the allowlist of caller SAs that the worker accepts (worker)
    allowed_task_service_accounts: list[str]
    # signing: the signer of GCS signed URLs. It impersonates itself through IAM SignBlob, so it
    # must be **the runtime SA of that process itself** (web).
    signing_service_account_email: str
    gcs_bucket: str
    gcs_output_prefix: str
    slack_webhook_url: str
    whisper_model: str
    # Upper limit on the run time of one job (download, subtitle generation, burn-in, upload).
    # 0 or less means unlimited. Details are in app/pipeline/deadline.py.
    pipeline_timeout_sec: float
    # Whisper model for the video compositing mode. The whole song is force-aligned in one pass,
    # so the default is a smaller model that fits in Cloud Run's memory and processing time.
    video_whisper_model: str
    # Names of the buckets that hold the artifacts of ap-mv/ap-music. Both source types read from them.
    ap_mv_bucket: str
    ap_music_bucket: str
    # OAuth2
    google_client_id: str
    google_client_secret: str
    session_secret: str
    # YouTube Data API
    youtube_refresh_token: str
    allowed_emails: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    # OIDC allowlist for M2M (server-to-server) calls
    allowed_m2m_service_accounts: list[str] = field(default_factory=list)

    def serves_web(self) -> bool:
        """Return whether this role serves the web side (/, /jobs, OAuth)."""
        return self.server_role in (SERVER_ROLE_BOTH, SERVER_ROLE_WEB)

    def serves_worker(self) -> bool:
        """Return whether this role serves the worker side (/tasks/*)."""
        return self.server_role in (SERVER_ROLE_BOTH, SERVER_ROLE_WORKER)

    def is_secure_url(self) -> bool:
        """Return whether the service URL is HTTPS."""
        return self.service_url.startswith("https://")

    def default_output_prefix(self) -> str:
        """Build the default output location from the configured GCS bucket and output prefix."""
        return f"gs://{self.gcs_bucket}/{self.gcs_output_prefix}" if self.gcs_bucket else ""

    @classmethod
    def from_env(cls) -> Config:
        """Load the application settings from environment variables."""
        server_role = os.getenv("SERVER_ROLE", "").strip().lower()
        if server_role not in _SERVER_ROLES:
            raise RuntimeError(
                f"SERVER_ROLE ({server_role!r}) は 'web', 'worker', 'both' のいずれかである必要があります"
            )
        service_url = os.getenv("SERVICE_URL", "http://localhost:8080").rstrip("/")
        worker_url = os.getenv("WORKER_URL", "").strip()
        if not worker_url:
            worker_url = f"{service_url}/tasks/generate"
        task_audience_url = os.getenv("TASK_AUDIENCE_URL", "").strip() or service_url
        return cls(
            service_url=service_url,
            server_role=server_role,
            port=os.getenv("PORT", "8080"),
            project_id=os.getenv("GCP_PROJECT_ID", ""),
            location_id=os.getenv("GCP_LOCATION_ID", "asia-northeast1"),
            queue_id=os.getenv("CLOUD_TASKS_QUEUE_ID", ""),
            worker_url=worker_url,
            task_audience_url=task_audience_url,
            task_caller_service_account_email=_task_caller_email(),
            allowed_task_service_accounts=_allowed_task_service_accounts(server_role),
            signing_service_account_email=_signing_email(),
            gcs_bucket=os.getenv("GCS_BUCKET", ""),
            gcs_output_prefix=os.getenv("GCS_OUTPUT_PREFIX", "lyric-video/output"),
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", ""),
            whisper_model=os.getenv("WHISPER_MODEL", "large-v3"),
            pipeline_timeout_sec=_pipeline_timeout_sec(server_role),
            video_whisper_model=os.getenv("VIDEO_WHISPER_MODEL", "small"),
            ap_mv_bucket=os.getenv("AP_MV_BUCKET", "ap-mv"),
            ap_music_bucket=os.getenv("AP_MUSIC_BUCKET", "ap-music"),
            google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
            # A process that serves only the worker side uses neither sessions nor OAuth, so this
            # is not required, which avoids handing out access to credentials it never uses.
            session_secret=(
                _require_env("SESSION_SECRET")
                if server_role in (SERVER_ROLE_BOTH, SERVER_ROLE_WEB)
                else os.getenv("SESSION_SECRET", "")
            ),
            youtube_refresh_token=os.getenv("YOUTUBE_REFRESH_TOKEN", ""),
            allowed_emails=_split_env("ALLOWED_EMAILS"),
            allowed_domains=_split_env("ALLOWED_DOMAINS"),
            allowed_m2m_service_accounts=_split_env("ALLOWED_M2M_SERVICE_ACCOUNTS"),
        )


def _task_caller_email() -> str:
    """The caller SA set on the tasks that are enqueued."""
    return os.getenv("TASK_CALLER_SERVICE_ACCOUNT_EMAIL", "").strip()


def _signing_email() -> str:
    """The signer of GCS signed URLs (that is, the runtime SA of this process itself)."""
    return os.getenv("SIGNING_SERVICE_ACCOUNT_EMAIL", "").strip()


def _allowed_task_service_accounts(server_role: str) -> list[str]:
    """The allowlist of caller SAs that the worker accepts.

    If the worker starts with this empty, verify_task_caller refuses every request, and Cloud Tasks
    uses up its retries and discards the task. So that a missing setting does not show up as tasks
    silently disappearing, startup fails instead.
    """
    allowed = _split_env("ALLOWED_TASK_SERVICE_ACCOUNTS")
    if not allowed and server_role in (SERVER_ROLE_BOTH, SERVER_ROLE_WORKER):
        raise RuntimeError("ALLOWED_TASK_SERVICE_ACCOUNTS が設定されていません（worker 面の OIDC 検証に必須です）")
    return allowed


_DURATION = re.compile(r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|m|h)?\s*$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(text: str) -> float:
    """Convert a notation such as `25m` / `1500s` / `1500` to seconds.

    PIPELINE_TIMEOUT in the Go implementations (ap-music / ap-mv / ap-story) is in the `25m` form,
    so this lets the same value be written as is. When the Terraform side (ap-infra) can hand the
    env to every app in the same notation, differences are readable when looking at the whole fleet.
    A number without a unit is treated as seconds.
    """
    match = _DURATION.match(text)
    if not match:
        raise ValueError(f"duration として解釈できません: {text!r}（例: '25m', '1500s', '1500'）")
    return float(match["value"]) * _UNIT_SECONDS[match["unit"] or "s"]


def _pipeline_timeout_sec(server_role: str) -> float:
    """Read the upper limit on the run time of one job, and check the three-level ordering at startup.

    ★ PIPELINE_TIMEOUT < dispatch_deadline is **verified mechanically here**.
      This function is the only place where both values are visible at the same time, and if they
      are reversed it breaks in the way that is hardest to notice: "Cloud Tasks cuts it off first,
      so neither the failure nor a notification is left" (see app/pipeline/deadline.py).

      The check runs only for a role that has the worker side. For a process that does not run the
      pipeline the value is irrelevant, and failing there would be an accident in which only web
      does not start.
    """
    raw = os.getenv("PIPELINE_TIMEOUT", "").strip() or _DEFAULT_PIPELINE_TIMEOUT
    seconds = parse_duration(raw)

    if seconds > 0 and server_role in (SERVER_ROLE_BOTH, SERVER_ROLE_WORKER) and seconds >= DISPATCH_DEADLINE_SEC:
        raise RuntimeError(
            f"PIPELINE_TIMEOUT ({raw} = {seconds:.0f}s) は dispatch_deadline "
            f"({DISPATCH_DEADLINE_SEC}s) より短くしてください。"
            "長いと Cloud Tasks が先にリクエストを打ち切り、失敗の記録も Slack 通知も残らず、"
            "max_attempts = 1 なので再試行もされません。"
        )
    return seconds


def _require_env(key: str) -> str:
    """Get a required environment variable, and raise an exception if it is not set."""
    value = os.getenv(key, "").strip()
    if not value:
        raise ValueError(f"{key} environment variable is required")
    return value


def _split_env(key: str) -> list[str]:
    """Convert a comma-separated environment variable to a list of lowercase strings."""
    return [v.strip().lower() for v in os.getenv(key, "").split(",") if v.strip()]
