from __future__ import annotations

import json
import logging

from google.cloud import tasks_v2
from google.protobuf import duration_pb2

logger = logging.getLogger(__name__)

# How long to wait for the worker's response. It is not a "time to wait" but the **effective upper limit on
# the worker's run time**: past it, Cloud Tasks stops waiting even while the work is still running.
#
# Unspecified, the Cloud Tasks default of 10 minutes becomes the limit, and it cannot be exceeded however far
# Cloud Run's --timeout is extended. Burning subtitles goes through ffmpeg, so there is no guarantee that it
# fits in 10 minutes, and whatever runs over is lost without even a retry, because lyric-video-queue has
# max_attempts = 1. This takes 30 minutes, the upper limit for HTTP targets, and matches Cloud Run's
# timeout (1800s).
#
# This is the **middle** of the three timeout levels. PIPELINE_TIMEOUT must be shorter than this
# (config._pipeline_timeout_sec checks it at startup). The reason is in pipeline/deadline.py.
DISPATCH_DEADLINE_SEC = 1800


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
        """Initialize the connection details of the Cloud Tasks queue and the settings for calling the worker."""
        self._client = tasks_v2.CloudTasksClient()
        self._parent = self._client.queue_path(project_id, location_id, queue_id)
        self._worker_url = worker_url
        self._service_account_email = service_account_email
        self._audience = audience

    def enqueue(self, payload: dict, worker_url: str | None = None) -> None:
        """Register the payload with Cloud Tasks as an HTTP task."""
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
            "dispatch_deadline": duration_pb2.Duration(seconds=DISPATCH_DEADLINE_SEC),
        }
        response = self._client.create_task(request={"parent": self._parent, "task": task})
        logger.info("Task enqueued name=%s", response.name)
