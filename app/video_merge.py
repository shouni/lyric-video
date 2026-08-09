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

# ★ 以下は**1 ステップあたり**の上限。これを足し合わせてもジョブ全体の上限にはならない
#   （trim + align + burn だけで 300 + 900 + 300 = 1500s、ダウンロードを足すと
#   dispatch_deadline の 1800s に迫る）。ジョブ全体の上限は Deadline が別に持ち、
#   下の _run が両者の短いほうを採る。理由は app/pipeline/deadline.py。
_SUBPROCESS_TIMEOUT_SEC = 300.0
# Whisperのモデルロード込みで動く可能性があるため、通常のffmpeg/ffprobe呼び出しより
# 長いタイムアウトを与える。
_ALIGN_TIMEOUT_SEC = 900.0
DEFAULT_WHISPER_MODEL = "small"
# 曲1本(セクションではなく全体)としてあり得る上限。異常なメタデータ(負値・極端な
# 大きさ)をここで弾き、ffmpegに不正な-ss/-tをそのまま渡さないようにする。
_MAX_PLAUSIBLE_SEC = 3600.0
_ALIGN_SCRIPT = Path(__file__).resolve().parent / "align_subtitles.py"


def _safe_sec(value, field: str) -> float:
    """メタデータの秒数値を検証しつつfloatへ変換する。"""
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
    """ap-mvの完成動画とap-musicの楽曲を合成し、字幕焼き込み済みMP4を作る。

    ap-mvのVeo video-to-video継続生成は video_extension の累積尺上限(30秒)を避けるため
    継続チェーンを内部でリセットすることがあるが、複数チェーンの検出・結合（ハードカット）は
    ap-mv側（internal/worker/filter/chain_finalize.go）で完結しており、この関数は
    `final_video_url`（1本化済みの完成動画）をそのままダウンロードするだけでよい。
    """
    meta = gcs.load_json(f"gs://{ap_mv_bucket}/veo/jobs/{ap_mv_job_id}/video_music_meta.json")
    cuts = meta.get("cuts") or []
    if not cuts:
        raise ValueError(f"ap-mv job {ap_mv_job_id!r} has no cuts")

    cuts_sorted = sorted(cuts, key=lambda c: c.get("cut_index", 0))
    # ap-mv側のstart_secはjson:"start_sec,omitempty"のため、先頭カットのように値が
    # 正当に0の場合キー自体が省略される。省略されたキーの値は常に0であると保証されて
    # いる(omitemptyがfloat64を省略するのは値が0.0の場合のみ)ため、欠落時は0.0として扱う。
    section_start_sec = _safe_sec(cuts_sorted[0].get("start_sec", 0.0), "cuts[0].start_sec")

    # final_video_urlが無いのは、ap-mv側のチェーン結合対応（final_video_url追加）より前に
    # 生成された既存ジョブのみ。後方互換として、その場合は従来通り最後のcutの動画を使う
    # （旧仕様では複数チェーンがあると欠落するが、これは新規ジョブでは発生しない）。
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
    """ffmpegのfiltergraphパーサが特殊扱いする文字(\\ : ')をエスケープする。

    このパスは常にtempfile生成のディレクトリ+固定ファイル名で、ユーザー入力
    (ap_mv_job_id/music_job_id)が混ざることはないが、Windows等コロンを含み得る
    パス上で -vf ass=... が壊れないよう防御的にエスケープしておく。
    """
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _write_reference_subtitles(
    cuts_sorted: list[dict],
    video_width: int,
    video_height: int,
    out_path: Path,
) -> int:
    """カット単位のdialogueを歌詞順に並べた、タイミング未確定のリファレンスASSを作る。

    ap-mvのcuts[].start_sec/end_secは、Veoにどの絵をどのカットへ割り当てるかという
    "演出上の割付け"であって実際の歌唱タイミングではない(例: 最初のカットを歌詞なしの
    導入ショットにする、等)。そのため録音済み音声をWhisperで強制アライメントして初めて
    正しい表示タイミングが得られる。ここではテキストと表示順序、スタイルだけを確定させ、
    実際の開始・終了時刻は_align_with_whisperが上書きする(align_subtitles.py側の仕様で、
    ここで設定するDialogueのStart/Endはそもそも使われない)。

    PlayResX/PlayResYは実際の動画解像度に一致させる。ap-mvの動画は720x1280の縦長
    (shorts想定)で、固定値(例:1920x1080)のままだとlibassが非一様スケーリングを
    かけてしまい、字幕が意図した位置からずれる。フォントサイズ・マージンも高さに
    比例させ、どの解像度でも同じ見た目比率にする。

    alignmentはTOP_CENTERを使う。bottom-center(\\an2)はテキストブロック全体の
    行数に応じて基準位置が変わるため、1行の歌詞と自動折り返しで2行になる歌詞とで
    表示の高さが揃わない。top-center(\\an8)ならブロックの上端が常に固定され、
    行数に関わらず一切の特別処理なしで位置が揃う。副次的に、shorts系プラットフォーム
    (YouTube Shorts/TikTok/Reels)が画面下部に被せがちな独自UIとの衝突も避けられる。
    """
    subs = pysubs2.SSAFile()
    subs.info["PlayResX"] = str(video_width)
    subs.info["PlayResY"] = str(video_height)
    # libassの自動折り返し(WrapStyle 0)はスペース位置でしか改行できず、日本語の
    # スペースなしの長い行は画面外へはみ出す。折り返しは_wrap_aligned_subtitlesで
    # 実測フォント幅に基づき自前で行うため、libass側の自動折り返しは無効化する。
    subs.info["WrapStyle"] = "2"
    fontsize = max(32, round(video_height * 0.055))
    marginv = max(40, round(video_height * 0.10))
    style = pysubs2.SSAStyle(
        fontname="Arial",
        fontsize=fontsize,
        # primarycolor: \kハイライト後(既に歌われた部分)の色。secondarycolor: ハイライト前
        # (まだ歌われていない部分)の色。pysubs2のデフォルトsecondarycolorは赤なので、
        # 明示的に白を指定しないと未ハイライト部分が赤字になってしまう。
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
    """align_subtitles.py(Whisper強制アライメント)でリファレンスASSの実際の発話時刻を求める。"""
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
    """Whisperアライメント後のカラオケテキストを、実測フォント幅に基づいて折り返す。

    libassの自動折り返し(WrapStyle 0)はスペースでしか改行できず、日本語のスペース
    なしの長い行は画面外へはみ出す(_write_reference_subtitlesでWrapStyle:2に設定し、
    libass側の自動折り返しは無効化済み)。ここでは{\\kNN}トークン単位で実際のグリフ幅を
    burn_subs.py同じフォントで測定し、必要な位置に\\Nを挿入する。スペースの有無に
    関わらず動作するため、_write_reference_subtitlesの想定と異なる歌詞が来ても崩れない。
    """
    font = load_font(fontsize)
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    max_width = video_width - 40

    subs = pysubs2.load(str(subs_path))
    for event in subs:
        event.text = _wrap_karaoke_text(event.text, draw, font, max_width)
    subs.save(str(subs_path))


def _wrap_karaoke_text(text: str, draw: ImageDraw.ImageDraw, font, max_width: float) -> str:
    """{\\kNN}トークン列を、指定フォントでの実測幅がmax_widthを超えない範囲で行分割する。

    まずスペースを含むトークンの直後を「単語」の区切りとして扱い、単語単位で
    詰め込む(通常の折り返しと同様、単語の途中では割らない)。1単語だけで
    max_widthを超える場合(スペースを含まない長い行)のみ、文字単位で強制的に
    分割する。
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
        """1単語だけでmax_widthを超える場合に、文字単位で強制的に折り返す。"""
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
    """1 ステップを実行する。ジョブ全体の締切があれば、待ち時間をその残りで頭打ちにする。

    deadline を渡さない呼び出しは従来どおりステップ単体の上限だけで動く。
    """
    if deadline is not None:
        # 残り時間のほうが短ければそちらを採る。使い切っていれば起動せず PipelineTimeout。
        timeout = deadline.clamp(timeout, step)
    logger.info("Running %s: %s", step, shlex.join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{step} timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise RuntimeError(f"{step} failed (exit {result.returncode}): {result.stderr}")
    return result
