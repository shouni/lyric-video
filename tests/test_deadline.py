"""Tests for the deadline of the whole job (PIPELINE_TIMEOUT).

There is only one property to protect: **the app gives up before Cloud Tasks does and raises an exception**.
Then the except in worker.py can write failed to meta.json and notify Slack.
If Cloud Tasks cuts the request off first, SIGTERM removes the whole process and nothing is left.
The background is in app/pipeline/deadline.py.
"""

import sys
import time

import pytest

from app.adapters.task_queue import DISPATCH_DEADLINE_SEC
from app.config import Config, parse_duration
from app.pipeline.deadline import Deadline, PipelineTimeout
from app.pipeline.runner import _run_subprocess


class TestParseDuration:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("25m", 1500.0),
            ("1500s", 1500.0),
            # No unit means seconds. The notation follows the Go implementation, and a bare number is accepted too.
            ("1500", 1500.0),
            ("1h", 3600.0),
            ("500ms", 0.5),
            ("1.5m", 90.0),
            ("  25m  ", 1500.0),
        ],
    )
    def test_parses(self, text, expected):
        assert parse_duration(text) == expected

    @pytest.mark.parametrize("text", ["", "abc", "25 minutes", "-5s", "m"])
    def test_rejects_garbage(self, text):
        with pytest.raises(ValueError):
            parse_duration(text)


class TestDeadline:
    def test_unlimited_when_not_positive(self):
        deadline = Deadline(0)
        assert deadline.unlimited
        assert deadline.remaining() is None
        # With no limit, both check and clamp pass through.
        deadline.check("step")
        assert deadline.clamp(300.0, "step") == 300.0

    def test_remaining_counts_down(self):
        deadline = Deadline(10)
        assert 9 < deadline.remaining() <= 10

    def test_check_raises_once_exhausted(self):
        deadline = Deadline(0.01)
        time.sleep(0.02)
        with pytest.raises(PipelineTimeout, match="pipeline timed out"):
            deadline.check("burn_subs")

    def test_clamp_takes_the_shorter_of_the_two(self):
        deadline = Deadline(5)
        # The remaining time (5s) is shorter than the step's own limit (900s), so the remaining time is used.
        assert deadline.clamp(900.0, "align") <= 5
        # When the step's limit (1s) is shorter than the remaining time (5s), it stays as it is.
        assert deadline.clamp(1.0, "probe") == 1.0

    def test_clamp_raises_instead_of_returning_zero(self):
        """Do not let a subprocess start with timeout=0 after the deadline is used up."""
        deadline = Deadline(0.01)
        time.sleep(0.02)
        with pytest.raises(PipelineTimeout):
            deadline.clamp(900.0, "align")


class TestRunSubprocess:
    """★ The core. proc.wait() used to have no limit, and when the process hung it waited forever."""

    def test_kills_a_hanging_subprocess_and_raises(self):
        # A child process that only sleeps for 60 seconds. The deadline is just 0.3 seconds, so it should be killed.
        cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
        started = time.monotonic()
        with pytest.raises(PipelineTimeout, match="exceeded the pipeline deadline"):
            _run_subprocess(cmd, "burn_subs", Deadline(0.3))
        elapsed = time.monotonic() - started
        # It must return without waiting 60 seconds (well below, even with the 10s SIGTERM grace added).
        assert elapsed < 15

    def test_does_not_start_when_deadline_already_spent(self):
        deadline = Deadline(0.01)
        time.sleep(0.02)
        # A command that would take 5 seconds if it started. It must fail at once without starting.
        cmd = [sys.executable, "-c", "import time; time.sleep(5)"]
        started = time.monotonic()
        with pytest.raises(PipelineTimeout):
            _run_subprocess(cmd, "align_subtitles", deadline)
        assert time.monotonic() - started < 1

    def test_passes_through_when_it_finishes_in_time(self):
        _run_subprocess([sys.executable, "-c", "print('ok')"], "align_subtitles", Deadline(30))

    def test_still_reports_exit_code_failures(self):
        """A failure unrelated to the deadline must be raised with stderr attached, as before."""
        cmd = [sys.executable, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"]
        with pytest.raises(RuntimeError, match="exit code 3") as excinfo:
            _run_subprocess(cmd, "burn_subs", Deadline(30))
        assert "boom" in str(excinfo.value)
        # It is not a timeout, so it must not be a PipelineTimeout.
        assert not isinstance(excinfo.value, PipelineTimeout)

    def test_unlimited_deadline_waits_normally(self):
        _run_subprocess([sys.executable, "-c", "import time; time.sleep(0.05)"], "burn_subs", Deadline(0))


class TestSigkillFallback:
    def test_kills_a_process_that_ignores_sigterm(self, monkeypatch):
        """Even a child process that swallows SIGTERM must be reliably stopped by the SIGKILL after the grace period."""
        # Shorten the grace period so that the SIGKILL path runs within the test time.
        monkeypatch.setattr("app.pipeline.runner._TERMINATE_GRACE_SEC", 0.3)
        script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        started = time.monotonic()
        with pytest.raises(PipelineTimeout):
            _run_subprocess([sys.executable, "-c", script], "burn_subs", Deadline(0.3))
        assert time.monotonic() - started < 15


class TestConfigInvariant:
    """★ Check the three-level ordering at startup. Only config can see both values."""

    @pytest.fixture
    def env(self, monkeypatch):
        monkeypatch.setenv("SESSION_SECRET", "secret")
        monkeypatch.setenv("SERVER_ROLE", "worker")
        monkeypatch.setenv("ALLOWED_TASK_SERVICE_ACCOUNTS", "tasks@project.iam.gserviceaccount.com")
        monkeypatch.delenv("PIPELINE_TIMEOUT", raising=False)
        return monkeypatch

    def test_defaults_to_25m(self, env):
        assert Config.from_env().pipeline_timeout_sec == 1500.0

    def test_default_is_below_the_dispatch_deadline(self, env):
        """The default value itself must satisfy the invariant."""
        assert Config.from_env().pipeline_timeout_sec < DISPATCH_DEADLINE_SEC

    def test_accepts_a_shorter_explicit_value(self, env):
        env.setenv("PIPELINE_TIMEOUT", "10m")
        assert Config.from_env().pipeline_timeout_sec == 600.0

    @pytest.mark.parametrize("value", ["30m", "1800s", "45m"])
    def test_rejects_values_at_or_above_the_dispatch_deadline(self, env, value):
        env.setenv("PIPELINE_TIMEOUT", value)
        with pytest.raises(RuntimeError, match="dispatch_deadline"):
            Config.from_env()

    def test_web_only_role_is_not_checked(self, env):
        """Do not fail in a role that does not run the pipeline.

        This avoids the accident where only web fails to start.
        """
        env.setenv("SERVER_ROLE", "web")
        env.setenv("PIPELINE_TIMEOUT", "45m")
        assert Config.from_env().pipeline_timeout_sec == 2700.0

    def test_zero_means_unlimited_and_skips_the_check(self, env):
        env.setenv("PIPELINE_TIMEOUT", "0")
        assert Config.from_env().pipeline_timeout_sec == 0.0


class TestRunnerWiring:
    """PipelineRunner must actually build the deadline and hand it out."""

    def test_run_builds_a_deadline_from_the_configured_timeout(self, monkeypatch):
        from app.domain.task import Task
        from app.pipeline.runner import PipelineRunner

        captured = {}

        def fake_slideshow(self, task, deadline):
            captured["remaining"] = deadline.remaining()
            return "gs://bucket/out.mp4"

        monkeypatch.setattr(PipelineRunner, "_run_image_slideshow", fake_slideshow)
        runner = PipelineRunner(pipeline_timeout_sec=1500.0)
        task = Task(job_id="j1", ap_mv_job_id="mv1", music_job_id="m1", output_prefix="gs://bucket/out")

        assert runner.run(task) == "gs://bucket/out.mp4"
        assert 1490 < captured["remaining"] <= 1500

    def test_default_runner_is_unlimited(self, monkeypatch):
        from app.domain.task import Task
        from app.pipeline.runner import PipelineRunner

        captured = {}

        def fake_slideshow(self, task, deadline):
            captured["unlimited"] = deadline.unlimited
            return "gs://bucket/out.mp4"

        monkeypatch.setattr(PipelineRunner, "_run_image_slideshow", fake_slideshow)
        PipelineRunner().run(Task(job_id="j1", ap_mv_job_id="mv1", music_job_id="m1", output_prefix="gs://b/o"))
        assert captured["unlimited"] is True

    def test_video_path_hands_the_deadline_to_video_merge(self, monkeypatch):
        """The source_type=video side must receive the deadline too (this side runs ffmpeg the longest)."""
        from app.domain.task import Task
        from app.pipeline import runner as runner_mod
        from app.pipeline.runner import PipelineRunner

        captured = {}

        def fake_build(*args, deadline=None, **kwargs):
            captured["remaining"] = deadline.remaining()
            return kwargs.get("output_path")

        monkeypatch.setattr(runner_mod.video_merge, "build", fake_build)
        monkeypatch.setattr(runner_mod.gcs, "upload", lambda *a, **k: None)

        runner = PipelineRunner(pipeline_timeout_sec=1500.0)
        task = Task(
            job_id="j1",
            ap_mv_job_id="mv1",
            music_job_id="m1",
            output_prefix="gs://bucket/out",
            source_type="video",
        )

        assert runner.run(task) == "gs://bucket/out/j1/output.mp4"
        assert 1490 < captured["remaining"] <= 1500


class TestWorkerRecordsTheTimeout:
    """★ Confirming the purpose. The cutoff must lead all the way to the failed record and the Slack notification.

    If it does not, there was no point in adding the limit (just as when Cloud Tasks
    cuts the request off, meta.json is left behind at running).
    """

    def test_pipeline_timeout_is_recorded_as_failed_and_notified(self, monkeypatch):
        from app.main import create_app

        monkeypatch.setenv("SESSION_SECRET", "test-secret")
        monkeypatch.setenv("SERVER_ROLE", "worker")
        monkeypatch.setenv("ALLOWED_TASK_SERVICE_ACCOUNTS", "tasks@project.iam.gserviceaccount.com")
        monkeypatch.setenv("GCS_BUCKET", "lyric-video")
        app = create_app()
        app.config.update(TESTING=True)

        # OIDC verification belongs to another test, so let it pass.
        monkeypatch.setattr("app.handlers.worker.verify_task_caller", lambda *a, **k: True)
        monkeypatch.setattr("app.handlers.worker._fetch_recipe_for_task", lambda *a, **k: {})

        updates: list[dict] = []
        monkeypatch.setattr("app.handlers.worker.job_store.update", lambda uri, patch: updates.append(patch))
        monkeypatch.setattr("app.handlers.worker.job_store.meta_uri", lambda prefix, job_id: "gs://b/meta.json")

        notified: list[str] = []
        app.notifier.notify_error = lambda job_id, exc: notified.append(str(exc))

        def raise_timeout(task):
            raise PipelineTimeout("burn_subs exceeded the pipeline deadline")

        app.pipeline.run = raise_timeout

        payload = {
            "job_id": "lyric-1",
            "ap_mv_job_id": "mv-1",
            "music_job_id": "music-1",
            "output_prefix": "gs://lyric-video/out",
        }
        response = app.test_client().post("/tasks/generate", json=payload)

        assert response.status_code == 500
        # After running is written it must be overwritten with failed and not stay at running.
        assert updates[0]["status"] == "running"
        assert updates[-1]["status"] == "failed"
        assert "exceeded the pipeline deadline" in updates[-1]["error"]
        # The reason must reach Slack.
        assert notified and "exceeded the pipeline deadline" in notified[0]
