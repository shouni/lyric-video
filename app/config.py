from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    service_url: str
    port: str
    project_id: str
    location_id: str
    queue_id: str
    worker_url: str
    task_audience_url: str
    service_account_email: str
    gcs_bucket: str
    gcs_output_prefix: str
    slack_webhook_url: str
    whisper_model: str
    # OAuth2
    google_client_id: str
    google_client_secret: str
    session_secret: str
    allowed_emails: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)

    def is_secure_url(self) -> bool:
        return self.service_url.startswith("https://")

    @classmethod
    def from_env(cls) -> "Config":
        service_url = os.getenv("SERVICE_URL", "http://localhost:8080").rstrip("/")
        worker_url = os.getenv("WORKER_URL", "").strip()
        if not worker_url:
            worker_url = f"{service_url}/tasks/generate"
        task_audience_url = os.getenv("TASK_AUDIENCE_URL", "").strip() or service_url
        return cls(
            service_url=service_url,
            port=os.getenv("PORT", "8080"),
            project_id=os.getenv("GCP_PROJECT_ID", ""),
            location_id=os.getenv("GCP_LOCATION_ID", "asia-northeast1"),
            queue_id=os.getenv("CLOUD_TASKS_QUEUE_ID", ""),
            worker_url=worker_url,
            task_audience_url=task_audience_url,
            service_account_email=os.getenv("SERVICE_ACCOUNT_EMAIL", ""),
            gcs_bucket=os.getenv("GCS_BUCKET", ""),
            gcs_output_prefix=os.getenv("GCS_OUTPUT_PREFIX", "lyric-video/output"),
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", ""),
            whisper_model=os.getenv("WHISPER_MODEL", "large-v3"),
            google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
            session_secret=os.getenv("SESSION_SECRET", ""),
            allowed_emails=_split_env("ALLOWED_EMAILS"),
            allowed_domains=_split_env("ALLOWED_DOMAINS"),
        )


def _split_env(key: str) -> list[str]:
    return [v.strip().lower() for v in os.getenv(key, "").split(",") if v.strip()]
