from __future__ import annotations

import logging

from google.auth import exceptions as google_auth_exceptions
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

logger = logging.getLogger(__name__)

_request = google_requests.Request()
_BEARER_PREFIX = "Bearer "


def verify_bearer_token(authorization_header: str, audience: str) -> dict | None:
    """Authorizationヘッダーの OIDC Bearer トークンを検証し、クレームを返す。

    Cloud Tasks からのワーカー呼び出し（verify_task_caller）と、MCPゲートウェイ等からの
    M2M呼び出し（verify_service_account）で同じ検証処理を共有する。
    """
    if not authorization_header.startswith(_BEARER_PREFIX):
        return None
    token = authorization_header[len(_BEARER_PREFIX) :]
    try:
        # verify_oauth2_token は無効なトークンに ValueError、発行者不一致や証明書取得の
        # 通信エラーに google.auth.exceptions.GoogleAuthError を送出する（公式docstring参照）。
        return google_id_token.verify_oauth2_token(token, _request, audience)
    except (ValueError, google_auth_exceptions.GoogleAuthError) as exc:
        logger.info("OIDC token verification failed: %s", exc)
        return None


def verify_service_account(authorization_header: str, audience: str, allowed_service_accounts: list[str]) -> str | None:
    """M2M(サーバー間通信)呼び出しを検証し、許可済みサービスアカウントのメールを返す。

    許可リストが空、ヘッダーが Bearer 形式でない、検証失敗、または送信元が
    許可リストに含まれない場合は None を返す（呼び出し元はセッション認証にフォールバックする）。
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


def verify_task_caller(authorization_header: str, audience: str, expected_email: str = "") -> bool:
    """Cloud Tasks ワーカーエンドポイントの呼び出し元を検証する。

    audience が未設定のまま検証を通すと誰でも /tasks/* を叩けてしまうため、
    設定漏れは拒否として扱う。
    """
    if not audience:
        logger.error("TASK_AUDIENCE_URL not configured — denying request to be safe")
        return False

    claims = verify_bearer_token(authorization_header, audience)
    if claims is None:
        return False

    if expected_email and claims.get("email") != expected_email:
        logger.warning("OIDC email mismatch: expected=%s got=%s", expected_email, claims.get("email"))
        return False
    return True
