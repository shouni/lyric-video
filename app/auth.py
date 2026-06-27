from __future__ import annotations

import logging

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

logger = logging.getLogger(__name__)

_oauth = OAuth()
auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def init_oauth(app, client_id: str, client_secret: str) -> None:
    _oauth.init_app(app)
    _oauth.register(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


@auth_bp.route("/login")
def login():
    error = request.args.get("error")
    if error:
        return render_template("login.html", error=error)
    redirect_uri = url_for("auth.callback", _external=True)
    return _oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route("/callback")
def callback():
    try:
        token = _oauth.google.authorize_access_token()
    except Exception as exc:
        logger.error("OAuth error: %s", exc)
        return redirect(url_for("auth.login", error="oauth_error"), 303)

    user = token.get("userinfo", {})
    email = user.get("email", "").lower()
    cfg = current_app.config_obj

    if not _is_allowed(email, cfg.allowed_emails, cfg.allowed_domains):
        logger.warning("Unauthorized login attempt email=%s", email)
        return redirect(url_for("auth.login", error="unauthorized"), 303)

    session["user_email"] = email
    logger.info("User logged in email=%s", email)
    return redirect("/", 303)


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"), 303)


def get_user_email() -> str | None:
    return session.get("user_email")


def _is_allowed(email: str, allowed_emails: list[str], allowed_domains: list[str]) -> bool:
    if not allowed_emails and not allowed_domains:
        logger.error("No ALLOWED_EMAILS or ALLOWED_DOMAINS configured — denying access by default")
        return False
    if email in allowed_emails:
        return True
    domain = email.split("@")[-1] if "@" in email else ""
    return domain in allowed_domains
