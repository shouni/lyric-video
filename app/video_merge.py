from __future__ import annotations

import logging
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pysubs2
from PIL import Image, ImageDraw

from app.adapters import gcs
from app.burn_subs import load_font
from app.domain.music_layout import music_audio_uri
from app.pipeline.deadline import Deadline

logger = logging.getLogger(__name__)

# ★ The values below are limits **per step**. Adding them up does not give a limit for the whole
#   job (trim + align + burn alone are 300 + 900 + 300 = 1500s, and with the downloads added it
#   approaches the 1800s of dispatch_deadline). The limit for the whole job is held separately by
#   Deadline, and _run below takes the shorter of the two. The reason is in
#   app/pipeline/deadline.py.
_SUBPROCESS_TIMEOUT_SEC = 300.0
# This can run with Whisper's model load included, so it gets a longer timeout than ordinary
# ffmpeg/ffprobe calls.
_ALIGN_TIMEOUT_SEC = 900.0
DEFAULT_WHISPER_MODEL = "small"
# The plausible upper limit for one song (the whole song, not a section). Abnormal metadata
# (negative values, extreme sizes) is rejected here so that an invalid -ss/-t is not passed to
# ffmpeg as is.
_MAX_PLAUSIBLE_SEC = 3600.0
_ALIGN_SCRIPT = Path(__file__).resolve().parent / "align_subtitles.py"


def _safe_sec(value, field: str) -> float:
    """Validate a seconds value from the metadata while converting it to float."""
    try:
        sec = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc
    if not (0 <= sec <= _MAX_PLAUSIBLE_SEC):
        raise ValueError(f"{field} out of plausible range: {sec}")
    return sec


def build(
    ap_mv_job_id: str,
    music_job_id: str,
    work_dir: Path,
    output_path: Path,
    ap_mv_bucket: str = "ap-mv",
    ap_music_bucket: str = "ap-music",
    whisper_model: str = DEFAULT_WHISPER_MODEL,
    deadline: Deadline | None = None,
) -> Path:
    """Combine ap-mv's finished video with ap-music's song and make an MP4 with subtitles burned in.

    ap-mv's Veo video-to-video continuation sometimes resets the continuation chain internally to
    avoid the cumulative length limit of video_extension (30 seconds), but detecting and joining
    multiple chains (hard cuts) is completed on the ap-mv side
    (internal/worker/filter/chain_finalize.go), so this function only has to download
    `final_video_url` (the finished video, already joined into one) as is.
    """
    meta = gcs.load_json(f"gs://{ap_mv_bucket}/veo/jobs/{ap_mv_job_id}/video_music_meta.json")
    cuts = meta.get("cuts") or []
    if not cuts:
        raise ValueError(f"ap-mv job {ap_mv_job_id!r} has no cuts")

    cuts_sorted = sorted(cuts, key=lambda c: c.get("cut_index", 0))
    # start_sec on the ap-mv side is json:"start_sec,omitempty", so when the value is legitimately
    # 0, as for the first cut, the key itself is omitted. The value of an omitted key is guaranteed
    # to always be 0 (omitempty omits a float64 only when the value is 0.0), so a missing key is
    # treated as 0.0.
    section_start_sec = _safe_sec(cuts_sorted[0].get("start_sec", 0.0), "cuts[0].start_sec")

    # final_video_url is missing only for existing jobs generated before the ap-mv side supported
    # chain joining (the addition of final_video_url). For backward compatibility, the last cut's
    # video is used in that case as before (under the old behaviour, content is missing when there
    # are multiple chains, but this does not happen for new jobs).
    video_uri = meta.get("final_video_url") or cuts_sorted[-1].get("video_url")
    if not video_uri:
        raise ValueError(f"ap-mv job {ap_mv_job_id!r} has no final_video_url or video_url")

    video_path = work_dir / "section_video.mp4"
    logger.info("Downloading video from %s", video_uri)
    gcs.download(video_uri, str(video_path))

    audio_uri = music_audio_uri(ap_music_bucket, music_job_id)
    full_audio_path = work_dir / "full_song.mp3"
    logger.info("Downloading music from %s", audio_uri)
    gcs.download(audio_uri, str(full_audio_path))

    video_duration = _probe_duration(video_path, deadline)
    video_width, video_height = _probe_dimensions(video_path, deadline)

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
        deadline=deadline,
    )

    reference_subs_path = work_dir / "reference_subtitles.ass"
    fontsize = _write_reference_subtitles(cuts_sorted, video_width, video_height, reference_subs_path)

    subs_path = work_dir / "section_subtitles.ass"
    _align_with_whisper(trimmed_audio_path, reference_subs_path, subs_path, whisper_model, deadline)
    _wrap_aligned_subtitles(subs_path, video_width, fontsize)

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
        deadline=deadline,
    )
    return output_path


def _escape_ffmpeg_filter_path(path: str) -> str:
    """Escape the characters that ffmpeg's filtergraph parser treats specially (\\ : ').

    This path is always a tempfile-generated directory plus a fixed file name, and user input
    (ap_mv_job_id/music_job_id) never gets mixed in, but it is escaped defensively so that
    -vf ass=... does not break on paths that can contain a colon, such as on Windows.
    """
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _write_reference_subtitles(
    cuts_sorted: list[dict],
    video_width: int,
    video_height: int,
    out_path: Path,
) -> int:
    """Make a reference ASS, with timing not yet decided, that lists each cut's dialogue in lyric order.

    ap-mv's cuts[].start_sec/end_sec are a "directorial allocation" of which picture Veo assigns
    to which cut, not the actual singing timing (for example, making the first cut an intro shot
    with no lyrics). So the correct display timing is obtained only by force-aligning the recorded
    audio with Whisper. Here only the text, the display order and the style are fixed, and the
    actual start and end times are overwritten by _align_with_whisper (by the behaviour of
    align_subtitles.py, the Start/End of the Dialogue set here are not used in the first place).

    PlayResX/PlayResY are made to match the actual video resolution. ap-mv's video is 720x1280
    portrait (intended for shorts), and with fixed values (for example 1920x1080) libass would
    apply non-uniform scaling and the subtitles would move from the intended position. The font
    size and margins are also made proportional to the height, so that the visual proportions are
    the same at any resolution.

    The alignment uses TOP_CENTER. With bottom-center (\\an2) the reference position changes
    with the number of lines in the whole text block, so the display height does not line up
    between a one-line lyric and a lyric that becomes two lines through automatic wrapping. With
    top-center (\\an8) the top edge of the block is always fixed, and the position lines up
    regardless of the number of lines with no special handling at all. As a side effect, it also
    avoids collisions with the platform-specific UI that shorts platforms
    (YouTube Shorts/TikTok/Reels) tend to overlay on the bottom of the screen.
    """
    subs = pysubs2.SSAFile()
    subs.info["PlayResX"] = str(video_width)
    subs.info["PlayResY"] = str(video_height)
    # libass's automatic wrapping (WrapStyle 0) can only break at spaces, and a long Japanese line
    # with no spaces runs off the screen. Wrapping is done by our own code in
    # _wrap_aligned_subtitles, based on measured font widths, so automatic wrapping on the libass
    # side is disabled.
    subs.info["WrapStyle"] = "2"
    fontsize = max(32, round(video_height * 0.055))
    marginv = max(40, round(video_height * 0.10))
    style = pysubs2.SSAStyle(
        fontname="Arial",
        fontsize=fontsize,
        # primarycolor: the color after the \k highlight (the part already sung). secondarycolor:
        # the color before the highlight (the part not yet sung). pysubs2's default secondarycolor
        # is red, so unless white is specified explicitly the unhighlighted part becomes red text.
        primarycolor=pysubs2.Color(255, 255, 0),
        secondarycolor=pysubs2.Color(255, 255, 255),
        outlinecolor=pysubs2.Color(0, 0, 0),
        bold=True,
        outline=3,
        shadow=1,
        alignment=pysubs2.Alignment.TOP_CENTER,
        marginv=marginv,
    )
    subs.styles["Karaoke"] = style

    placeholder_sec = 0.0
    for cut in cuts_sorted:
        text = (cut.get("dialogue") or "").strip()
        if not text:
            continue
        event = pysubs2.SSAEvent(
            start=pysubs2.make_time(s=placeholder_sec), end=pysubs2.make_time(s=placeholder_sec + 1), text=text
        )
        event.style = "Karaoke"
        subs.append(event)
        placeholder_sec += 1

    subs.save(str(out_path))
    return fontsize


def _align_with_whisper(
    audio_path: Path,
    reference_subs_path: Path,
    out_path: Path,
    whisper_model: str = DEFAULT_WHISPER_MODEL,
    deadline: Deadline | None = None,
) -> None:
    """Find the actual utterance times of the reference ASS with align_subtitles.py (Whisper forced alignment)."""
    _run(
        [
            sys.executable,
            str(_ALIGN_SCRIPT),
            str(audio_path),
            str(reference_subs_path),
            str(out_path),
            "--model",
            whisper_model,
        ],
        "align subtitles",
        timeout=_ALIGN_TIMEOUT_SEC,
        deadline=deadline,
    )


_KARAOKE_TOKEN = re.compile(r"(\{\\k\d+\})([^{]*)")


def _wrap_aligned_subtitles(subs_path: Path, video_width: int, fontsize: int) -> None:
    """Wrap the karaoke text after Whisper alignment, based on measured font widths.

    libass's automatic wrapping (WrapStyle 0) can only break at spaces, and a long Japanese line
    with no spaces runs off the screen (_write_reference_subtitles sets WrapStyle:2, so automatic
    wrapping on the libass side is already disabled). Here the actual glyph width is measured per
    {\\kNN} token with the same font as burn_subs.py, and \\N is inserted where needed. It works
    whether or not there are spaces, so it does not break even when lyrics arrive that differ from
    what _write_reference_subtitles assumes.
    """
    font = load_font(fontsize)
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    max_width = video_width - 40

    subs = pysubs2.load(str(subs_path))
    for event in subs:
        event.text = _wrap_karaoke_text(event.text, draw, font, max_width)
    subs.save(str(subs_path))


def _wrap_karaoke_text(text: str, draw: ImageDraw.ImageDraw, font, max_width: float) -> str:
    """Split a sequence of {\\kNN} tokens into lines whose measured width in the given font stays within max_width.

    First, the position right after a token that contains a space is treated as a "word" boundary,
    and lines are filled word by word (as in ordinary wrapping, a word is not split in the middle).
    Only when a single word alone exceeds max_width (a long line with no spaces) is it forcibly
    split character by character.
    """
    tokens = _KARAOKE_TOKEN.findall(text)
    if not tokens:
        return text

    words: list[list[tuple[str, str]]] = []
    current_word: list[tuple[str, str]] = []
    for tag, chunk in tokens:
        current_word.append((tag, chunk))
        if " " in chunk:
            words.append(current_word)
            current_word = []
    if current_word:
        words.append(current_word)

    def token_str(tokens_: list[tuple[str, str]]) -> str:
        return "".join(t + c for t, c in tokens_)

    def token_plain(tokens_: list[tuple[str, str]]) -> str:
        return "".join(c for _, c in tokens_)

    def hard_break(word: list[tuple[str, str]]) -> list[str]:
        """Forcibly wrap character by character when a single word alone exceeds max_width."""
        sub_lines: list[str] = []
        current: list[tuple[str, str]] = []
        current_plain = ""
        for tag, chunk in word:
            candidate = current_plain + chunk
            if draw.textlength(candidate, font=font) > max_width and current:
                sub_lines.append(token_str(current))
                current = [(tag, chunk)]
                current_plain = chunk
            else:
                current.append((tag, chunk))
                current_plain = candidate
        if current:
            sub_lines.append(token_str(current))
        return sub_lines

    lines: list[str] = []
    current_line: list[tuple[str, str]] = []
    current_plain = ""
    for word in words:
        word_plain = token_plain(word)
        candidate_plain = current_plain + word_plain
        if draw.textlength(candidate_plain, font=font) > max_width and current_line:
            lines.append(token_str(current_line))
            current_line = []
            current_plain = ""

        if draw.textlength(word_plain, font=font) > max_width:
            sub_lines = hard_break(word)
            lines.extend(sub_lines[:-1])
            current_line = _KARAOKE_TOKEN.findall(sub_lines[-1]) if sub_lines else []
            current_plain = token_plain(current_line)
        else:
            current_line.extend(word)
            current_plain += word_plain

    if current_line:
        lines.append(token_str(current_line))
    return r"\N".join(lines)


def _probe_dimensions(path: Path, deadline: Deadline | None = None) -> tuple[int, int]:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(path),
        ],
        "probe dimensions",
        deadline=deadline,
    )
    output = result.stdout.strip()
    try:
        width_str, height_str = output.split("x")
        width, height = int(width_str), int(height_str)
    except ValueError as exc:
        raise RuntimeError(f"failed to parse dimensions from ffprobe output: {output!r}") from exc
    if width <= 0 or height <= 0:
        raise RuntimeError(f"probed dimensions must be positive, got {width}x{height}")
    return width, height


def _probe_duration(path: Path, deadline: Deadline | None = None) -> float:
    result = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        "probe duration",
        deadline=deadline,
    )
    output = result.stdout.strip()
    try:
        duration = float(output)
    except ValueError as exc:
        raise RuntimeError(f"failed to parse duration from ffprobe output: {output!r}") from exc
    if duration <= 0:
        raise RuntimeError(f"probed duration must be positive, got {duration}")
    return duration


def _run(
    cmd: list[str],
    step: str,
    timeout: float = _SUBPROCESS_TIMEOUT_SEC,
    deadline: Deadline | None = None,
) -> subprocess.CompletedProcess:
    """Run one step. If there is a deadline for the whole job, cap the wait at its remaining time.

    A call that does not pass deadline runs with only the step's own limit, as before.
    """
    if deadline is not None:
        # If the time remaining is shorter, take that. If it is used up, do not start, and raise
        # PipelineTimeout.
        timeout = deadline.clamp(timeout, step)
    logger.info("Running %s: %s", step, shlex.join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{step} timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise RuntimeError(f"{step} failed (exit {result.returncode}): {result.stderr}")
    return result
