from __future__ import annotations

import logging
import sys

from flask import Flask, redirect, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from app.auth import auth_bp, get_user_email, init_oauth
from app.config import Config
from app.adapters.slack import SlackNotifier
from app.adapters.task_queue import CloudTasksQueue
from app.adapters.youtube import YouTubeUploader
from app.pipeline.runner import PipelineRunner
from app.handlers.web import web_bp
from app.handlers.worker import worker_bp

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_PUBLIC_PATHS = {"/healthz", "/auth/login", "/auth/callback", "/tasks/generate", "/tasks/youtube"}


def create_app() -> Flask:
    """Flaskアプリを生成し、設定・認証・外部サービス・Blueprintを初期化する。"""
    cfg = Config.from_env()

    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    app.secret_key = cfg.session_secret
    app.config.update(
        SESSION_COOKIE_SECURE=cfg.is_secure_url(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

    init_oauth(app, cfg.google_client_id, cfg.google_client_secret)

    queue = None
    if cfg.project_id and cfg.queue_id and cfg.service_account_email:
        try:
            queue = CloudTasksQueue(
                project_id=cfg.project_id,
                location_id=cfg.location_id,
                queue_id=cfg.queue_id,
                worker_url=cfg.worker_url,
                service_account_email=cfg.service_account_email,
                audience=cfg.task_audience_url,
            )
            logger.info("Cloud Tasks queue initialized queue=%s", cfg.queue_id)
        except Exception as exc:
            logger.error("Failed to initialize Cloud Tasks queue: %s", exc)

    youtube_uploader = None
    if cfg.youtube_client_id and cfg.youtube_client_secret and cfg.youtube_refresh_token:
        youtube_uploader = YouTubeUploader(cfg.youtube_client_id, cfg.youtube_client_secret, cfg.youtube_refresh_token)
        logger.info("YouTube uploader initialized")

    app.config_obj = cfg
    app.queue = queue
    app.notifier = SlackNotifier(cfg.slack_webhook_url, cfg.service_url)
    app.pipeline = PipelineRunner()
    app.youtube_uploader = youtube_uploader

    app.register_blueprint(auth_bp)
    app.register_blueprint(web_bp)
    app.register_blueprint(worker_bp)

    @app.before_request
    def check_auth():
        """公開パス以外へのアクセスにログイン済みセッションを要求する。"""
        if request.path in _PUBLIC_PATHS:
            return None
        if not get_user_email():
            return redirect(url_for("auth.login"), 303)

    @app.get("/healthz")
    def healthz():
        """ヘルスチェック用の最小レスポンスを返す。"""
        return {"status": "ok"}

    logger.info("Application created port=%s", cfg.port)
    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(app.config_obj.port))
