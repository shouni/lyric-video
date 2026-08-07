from __future__ import annotations

import logging
import secrets
from dataclasses import asdict

from flask import Blueprint, abort, current_app, g, render_template, request, session
from googleapiclient.errors import HttpError

from app.adapters import gcs, job_store
from app.adapters.youtube import extract_video_id
from app.domain.job import JobRecord
from app.domain.task import Task, new_job_id
from app.domain.youtube_task import MAX_YOUTUBE_TITLE_BASE_LENGTH, YouTubeTask

logger = logging.getLogger(__name__)
web_bp = Blueprint("web", __name__)


def _wants_json() -> bool:
    """呼び出し元が`Accept: application/json`でJSONレスポンスを要求しているか判定する。"""
    return "application/json" in request.headers.get("Accept", "")


def _is_m2m() -> bool:
    """M2M(サーバー間通信)のOIDC認証済みリクエストかどうかを返す。"""
    return g.get("m2m_email") is not None


def _require_csrf(token: str) -> None:
    """M2Mリクエスト以外は、セッションに保存されたCSRFトークンとの一致を要求する。"""
    if _is_m2m():
        return
    expected = session.get("csrf_token")
    if not expected or expected != token:
        abort(403)


def _fail(status: int, message: str):
    """JSON呼び出し元にはJSONエラーを返し、それ以外は従来通りHTMLエラーページへabortする。"""
    if _wants_json():
        return {"status": "error", "error": message}, status
    abort(status)


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
    _require_csrf(request.form.get("csrf_token", ""))

    cfg = current_app.config_obj
    queue = current_app.queue

    output_prefix = cfg.default_output_prefix()
    job_id = new_job_id("lyric")
    task = Task(
        job_id=job_id,
        ap_mv_job_id=request.form.get("ap_mv_job_id", "").strip(),
        music_job_id=request.form.get("music_job_id", "").strip(),
        subs_url=request.form.get("subs_url", "").strip(),
        whisper_model=request.form.get("whisper_model") or cfg.whisper_model,
        output_prefix=output_prefix,
        source_type=request.form.get("source_type", "images").strip(),
    )

    def render_error(error: str, status: int):
        if _wants_json():
            return {"status": "error", "error": error}, status
        return render_template(
            "new.html",
            error=error,
            whisper_model=task.whisper_model,
            csrf_token=session.get("csrf_token"),
            subs_url=task.subs_url,
            source_type=task.source_type,
            ap_mv_job_id=task.ap_mv_job_id,
            music_job_id=task.music_job_id,
        ), status

    try:
        task.validate(allowed_bucket=cfg.gcs_bucket)
    except ValueError as exc:
        return render_error(str(exc), 400)

    if queue is None:
        logger.error("Cloud Tasks queue is not configured job_id=%s", job_id)
        return render_error("システムエラー: Cloud Tasks キューが構成されていないため、タスクを実行できません。", 501)

    # meta.json はキュー投入より先に、同期で保存する。Cloud Run はレスポンス返却後に
    # CPU が絞られる（インスタンスが停止し得る）ため、バックグラウンドスレッドでの保存は
    # 取りこぼしが起き、ジョブ一覧に queued のジョブが出てこないことがある。
    _save_job_meta(JobRecord.from_task(task))

    try:
        queue.enqueue(asdict(task))
    except Exception as exc:
        logger.error("Failed to enqueue task job_id=%s: %s", job_id, exc)
        job_store.update(
            job_store.meta_uri(task.output_prefix, job_id),
            {"status": "failed", "error": f"Failed to enqueue task: {exc}"},
        )
        return render_error(f"タスクのキュー追加に失敗しました: {exc}", 502)

    if _wants_json():
        return {"status": "queued", "job_id": job_id}, 202
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

    if _wants_json():
        if error:
            return {"status": "error", "error": error}, 502
        return {"jobs": jobs}, 200

    return render_template("jobs.html", jobs=jobs, error=error)


@web_bp.route("/jobs/<job_id>/youtube", methods=["POST"])
def publish_youtube(job_id: str):
    """完了済みジョブの動画をYouTubeへアップロードするタスクをキューに追加する。"""
    _require_csrf(request.form.get("csrf_token", ""))

    cfg = current_app.config_obj
    queue = current_app.queue
    uploader = current_app.youtube_uploader

    if uploader is None:
        return _fail(501, "YouTube integration is not configured")
    if queue is None:
        return _fail(501, "Cloud Tasks queue is not configured")

    meta_uri = job_store.meta_uri(cfg.default_output_prefix(), job_id)
    try:
        job = job_store.load(meta_uri)
    except Exception:
        return _fail(404, "Job not found")

    output_uri = job.get("output_uri", "")
    if not output_uri:
        return _fail(400, "Job output is not ready yet")

    title = request.form.get("youtube_title", "").strip()
    if not title:
        return _fail(400, "youtube_title is required")

    yt_task = YouTubeTask(
        job_id=job_id,
        output_uri=output_uri,
        title=title,
        description=request.form.get("youtube_description", "").strip(),
        tags=request.form.get("youtube_tags", "").strip(),
        privacy=request.form.get("youtube_privacy", "private"),
        thumbnail_uri=request.form.get("youtube_thumbnail_uri", "").strip(),
    )

    job_store.update(meta_uri, {"youtube_status": "queued", "youtube_thumbnail_uri": yt_task.thumbnail_uri})

    # 宛先は task_audience_url から組み立てる。service_url は Web 面自身のURLで、
    # SERVER_ROLE=web のプロセスは worker_bp を登録しないため /tasks/* は 404 になる。
    # CloudTasksQueue.enqueue は worker_url を差し替えても audience は
    # task_audience_url のままなので、ここを揃えないと宛先とaudienceも食い違う。
    # 単一プロセス(SERVER_ROLE未設定)では task_audience_url が service_url に
    # フォールバックし、その場合は worker_bp も同じプロセスに載っている。
    youtube_worker_url = cfg.task_audience_url.rstrip("/") + "/tasks/youtube"
    try:
        queue.enqueue(asdict(yt_task), worker_url=youtube_worker_url)
    except Exception as exc:
        logger.error("Failed to enqueue YouTube task job_id=%s: %s", job_id, exc)
        job_store.update(meta_uri, {"youtube_status": "failed", "youtube_error": "Failed to enqueue task"})
        return _fail(502, f"Failed to enqueue YouTube task: {exc}")

    if _wants_json():
        return {"status": "queued", "job_id": job_id}, 202
    return "", 204


@web_bp.route("/jobs/<job_id>/youtube", methods=["DELETE"])
def delete_youtube(job_id: str):
    """YouTube動画を削除し、meta.jsonのYouTube関連情報をクリアする。"""
    _require_csrf(request.headers.get("X-CSRF-Token", ""))

    cfg = current_app.config_obj
    uploader = current_app.youtube_uploader
    if uploader is None:
        abort(501)

    meta_uri = job_store.meta_uri(cfg.default_output_prefix(), job_id)
    try:
        meta = job_store.load(meta_uri)
    except Exception:
        abort(404)

    youtube_url = meta.get("youtube_url", "")
    video_id = extract_video_id(youtube_url)
    if not video_id:
        abort(400)

    try:
        uploader.delete_video(video_id)
    except HttpError as exc:
        if exc.status_code == 404:
            logger.info("YouTube video already deleted job_id=%s video_id=%s", job_id, video_id)
        else:
            logger.error("Failed to delete YouTube video job_id=%s: %s", job_id, exc)
            return {"error": str(exc)}, 500
    except Exception as exc:
        logger.error("Failed to delete YouTube video job_id=%s: %s", job_id, exc)
        return {"error": str(exc)}, 500

    job_store.update(meta_uri, {"youtube_status": None, "youtube_url": None})

    return {"job_id": job_id, "deleted": video_id}, 200


@web_bp.route("/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id: str):
    """ジョブのmeta.jsonと出力ファイルをGCSから削除する。"""
    _require_csrf(request.headers.get("X-CSRF-Token", ""))

    cfg = current_app.config_obj
    meta_uri = job_store.meta_uri(cfg.default_output_prefix(), job_id)

    try:
        job = job_store.load(meta_uri)
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
    if not _is_m2m() and "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)

    cfg = current_app.config_obj
    meta_uri = job_store.meta_uri(cfg.default_output_prefix(), job_id)

    try:
        job = job_store.load(meta_uri)
        if not job.get("job_id"):
            job["job_id"] = job_id
    except Exception as exc:
        logger.error("Failed to load job %s: %s", job_id, exc)
        return _fail(404, "Job not found")

    signed_url = None
    download_url = None
    if job.get("output_uri") and cfg.signing_service_account_email:
        try:
            signed_url = gcs.generate_signed_url(job["output_uri"], cfg.signing_service_account_email)
            download_url = gcs.generate_signed_url(
                job["output_uri"], cfg.signing_service_account_email, filename="output.mp4"
            )
        except Exception as exc:
            logger.error("Failed to generate signed URL job_id=%s: %s", job_id, exc)

    if _wants_json():
        return {
            **job,
            "video_url": signed_url,
            "download_url": download_url,
            "youtube_enabled": current_app.youtube_uploader is not None,
        }, 200

    return render_template(
        "job_detail.html",
        job=job,
        signed_url=signed_url,
        download_url=download_url,
        # M2MのHTMLアクセスではセッションを張らないためCSRFトークンは存在しない
        csrf_token=session.get("csrf_token"),
        youtube_enabled=current_app.youtube_uploader is not None,
        max_youtube_title_len=MAX_YOUTUBE_TITLE_BASE_LENGTH,
    )


def _save_job_meta(record: JobRecord) -> None:
    """ジョブ受付時のmeta.jsonを保存する。保存に失敗してもジョブ投入自体は続行する。"""
    uri = job_store.meta_uri(record.output_prefix, record.job_id)
    if not uri:
        return
    try:
        job_store.save(record.to_dict(), uri)
    except Exception as exc:
        logger.warning("Failed to save job metadata job_id=%s: %s", record.job_id, exc)
