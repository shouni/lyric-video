from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

_COMPLETE_TITLE = "✅ Lyric Video 処理が完了しました"
_ERROR_TITLE = "❌ Lyric Video 処理中にエラーが発生しました"


class SlackNotifier:
    def __init__(self, webhook_url: str, service_url: str = "") -> None:
        self.webhook_url = webhook_url.strip()
        self.service_url = service_url.rstrip("/")

    def _send(self, title: str, text: str) -> None:
        if not self.webhook_url:
            logger.info("Slack notification skipped (no webhook URL configured)")
            return
        payload = json.dumps({"text": f"*{title}*\n{text}"}).encode()
        req = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10):
            pass

    def notify_complete(self, job_id: str, output_uri: str) -> None:
        lines = [f"*Job ID:* `{job_id}`"]
        if output_uri:
            lines.append(f"*Output:* `{output_uri}`")
        try:
            self._send(_COMPLETE_TITLE, "\n".join(lines))
            logger.info("Slack completion notification sent job_id=%s", job_id)
        except Exception as exc:
            logger.error("Failed to send Slack notification job_id=%s: %s", job_id, exc)

    def notify_error(self, job_id: str, error: Exception | str) -> None:
        lines = [
            f"*Job ID:* `{job_id}`",
            f"*Error:* {error}",
        ]
        try:
            self._send(_ERROR_TITLE, "\n".join(lines))
            logger.info("Slack error notification sent job_id=%s", job_id)
        except Exception as exc:
            logger.error("Failed to send Slack error notification job_id=%s: %s", job_id, exc)
