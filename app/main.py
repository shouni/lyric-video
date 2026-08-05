from __future__ import annotations

import logging
import secrets
import sys

from flask import Flask, g, redirect, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from app.adapters.oidc import verify_service_account
from app.adapters.slack import SlackNotifier
from app.adapters.task_queue import CloudTasksQueue
from app.adapters.youtube import YouTubeUploader
from app.auth import auth_bp, get_user_email, init_oauth
from app.config import Config
from app.handlers.web import web_bp
from app.handlers.worker import worker_bp
from app.pipeline.runner import PipelineRunner

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# before_request の認証を素通しするパス。/tasks/* は各ハンドラーが Cloud Tasks の
# OIDC トークンを個別に検証するため、ここでは通す。
_PUBLIC_PATHS = {"/healthz", "/auth/login", "/auth/callback"}
_WORKER_PATHS = {"/tasks/generate", "/tasks/youtube"}


def create_app() -> Flask:
    """Flaskアプリを生成し、設定・認証・外部サービス・Blueprintを初期化する。"""
    cfg = Config.from_env()

    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    # Worker 面だけのプロセスはセッションを使わないが、Flask が secret_key 未設定で
    # 例外を投げる経路を踏まないよう、その場限りの値を入れておく。
    app.secret_key = cfg.session_secret or secrets.token_urlsafe(32)
    app.config.update(
        SESSION_COOKIE_SECURE=cfg.is_secure_url(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

    if cfg.serves_web():
        init_oauth(app, cfg.google_client_id, cfg.google_client_secret)

    # タスクを投入するのは Web 面だけ。Worker は受け取る側なので組み立てない。
    queue = None
    if cfg.serves_web() and cfg.project_id and cfg.queue_id and cfg.service_account_email:
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
    if cfg.google_client_id and cfg.google_client_secret and cfg.youtube_refresh_token:
        youtube_uploader = YouTubeUploader(cfg.google_client_id, cfg.google_client_secret, cfg.youtube_refresh_token)
        logger.info("YouTube uploader initialized")

    app.config_obj = cfg
    app.queue = queue
    app.notifier = SlackNotifier(cfg.slack_webhook_url, cfg.service_url)
    app.pipeline = PipelineRunner(
        ap_mv_bucket=cfg.ap_mv_bucket,
        ap_music_bucket=cfg.ap_music_bucket,
        video_whisper_model=cfg.video_whisper_model,
    )
    app.youtube_uploader = youtube_uploader

    # 担当しない面は Blueprint ごと登録しない。SERVER_ROLE=web では /tasks/* が
    # 404 になり、公開サービス上からタスク受付口が消える。
    if cfg.serves_web():
        app.register_blueprint(auth_bp)
        app.register_blueprint(web_bp)
    if cfg.serves_worker():
        app.register_blueprint(worker_bp)

    # Worker 面だけのプロセスではログイン画面を持たないため、この検査を登録しない。
    # 登録すると未知のパスへのリクエストが url_for("auth.login") に到達し、
    # auth_bp が無いので 404 ではなく BuildError（500）になる。
    # Worker のルートは各ハンドラーが OIDC を個別に検証するため、素通しで問題ない。
    if cfg.serves_web():

        @app.before_request
        def check_auth():
            """公開パス以外へのアクセスに、ログイン済みセッションまたはM2M OIDCトークンを要求する。"""
            if request.path in _PUBLIC_PATHS or request.path in _WORKER_PATHS:
                return None
            m2m_email = verify_service_account(
                request.headers.get("Authorization", ""), cfg.service_url, cfg.allowed_m2m_service_accounts
            )
            if m2m_email:
                g.m2m_email = m2m_email
                return None
            if not get_user_email():
                return redirect(url_for("auth.login"), 303)

    @app.get("/healthz")
    def healthz():
        """ヘルスチェック用の最小レスポンスを返す。"""
        return {"status": "ok"}

    logger.info("Application created port=%s role=%s", cfg.port, cfg.server_role or "both")
    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(app.config_obj.port))
