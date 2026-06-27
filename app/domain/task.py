from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def new_job_id(prefix: str = "lyric") -> str:
    """接頭辞・UTC時刻・乱数からCloud Tasks向けのジョブIDを生成する。"""
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
        """タスク内容をJSON文字列へ変換する。"""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str | bytes) -> "Task":
        """JSON文字列またはバイト列からTaskを復元する。"""
        return cls(**json.loads(data))

    def validate(self, allowed_bucket: str = "") -> None:
        """タスクの必須項目・モデル名・GCS URIの妥当性を検証する。"""
        errors: list[str] = []
        if not _JOB_ID_PATTERN.match(self.job_id):
            errors.append(f"invalid job_id: {self.job_id!r}")
        if self.whisper_model not in {"large-v3", "medium", "small", "base"}:
            errors.append(f"invalid whisper_model: {self.whisper_model!r}")

        def _check_uri(url: str, field: str, required: bool = True) -> None:
            """各URLが必須条件と許可バケット制約を満たすか確認する。"""
            if not url:
                if required:
                    errors.append(f"{field} is required")
                return
            if not url.startswith("gs://"):
                errors.append(f"{field} must be a GCS URI (gs://...)")
            elif allowed_bucket and not url.startswith(f"gs://{allowed_bucket}/"):
                errors.append(f"{field} must be within bucket: {allowed_bucket}")

        _check_uri(self.audio_url, "audio_url")
        _check_uri(self.keyframes_url, "keyframes_url")
        _check_uri(self.subs_url, "subs_url", required=False)
        _check_uri(self.output_prefix, "output_prefix", required=False)
        if errors:
            raise ValueError("; ".join(errors))
