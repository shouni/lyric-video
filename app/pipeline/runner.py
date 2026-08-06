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

logger = logging.getLogger(__name__)

# app/ directory where align_subtitles.py and burn_subs.py live
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
# 失敗時に例外メッセージへ含める標準エラーの行数
_STDERR_TAIL_LINES = 5


class PipelineRunner:
    def __init__(
        self,
        ap_mv_bucket: str = "ap-mv",
        ap_music_bucket: str = "ap-music",
        video_whisper_model: str = video_merge.DEFAULT_WHISPER_MODEL,
    ) -> None:
        self._ap_mv_bucket = ap_mv_bucket
        self._ap_music_bucket = ap_music_bucket
        self._video_whisper_model = video_whisper_model

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
                whisper_model=self._video_whisper_model,
            )

            output_uri = f"{task.output_prefix.rstrip('/')}/{task.job_id}/output.mp4"
            logger.info("Uploading output to %s", output_uri)
            gcs.upload(str(output_path), output_uri)
            return output_uri

    def _run_image_slideshow(self, task: Task) -> str:
        """静止画キーフレーム+カラオケ字幕焼き込みでMVを生成する既存経路。"""
        audio_url = music_audio_uri(self._ap_music_bucket, task.music_job_id)
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


def _stream(pipe, log_fn, sink: deque | None = None):
    """サブプロセスの出力を1行ずつ指定されたログ関数へ流す。"""
    for line in iter(pipe.readline, ""):
        line = line.rstrip()
        log_fn(line)
        if sink is not None and line:
            sink.append(line)


def _run_subprocess(cmd: list[str], name: str) -> None:
    """サブプロセスを実行し、標準出力と標準エラーをログへ転送する。

    失敗時は終了コードだけでなく標準エラーの末尾も例外メッセージへ含める。この文言は
    meta.json の error とSlack通知にそのまま出るため、ログを見に行かずに
    「歌詞とWhisperの文字数が一致しない」等の原因が分かるようにする。
    """
    stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        t_out = threading.Thread(target=_stream, args=(proc.stdout, logger.info))
        t_err = threading.Thread(target=_stream, args=(proc.stderr, logger.warning, stderr_tail))
        t_out.start()
        t_err.start()
        proc.wait()
        t_out.join()
        t_err.join()
        if proc.returncode != 0:
            detail = " / ".join(stderr_tail)
            message = f"{name} failed with exit code {proc.returncode}"
            raise RuntimeError(f"{message}: {detail}" if detail else message)
