from __future__ import annotations

import logging
import secrets
from dataclasses import asdict

from flask import Blueprint, abort, current_app, render_template, request, session

from app.domain.task import Task, new_job_id

logger = logging.getLogger(__name__)
web_bp = Blueprint("web", __name__)


@web_bp.route("/", methods=["GET"])
def get_form():
    """入力フォームを表示し、初回アクセス時はCSRFトークンを発行する。"""
    cfg = current_app.config_obj
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return render_template(
        "index.html",
        default_output=cfg.default_output_prefix(),
        whisper_model=cfg.whisper_model,
        csrf_token=session["csrf_token"],
    )


@web_bp.route("/", methods=["POST"])
def post_form():
    """フォーム送信を受け取り、入力値を検証してCloud Tasksへジョブを登録する。"""
    token = session.get("csrf_token")
    if not token or token != request.form.get("csrf_token"):
        abort(403)

    cfg = current_app.config_obj
    queue = current_app.queue

    output_prefix = request.form.get("output_prefix", "").strip() or cfg.default_output_prefix()
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
        """エラー内容を入力フォームへ戻して指定ステータスで返す。"""
        return render_template(
            "index.html",
            error=error,
            default_output=cfg.default_output_prefix(),
            whisper_model=cfg.whisper_model,
            csrf_token=session.get("csrf_token"),
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

    return render_template("queued.html", job_id=job_id), 202
