from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


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
    def from_task(cls, task) -> "JobRecord":
        from dataclasses import asdict as _asdict
        d = _asdict(task)
        return cls(
            job_id=d["job_id"],
            status="queued",
            created_at=d.get("created_at") or datetime.now(timezone.utc).isoformat(),
            audio_url=d["audio_url"],
            keyframes_url=d["keyframes_url"],
            subs_url=d.get("subs_url", ""),
            whisper_model=d.get("whisper_model", "large-v3"),
            output_prefix=d.get("output_prefix", ""),
        )
