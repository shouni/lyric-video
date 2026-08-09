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
# 失敗時に例外メッセージへ含める標準エラーの行数
_STDERR_TAIL_LINES = 5
# 打ち切ったサブプロセスが SIGTERM で畳まれるのを待つ時間。これを過ぎたら SIGKILL。
# ffmpeg は終了時に出力ファイルを閉じる後始末をするので、まず穏当に止める。
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
        # 0 以下は上限なし。既定を 0 にしてあるのは、この引数を渡さない
        # 既存の呼び出し（テストや将来の別経路）の挙動を変えないため。
        # 本番の値は config が入れる（PIPELINE_TIMEOUT、既定 25m）。
        self._pipeline_timeout_sec = pipeline_timeout_sec

    def run(self, task: Task) -> str:
        """入力素材を取得し、字幕生成・焼き込み・アップロードまでの全工程を実行する。

        ★ ジョブ全体の締切をここで 1 つ作り、以降の全工程へ持ち回る。
          個別ステップの上限（Whisper の 900s など）をいくら足しても「ジョブ全体の
          上限」にはならないので、上から被せる形にしている（deadline.py 参照）。
        """
        deadline = Deadline(self._pipeline_timeout_sec)
        if task.source_type == "video":
            return self._run_video_merge(task, deadline)
        return self._run_image_slideshow(task, deadline)

    def _run_video_merge(self, task: Task, deadline: Deadline) -> str:
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
                deadline=deadline,
            )

            output_uri = f"{task.output_prefix.rstrip('/')}/{task.job_id}/output.mp4"
            # アップロードの手前で締切を見る。使い切っていればここで諦めたほうが、
            # 中途半端なオブジェクトを GCS に残さずに済む。
            deadline.check("upload output")
            logger.info("Uploading output to %s", output_uri)
            gcs.upload(str(output_path), output_uri)
            return output_uri

    def _run_image_slideshow(self, task: Task, deadline: Deadline) -> str:
        """静止画キーフレーム+カラオケ字幕焼き込みでMVを生成する既存経路。"""
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
        """音声と元字幕からカラオケタイミング付きASS字幕を生成する。"""
        script = str(_SCRIPTS_DIR / "align_subtitles.py")
        cmd = [sys.executable, script, audio, keyframes, output_ass, "--model", model]
        logger.info("Running align_subtitles model=%s", model)
        _run_subprocess(cmd, "align_subtitles", deadline)

    def _run_burn(self, audio: str, keyframes: str, output: str, subs: str, deadline: Deadline) -> None:
        """ASS字幕をキーフレーム画像へ焼き込み、MP4を生成する。"""
        script = str(_SCRIPTS_DIR / "burn_subs.py")
        cmd = [sys.executable, script, audio, keyframes, output, "--subs", subs]
        logger.info("Running burn_subs")
        _run_subprocess(cmd, "burn_subs", deadline)


def _stream(pipe, log_fn, sink: deque | None = None):
    """サブプロセスの出力を1行ずつ指定されたログ関数へ流す。"""
    for line in iter(pipe.readline, ""):
        line = line.rstrip()
        log_fn(line)
        if sink is not None and line:
            sink.append(line)


def _terminate(proc: subprocess.Popen) -> None:
    """サブプロセスを穏当に、駄目なら強制的に停止する。

    まず SIGTERM を送る。ffmpeg も Whisper も終了時に出力ファイルを閉じる後始末をするので、
    いきなり SIGKILL すると tmpfs に壊れたファイルが残る。猶予を過ぎたら SIGKILL。
    """
    proc.terminate()
    try:
        proc.wait(timeout=_TERMINATE_GRACE_SEC)
    except subprocess.TimeoutExpired:
        logger.warning("Subprocess did not exit after SIGTERM, sending SIGKILL")
        proc.kill()
        proc.wait()


def _run_subprocess(cmd: list[str], name: str, deadline: Deadline) -> None:
    """サブプロセスを実行し、標準出力と標準エラーをログへ転送する。

    失敗時は終了コードだけでなく標準エラーの末尾も例外メッセージへ含める。この文言は
    meta.json の error とSlack通知にそのまま出るため、ログを見に行かずに
    「歌詞とWhisperの文字数が一致しない」等の原因が分かるようにする。

    ★ 待ち時間はジョブ全体の残り時間で頭打ちにする。以前は proc.wait() に上限が無く、
      align_subtitles や burn_subs が固まると**そのまま無限に待った**。実際には
      Cloud Tasks が 30 分で打ち切るので無限には見えないが、打ち切られるとプロセスごと
      SIGTERM で消えるため、失敗の記録も Slack 通知も残らなかった（deadline.py 参照）。
      ここで先に諦めれば、PipelineTimeout が worker.py まで上がって failed を書ける。
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
            # パイプが閉じてリーダースレッドが EOF を見るのを待つ。join しないまま
            # Popen の with を抜けると、閉じた fd を読み続けるスレッドが残る。
            t_out.join()
            t_err.join()
            raise PipelineTimeout(f"{name} exceeded the pipeline deadline") from None
        t_out.join()
        t_err.join()
        if proc.returncode != 0:
            detail = " / ".join(stderr_tail)
            message = f"{name} failed with exit code {proc.returncode}"
            raise RuntimeError(f"{message}: {detail}" if detail else message)
