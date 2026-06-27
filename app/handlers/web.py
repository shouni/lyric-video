from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.domain.task import Task, new_job_id

logger = logging.getLogger(__name__)
router = APIRouter()
_templates = Jinja2Templates(directory="templates")


@router.get("/", response_class=HTMLResponse)
async def get_form(request: Request):
    cfg = request.app.state.config
    default_output = f"gs://{cfg.gcs_bucket}/{cfg.gcs_output_prefix}" if cfg.gcs_bucket else ""
    return _templates.TemplateResponse(
        "index.html",
        {"request": request, "default_output": default_output, "whisper_model": cfg.whisper_model},
    )


@router.post("/", response_class=HTMLResponse)
async def post_form(
    request: Request,
    audio_url: str = Form(...),
    keyframes_url: str = Form(...),
    subs_url: str = Form(""),
    whisper_model: str = Form("large-v3"),
    output_prefix: str = Form(""),
):
    cfg = request.app.state.config
    queue = request.app.state.queue

    if not output_prefix:
        output_prefix = f"gs://{cfg.gcs_bucket}/{cfg.gcs_output_prefix}"

    job_id = new_job_id("lyric")
    task = Task(
        job_id=job_id,
        audio_url=audio_url.strip(),
        keyframes_url=keyframes_url.strip(),
        subs_url=subs_url.strip(),
        whisper_model=whisper_model,
        output_prefix=output_prefix.strip(),
    )

    try:
        task.validate()
    except ValueError as exc:
        default_output = f"gs://{cfg.gcs_bucket}/{cfg.gcs_output_prefix}" if cfg.gcs_bucket else ""
        return _templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "error": str(exc),
                "default_output": default_output,
                "whisper_model": cfg.whisper_model,
            },
            status_code=400,
        )

    if queue is None:
        logger.warning("No queue configured — task not enqueued job_id=%s", job_id)
    else:
        try:
            queue.enqueue(asdict(task))
        except Exception as exc:
            logger.error("Failed to enqueue task job_id=%s: %s", job_id, exc)
            default_output = f"gs://{cfg.gcs_bucket}/{cfg.gcs_output_prefix}" if cfg.gcs_bucket else ""
            return _templates.TemplateResponse(
                "index.html",
                {
                    "request": request,
                    "error": f"タスクのキュー追加に失敗しました: {exc}",
                    "default_output": default_output,
                    "whisper_model": cfg.whisper_model,
                },
                status_code=502,
            )

    return _templates.TemplateResponse(
        "queued.html",
        {"request": request, "job_id": job_id},
        status_code=202,
    )
