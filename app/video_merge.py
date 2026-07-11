from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

import pysubs2

from app.adapters import gcs

logger = logging.getLogger(__name__)

_AP_MV_BUCKET = "ap-mv"
_AP_MUSIC_BUCKET = "ap-music"
_SUBPROCESS_TIMEOUT_SEC = 300.0
# 曲1本(セクションではなく全体)としてあり得る上限。異常なメタデータ(負値・極端な
# 大きさ)をここで弾き、ffmpegに不正な-ss/-tをそのまま渡さないようにする。
_MAX_PLAUSIBLE_SEC = 3600.0


def _safe_sec(value, field: str) -> float:
    """メタデータの秒数値を検証しつつfloatへ変換する。"""
    try:
        sec = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc
    if not (0 <= sec <= _MAX_PLAUSIBLE_SEC):
        raise ValueError(f"{field} out of plausible range: {sec}")
    return sec


def build(ap_mv_job_id: str, music_job_id: str, work_dir: Path, output_path: Path) -> Path:
    """ap-mvの1セクション分の完成動画とap-musicの楽曲を合成し、字幕焼き込み済みMP4を作る。

    ap-mvのVeo video-to-video継続生成では後続カットほど直前カットを含む累積動画に
    なるため、cut_indexが最大のカット（＝そのセクションの完成版）だけを使う。
    """
    meta = gcs.load_json(f"gs://{_AP_MV_BUCKET}/veo/jobs/{ap_mv_job_id}/video_music_meta.json")
    cuts = meta.get("cuts") or []
    if not cuts:
        raise ValueError(f"ap-mv job {ap_mv_job_id!r} has no cuts")

    cuts_sorted = sorted(cuts, key=lambda c: c.get("cut_index", 0))
    section_start_sec = _safe_sec(cuts_sorted[0].get("start_sec"), "cuts[0].start_sec")
    last_cut = cuts_sorted[-1]
    video_uri = last_cut.get("video_url")
    if not video_uri:
        raise ValueError(f"ap-mv job {ap_mv_job_id!r} last cut has no video_url")

    video_path = work_dir / "section_video.mp4"
    logger.info("Downloading section video from %s", video_uri)
    gcs.download(video_uri, str(video_path))

    audio_uri = f"gs://{_AP_MUSIC_BUCKET}/{music_job_id}.mp3"
    full_audio_path = work_dir / "full_song.mp3"
    logger.info("Downloading music from %s", audio_uri)
    gcs.download(audio_uri, str(full_audio_path))

    video_duration = _probe_duration(video_path)

    trimmed_audio_path = work_dir / "section_audio.mp3"
    _run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(full_audio_path),
            "-ss",
            str(section_start_sec),
            "-t",
            str(video_duration),
            "-c",
            "copy",
            str(trimmed_audio_path),
        ],
        "trim audio",
    )

    subs_path = work_dir / "section_subtitles.ass"
    _build_subtitles(cuts_sorted, section_start_sec, video_duration, subs_path)

    _run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(video_path),
            "-i",
            str(trimmed_audio_path),
            "-vf",
            f"ass='{_escape_ffmpeg_filter_path(str(subs_path))}'",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(output_path),
        ],
        "burn subtitles and mux",
    )
    return output_path


def _escape_ffmpeg_filter_path(path: str) -> str:
    """ffmpegのfiltergraphパーサが特殊扱いする文字(\\ : ')をエスケープする。

    このパスは常にtempfile生成のディレクトリ+固定ファイル名で、ユーザー入力
    (ap_mv_job_id/music_job_id)が混ざることはないが、Windows等コロンを含み得る
    パス上で -vf ass=... が壊れないよう防御的にエスケープしておく。
    """
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _build_subtitles(cuts_sorted: list[dict], section_start_sec: float, video_duration: float, out_path: Path) -> None:
    """カット単位のdialogue/start_sec/end_secを、セクション開始基準の相対時刻へシフトしてASS化する。

    end_secはap-mvレシピ上のnominal値で、Veo生成後の実動画長(video_duration)より
    長いことがあるため、実尺を超えないようクランプする。
    """
    subs = pysubs2.SSAFile()
    subs.info["PlayResX"] = "1920"
    subs.info["PlayResY"] = "1080"
    style = pysubs2.SSAStyle(
        fontname="Arial",
        fontsize=72,
        primarycolor=pysubs2.Color(255, 255, 0),
        outlinecolor=pysubs2.Color(0, 0, 0),
        bold=True,
        outline=3,
        shadow=1,
        alignment=pysubs2.Alignment.BOTTOM_CENTER,
        marginv=60,
    )
    subs.styles["Karaoke"] = style

    for cut in cuts_sorted:
        text = (cut.get("dialogue") or "").strip()
        if not text:
            continue
        start = _safe_sec(cut.get("start_sec"), "cuts[].start_sec") - section_start_sec
        end = _safe_sec(cut.get("end_sec"), "cuts[].end_sec") - section_start_sec
        start = max(0.0, min(start, video_duration))
        end = max(0.0, min(end, video_duration))
        if end <= start:
            continue
        event = pysubs2.SSAEvent(start=pysubs2.make_time(s=start), end=pysubs2.make_time(s=end), text=text)
        event.style = "Karaoke"
        subs.append(event)

    subs.save(str(out_path))


def _probe_duration(path: Path) -> float:
    result = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        "probe duration",
    )
    output = result.stdout.strip()
    try:
        duration = float(output)
    except ValueError as exc:
        raise RuntimeError(f"failed to parse duration from ffprobe output: {output!r}") from exc
    if duration <= 0:
        raise RuntimeError(f"probed duration must be positive, got {duration}")
    return duration


def _run(cmd: list[str], step: str) -> subprocess.CompletedProcess:
    logger.info("Running %s: %s", step, shlex.join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{step} timed out after {_SUBPROCESS_TIMEOUT_SEC}s") from exc
    if result.returncode != 0:
        raise RuntimeError(f"{step} failed (exit {result.returncode}): {result.stderr}")
    return result
