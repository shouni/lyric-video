from __future__ import annotations

import logging

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

logger = logging.getLogger(__name__)

_request = google_requests.Request()


def verify_m2m_token(authorization_header: str, audience: str, allowed_service_accounts: list[str]) -> str | None:
    """Authorizationヘッダーの OIDC Bearer トークンを検証し、許可済みサービスアカウントのメールを返す。

    許可リストが空、ヘッダーが Bearer 形式でない、検証失敗、または送信元が
    許可リストに含まれない場合は None を返す（呼び出し元はセッション認証にフォールバックする）。
    """
    if not allowed_service_accounts or not authorization_header.startswith("Bearer "):
        return None

    token = authorization_header[len("Bearer "):]
    try:
        claims = google_id_token.verify_oauth2_token(token, _request, audience)
    except Exception as exc:
        logger.info("M2M OIDC token verification failed: %s", exc)
        return None

    email = claims.get("email", "").lower()
    if not claims.get("email_verified") or email not in allowed_service_accounts:
        logger.warning("M2M token rejected: unauthorized service account email=%s", email)
        return None
    return email
