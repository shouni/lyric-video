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


# YouTube Data API の thumbnails.set はメディアサイズ上限が 2MB。
# カバーアートは高解像度PNGで出力されるため、そのままだと超過することがある。
_YOUTUBE_THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024
# YouTubeサムネイルの推奨解像度。これを超える入力は縮小してから圧縮する。
_YOUTUBE_THUMBNAIL_MAX_SIZE = (1280, 720)


def _fetch_recipe_for_task(task: Task, ap_music_bucket: str) -> dict:
    """タスクのmusic_job_idから、YouTube投稿フォーム補助用のレシピ情報を取得する。

    レシピが存在しない・壊れている場合は空dictを返す（YouTube投稿フォームの
    タイトル・説明欄のデフォルト値を埋めるための補助情報であり、動画生成の
    成否には影響しないため）。

    以前は音声URIの拡張子を .mp3 -> .json と差し替えて探していたが、ap-music の
    レシピは audio.json ではなく recipe.json という別名で置かれるため当たらず、
    常に空dictを返していた。フォームの初期値が埋まらないだけで例外にはならないので
    表面化していなかった。
    """
    if not task.music_job_id:
        return {}
    try:
        return gcs.load_json(music_recipe_uri(ap_music_bucket, task.music_job_id))
    except Exception:
        return {}


def _prepare_thumbnail(image_bytes: bytes) -> BytesIO:
    """YouTubeサムネイルの上限(2MB)に収まるようJPEGへ再エンコードする。

    PNGはロスレスで容量が大きくなりがちなため、まず推奨解像度(1280x720)まで縮小し、
    さらに画質を段階的に落としながらJPEG再エンコードして上限内に収める。上限に収まらない
    ままアップロードするとYouTube側が400を返すため、最後は最低画質まで落とし切る。
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
    """Cloud Tasksから受け取ったジョブを検証し、動画生成パイプラインを実行する。"""
    cfg = current_app.config_obj
    notifier = current_app.notifier
    pipeline = current_app.pipeline

    auth_header = request.headers.get("Authorization", "")
    if not verify_task_caller(auth_header, cfg.task_audience_url, cfg.service_account_email):
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
    """Cloud Tasksから受け取ったYouTubeアップロードジョブを実行する。"""
    cfg = current_app.config_obj
    notifier = current_app.notifier
    uploader = current_app.youtube_uploader

    auth_header = request.headers.get("Authorization", "")
    if not verify_task_caller(auth_header, cfg.task_audience_url, cfg.service_account_email):
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
