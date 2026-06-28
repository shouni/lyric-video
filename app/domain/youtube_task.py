from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass
class YouTubeTask:
    job_id: str
    output_uri: str
    title: str
    description: str = ""
    tags: str = ""
    privacy: str = "private"

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str | bytes) -> "YouTubeTask":
        return cls(**json.loads(data))
