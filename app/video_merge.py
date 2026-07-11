from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pysubs2

from app.adapters import gcs

logger = logging.getLogger(__name__)

_AP_MV_BUCKET = "ap-mv"
_AP_MUSIC_BUCKET = "ap-music"


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
    section_start_sec = float(cuts_sorted[0]["start_sec"])
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
    _build_subtitles(cuts_sorted, section_start_sec, subs_path)

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
            f"ass={subs_path}",
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


def _build_subtitles(cuts_sorted: list[dict], section_start_sec: float, out_path: Path) -> None:
    """カット単位のdialogue/start_sec/end_secを、セクション開始基準の相対時刻へシフトしてASS化する。"""
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
        start = float(cut["start_sec"]) - section_start_sec
        end = float(cut["end_sec"]) - section_start_sec
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
    return float(result.stdout.strip())


def _run(cmd: list[str], step: str) -> subprocess.CompletedProcess:
    logger.info("Running %s: %s", step, " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{step} failed (exit {result.returncode}): {result.stderr}")
    return result
