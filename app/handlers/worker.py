from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.adapters import gcs
from app.domain.task import Task
from app.domain.youtube_task import MAX_YOUTUBE_TITLE_BASE_LENGTH, TITLE_SUFFIX, YouTubeTask

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
    if not _verify_oidc_token(auth_header, cfg.task_audience_url, cfg.service_account_email):
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


@worker_bp.route("/tasks/youtube", methods=["POST"])
def process_youtube():
    """Cloud Tasksから受け取ったYouTubeアップロードジョブを実行する。"""
    cfg = current_app.config_obj
    notifier = current_app.notifier
    uploader = current_app.youtube_uploader

    auth_header = request.headers.get("Authorization", "")
    if not _verify_oidc_token(auth_header, cfg.task_audience_url, cfg.service_account_email):
        return jsonify({"error": "Unauthorized"}), 401

    if uploader is None:
        return jsonify({"error": "YouTube uploader not configured"}), 501

    try:
        yt_task = YouTubeTask.from_json(request.data)
    except Exception as exc:
        logger.error("Invalid YouTube task payload: %s", exc)
        return jsonify({"error": str(exc)}), 400

    output_prefix = cfg.default_output_prefix()
    meta_uri = f"{output_prefix.rstrip('/')}/{yt_task.job_id}/meta.json"

    try:
        meta = gcs.load_json(meta_uri)
        if meta.get("youtube_status") == "complete" or meta.get("youtube_url"):
            youtube_url = meta.get("youtube_url", "")
            video_id = youtube_url.rstrip("/").rsplit("/", 1)[-1] if youtube_url else ""
            if video_id and uploader.video_exists(video_id):
                logger.info("YouTube upload already completed job_id=%s", yt_task.job_id)
                return jsonify({"status": "already_completed"}), 200
            logger.info("YouTube video deleted, re-uploading job_id=%s video_id=%s", yt_task.job_id, video_id)
            _update_meta(meta_uri, {"youtube_status": None, "youtube_url": None})
    except Exception as exc:
        logger.warning("Failed to check meta job_id=%s: %s", yt_task.job_id, exc)

    _update_meta(meta_uri, {"youtube_status": "uploading"})

    logger.info("Starting YouTube upload job_id=%s", yt_task.job_id)
    try:
        _FIXED_TAGS = ["lyric video", "lyria3", "AI音楽", "Digital Armor Style"]
        tags = [t.strip() for t in yt_task.tags.split(",") if t.strip()]
        for tag in reversed(_FIXED_TAGS):
            if tag not in tags:
                tags.insert(0, tag)
        if yt_task.title.endswith(TITLE_SUFFIX):
            title = yt_task.title
        else:
            title = yt_task.title[:MAX_YOUTUBE_TITLE_BASE_LENGTH] + TITLE_SUFFIX
        _FIXED_HASHTAGS = "#AI音楽 #lyricvideo #lyria3"
        description = yt_task.description.rstrip() + "\n\n" + _FIXED_HASHTAGS if yt_task.description else _FIXED_HASHTAGS
        with gcs.open_blob(yt_task.output_uri) as f:
            video_id = uploader.upload_from_stream(
                f,
                title=title,
                description=description,
                tags=tags,
                privacy=yt_task.privacy,
            )

        if yt_task.thumbnail_uri:
            try:
                mimetype = "image/png" if yt_task.thumbnail_uri.lower().endswith(".png") else "image/jpeg"
                with gcs.open_blob(yt_task.thumbnail_uri) as img:
                    uploader.set_thumbnail(video_id, img, mimetype=mimetype)
            except Exception as exc:
                logger.warning("Failed to set thumbnail job_id=%s: %s", yt_task.job_id, exc)

        youtube_url = f"https://youtu.be/{video_id}"
        _update_meta(meta_uri, {"youtube_status": "complete", "youtube_url": youtube_url})
        notifier.notify_complete(yt_task.job_id, yt_task.output_uri, youtube_url=youtube_url)
        logger.info("YouTube upload complete job_id=%s url=%s", yt_task.job_id, youtube_url)
        return jsonify({"job_id": yt_task.job_id, "youtube_url": youtube_url})
    except Exception as exc:
        logger.error("YouTube upload failed job_id=%s: %s", yt_task.job_id, exc, exc_info=True)
        _update_meta(meta_uri, {"youtube_status": "failed", "youtube_error": str(exc)})
        notifier.notify_error(yt_task.job_id, f"YouTube upload failed: {exc}")
        if hasattr(exc, "resp") and hasattr(exc.resp, "status") and 400 <= exc.resp.status < 500:
            logger.info("Dropping unrecoverable task job_id=%s status=%s", yt_task.job_id, exc.resp.status)
            return jsonify({"error": str(exc), "dropped": True}), 200
        return jsonify({"error": str(exc)}), 500


def _verify_oidc_token(authorization: str, audience: str, expected_email: str = "") -> bool:
    """AuthorizationヘッダーのOIDCトークンをaudienceとservice account emailで検証する。"""
    if not audience:
        logger.error("TASK_AUDIENCE_URL not configured — denying request to be safe")
        return False
    if not authorization.startswith("Bearer "):
        return False
    token = authorization[len("Bearer "):]
    try:
        id_info = id_token.verify_oauth2_token(token, _GOOGLE_AUTH_REQUEST, audience)
        if expected_email and id_info.get("email") != expected_email:
            logger.warning("OIDC email mismatch: expected=%s got=%s", expected_email, id_info.get("email"))
            return False
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
