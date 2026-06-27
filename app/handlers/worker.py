from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.adapters import gcs
from app.domain.task import Task

logger = logging.getLogger(__name__)
worker_bp = Blueprint("worker", __name__)

_GOOGLE_AUTH_REQUEST = google_requests.Request()


@worker_bp.route("/tasks/generate", methods=["POST"])
def process_task():
    """Cloud Tasksから受け取ったジョブを検証し、動画生成パイプラインを実行する。"""
    cfg = current_app.config_obj
    notifier = current_app.notifier
    pipeline = current_app.pipeline

    auth_header = request.headers.get("Authorization", "")
    if not _verify_oidc_token(auth_header, cfg.task_audience_url):
        return jsonify({"error": "Unauthorized"}), 401

    try:
        task = Task.from_json(request.data)
        task.validate(allowed_bucket=cfg.gcs_bucket)
    except Exception as exc:
        logger.error("Invalid task payload: %s", exc)
        return jsonify({"error": str(exc)}), 400

    meta_uri = f"{task.output_prefix.rstrip('/')}/{task.job_id}/meta.json" if task.output_prefix else None
    _update_meta(meta_uri, {
        "status": "running",
        "job_id": task.job_id,
        "audio_url": task.audio_url,
        "keyframes_url": task.keyframes_url,
        "subs_url": task.subs_url,
        "whisper_model": task.whisper_model,
        "output_prefix": task.output_prefix,
        "created_at": task.created_at,
    })

    logger.info("Processing task job_id=%s", task.job_id)
    try:
        output_uri = pipeline.run(task)
        _update_meta(meta_uri, {"status": "complete", "output_uri": output_uri})
        notifier.notify_complete(task.job_id, output_uri)
        logger.info("Task completed job_id=%s output=%s", task.job_id, output_uri)
        return jsonify({"job_id": task.job_id, "status": "complete", "output": output_uri})
    except Exception as exc:
        logger.error("Task failed job_id=%s: %s", task.job_id, exc, exc_info=True)
        _update_meta(meta_uri, {"status": "failed", "error": str(exc)})
        notifier.notify_error(task.job_id, exc)
        return jsonify({"error": str(exc)}), 500


def _verify_oidc_token(authorization: str, audience: str) -> bool:
    """AuthorizationヘッダーのOIDCトークンを指定audienceで検証する。"""
    if not audience:
        logger.error("TASK_AUDIENCE_URL not configured — denying request to be safe")
        return False
    if not authorization.startswith("Bearer "):
        return False
    token = authorization[len("Bearer "):]
    try:
        id_token.verify_oauth2_token(token, _GOOGLE_AUTH_REQUEST, audience)
        return True
    except Exception as exc:
        logger.warning("OIDC verification failed: %s", exc)
        return False


def _update_meta(uri: str | None, updates: dict) -> None:
    if not uri:
        return
    try:
        try:
            meta = gcs.load_json(uri)
        except Exception:
            meta = {}
        meta.update(updates)
        gcs.save_json(meta, uri)
    except Exception as exc:
        logger.warning("Failed to update job metadata uri=%s: %s", uri, exc)
