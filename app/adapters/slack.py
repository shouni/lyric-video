from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_COMPLETE_TITLE = "✅ Lyric Video 処理が完了しました"
_ERROR_TITLE = "❌ Lyric Video 処理中にエラーが発生しました"


class SlackNotifier:
    def __init__(self, webhook_url: str, service_url: str = "") -> None:
        """Slack通知に使うWebhook URLとサービスURLを保持する。"""
        self.webhook_url = webhook_url.strip()
        self.service_url = service_url.rstrip("/")

    def notify_complete(self, job_id: str, output_uri: str) -> None:
        """ジョブ完了時のSlack通知を送信する。"""
        lines = [f"*Job ID:* `{job_id}`"]
        if output_uri:
            lines.append(f"*Output:* `{output_uri}`")
        try:
            self._send(_COMPLETE_TITLE, "\n".join(lines))
            logger.info("Slack completion notification sent job_id=%s", job_id)
        except Exception as exc:
            logger.error("Failed to send Slack notification job_id=%s: %s", job_id, exc)

    def notify_error(self, job_id: str, error: Exception | str) -> None:
        """ジョブ失敗時のSlack通知を送信する。"""
        error_msg = str(error)
        if len(error_msg) > 500:
            error_msg = error_msg[:500] + " ... (truncated)"
        lines = [
            f"*Job ID:* `{job_id}`",
            f"*Error:* {error_msg}",
        ]
        try:
            self._send(_ERROR_TITLE, "\n".join(lines))
            logger.info("Slack error notification sent job_id=%s", job_id)
        except Exception as exc:
            logger.error("Failed to send Slack error notification job_id=%s: %s", job_id, exc)

    def _send(self, title: str, text: str) -> None:
        """Slack Incoming Webhookへ通知本文を送信する。"""
        if not self.webhook_url:
            logger.info("Slack notification skipped (no webhook URL configured)")
            return
        payload = json.dumps({"text": f"*{title}*\n{text}"}).encode()
        req = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
        except urllib.error.HTTPError as exc:
            logger.error("Slack API error: %s %s", exc.code, exc.read().decode("utf-8"))
            raise
