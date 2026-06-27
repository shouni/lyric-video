from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from app.adapters import gcs
from app.domain.task import Task

logger = logging.getLogger(__name__)

# app/ directory where align_subtitles.py and burn_subs.py live
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent


class PipelineRunner:
    def run(self, task: Task) -> str:
        """Run the full pipeline and return the output GCS URI."""
        with tempfile.TemporaryDirectory(prefix="lyric_video_") as work_dir:
            work = Path(work_dir)

            logger.info("Downloading audio from %s", task.audio_url)
            audio_path = str(work / "audio.mp3")
            gcs.download(task.audio_url, audio_path)

            logger.info("Downloading keyframes from %s", task.keyframes_url)
            keyframes_path = str(work / "keyframes.zip")
            gcs.download(task.keyframes_url, keyframes_path)

            if task.subs_url:
                ass_path = str(work / "subtitles.ass")
                logger.info("Downloading pre-aligned ASS from %s", task.subs_url)
                gcs.download(task.subs_url, ass_path)
            else:
                ass_path = str(work / "subtitles_aligned.ass")
                self._run_align(audio_path, keyframes_path, ass_path, task.whisper_model)

            output_path = str(work / "output.mp4")
            self._run_burn(audio_path, keyframes_path, output_path, ass_path)

            output_uri = f"{task.output_prefix.rstrip('/')}/{task.job_id}/output.mp4"
            logger.info("Uploading output to %s", output_uri)
            gcs.upload(output_path, output_uri)

            return output_uri

    def _run_align(self, audio: str, keyframes: str, output_ass: str, model: str) -> None:
        script = str(_SCRIPTS_DIR / "align_subtitles.py")
        cmd = [sys.executable, script, audio, keyframes, output_ass, "--model", model]
        logger.info("Running align_subtitles model=%s", model)
        _run_subprocess(cmd, "align_subtitles")

    def _run_burn(self, audio: str, keyframes: str, output: str, subs: str) -> None:
        script = str(_SCRIPTS_DIR / "burn_subs.py")
        cmd = [sys.executable, script, audio, keyframes, output, "--subs", subs]
        logger.info("Running burn_subs")
        _run_subprocess(cmd, "burn_subs")


def _stream(pipe, log_fn):
    for line in iter(pipe.readline, ""):
        log_fn(line.rstrip())


def _run_subprocess(cmd: list[str], name: str) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    t_out = threading.Thread(target=_stream, args=(proc.stdout, logger.info))
    t_err = threading.Thread(target=_stream, args=(proc.stderr, logger.warning))
    t_out.start()
    t_err.start()
    proc.wait()
    t_out.join()
    t_err.join()
    if proc.returncode != 0:
        raise RuntimeError(f"{name} failed with exit code {proc.returncode}")
