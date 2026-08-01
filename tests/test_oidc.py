"""OIDC検証（Cloud Tasksワーカー / M2M）のテスト。トークン検証自体はモックする。"""

import pytest

from app.adapters import oidc

AUDIENCE = "https://lyric.example.com"
SA = "sa@project.iam.gserviceaccount.com"


@pytest.fixture
def verified(monkeypatch):
    """verify_oauth2_token を差し替え、返すクレームをテストから指定できるようにする。"""

    def _install(claims=None, error=None):
        def _fake(token, request, audience):
            if error is not None:
                raise error
            return {"aud": audience, **(claims or {})}

        monkeypatch.setattr(oidc.google_id_token, "verify_oauth2_token", _fake)

    return _install


class TestVerifyBearerToken:
    def test_returns_claims(self, verified):
        verified({"email": SA})
        assert oidc.verify_bearer_token("Bearer tok", AUDIENCE)["email"] == SA

    def test_rejects_non_bearer_header(self, verified):
        verified({"email": SA})
        assert oidc.verify_bearer_token("Basic abc", AUDIENCE) is None
        assert oidc.verify_bearer_token("", AUDIENCE) is None

    def test_returns_none_on_invalid_token(self, verified):
        verified(error=ValueError("bad token"))
        assert oidc.verify_bearer_token("Bearer tok", AUDIENCE) is None


class TestVerifyServiceAccount:
    def test_accepts_allowed_service_account(self, verified):
        verified({"email": SA, "email_verified": True})
        assert oidc.verify_service_account("Bearer tok", AUDIENCE, [SA]) == SA

    def test_is_case_insensitive_on_email(self, verified):
        verified({"email": "SA@project.iam.gserviceaccount.com", "email_verified": True})
        assert oidc.verify_service_account("Bearer tok", AUDIENCE, [SA]) == SA

    def test_rejects_unlisted_service_account(self, verified):
        verified({"email": "other@example.com", "email_verified": True})
        assert oidc.verify_service_account("Bearer tok", AUDIENCE, [SA]) is None

    def test_rejects_unverified_email(self, verified):
        verified({"email": SA, "email_verified": False})
        assert oidc.verify_service_account("Bearer tok", AUDIENCE, [SA]) is None

    def test_denies_when_allow_list_is_empty(self, verified):
        verified({"email": SA, "email_verified": True})
        assert oidc.verify_service_account("Bearer tok", AUDIENCE, []) is None


class TestVerifyTaskCaller:
    def test_accepts_expected_service_account(self, verified):
        verified({"email": SA})
        assert oidc.verify_task_caller("Bearer tok", AUDIENCE, SA) is True

    def test_rejects_other_service_account(self, verified):
        verified({"email": "other@example.com"})
        assert oidc.verify_task_caller("Bearer tok", AUDIENCE, SA) is False

    def test_accepts_any_email_when_expectation_is_empty(self, verified):
        verified({"email": "other@example.com"})
        assert oidc.verify_task_caller("Bearer tok", AUDIENCE, "") is True

    def test_denies_when_audience_is_not_configured(self, verified):
        verified({"email": SA})
        assert oidc.verify_task_caller("Bearer tok", "", SA) is False

    def test_denies_invalid_token(self, verified):
        verified(error=ValueError("expired"))
        assert oidc.verify_task_caller("Bearer tok", AUDIENCE, SA) is False
