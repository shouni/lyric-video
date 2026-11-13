from __future__ import annotations

import logging
from io import BytesIO

from flask import Blueprint, current_app, jsonify, request
from PIL import Image

from app.adapters import gcs, job_store
from app.adapters.oidc import verify_task_caller
from app.adapters.youtube import extract_video_id
from app.domain.music_layout import music_recipe_uri
from app.domain.task import Task
from app.domain.youtube_task import YouTubeTask

logger = logging.getLogger(__name__)
worker_bp = Blueprint("worker", __name__)


# thumbnails.set in the YouTube Data API has a media size limit of 2MB.
# Cover art is output as a high-resolution PNG, so as is it can exceed the limit.
_YOUTUBE_THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024
# Recommended resolution for a YouTube thumbnail. Input larger than this is scaled down before compressing.
_YOUTUBE_THUMBNAIL_MAX_SIZE = (1280, 720)


def _fetch_recipe_for_task(task: Task, ap_music_bucket: str) -> dict:
    """Fetch the recipe used to help fill the YouTube upload form, from the task's music_job_id.

    Returns an empty dict if the recipe does not exist or is corrupt (it is supplementary information
    for filling the default title and description of the YouTube upload form, and does not affect
    whether video generation succeeds).

    This used to look for it by swapping the extension of the audio URI from .mp3 to .json, but
    ap-music's recipe is stored under a different name, recipe.json, not audio.json, so the lookup
    never matched and this always returned an empty dict. It did not surface because the only effect
    was that the form's initial values were not filled, with no exception.
    """
    if not task.music_job_id:
        return {}
    try:
        return gcs.load_json(music_recipe_uri(ap_music_bucket, task.music_job_id))
    except Exception:
        return {}


def _prepare_thumbnail(image_bytes: bytes) -> BytesIO:
    """Re-encode to JPEG so that it fits within the YouTube thumbnail limit (2MB).

    PNG is lossless and tends to be large, so the image is first scaled down to the recommended
    resolution (1280x720), and then re-encoded as JPEG while lowering the quality step by step until
    it fits within the limit. Uploading while still over the limit makes YouTube return 400, so in
    the end the quality is lowered all the way to the minimum.
    """
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    img.thumbnail(_YOUTUBE_THUMBNAIL_MAX_SIZE, Image.LANCZOS)
    for quality in (90, 80, 70, 60, 50, 40, 30, 20):
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= _YOUTUBE_THUMBNAIL_MAX_BYTES:
            buf.seek(0)
            return buf
    raise ValueError(f"thumbnail exceeds {_YOUTUBE_THUMBNAIL_MAX_BYTES} bytes even at lowest quality")


@worker_bp.route("/tasks/generate", methods=["POST"])
def process_task():
    """Validate a job received from Cloud Tasks and run the video generation pipeline."""
    cfg = current_app.config_obj
    notifier = current_app.notifier
    pipeline = current_app.pipeline

    auth_header = request.headers.get("Authorization", "")
    if not verify_task_caller(auth_header, cfg.task_audience_url, cfg.allowed_task_service_accounts):
        return jsonify({"error": "Unauthorized"}), 401

    try:
        task = Task.from_json(request.data)
        task.validate(allowed_bucket=cfg.gcs_bucket)
    except Exception as exc:
        logger.error("Invalid task payload: %s", exc)
        return jsonify({"error": str(exc)}), 400

    recipe = _fetch_recipe_for_task(task, cfg.ap_music_bucket)
    meta_uri = job_store.meta_uri(task.output_prefix, task.job_id)
    job_store.update(
        meta_uri,
        {
            "status": "running",
            "job_id": task.job_id,
            "subs_url": task.subs_url,
            "whisper_model": task.whisper_model,
            "output_prefix": task.output_prefix,
            "created_at": task.created_at,
            "source_type": task.source_type,
            "ap_mv_job_id": task.ap_mv_job_id,
            "music_job_id": task.music_job_id,
            "theme": recipe.get("theme", ""),
            "title": recipe.get("title", ""),
            "hook": (recipe.get("lyrics") or {}).get("hook", ""),
        },
    )

    logger.info("Processing task job_id=%s", task.job_id)
    try:
        output_uri = pipeline.run(task)
        job_store.update(meta_uri, {"status": "complete", "output_uri": output_uri})
        notifier.notify_complete(task.job_id, output_uri)
        logger.info("Task completed job_id=%s output=%s", task.job_id, output_uri)
        return jsonify({"job_id": task.job_id, "status": "complete", "output": output_uri})
    except Exception as exc:
        logger.error("Task failed job_id=%s: %s", task.job_id, exc, exc_info=True)
        job_store.update(meta_uri, {"status": "failed", "error": str(exc)})
        notifier.notify_error(task.job_id, exc)
        return jsonify({"error": str(exc)}), 500


@worker_bp.route("/tasks/youtube", methods=["POST"])
def process_youtube():
    """Run a YouTube upload job received from Cloud Tasks."""
    cfg = current_app.config_obj
    notifier = current_app.notifier
    uploader = current_app.youtube_uploader

    auth_header = request.headers.get("Authorization", "")
    if not verify_task_caller(auth_header, cfg.task_audience_url, cfg.allowed_task_service_accounts):
        return jsonify({"error": "Unauthorized"}), 401

    if uploader is None:
        return jsonify({"error": "YouTube uploader not configured"}), 501

    try:
        yt_task = YouTubeTask.from_json(request.data)
    except Exception as exc:
        logger.error("Invalid YouTube task payload: %s", exc)
        return jsonify({"error": str(exc)}), 400

    meta_uri = job_store.meta_uri(cfg.default_output_prefix(), yt_task.job_id)

    try:
        meta = job_store.load(meta_uri)
        if meta.get("youtube_status") == "complete" or meta.get("youtube_url"):
            youtube_url = meta.get("youtube_url", "")
            video_id = extract_video_id(youtube_url)
            if video_id and uploader.video_exists(video_id):
                logger.info("YouTube upload already completed job_id=%s", yt_task.job_id)
                return jsonify({"status": "already_completed"}), 200
            logger.info("YouTube video deleted, re-uploading job_id=%s video_id=%s", yt_task.job_id, video_id)
            job_store.update(meta_uri, {"youtube_status": None, "youtube_url": None})
    except Exception as exc:
        logger.warning("Failed to check meta job_id=%s: %s", yt_task.job_id, exc)

    job_store.update(meta_uri, {"youtube_status": "uploading"})

    logger.info("Starting YouTube upload job_id=%s", yt_task.job_id)
    try:
        with gcs.open_blob(yt_task.output_uri) as f:
            video_id = uploader.upload_from_stream(
                f,
                title=yt_task.full_title(),
                description=yt_task.full_description(),
                tags=yt_task.tag_list(),
                privacy=yt_task.privacy,
            )

        if yt_task.thumbnail_uri:
            try:
                with gcs.open_blob(yt_task.thumbnail_uri) as img:
                    thumbnail = _prepare_thumbnail(img.read())
                uploader.set_thumbnail(video_id, thumbnail, mimetype="image/jpeg")
            except Exception as exc:
                logger.warning("Failed to set thumbnail job_id=%s: %s", yt_task.job_id, exc)

        youtube_url = f"https://www.youtube.com/watch?v={video_id}"
        job_store.update(meta_uri, {"youtube_status": "complete", "youtube_url": youtube_url})
        notifier.notify_complete(yt_task.job_id, yt_task.output_uri, youtube_url=youtube_url)
        logger.info("YouTube upload complete job_id=%s url=%s", yt_task.job_id, youtube_url)
        return jsonify({"job_id": yt_task.job_id, "youtube_url": youtube_url})
    except Exception as exc:
        logger.error("YouTube upload failed job_id=%s: %s", yt_task.job_id, exc, exc_info=True)
        job_store.update(meta_uri, {"youtube_status": "failed", "youtube_error": str(exc)})
        notifier.notify_error(yt_task.job_id, f"YouTube upload failed: {exc}")
        if hasattr(exc, "resp") and hasattr(exc.resp, "status") and 400 <= exc.resp.status < 500:
            logger.info("Dropping unrecoverable task job_id=%s status=%s", yt_task.job_id, exc.resp.status)
            return jsonify({"error": str(exc), "dropped": True}), 200
        return jsonify({"error": str(exc)}), 500
