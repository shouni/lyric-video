from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Literal

TITLE_SUFFIX = " | 【AI音楽 / リリックMV】Digital Armor Style"
MAX_YOUTUBE_TITLE_LENGTH = 100
MAX_YOUTUBE_TITLE_BASE_LENGTH = MAX_YOUTUBE_TITLE_LENGTH - len(TITLE_SUFFIX)

# チャンネル横断で必ず付与するタグ・ハッシュタグ
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
        """YouTubeへ送るタイトル（サフィックス付き・100文字以内）を返す。

        削除済み動画の再アップロード時にサフィックスが二重に付かないよう、
        既に付いている場合はそのまま使う。
        """
        if self.title.endswith(TITLE_SUFFIX):
            return self.title
        return self.title[:MAX_YOUTUBE_TITLE_BASE_LENGTH] + TITLE_SUFFIX

    def full_description(self) -> str:
        """説明欄の末尾に固定ハッシュタグを付与した本文を返す。"""
        if not self.description:
            return FIXED_HASHTAGS
        return self.description.rstrip() + "\n\n" + FIXED_HASHTAGS

    def tag_list(self) -> list[str]:
        """カンマ区切りのタグを配列にし、固定タグを（重複させずに）先頭へ差し込む。"""
        tags = [t.strip() for t in self.tags.split(",") if t.strip()]
        for tag in reversed(FIXED_TAGS):
            if tag not in tags:
                tags.insert(0, tag)
        return tags
