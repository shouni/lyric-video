from __future__ import annotations

import json
import logging

from google.cloud import tasks_v2
from google.protobuf import duration_pb2

logger = logging.getLogger(__name__)

# ワーカーの応答を待つ時間。「待つ時間」ではなく **ワーカーの実行時間の実効上限** で、
# これを超えると処理中でも Cloud Tasks は待受を打ち切る。
#
# 未指定だと Cloud Tasks の既定 10 分が上限になり、Cloud Run 側の --timeout を
# いくら伸ばしても超えられない。字幕合成は ffmpeg を通すため 10 分に収まる保証がなく、
# 超えた分は lyric-video-queue が max_attempts = 1 のため再試行もされず失われる。
# HTTP ターゲットの上限である 30 分を取り、Cloud Run の timeout (1800s) と揃える。
_DISPATCH_DEADLINE_SEC = 1800


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

    def enqueue(self, payload: dict, worker_url: str | None = None) -> None:
        """ペイロードをHTTPタスクとしてCloud Tasksへ登録する。"""
        task = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": worker_url or self._worker_url,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload).encode(),
                "oidc_token": {
                    "service_account_email": self._service_account_email,
                    "audience": self._audience,
                },
            },
            "dispatch_deadline": duration_pb2.Duration(seconds=_DISPATCH_DEADLINE_SEC),
        }
        response = self._client.create_task(request={"parent": self._parent, "task": task})
        logger.info("Task enqueued name=%s", response.name)
