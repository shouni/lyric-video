from __future__ import annotations

import logging

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB


class YouTubeUploader:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str) -> None:
        self._credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=_TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret,
            scopes=_SCOPES,
        )

    def upload(
        self,
        video_path: str,
        title: str,
        description: str = "",
        tags: list[str] | None = None,
        privacy: str = "private",
    ) -> str:
        """MP4ファイルをYouTubeにアップロードし、動画IDを返す。"""
        youtube = build("youtube", "v3", credentials=self._credentials, cache_discovery=False)
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags or [],
                "categoryId": "10",  # Music
            },
            "status": {
                "privacyStatus": privacy,
            },
        }
        media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True, chunksize=_CHUNK_SIZE)
        insert_request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        while response is None:
            status, response = insert_request.next_chunk()
            if status:
                logger.info("YouTube upload progress: %d%%", int(status.progress() * 100))

        video_id = response["id"]
        logger.info("YouTube upload complete video_id=%s", video_id)
        return video_id
