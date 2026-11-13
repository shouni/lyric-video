from __future__ import annotations

import logging

from google.auth import exceptions as google_auth_exceptions
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

logger = logging.getLogger(__name__)

_request = google_requests.Request()
_BEARER_PREFIX = "Bearer "


def verify_bearer_token(authorization_header: str, audience: str) -> dict | None:
    """Verify the OIDC Bearer token in the Authorization header and return its claims.

    The same verification is shared by worker calls from Cloud Tasks (verify_task_caller) and
    M2M calls from the MCP gateway and others (verify_service_account).
    """
    if not authorization_header.startswith(_BEARER_PREFIX):
        return None
    token = authorization_header[len(_BEARER_PREFIX) :]
    try:
        # verify_oauth2_token raises ValueError for an invalid token, and
        # google.auth.exceptions.GoogleAuthError for an issuer mismatch or a network error while fetching
        # the certificates (see the official docstring).
        return google_id_token.verify_oauth2_token(token, _request, audience)
    except (ValueError, google_auth_exceptions.GoogleAuthError) as exc:
        logger.info("OIDC token verification failed: %s", exc)
        return None


def verify_service_account(authorization_header: str, audience: str, allowed_service_accounts: list[str]) -> str | None:
    """Verify an M2M (server-to-server) call and return the email of the allowed service account.

    Returns None if the allowlist is empty, the header is not in Bearer form, verification fails, or
    the sender is not in the allowlist (the caller falls back to session authentication).
    """
    if not allowed_service_accounts:
        return None

    claims = verify_bearer_token(authorization_header, audience)
    if claims is None:
        return None

    email = claims.get("email", "").lower()
    if not claims.get("email_verified") or email not in allowed_service_accounts:
        logger.warning("M2M token rejected: unauthorized service account email=%s", email)
        return None
    return email


def verify_task_caller(authorization_header: str, audience: str, allowed_service_accounts: list[str]) -> bool:
    """Verify the caller of a Cloud Tasks worker endpoint.

    If either the audience or the allowlist is not set, the request is refused (fail-closed). The
    audience only says where the token is **addressed**, and does not show whether the caller is an
    allowed one. Any service account can obtain a token with the same audience, so the caller is
    authorized only once the email claim has been checked against the allowlist.

    This is the opposite of "forget to set it and everything gets through". Previously, an empty
    expected_email skipped the check altogether, so a missing setting directly disabled verification.
    """
    if not audience:
        logger.error("TASK_AUDIENCE_URL not configured — denying request to be safe")
        return False
    if not allowed_service_accounts:
        logger.error("ALLOWED_TASK_SERVICE_ACCOUNTS not configured — denying request to be safe")
        return False

    claims = verify_bearer_token(authorization_header, audience)
    if claims is None:
        return False

    email = claims.get("email", "").lower()
    allowed = {sa.strip().lower() for sa in allowed_service_accounts if sa.strip()}
    if not claims.get("email_verified") or email not in allowed:
        logger.warning("task token rejected: unauthorized service account email=%s", email)
        return False
    return True
