"""アプリ全体の配線（認証ミドルウェア・公開パス）のテスト。外部サービスへは接続しない。"""

import pytest

from app.main import create_app


@pytest.fixture
def client(monkeypatch):
    for key in ("GCP_PROJECT_ID", "CLOUD_TASKS_QUEUE_ID", "SERVICE_ACCOUNT_EMAIL", "YOUTUBE_REFRESH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("ALLOWED_EMAILS", "me@example.com")
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


class TestHealthz:
    def test_is_public(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.get_json() == {"status": "ok"}


class TestAuthMiddleware:
    @pytest.mark.parametrize("path", ["/", "/new", "/jobs", "/jobs/abc"])
    def test_protected_paths_redirect_to_login(self, client, path):
        response = client.get(path)
        assert response.status_code == 303
        assert "/auth/login" in response.headers["Location"]

    def test_worker_endpoints_reject_missing_oidc_token(self, client):
        response = client.post("/tasks/generate", json={})
        assert response.status_code == 401

    def test_youtube_worker_endpoint_rejects_missing_oidc_token(self, client):
        response = client.post("/tasks/youtube", json={})
        assert response.status_code == 401

    def test_m2m_token_is_rejected_when_allow_list_is_empty(self, client):
        response = client.get("/jobs", headers={"Authorization": "Bearer whatever"})
        assert response.status_code == 303


class TestQueueNotConfigured:
    def test_post_new_reports_configuration_error_as_json(self, client, monkeypatch):
        # 認証を通した上で、キュー未設定時に 501 とJSONエラーが返ることを確認する
        monkeypatch.setattr("app.main.get_user_email", lambda: "me@example.com")
        with client.session_transaction() as session:
            session["user_email"] = "me@example.com"
            session["csrf_token"] = "token"
        response = client.post(
            "/new",
            data={"csrf_token": "token", "ap_mv_job_id": "mv-1", "music_job_id": "music-1"},
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 501
        assert response.get_json()["status"] == "error"
