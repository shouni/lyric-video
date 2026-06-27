from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.domain.task import Task

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/tasks/generate")
async def process_task(request: Request):
    cfg = request.app.state.config
    notifier = request.app.state.notifier
    pipeline = request.app.state.pipeline

    auth_header = request.headers.get("Authorization", "")
    if not _verify_oidc_token(auth_header, cfg.task_audience_url):
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.body()
    try:
        task = Task.from_json(body)
        task.validate()
    except Exception as exc:
        logger.error("Invalid task payload: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=400)

    logger.info("Processing task job_id=%s", task.job_id)
    try:
        output_uri = await run_in_threadpool(pipeline.run, task)
        notifier.notify_complete(task.job_id, output_uri)
        logger.info("Task completed job_id=%s output=%s", task.job_id, output_uri)
        return JSONResponse({"job_id": task.job_id, "status": "complete", "output": output_uri})
    except Exception as exc:
        logger.error("Task failed job_id=%s: %s", task.job_id, exc, exc_info=True)
        notifier.notify_error(task.job_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))


def _verify_oidc_token(authorization: str, audience: str) -> bool:
    if not audience:
        logger.warning("TASK_AUDIENCE_URL not configured — skipping OIDC verification")
        return True
    if not authorization.startswith("Bearer "):
        return False
    token = authorization[len("Bearer "):]
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
        id_token.verify_oauth2_token(token, google_requests.Request(), audience)
        return True
    except Exception as exc:
        logger.warning("OIDC verification failed: %s", exc)
        return False
