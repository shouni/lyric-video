from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import auth
from app.config import Config
from app.adapters.slack import SlackNotifier
from app.adapters.task_queue import CloudTasksQueue
from app.pipeline.runner import PipelineRunner
from app.handlers.web import router as web_router
from app.handlers.worker import router as worker_router

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "message": "%(message)s"}',
)
logger = logging.getLogger(__name__)

_PUBLIC_PATHS = {"/healthz", "/auth/login", "/auth/callback", "/tasks/generate"}

# モジュールロード時に設定を読み込む（ミドルウェア初期化に必要）
_cfg = Config.from_env()


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        if not auth.get_user_email(request):
            return RedirectResponse("/auth/login")
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.config = _cfg

    auth.setup_oauth(_cfg.google_client_id, _cfg.google_client_secret)

    queue = None
    if _cfg.project_id and _cfg.queue_id and _cfg.service_account_email:
        try:
            queue = CloudTasksQueue(
                project_id=_cfg.project_id,
                location_id=_cfg.location_id,
                queue_id=_cfg.queue_id,
                worker_url=_cfg.worker_url,
                service_account_email=_cfg.service_account_email,
                audience=_cfg.task_audience_url,
            )
            logger.info("Cloud Tasks queue initialized queue=%s", _cfg.queue_id)
        except Exception as exc:
            logger.error("Failed to initialize Cloud Tasks queue: %s", exc)

    app.state.queue = queue
    app.state.notifier = SlackNotifier(_cfg.slack_webhook_url, _cfg.service_url)
    app.state.pipeline = PipelineRunner()

    logger.info("Application started port=%s", _cfg.port)
    yield
    logger.info("Application shutting down")


app = FastAPI(title="Lyric Video", lifespan=lifespan)

# add_middleware は後から追加したものが外側（先に実行）になる。
# SessionMiddleware を先に追加 → 外側に配置される（リクエストを最初に処理）
# AuthMiddleware を後に追加 → 内側に配置される（Session 確立後に実行）
app.add_middleware(
    SessionMiddleware,
    secret_key=_cfg.session_secret,
    https_only=_cfg.is_secure_url(),
    same_site="lax",
)
app.add_middleware(AuthMiddleware)

app.include_router(web_router)
app.include_router(worker_router)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/auth/login", include_in_schema=False)
async def auth_login(request: Request):
    return await auth.login(request)


@app.get("/auth/callback", name="auth_callback", include_in_schema=False)
async def auth_callback(request: Request):
    cfg = request.app.state.config
    return await auth.callback(request, cfg.allowed_emails, cfg.allowed_domains)


@app.get("/auth/logout", include_in_schema=False)
def auth_logout(request: Request):
    return auth.logout(request)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(_cfg.port))
