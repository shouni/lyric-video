from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Literal

TITLE_SUFFIX = " | 【AI音楽 / リリックMV】Digital Armor Style"
MAX_YOUTUBE_TITLE_LENGTH = 100
MAX_YOUTUBE_TITLE_BASE_LENGTH = MAX_YOUTUBE_TITLE_LENGTH - len(TITLE_SUFFIX)

# Tags and hashtags that are always added, across the whole channel
FIXED_TAGS = ["lyric video", "lyria3", "AI音楽", "Digital Armor Style"]
FIXED_HASHTAGS = "#AI音楽 #lyricvideo #lyria3"


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
    def from_json(cls, data: str | bytes) -> YouTubeTask:
        return cls(**json.loads(data))

    def full_title(self) -> str:
        """Return the title sent to YouTube (with the suffix, within 100 characters).

        If the suffix is already there, the title is used as is, so that the suffix is not added
        twice when a deleted video is uploaded again.
        """
        if self.title.endswith(TITLE_SUFFIX):
            return self.title
        return self.title[:MAX_YOUTUBE_TITLE_BASE_LENGTH] + TITLE_SUFFIX

    def full_description(self) -> str:
        """Return the description with the fixed hashtags appended at its end."""
        if not self.description:
            return FIXED_HASHTAGS
        return self.description.rstrip() + "\n\n" + FIXED_HASHTAGS

    def tag_list(self) -> list[str]:
        """Split the comma-separated tags into a list and insert the fixed tags at the front (without duplicates)."""
        tags = [t.strip() for t in self.tags.split(",") if t.strip()]
        for tag in reversed(FIXED_TAGS):
            if tag not in tags:
                tags.insert(0, tag)
        return tags
