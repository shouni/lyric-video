from __future__ import annotations

import logging

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import Request
from fastapi.responses import RedirectResponse

logger = logging.getLogger(__name__)

_oauth = OAuth()


def setup_oauth(client_id: str, client_secret: str) -> None:
    _oauth.register(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


async def login(request: Request) -> RedirectResponse:
    redirect_uri = str(request.url_for("auth_callback"))
    return await _oauth.google.authorize_redirect(request, redirect_uri)


async def callback(
    request: Request,
    allowed_emails: list[str],
    allowed_domains: list[str],
) -> RedirectResponse:
    try:
        token = await _oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        logger.error("OAuth error: %s", exc)
        return RedirectResponse("/auth/login?error=oauth_error")

    user = token.get("userinfo", {})
    email = user.get("email", "").lower()

    if not _is_allowed(email, allowed_emails, allowed_domains):
        logger.warning("Unauthorized login attempt email=%s", email)
        return RedirectResponse("/auth/login?error=unauthorized")

    request.session["user_email"] = email
    logger.info("User logged in email=%s", email)
    return RedirectResponse("/")


def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/auth/login")


def get_user_email(request: Request) -> str | None:
    return request.session.get("user_email")


def _is_allowed(email: str, allowed_emails: list[str], allowed_domains: list[str]) -> bool:
    if not allowed_emails and not allowed_domains:
        return True
    if email in allowed_emails:
        return True
    domain = email.split("@")[-1] if "@" in email else ""
    return domain in allowed_domains
