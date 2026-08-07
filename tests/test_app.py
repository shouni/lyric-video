"""アプリ全体の配線（認証ミドルウェア・公開パス）のテスト。外部サービスへは接続しない。"""

import pytest

from app.adapters import job_store
from app.main import create_app


def _build_client(monkeypatch, role: str | None = None):
    for key in ("GCP_PROJECT_ID", "CLOUD_TASKS_QUEUE_ID", "TASK_CALLER_SERVICE_ACCOUNT_EMAIL", "YOUTUBE_REFRESH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("ALLOWED_EMAILS", "me@example.com")
    monkeypatch.setenv("ALLOWED_TASK_SERVICE_ACCOUNTS", "tasks@project.iam.gserviceaccount.com")
    monkeypatch.setenv("SERVER_ROLE", role or "both")
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture
def client(monkeypatch):
    return _build_client(monkeypatch)


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


class TestServerRoleRouting:
    """SERVER_ROLE に応じて Blueprint ごと登録されないことを確認する。

    公開サービス上に /tasks/* が存在しないことが分離の目的なので、
    「401 で拒否される」ではなく「404 でルートが無い」であることを見る。
    """

    def test_web_role_does_not_serve_worker_routes(self, monkeypatch):
        client = _build_client(monkeypatch, role="web")

        assert client.post("/tasks/generate", json={}).status_code == 404
        assert client.post("/tasks/youtube", json={}).status_code == 404

    def test_worker_role_does_not_serve_web_routes(self, monkeypatch):
        client = _build_client(monkeypatch, role="worker")

        assert client.get("/").status_code == 404
        assert client.get("/jobs").status_code == 404

    def test_worker_role_still_serves_worker_routes(self, monkeypatch):
        client = _build_client(monkeypatch, role="worker")

        # OIDC トークンが無いので 401。404 でないことがルート登録の証拠になる。
        assert client.post("/tasks/generate", json={}).status_code == 401

    def test_worker_role_keeps_healthz(self, monkeypatch):
        client = _build_client(monkeypatch, role="worker")

        assert client.get("/healthz").status_code == 200

    def test_both_role_serves_web_and_worker(self, monkeypatch):
        client = _build_client(monkeypatch, role="both")

        assert client.post("/tasks/generate", json={}).status_code == 401
        assert client.get("/").status_code in (200, 302, 303)


class TestYouTubeEnqueueTarget:
    """YouTube投稿タスクの宛先が worker であることを確認する。

    /tasks/youtube は worker_bp にあり、SERVER_ROLE=web のプロセスには
    存在しない（TestServerRoleRouting がそれを固定している）。宛先を
    service_url（Web 面自身）から組み立てると自分の 404 へ投げることになる。
    """

    def _build_web_app(self, monkeypatch):
        for key in ("GCP_PROJECT_ID", "CLOUD_TASKS_QUEUE_ID", "TASK_CALLER_SERVICE_ACCOUNT_EMAIL"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("SESSION_SECRET", "test-secret")
        monkeypatch.setenv("ALLOWED_EMAILS", "me@example.com")
        monkeypatch.setenv("SERVER_ROLE", "web")
        monkeypatch.setenv("SERVICE_URL", "https://web.example.com")
        monkeypatch.setenv("TASK_AUDIENCE_URL", "https://worker.example.com")
        monkeypatch.setenv("GCS_BUCKET", "bucket")

        app = create_app()
        app.config.update(TESTING=True)
        return app

    def test_enqueues_to_the_worker_not_to_itself(self, monkeypatch):
        app = self._build_web_app(monkeypatch)

        captured = {}

        class FakeQueue:
            def enqueue(self, payload, worker_url=None):
                captured["worker_url"] = worker_url

        app.queue = FakeQueue()
        # uploader が None だと 501 で早期に返るため、非 None にしておく。
        app.youtube_uploader = object()

        monkeypatch.setattr("app.main.get_user_email", lambda: "me@example.com")
        monkeypatch.setattr(job_store, "load", lambda uri: {"output_uri": "gs://bucket/out.mp4"})
        monkeypatch.setattr(job_store, "update", lambda uri, patch: True)

        client = app.test_client()
        with client.session_transaction() as session:
            session["user_email"] = "me@example.com"
            session["csrf_token"] = "token"

        response = client.post(
            "/jobs/job-1/youtube",
            data={"csrf_token": "token", "youtube_title": "title"},
        )

        assert response.status_code == 204
        assert captured["worker_url"] == "https://worker.example.com/tasks/youtube"
        assert not captured["worker_url"].startswith("https://web.example.com")
