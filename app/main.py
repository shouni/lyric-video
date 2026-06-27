from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
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

# Paths that do not require a login session
_PUBLIC_PATHS = {"/healthz", "/auth/login", "/auth/callback", "/tasks/generate"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = Config.from_env()
    app.state.config = cfg

    auth.setup_oauth(cfg.google_client_id, cfg.google_client_secret)

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

    app.state.queue = queue
    app.state.notifier = SlackNotifier(cfg.slack_webhook_url, cfg.service_url)
    app.state.pipeline = PipelineRunner()

    logger.info("Application started port=%s", cfg.port)
    yield
    logger.info("Application shutting down")


app = FastAPI(title="Lyric Video", lifespan=lifespan)


@app.middleware("http")
async def session_auth_middleware(request: Request, call_next):
    if request.url.path in _PUBLIC_PATHS:
        return await call_next(request)
    if not auth.get_user_email(request):
        return RedirectResponse("/auth/login")
    return await call_next(request)


# Session middleware must be added after the auth middleware
app.add_middleware(
    SessionMiddleware,
    secret_key=lambda: app.state.config.session_secret,
    https_only=False,  # set True in production (Cloud Run uses HTTPS)
    same_site="lax",
)

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
    import os
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
