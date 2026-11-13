from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import threading
from collections import deque
from pathlib import Path

from app import video_merge
from app.adapters import gcs
from app.domain.music_layout import music_audio_uri
from app.domain.task import Task
from app.pipeline.deadline import Deadline, PipelineTimeout

logger = logging.getLogger(__name__)

# app/ directory where align_subtitles.py and burn_subs.py live
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
# Number of stderr lines included in the exception message on failure
_STDERR_TAIL_LINES = 5
# How long to wait for a terminated subprocess to shut down on SIGTERM. Past this, SIGKILL.
# ffmpeg cleans up on exit by closing its output file, so it is stopped gently first.
_TERMINATE_GRACE_SEC = 10.0


class PipelineRunner:
    def __init__(
        self,
        ap_mv_bucket: str = "ap-mv",
        ap_music_bucket: str = "ap-music",
        video_whisper_model: str = video_merge.DEFAULT_WHISPER_MODEL,
        pipeline_timeout_sec: float = 0.0,
    ) -> None:
        self._ap_mv_bucket = ap_mv_bucket
        self._ap_music_bucket = ap_music_bucket
        self._video_whisper_model = video_whisper_model
        # 0 or less means no limit. The default is 0 so that the behaviour of existing calls that
        # do not pass this argument (tests, and other paths in the future) does not change.
        # The production value is set by config (PIPELINE_TIMEOUT, default 25m).
        self._pipeline_timeout_sec = pipeline_timeout_sec

    def run(self, task: Task) -> str:
        """Fetch the input material and run every step through subtitle generation, burn-in and upload.

        ★ One deadline for the whole job is created here and carried through every later step.
          However many per-step limits (such as Whisper's 900s) are added up, they do not make a
          "limit for the whole job", so this one is laid over them from above (see deadline.py).
        """
        deadline = Deadline(self._pipeline_timeout_sec)
        if task.source_type == "video":
            return self._run_video_merge(task, deadline)
        return self._run_image_slideshow(task, deadline)

    def _run_video_merge(self, task: Task, deadline: Deadline) -> str:
        """The path that combines ap-mv's finished video with ap-music's song (no still-image slideshow)."""
        with tempfile.TemporaryDirectory(prefix="lyric_video_merge_") as work_dir:
            work = Path(work_dir)
            output_path = work / "output.mp4"
            video_merge.build(
                task.ap_mv_job_id,
                task.music_job_id,
                work,
                output_path,
                ap_mv_bucket=self._ap_mv_bucket,
                ap_music_bucket=self._ap_music_bucket,
                whisper_model=self._video_whisper_model,
                deadline=deadline,
            )

            output_uri = f"{task.output_prefix.rstrip('/')}/{task.job_id}/output.mp4"
            # Check the deadline just before the upload. If it is used up, giving up here avoids
            # leaving a half-written object in GCS.
            deadline.check("upload output")
            logger.info("Uploading output to %s", output_uri)
            gcs.upload(str(output_path), output_uri)
            return output_uri

    def _run_image_slideshow(self, task: Task, deadline: Deadline) -> str:
        """The existing path that generates an MV from still keyframes with karaoke subtitles burned in."""
        audio_url = music_audio_uri(self._ap_music_bucket, task.music_job_id)
        keyframes_url = f"gs://{self._ap_mv_bucket}/veo/jobs/{task.ap_mv_job_id}/keyframes.zip"

        with tempfile.TemporaryDirectory(prefix="lyric_video_") as work_dir:
            work = Path(work_dir)

            logger.info("Downloading audio from %s", audio_url)
            audio_path = str(work / "audio.mp3")
            gcs.download(audio_url, audio_path)

            deadline.check("download keyframes")
            logger.info("Downloading keyframes from %s", keyframes_url)
            keyframes_path = str(work / "keyframes.zip")
            gcs.download(keyframes_url, keyframes_path)

            if task.subs_url:
                ass_path = str(work / "subtitles.ass")
                logger.info("Downloading pre-aligned ASS from %s", task.subs_url)
                gcs.download(task.subs_url, ass_path)
            else:
                ass_path = str(work / "subtitles_aligned.ass")
                self._run_align(audio_path, keyframes_path, ass_path, task.whisper_model, deadline)

            output_path = str(work / "output.mp4")
            self._run_burn(audio_path, keyframes_path, output_path, ass_path, deadline)

            output_uri = f"{task.output_prefix.rstrip('/')}/{task.job_id}/output.mp4"
            try:
                deadline.check("upload output")
                logger.info("Uploading output to %s", output_uri)
                gcs.upload(output_path, output_uri)
                return output_uri
            finally:
                # Free tmpfs (RAM) early — large files no longer needed after upload
                for p in (audio_path, keyframes_path, ass_path, output_path):
                    Path(p).unlink(missing_ok=True)

    def _run_align(self, audio: str, keyframes: str, output_ass: str, model: str, deadline: Deadline) -> None:
        """Generate ASS subtitles with karaoke timing from the audio and the original subtitles."""
        script = str(_SCRIPTS_DIR / "align_subtitles.py")
        cmd = [sys.executable, script, audio, keyframes, output_ass, "--model", model]
        logger.info("Running align_subtitles model=%s", model)
        _run_subprocess(cmd, "align_subtitles", deadline)

    def _run_burn(self, audio: str, keyframes: str, output: str, subs: str, deadline: Deadline) -> None:
        """Burn the ASS subtitles into the keyframe images and generate an MP4."""
        script = str(_SCRIPTS_DIR / "burn_subs.py")
        cmd = [sys.executable, script, audio, keyframes, output, "--subs", subs]
        logger.info("Running burn_subs")
        _run_subprocess(cmd, "burn_subs", deadline)


def _stream(pipe, log_fn, sink: deque | None = None):
    """Pass the subprocess output, line by line, to the given log function."""
    for line in iter(pipe.readline, ""):
        line = line.rstrip()
        log_fn(line)
        if sink is not None and line:
            sink.append(line)


def _terminate(proc: subprocess.Popen) -> None:
    """Stop a subprocess gently, and forcibly if that does not work.

    SIGTERM is sent first. Both ffmpeg and Whisper clean up on exit by closing their output files,
    so an immediate SIGKILL leaves corrupt files on tmpfs. Past the grace period, SIGKILL.
    """
    proc.terminate()
    try:
        proc.wait(timeout=_TERMINATE_GRACE_SEC)
    except subprocess.TimeoutExpired:
        logger.warning("Subprocess did not exit after SIGTERM, sending SIGKILL")
        proc.kill()
        proc.wait()


def _run_subprocess(cmd: list[str], name: str, deadline: Deadline) -> None:
    """Run a subprocess and forward its stdout and stderr to the log.

    On failure, the exception message includes the tail of stderr as well as the exit code. This
    text appears as is in the error of meta.json and in the Slack notification, so that a cause
    such as "the character counts of the lyrics and Whisper do not match" can be understood without
    going to the logs.

    ★ The wait is capped at the time remaining for the whole job. Previously proc.wait() had no
      limit, and when align_subtitles or burn_subs hung it **simply waited forever**. In practice
      Cloud Tasks cuts it off at 30 minutes, so it does not look infinite, but when it is cut off
      the whole process disappears with SIGTERM, so neither a record of the failure nor a Slack
      notification was left (see deadline.py). Giving up here first lets PipelineTimeout rise to
      worker.py, which can then write failed.
    """
    deadline.check(name)
    stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        t_out = threading.Thread(target=_stream, args=(proc.stdout, logger.info))
        t_err = threading.Thread(target=_stream, args=(proc.stderr, logger.warning, stderr_tail))
        t_out.start()
        t_err.start()
        try:
            proc.wait(timeout=deadline.remaining())
        except subprocess.TimeoutExpired:
            logger.error("%s exceeded the pipeline deadline, terminating", name)
            _terminate(proc)
            # Wait for the pipes to close and the reader threads to see EOF. Leaving the Popen
            # with block without joining leaves threads that keep reading a closed fd.
            t_out.join()
            t_err.join()
            raise PipelineTimeout(f"{name} exceeded the pipeline deadline") from None
        t_out.join()
        t_err.join()
        if proc.returncode != 0:
            detail = " / ".join(stderr_tail)
            message = f"{name} failed with exit code {proc.returncode}"
            raise RuntimeError(f"{message}: {detail}" if detail else message)
