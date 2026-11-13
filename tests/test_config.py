"""Tests for the defaults and the derivation logic of Config.from_env."""

import pytest

from app.config import Config

_ENV_KEYS = [
    "SERVICE_URL",
    "WORKER_URL",
    "TASK_AUDIENCE_URL",
    "PORT",
    "GCP_PROJECT_ID",
    "GCP_LOCATION_ID",
    "CLOUD_TASKS_QUEUE_ID",
    "TASK_CALLER_SERVICE_ACCOUNT_EMAIL",
    "SIGNING_SERVICE_ACCOUNT_EMAIL",
    "ALLOWED_TASK_SERVICE_ACCOUNTS",
    "GCS_BUCKET",
    "GCS_OUTPUT_PREFIX",
    "SLACK_WEBHOOK_URL",
    "WHISPER_MODEL",
    "VIDEO_WHISPER_MODEL",
    "AP_MV_BUCKET",
    "AP_MUSIC_BUCKET",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "SESSION_SECRET",
    "YOUTUBE_REFRESH_TOKEN",
    "ALLOWED_EMAILS",
    "ALLOWED_DOMAINS",
    "ALLOWED_M2M_SERVICE_ACCOUNTS",
    "SERVER_ROLE",
]


@pytest.fixture
def env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SESSION_SECRET", "secret")
    # SERVER_ROLE must be set explicitly. Tests that do not care about the role load it as both.
    monkeypatch.setenv("SERVER_ROLE", "both")
    # both includes the worker face, so it does not start without the allowlist (fail-closed).
    monkeypatch.setenv("ALLOWED_TASK_SERVICE_ACCOUNTS", "tasks@project.iam.gserviceaccount.com")
    return monkeypatch


class TestFromEnv:
    def test_requires_session_secret(self, env):
        env.delenv("SESSION_SECRET")
        with pytest.raises(ValueError, match="SESSION_SECRET"):
            Config.from_env()

    def test_defaults(self, env):
        cfg = Config.from_env()
        assert cfg.service_url == "http://localhost:8080"
        assert cfg.location_id == "asia-northeast1"
        assert cfg.gcs_output_prefix == "lyric-video/output"
        assert cfg.whisper_model == "large-v3"
        assert cfg.video_whisper_model == "small"
        assert (cfg.ap_mv_bucket, cfg.ap_music_bucket) == ("ap-mv", "ap-music")

    def test_derives_worker_and_audience_urls_from_service_url(self, env):
        env.setenv("SERVICE_URL", "https://lyric.example.com/")
        cfg = Config.from_env()
        assert cfg.service_url == "https://lyric.example.com"
        assert cfg.worker_url == "https://lyric.example.com/tasks/generate"
        assert cfg.task_audience_url == "https://lyric.example.com"

    def test_explicit_worker_and_audience_urls_win(self, env):
        env.setenv("SERVICE_URL", "https://lyric.example.com")
        env.setenv("WORKER_URL", "https://worker.example.com/tasks/generate")
        env.setenv("TASK_AUDIENCE_URL", "https://audience.example.com")
        cfg = Config.from_env()
        assert cfg.worker_url == "https://worker.example.com/tasks/generate"
        assert cfg.task_audience_url == "https://audience.example.com"

    def test_splits_and_lowercases_allow_lists(self, env):
        env.setenv("ALLOWED_EMAILS", " Foo@Example.com , bar@example.com ,")
        env.setenv("ALLOWED_M2M_SERVICE_ACCOUNTS", "SA@project.iam.gserviceaccount.com")
        cfg = Config.from_env()
        assert cfg.allowed_emails == ["foo@example.com", "bar@example.com"]
        assert cfg.allowed_m2m_service_accounts == ["sa@project.iam.gserviceaccount.com"]

    def test_video_whisper_model_is_overridable(self, env):
        env.setenv("VIDEO_WHISPER_MODEL", "medium")
        assert Config.from_env().video_whisper_model == "medium"


class TestDerivedHelpers:
    def test_is_secure_url(self, env):
        env.setenv("SERVICE_URL", "https://x.example.com")
        assert Config.from_env().is_secure_url() is True
        env.setenv("SERVICE_URL", "http://localhost:8080")
        assert Config.from_env().is_secure_url() is False

    def test_default_output_prefix(self, env):
        env.setenv("GCS_BUCKET", "my-bucket")
        assert Config.from_env().default_output_prefix() == "gs://my-bucket/lyric-video/output"

    def test_default_output_prefix_is_empty_without_bucket(self, env):
        assert Config.from_env().default_output_prefix() == ""


class TestServerRole:
    def test_both_serves_web_and_worker(self, env):
        cfg = Config.from_env()
        assert cfg.server_role == "both"
        assert cfg.serves_web()
        assert cfg.serves_worker()

    def test_rejects_unset_role(self, env):
        """Do not fall back to both when it is unset.

        With a fallback, a single missing environment variable in production brings /tasks/* back on the public web.
        """
        env.delenv("SERVER_ROLE")
        with pytest.raises(RuntimeError, match="SERVER_ROLE"):
            Config.from_env()

    @pytest.mark.parametrize(
        ("raw", "serves_web", "serves_worker"),
        [
            ("web", True, False),
            ("worker", False, True),
            (" WORKER ", False, True),
        ],
    )
    def test_parses_role(self, env, raw, serves_web, serves_worker):
        env.setenv("SERVER_ROLE", raw)
        cfg = Config.from_env()
        assert cfg.serves_web() is serves_web
        assert cfg.serves_worker() is serves_worker

    def test_rejects_unknown_role(self, env):
        """Do not let an unknown value silently fall back to building both.

        With a fallback, a typo exposes /tasks/* unintentionally.
        """
        env.setenv("SERVER_ROLE", "batch")
        with pytest.raises(RuntimeError, match="SERVER_ROLE"):
            Config.from_env()

    def test_worker_does_not_require_session_secret(self, env):
        """A process with only the worker face does not use OAuth or sessions, so it is not required.

        If it stayed required, the worker's service account would have to be granted
        access to credentials it never uses.
        """
        env.delenv("SESSION_SECRET")
        env.setenv("SERVER_ROLE", "worker")

        cfg = Config.from_env()

        assert cfg.session_secret == ""

    def test_web_still_requires_session_secret(self, env):
        env.delenv("SESSION_SECRET")
        env.setenv("SERVER_ROLE", "web")
        with pytest.raises(ValueError, match="SESSION_SECRET"):
            Config.from_env()
