from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Literal

TITLE_SUFFIX = " | 【AI音楽 / リリックビデオ】Digital Armor Style"
MAX_YOUTUBE_TITLE_LENGTH = 100
MAX_YOUTUBE_TITLE_BASE_LENGTH = MAX_YOUTUBE_TITLE_LENGTH - len(TITLE_SUFFIX)


@dataclass
class YouTubeTask:
    job_id: str
    output_uri: str
    title: str
    description: str = ""
    tags: str = ""
    privacy: Literal["private", "unlisted", "public"] = "private"
    thumbnail_uri: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str | bytes) -> "YouTubeTask":
        return cls(**json.loads(data))
