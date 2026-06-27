from __future__ import annotations

import json
import logging

from google.cloud import tasks_v2

logger = logging.getLogger(__name__)


class CloudTasksQueue:
    def __init__(
        self,
        project_id: str,
        location_id: str,
        queue_id: str,
        worker_url: str,
        service_account_email: str,
        audience: str,
    ) -> None:
        """Cloud Tasksキューへの接続情報とワーカー呼び出し設定を初期化する。"""
        self._client = tasks_v2.CloudTasksClient()
        self._parent = self._client.queue_path(project_id, location_id, queue_id)
        self._worker_url = worker_url
        self._service_account_email = service_account_email
        self._audience = audience

    def enqueue(self, payload: dict) -> None:
        """ペイロードをHTTPタスクとしてCloud Tasksへ登録する。"""
        task = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": self._worker_url,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload).encode(),
                "oidc_token": {
                    "service_account_email": self._service_account_email,
                    "audience": self._audience,
                },
            }
        }
        response = self._client.create_task(request={"parent": self._parent, "task": task})
        logger.info("Task enqueued name=%s", response.name)
