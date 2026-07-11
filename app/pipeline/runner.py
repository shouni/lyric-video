from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from app import video_merge
from app.adapters import gcs
from app.domain.task import Task

logger = logging.getLogger(__name__)

# app/ directory where align_subtitles.py and burn_subs.py live
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent


class PipelineRunner:
    def __init__(self, ap_mv_bucket: str = "ap-mv", ap_music_bucket: str = "ap-music") -> None:
        self._ap_mv_bucket = ap_mv_bucket
        self._ap_music_bucket = ap_music_bucket

    def run(self, task: Task) -> str:
        """入力素材を取得し、字幕生成・焼き込み・アップロードまでの全工程を実行する。"""
        if task.source_type == "video":
            return self._run_video_merge(task)
        return self._run_image_slideshow(task)

    def _run_video_merge(self, task: Task) -> str:
        """ap-mvの完成動画とap-musicの楽曲を合成する経路(静止画スライドショーを使わない)。"""
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
            )

            output_uri = f"{task.output_prefix.rstrip('/')}/{task.job_id}/output.mp4"
            logger.info("Uploading output to %s", output_uri)
            gcs.upload(str(output_path), output_uri)
            return output_uri

    def _run_image_slideshow(self, task: Task) -> str:
        """静止画キーフレーム+カラオケ字幕焼き込みでMVを生成する既存経路。"""
        audio_url = f"gs://{self._ap_music_bucket}/{task.music_job_id}.mp3"
        keyframes_url = f"gs://{self._ap_mv_bucket}/veo/jobs/{task.ap_mv_job_id}/keyframes.zip"

        with tempfile.TemporaryDirectory(prefix="lyric_video_") as work_dir:
            work = Path(work_dir)

            logger.info("Downloading audio from %s", audio_url)
            audio_path = str(work / "audio.mp3")
            gcs.download(audio_url, audio_path)

            logger.info("Downloading keyframes from %s", keyframes_url)
            keyframes_path = str(work / "keyframes.zip")
            gcs.download(keyframes_url, keyframes_path)

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
            try:
                logger.info("Uploading output to %s", output_uri)
                gcs.upload(output_path, output_uri)
                return output_uri
            finally:
                # Free tmpfs (RAM) early — large files no longer needed after upload
                for p in (audio_path, keyframes_path, ass_path, output_path):
                    Path(p).unlink(missing_ok=True)

    def _run_align(self, audio: str, keyframes: str, output_ass: str, model: str) -> None:
        """音声と元字幕からカラオケタイミング付きASS字幕を生成する。"""
        script = str(_SCRIPTS_DIR / "align_subtitles.py")
        cmd = [sys.executable, script, audio, keyframes, output_ass, "--model", model]
        logger.info("Running align_subtitles model=%s", model)
        _run_subprocess(cmd, "align_subtitles")

    def _run_burn(self, audio: str, keyframes: str, output: str, subs: str) -> None:
        """ASS字幕をキーフレーム画像へ焼き込み、MP4を生成する。"""
        script = str(_SCRIPTS_DIR / "burn_subs.py")
        cmd = [sys.executable, script, audio, keyframes, output, "--subs", subs]
        logger.info("Running burn_subs")
        _run_subprocess(cmd, "burn_subs")


def _stream(pipe, log_fn):
    """サブプロセスの出力を1行ずつ指定されたログ関数へ流す。"""
    for line in iter(pipe.readline, ""):
        log_fn(line.rstrip())


def _run_subprocess(cmd: list[str], name: str) -> None:
    """サブプロセスを実行し、標準出力と標準エラーをログへ転送する。"""
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        t_out = threading.Thread(target=_stream, args=(proc.stdout, logger.info))
        t_err = threading.Thread(target=_stream, args=(proc.stderr, logger.warning))
        t_out.start()
        t_err.start()
        proc.wait()
        t_out.join()
        t_err.join()
        if proc.returncode != 0:
            raise RuntimeError(f"{name} failed with exit code {proc.returncode}")
