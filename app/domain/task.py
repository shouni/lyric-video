from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def new_job_id(prefix: str = "lyric") -> str:
    """接頭辞・UTC時刻・乱数からCloud Tasks向けのジョブIDを生成する。"""
    prefix = re.sub(r"[^a-z0-9]", "", prefix.lower()) or "job"
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    rand = secrets.token_hex(6)
    return f"{prefix}-{ts}-{rand}"


@dataclass
class Task:
    job_id: str
    audio_url: str = ""
    keyframes_url: str = ""
    subs_url: str = ""
    whisper_model: str = "large-v3"
    output_prefix: str = ""
    # source_type == "video" のとき、audio_url/keyframes_url の代わりに
    # ap_mv_job_id/music_job_id からap-mv/ap-musicの成果物を直接参照する
    # （静止画スライドショーではなく、Veo生成済みの実写動画を使う経路）。
    source_type: str = "images"
    ap_mv_job_id: str = ""
    music_job_id: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_json(self) -> str:
        """タスク内容をJSON文字列へ変換する。"""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str | bytes) -> Task:
        """JSON文字列またはバイト列からTaskを復元する。"""
        return cls(**json.loads(data))

    def validate(self, allowed_bucket: str = "") -> None:
        """タスクの必須項目・モデル名・GCS URIの妥当性を検証する。"""
        errors: list[str] = []
        if not _JOB_ID_PATTERN.match(self.job_id):
            errors.append(f"invalid job_id: {self.job_id!r}")
        if self.whisper_model not in {"large-v3", "medium", "small", "base"}:
            errors.append(f"invalid whisper_model: {self.whisper_model!r}")
        if self.source_type not in {"images", "video"}:
            errors.append(f"invalid source_type: {self.source_type!r}")

        def _check_gcs_uri(url: str, field: str, required: bool = True) -> None:
            if not url:
                if required:
                    errors.append(f"{field} is required")
                return
            if not url.startswith("gs://"):
                errors.append(f"{field} must be a GCS URI (gs://...)")

        def _check_output_uri(url: str, field: str) -> None:
            _check_gcs_uri(url, field, required=False)
            if url and allowed_bucket and not url.startswith(f"gs://{allowed_bucket}/"):
                errors.append(f"{field} must be within bucket: {allowed_bucket}")

        def _check_job_id_ref(value: str, field: str) -> None:
            if not value:
                errors.append(f"{field} is required")
            elif not _JOB_ID_PATTERN.match(value):
                errors.append(f"invalid {field}: {value!r}")

        if self.source_type == "video":
            _check_job_id_ref(self.ap_mv_job_id, "ap_mv_job_id")
            _check_job_id_ref(self.music_job_id, "music_job_id")
        else:
            _check_gcs_uri(self.audio_url, "audio_url")
            _check_gcs_uri(self.keyframes_url, "keyframes_url")
            _check_gcs_uri(self.subs_url, "subs_url", required=False)

        _check_output_uri(self.output_prefix, "output_prefix")
        if errors:
            raise ValueError("; ".join(errors))
