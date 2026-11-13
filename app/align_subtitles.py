#!/usr/bin/env python3
"""Get karaoke timing from the audio and regenerate the ASS subtitles.

Usage:
    python3 align_subtitles.py <audio.mp3> <keyframes.zip|subtitles.ass|lyrics.txt> [output.ass]

The lyrics input can be the subtitles.ass inside keyframes.zip, a standalone ASS file,
or plain text (1 line = 1 subtitle line, blank lines are ignored).

Keep the same logic as align_subtitles.py in the sister repository lyric-video-maker.
"""

import argparse
import os
import re
import sys
import zipfile

import pysubs2
import stable_whisper

PUNCT_PATTERN = re.compile(r"[　 、。！？!?,.\s\-\[\]\(\)「」『』〜♪…※☆★●○◎]")
TAG_PATTERN = re.compile(r"\{[^}]*\}")
LINE_BREAK_PATTERN = re.compile(r"\\[nNh]")
TAIL_MS = 300  # Extra display time after the last character
FILL_GAP_MARGIN_MS = 100  # Margin left before the next line when singing repeats
# For the first line only: if the start in the original ASS is earlier than Whisper's result by no
# more than this range, the original ASS is used (to handle the start of the singing)
LEAD_IN_TOLERANCE_MS = 3000
DEFAULT_K_CS = 10  # Default display length assigned to characters whose timings have run out


def words_to_chars(segments):
    """Distribute Whisper word timestamps evenly into per-character timings."""
    chars = []
    for seg in segments:
        if not (hasattr(seg, "words") and seg.words):
            continue
        for w in seg.words:
            word = w.word.strip()
            if not word:
                continue
            n = len(word)
            per = (w.end - w.start) / n
            for i, ch in enumerate(word):
                chars.append(
                    {
                        "char": ch,
                        "start": w.start + i * per,
                        "end": w.start + (i + 1) * per,
                    }
                )
    return chars


def ms(sec):
    """Convert a time in seconds to an integer in milliseconds for ASS events."""
    return int(sec * 1000)


def subs_from_txt(path):
    """Synthesize an ASS file from plain-text lyrics (1 line = 1 subtitle line).

    The style is created as the same Karaoke style as the existing subtitles.ass
    (Arial 64px, yellow highlight, PlayRes 1920x1080), in a form that burn_subs.py can read as is.
    The event times are placeholders (they are overwritten by the alignment).
    """
    with open(path, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    if not lines:
        raise SystemExit(f"Error: 歌詞テキストが空です: {path}")

    subs = pysubs2.SSAFile()
    subs.info["PlayResX"] = "1920"
    subs.info["PlayResY"] = "1080"
    subs.styles["Karaoke"] = pysubs2.SSAStyle(
        fontname="Arial",
        fontsize=64,
        primarycolor=pysubs2.Color(255, 255, 0),  # &H0000FFFF yellow
        secondarycolor=pysubs2.Color(255, 255, 255),  # &H00FFFFFF white
        outlinecolor=pysubs2.Color(0, 0, 0),
        backcolor=pysubs2.Color(0, 0, 0, 128),
        bold=True,
        outline=3,
        shadow=1,
        alignment=pysubs2.Alignment.BOTTOM_CENTER,
        marginl=10,
        marginr=10,
        marginv=80,
    )
    for i, line in enumerate(lines):
        subs.append(pysubs2.SSAEvent(start=i * 5000, end=(i + 1) * 5000, style="Karaoke", text=line))
    return subs


def plain_text(text):
    """Remove the styling tags and line-break tags from the text of an ASS event."""
    return LINE_BREAK_PATTERN.sub("", TAG_PATTERN.sub("", text)).strip()


def load_source_subs(path):
    """Load the lyrics input (keyframes.zip / ASS / plain text) and return an SSAFile."""
    if not os.path.exists(path):
        raise SystemExit(f"Error: 歌詞入力が見つかりません: {path}")
    if path.endswith(".txt"):
        return subs_from_txt(path)
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if "subtitles.ass" not in names:
                raise SystemExit(f"Error: keyframes.zip に subtitles.ass が含まれていません。 ZIP内のファイル: {names}")
            with zf.open("subtitles.ass") as f:
                return pysubs2.SSAFile.from_string(f.read().decode("utf-8"))
    return pysubs2.load(path)


def extract_lyric_lines(subs):
    """Extract, in order, only the lyric lines of events that have text."""
    return [plain for plain in (plain_text(e.text) for e in subs) if plain]


def map_chars_to_lines(lyric_lines, whisper_chars):
    """Assign the character timings detected by Whisper to each line of the lyrics.

    If the character counts excluding punctuation and symbols (PUNCT_PATTERN) do not match, the
    timing of whole lines would be off, so this raises ValueError and aborts (detecting a mismatch
    between the lyrics and the singing).
    """
    flat_orig = [
        {"line": line_idx, "char": ch}
        for line_idx, line in enumerate(lyric_lines)
        for ch in line
        if not PUNCT_PATTERN.match(ch)
    ]
    flat_whisper = [c for c in whisper_chars if PUNCT_PATTERN.match(c["char"]) is None]

    if len(flat_orig) != len(flat_whisper):
        raise ValueError(
            f"文字数が一致しません (orig={len(flat_orig)}, whisper={len(flat_whisper)})。"
            "タイミングが全体的にずれるためアライメントを中断します。"
        )

    line_char_map = {}
    for orig, timing in zip(flat_orig, flat_whisper, strict=True):
        line_char_map.setdefault(orig["line"], []).append(timing)
    return line_char_map


def build_karaoke_text(plain, char_timings):
    """Build ASS text with \\k tags from one line of lyrics and its character timings.

    Punctuation is absorbed into the \\k of the preceding character (punctuation itself has no
    singing time).
    """
    k_parts = []
    ti = 0
    for ch in plain:
        if PUNCT_PATTERN.match(ch):
            if k_parts:
                k_parts[-1]["text"] += ch
            else:
                k_parts.append({"k_cs": 0, "text": ch})
        elif ti < len(char_timings):
            t = char_timings[ti]
            k_parts.append({"k_cs": max(1, round((t["end"] - t["start"]) * 100)), "text": ch})
            ti += 1
        else:
            k_parts.append({"k_cs": DEFAULT_K_CS, "text": ch})
    return "".join(f"{{\\k{p['k_cs']}}}{p['text']}" for p in k_parts)


def fill_repeat_gaps(subs):
    """Close the gap between lines up to just before the next line starts.

    The lyrics stay on screen during repeated singing as well.
    """
    text_events = [e for e in subs if plain_text(e.text)]
    for curr, next_ev in zip(text_events, text_events[1:], strict=False):
        if next_ev.start - curr.end > FILL_GAP_MARGIN_MS:
            curr.end = next_ev.start - FILL_GAP_MARGIN_MS


def trim_overlaps(subs):
    """Cut a line end that overlaps the next line at the start of the next line.

    TAIL_MS of lingering time is added to each line end, so it overlaps when the next line starts
    right away. burn_subs.py draws only one line, with "the earlier line first", so it is not
    affected, but when the file is passed as is to ffmpeg's ass filter (libass), as video_merge.py
    does, two lines are stacked and shown during the overlap, so it is resolved on the ASS side.
    """
    text_events = [e for e in subs if plain_text(e.text)]
    for curr, next_ev in zip(text_events, text_events[1:], strict=False):
        # When the line has overtaken even the start, cutting would make the display length 0 or
        # less, so it is left alone
        if curr.start < next_ev.start < curr.end:
            curr.end = next_ev.start


def build_aligned_subs(subs_orig, line_char_map, verbose=True):
    """Generate a new ASS with \\k tags from the original ASS and the per-line character timings."""
    new_subs = pysubs2.SSAFile()
    new_subs.info = subs_orig.info.copy()
    new_subs.styles = subs_orig.styles.copy()

    valid_line_idx = 0
    for event in subs_orig:
        plain = plain_text(event.text)
        if not plain:
            # Events that have no text (blank lines or tags only) are kept as they are
            new_subs.append(event.copy())
            continue

        char_timings = line_char_map.get(valid_line_idx)
        valid_line_idx += 1
        if not char_timings:
            # A line of punctuation only has no characters to match, so no timing is assigned.
            # The time in the original ASS (a placeholder time for txt input) stays as is, and the
            # line ends up at an unrelated time.
            print(
                f"Warning: 行{valid_line_idx} にタイミングを割り当てられません"
                f"（句読点のみ？）: {plain} → 元ASSの時刻のまま出力します",
                file=sys.stderr,
            )
            new_subs.append(event.copy())
            continue

        line_start_s = char_timings[0]["start"]
        line_end_s = char_timings[-1]["end"] + TAIL_MS / 1000

        new_event = event.copy()
        # For the first line only, if the original ASS is slightly earlier than Whisper, use it
        # (to handle the start of the singing)
        if valid_line_idx == 1 and 0 < ms(line_start_s) - event.start <= LEAD_IN_TOLERANCE_MS:
            new_event.start = event.start
        else:
            new_event.start = ms(line_start_s)
        new_event.end = ms(line_end_s)
        new_event.text = build_karaoke_text(plain, char_timings)
        new_subs.append(new_event)

        if verbose:
            print(f"  行{valid_line_idx}: {line_start_s:.2f}s - {line_end_s:.2f}s | {plain}")

    fill_repeat_gaps(new_subs)
    trim_overlaps(new_subs)
    return new_subs


def main():
    """Fit the text of the existing subtitles to the audio and save it as ASS with karaoke timing."""
    parser = argparse.ArgumentParser(description="音声からカラオケタイミングを取得し、ASS字幕を再生成する。")
    parser.add_argument("audio", help="Input audio file (mp3)")
    parser.add_argument("subtitles_in", help="Input keyframes ZIP, subtitles ASS, or plain lyrics TXT file")
    parser.add_argument("subtitles_out", nargs="?", default="subtitles_aligned.ass", help="Output subtitles file (ass)")
    parser.add_argument("--model", default="large-v3", help="Whisper model size (e.g., base, small, medium, large-v3)")
    parser.add_argument("--language", default="ja", help="Lyrics language code passed to Whisper (e.g., ja, en)")
    args = parser.parse_args()

    audio = args.audio
    ass_out = args.subtitles_out

    if not os.path.exists(audio):
        raise SystemExit(f"Error: 音声ファイルが見つかりません: {audio}")

    subs_orig = load_source_subs(args.subtitles_in)
    lyric_lines = extract_lyric_lines(subs_orig)
    if not lyric_lines:
        raise SystemExit(f"Error: 歌詞行が1行もありません: {args.subtitles_in}")

    print("Whisperモデル読み込み中...")
    model = stable_whisper.load_model(args.model)

    print("アライメント実行中...")
    result = model.align(audio, "\n".join(lyric_lines), language=args.language)

    # result.adjust_by_silence(audio, vad=True) corrects the timing using silence, but on a music
    # track with accompaniment no silence is detected and it is counterproductive, so it is not used.

    all_chars = words_to_chars(result.segments)
    print(f"取得文字数: {len(all_chars)}")

    try:
        line_char_map = map_chars_to_lines(lyric_lines, all_chars)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"照合文字数: {sum(len(v) for v in line_char_map.values())}")

    new_subs = build_aligned_subs(subs_orig, line_char_map)
    new_subs.save(ass_out)
    print(f"\n完了: {ass_out}")


if __name__ == "__main__":
    main()
