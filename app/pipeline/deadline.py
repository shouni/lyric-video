"""A deadline for putting an upper limit on the run time of the whole job.

Why it is needed: the three timeout levels
------------------------------------------

As a fleet-wide convention there are three timeouts, and this ordering between them is kept.

    PIPELINE_TIMEOUT  <  dispatch_deadline  <=  Cloud Run timeout
        (app)                (task)                (service)

The effective limit is decided by the **smallest value**, and the others are decoration. The point
is to make PIPELINE_TIMEOUT the **shortest** here: because **the app gives up first by itself**,
it can write failed to meta.json and notify Slack before it ends.

In the reverse order, Cloud Tasks cuts the request off first. The cutoff is a SIGTERM to the whole
process, so **neither the failure handler nor the Slack notification runs**. lyric-video-queue has
max_attempts = 1, so no retry comes either, and meta.json is left behind at status: running.
This is the cause of "a job stopped at running with no Task failed in the logs".

This path has actually been hit. One job was cut off while verifying source_type=images (the path
that makes a slideshow from keyframes). The breakdown was about 8 and a half minutes for the
Whisper large-v3 alignment, and the remaining 20 minutes or more for drawing the karaoke subtitle
PNGs.

For a long time the Python version did not have this top level. Each ffmpeg step has its own
timeout (video_merge._SUBPROCESS_TIMEOUT_SEC), but **there is no guarantee that the total fits
within the limit**. However many per-step limits are added up, they do not make a "limit for the
whole job", and that is the reason this module exists.

Why not signal.alarm
--------------------

signal.alarm fires only on the main thread. Flask request handlers run on gunicorn worker threads,
so setting it there has no effect.
Instead, the approach is to "carry a deadline around and cap each subprocess's wait at the time
remaining". It works the same from whichever thread it is called, and the cutoff rises as an
exception, so the existing except in worker.py can handle recording failed and notifying Slack
as it is.
"""

from __future__ import annotations

import time


class PipelineTimeout(RuntimeError):
    """Indicates that the time limit for the whole job has been used up.

    It inherits from RuntimeError so that it rides, as is, the existing path in which the
    except Exception in worker.py catches it, writes failed to meta.json and notifies Slack.
    """


class Deadline:
    """Sets the deadline at timeout_sec after creation and hands out the time remaining.

    Uses time.monotonic. With the system clock, the deadline can move back and forth with NTP
    corrections.
    """

    def __init__(self, timeout_sec: float) -> None:
        self._timeout_sec = timeout_sec
        # 0 or less means "no limit". It is an escape hatch for long debugging sessions locally,
        # and is not taken in production because config sets the default value.
        self._expires_at = time.monotonic() + timeout_sec if timeout_sec > 0 else None

    @property
    def unlimited(self) -> bool:
        return self._expires_at is None

    def remaining(self) -> float | None:
        """Seconds remaining. None if there is no limit. Returns a value of 0 or less if used up."""
        if self._expires_at is None:
            return None
        return self._expires_at - time.monotonic()

    def check(self, step: str) -> None:
        """Raise PipelineTimeout if the time remaining has run out.

        Called at the boundary between steps. If nothing remains just before starting a subprocess,
        failing here without starting it is faster, and the message is easier to understand too.
        """
        remaining = self.remaining()
        if remaining is not None and remaining <= 0:
            raise PipelineTimeout(f"pipeline timed out after {self._timeout_sec:.0f}s (before {step})")

    def clamp(self, preferred_sec: float, step: str) -> float:
        """Cap a per-step limit at the time remaining for the whole job.

        Using the step's own limit as is (for example Whisper's 900s) can make the total exceed
        the limit for the whole job. The shorter of the two is taken.
        """
        self.check(step)
        remaining = self.remaining()
        if remaining is None:
            return preferred_sec
        return min(preferred_sec, remaining)
