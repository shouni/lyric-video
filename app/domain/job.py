from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.domain.task import Task


@dataclass
class JobRecord:
    job_id: str
    status: str  # queued | running | complete | failed
    created_at: str
    audio_url: str
    keyframes_url: str
    subs_url: str = ""
    whisper_model: str = "large-v3"
    output_prefix: str = ""
    output_uri: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "JobRecord":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})

    @classmethod
    def from_task(cls, task: "Task") -> "JobRecord":
        return cls(
            job_id=task.job_id,
            status="queued",
            created_at=task.created_at or datetime.now(timezone.utc).isoformat(),
            audio_url=task.audio_url,
            keyframes_url=task.keyframes_url,
            subs_url=task.subs_url,
            whisper_model=task.whisper_model,
            output_prefix=task.output_prefix,
        )
