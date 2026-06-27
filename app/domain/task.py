from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def new_job_id(prefix: str = "lyric") -> str:
    prefix = re.sub(r"[^a-z0-9]", "", prefix.lower()) or "job"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rand = secrets.token_hex(6)
    return f"{prefix}-{ts}-{rand}"


@dataclass
class Task:
    job_id: str
    audio_url: str
    keyframes_url: str
    subs_url: str = ""
    whisper_model: str = "large-v3"
    output_prefix: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str | bytes) -> "Task":
        return cls(**json.loads(data))

    def validate(self) -> None:
        errors: list[str] = []
        if not _JOB_ID_PATTERN.match(self.job_id):
            errors.append(f"invalid job_id: {self.job_id!r}")
        if not self.audio_url.startswith("gs://"):
            errors.append("audio_url must be a GCS URI (gs://...)")
        if not self.keyframes_url.startswith("gs://"):
            errors.append("keyframes_url must be a GCS URI (gs://...)")
        if self.subs_url and not self.subs_url.startswith("gs://"):
            errors.append("subs_url must be a GCS URI (gs://...) if provided")
        if self.output_prefix and not self.output_prefix.startswith("gs://"):
            errors.append("output_prefix must be a GCS URI (gs://...) if provided")
        if errors:
            raise ValueError("; ".join(errors))
