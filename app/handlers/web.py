from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import asdict

from flask import Blueprint, abort, current_app, render_template, request, session

from app.adapters import gcs
from app.domain.job import JobRecord
from app.domain.task import Task, new_job_id
from app.domain.youtube_task import YouTubeTask

logger = logging.getLogger(__name__)
web_bp = Blueprint("web", __name__)


@web_bp.route("/", methods=["GET"])
def home():
    """ホームダッシュボードを表示し、最新ジョブ5件を一覧する。"""
    cfg = current_app.config_obj
    jobs = []
    if cfg.gcs_bucket:
        try:
            jobs = gcs.list_job_metadata(cfg.default_output_prefix())[:5]
        except Exception as exc:
            logger.error("Failed to list jobs for home: %s", exc)
    return render_template("home.html", jobs=jobs)


@web_bp.route("/new", methods=["GET"])
def get_form():
    """新規作成フォームを表示し、初回アクセス時はCSRFトークンを発行する。"""
    cfg = current_app.config_obj
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return render_template(
        "new.html",
        whisper_model=cfg.whisper_model,
        csrf_token=session["csrf_token"],
    )


@web_bp.route("/new", methods=["POST"])
def post_form():
    """フォーム送信を受け取り、入力値を検証してCloud Tasksへジョブを登録する。"""
    token = session.get("csrf_token")
    if not token or token != request.form.get("csrf_token"):
        abort(403)

    cfg = current_app.config_obj
    queue = current_app.queue

    output_prefix = cfg.default_output_prefix()
    job_id = new_job_id("lyric")
    task = Task(
        job_id=job_id,
        audio_url=request.form.get("audio_url", "").strip(),
        keyframes_url=request.form.get("keyframes_url", "").strip(),
        subs_url=request.form.get("subs_url", "").strip(),
        whisper_model=request.form.get("whisper_model", "large-v3"),
        output_prefix=output_prefix,
    )

    def render_error(error: str, status: int):
        return render_template(
            "new.html",
            error=error,
            whisper_model=task.whisper_model,
            csrf_token=session.get("csrf_token"),
            audio_url=task.audio_url,
            keyframes_url=task.keyframes_url,
            subs_url=task.subs_url,
        ), status

    try:
        task.validate(allowed_bucket=cfg.gcs_bucket)
    except ValueError as exc:
        return render_error(str(exc), 400)

    if queue is None:
        logger.error("Cloud Tasks queue is not configured job_id=%s", job_id)
        return render_error("システムエラー: Cloud Tasks キューが構成されていないため、タスクを実行できません。", 501)

    try:
        queue.enqueue(asdict(task))
    except Exception as exc:
        logger.error("Failed to enqueue task job_id=%s: %s", job_id, exc)
        return render_error(f"タスクのキュー追加に失敗しました: {exc}", 502)

    threading.Thread(target=_save_job_meta, args=(JobRecord.from_task(task),), daemon=True).start()

    return render_template("queued.html", job_id=job_id), 202


@web_bp.route("/jobs")
def job_list():
    """ジョブ履歴一覧を表示する。"""
    cfg = current_app.config_obj
    jobs = []
    error = None
    if not cfg.gcs_bucket:
        error = "GCS_BUCKET が未設定のため履歴を取得できません。"
    else:
        try:
            jobs = gcs.list_job_metadata(cfg.default_output_prefix())
        except Exception as exc:
            logger.error("Failed to list jobs: %s", exc)
            error = "履歴の取得に失敗しました。"
    return render_template("jobs.html", jobs=jobs, error=error)


@web_bp.route("/jobs/<job_id>/youtube", methods=["POST"])
def publish_youtube(job_id: str):
    """完了済みジョブの動画をYouTubeへアップロードするタスクをキューに追加する。"""
    token = session.get("csrf_token")
    if not token or token != request.form.get("csrf_token"):
        abort(403)

    cfg = current_app.config_obj
    queue = current_app.queue
    uploader = current_app.youtube_uploader

    if uploader is None:
        abort(501)
    if queue is None:
        abort(501)

    output_prefix = cfg.default_output_prefix()
    meta_uri = f"{output_prefix.rstrip('/')}/{job_id}/meta.json"
    try:
        job = gcs.load_json(meta_uri)
    except Exception:
        abort(404)

    output_uri = job.get("output_uri", "")
    if not output_uri:
        abort(400)

    title = request.form.get("youtube_title", "").strip()
    if not title:
        abort(400)

    yt_task = YouTubeTask(
        job_id=job_id,
        output_uri=output_uri,
        title=title,
        description=request.form.get("youtube_description", "").strip(),
        tags=request.form.get("youtube_tags", "").strip(),
        privacy=request.form.get("youtube_privacy", "private"),
    )

    try:
        job["youtube_status"] = "queued"
        gcs.save_json(job, meta_uri)
    except Exception as exc:
        logger.warning("Failed to update meta for YouTube queue job_id=%s: %s", job_id, exc)

    youtube_worker_url = cfg.service_url.rstrip("/") + "/tasks/youtube"
    try:
        queue.enqueue(asdict(yt_task), worker_url=youtube_worker_url)
    except Exception as exc:
        logger.error("Failed to enqueue YouTube task job_id=%s: %s", job_id, exc)
        try:
            job["youtube_status"] = "failed"
            job["youtube_error"] = "Failed to enqueue task"
            gcs.save_json(job, meta_uri)
        except Exception:
            pass
        abort(502)

    return "", 204


@web_bp.route("/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id: str):
    """ジョブのmeta.jsonと出力ファイルをGCSから削除する。"""
    token = session.get("csrf_token")
    header_token = request.headers.get("X-CSRF-Token", "")
    if not token or token != header_token:
        abort(403)

    cfg = current_app.config_obj
    output_prefix = cfg.default_output_prefix()
    meta_uri = f"{output_prefix.rstrip('/')}/{job_id}/meta.json"

    try:
        job = gcs.load_json(meta_uri)
    except Exception:
        job = {}

    deleted = []
    for uri in [meta_uri, job.get("output_uri")]:
        if not uri:
            continue
        try:
            gcs.delete(uri)
            deleted.append(uri)
        except Exception as exc:
            logger.warning("Failed to delete %s: %s", uri, exc)

    return {"job_id": job_id, "deleted": deleted}, 200


@web_bp.route("/jobs/<job_id>")
def job_detail(job_id: str):
    """ジョブ詳細・動画プレーヤーを表示する。"""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)

    cfg = current_app.config_obj
    output_prefix = cfg.default_output_prefix()
    meta_uri = f"{output_prefix.rstrip('/')}/{job_id}/meta.json"

    try:
        job = gcs.load_json(meta_uri)
        if not job.get("job_id"):
            job["job_id"] = job_id
    except Exception as exc:
        logger.error("Failed to load job %s: %s", job_id, exc)
        abort(404)

    signed_url = None
    download_url = None
    if job.get("output_uri") and cfg.service_account_email:
        try:
            signed_url = gcs.generate_signed_url(job["output_uri"], cfg.service_account_email)
            download_url = gcs.generate_signed_url(job["output_uri"], cfg.service_account_email, filename="output.mp4")
        except Exception as exc:
            logger.error("Failed to generate signed URL job_id=%s: %s", job_id, exc)

    return render_template("job_detail.html", job=job, signed_url=signed_url, download_url=download_url,
                           csrf_token=session["csrf_token"],
                           youtube_enabled=current_app.youtube_uploader is not None)


def _save_job_meta(record: JobRecord) -> None:
    if not record.output_prefix:
        return
    try:
        meta_uri = f"{record.output_prefix.rstrip('/')}/{record.job_id}/meta.json"
        gcs.save_json(record.to_dict(), meta_uri)
    except Exception as exc:
        logger.warning("Failed to save job metadata job_id=%s: %s", record.job_id, exc)
