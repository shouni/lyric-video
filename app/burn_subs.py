#!/usr/bin/env python3
"""Burn ASS karaoke subtitles onto PNG images and create MP4.

Usage:
    python3 burn_subs.py <audio.mp3> <keyframes.zip> [output.mp4] [--subs <subtitles.ass>]
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Any

import pysubs2
from PIL import Image, ImageDraw, ImageFont


@dataclass
class RenderConfig:
    font: Any
    img_size: tuple
    margin_v: int
    outline: int
    primary: tuple
    secondary: tuple
    outline_color: tuple


FONT_CANDIDATES = [
    # macOS
    "/System/Library/Fonts/ヒラギノ角ゴシック W7.ttc",
    os.path.expanduser("~/Library/Fonts/SourceHanSans-VF.otf.ttc"),
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux (Docker: fonts-noto-cjk)
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
    # Linux (custom install)
    "/usr/local/share/fonts/SourceHanSans-VF.otf.ttc",
]


def safe_extract(zip_file, target_dir):
    """Validate the paths in the ZIP, refuse any file that would land outside the target, then extract."""
    abs_target = os.path.abspath(target_dir) + os.sep
    for member in zip_file.infolist():
        dest = os.path.abspath(os.path.join(abs_target, member.filename))
        if not dest.startswith(abs_target):
            raise ValueError(f"Attempted path traversal in zip: {member.filename}")
    zip_file.extractall(target_dir)


def color_to_rgb(c):
    """Convert a pysubs2 ASS color into an RGB tuple that PIL can use."""
    return (c.r, c.g, c.b)


def parse_karaoke(raw_text):
    """Split the ASS karaoke \\k tags into text segments in centiseconds."""
    segments = []
    cs = 0
    for m in re.finditer(r"\{\\k(\d+)\}([^{]*)", raw_text):
        k = int(m.group(1))
        text = re.sub(r"\\[nNh]", "", m.group(2))
        segments.append((cs, cs + k, text))
        cs += k
    return segments


def read_images_with_durations(inputs_txt):
    """Read the pairs of image name and display seconds from an input file for ffmpeg concat."""
    images_with_durations = []
    current_file = None
    with open(inputs_txt, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("file "):
                current_file = line.split("'")[1]
            elif line.startswith("duration "):
                dur = float(line.split()[1])
                images_with_durations.append((current_file, dur))
    return images_with_durations


def build_timeline(work_dir, images_with_durations):
    """Build a timeline in absolute time from the display seconds of each image."""
    timeline = []
    t = 0.0
    for img, dur in images_with_durations:
        timeline.append((t, t + dur, os.path.join(work_dir, img)))
        t += dur
    return timeline, t


def img_at(timeline, t_sec):
    """Return the path of the image shown at the given second."""
    for start, end, img in timeline:
        if start <= t_sec < end:
            return img
    return timeline[-1][2]


def event_at(events, t_sec):
    """Return the subtitle event active at the given second."""
    for start, end, text in events:
        if start <= t_sec < end:
            return (start, end, text)
    return None


def load_font(font_size):
    """Load a font that can draw Japanese karaoke text."""
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, font_size)
                print(f"Font: {path}")
                return font
            except Exception:
                continue

    print("Warning: no CJK font found, using default")
    return ImageFont.load_default()


def get_base_img(path, img_cache):
    """Return a copy of the cached RGB source image."""
    if path not in img_cache:
        img_cache[path] = Image.open(path).convert("RGB")
    return img_cache[path].copy()


LINE_MAX_WIDTH_RATIO = 0.92  # Upper limit on the share of the image width one line may take
# Lower limit on how far a line may shrink while staying one line. If it would shrink below this, wrap to 2 lines
LINE_SHRINK_FLOOR = 0.7
MIN_FONT_SIZE = 12  # Lower limit for automatic shrinking (any smaller is illegible)


def measure_line(draw, text, font):
    """Return the drawn width of one line."""
    return draw.textlength(text, font=font)


def shrink_to_fit(draw, lines, font, max_w):
    """Shrink the font until the given lines fit within max_w.

    If they do not fit even at MIN_FONT_SIZE, give up at that size (overflow is accepted in
    preference to showing unreadable subtitles). The returned font and size always match.
    """
    size = getattr(font, "size", 0) or 0
    if size <= MIN_FONT_SIZE:
        return font, size
    while True:
        f = font.font_variant(size=size)
        if size <= MIN_FONT_SIZE or all(measure_line(draw, ln, f) <= max_w for ln in lines):
            return f, size
        size -= 2


def best_split_segs(draw, segs, font):
    """Return the split point that divides a sequence of karaoke \\k segments into 2 lines.

    A segment is never split inside (in existing ASS files, punctuation and word-final spaces are
    absorbed into the preceding segment, so segment boundaries correspond directly to word or
    character boundaries).
    """
    texts = [t for _, _, t in segs]
    candidates = [i + 1 for i, t in enumerate(texts[:-1]) if t.endswith(" ")]
    if not candidates:
        candidates = list(range(1, len(segs)))
    best = (segs, [])
    best_w = None
    for idx in candidates:
        segs1, segs2 = segs[:idx], segs[idx:]
        text1 = "".join(texts[:idx]).strip()
        text2 = "".join(texts[idx:]).strip()
        if not text1 or not text2:
            continue
        w = max(measure_line(draw, text1, font), measure_line(draw, text2, font))
        if best_w is None or w < best_w:
            best_w = w
            best = (segs1, segs2)
    return best


def fit_karaoke_layout(draw, segs, cfg):
    """Decide the font and the line split (1 or 2 lines) with which the karaoke \\k segments fit the image width.

    Shrinking on one line is tried first, and if the text is so long that the shrink ratio falls
    below LINE_SHRINK_FLOOR, it is wrapped to 2 lines and then shrunk to a size that fits.
    """
    max_w = cfg.img_size[0] * LINE_MAX_WIDTH_RATIO
    font = cfg.font
    base_size = getattr(font, "size", 0) or 0
    full_text = "".join(t for _, _, t in segs)
    if base_size <= 0 or measure_line(draw, full_text, font) <= max_w:
        return font, [segs]

    _, size_one_line = shrink_to_fit(draw, [full_text], font, max_w)
    if size_one_line >= base_size * LINE_SHRINK_FLOOR:
        return font.font_variant(size=size_one_line), [segs]

    segs1, segs2 = best_split_segs(draw, segs, font)
    if not segs2:
        return font.font_variant(size=size_one_line), [segs]
    line_texts = ["".join(t for _, _, t in segs1), "".join(t for _, _, t in segs2)]
    fitted, _ = shrink_to_fit(draw, line_texts, font, max_w)
    return fitted, [segs1, segs2]


def render_frame(img_path, evt, elapsed_cs, img_cache, cfg):
    """Draw the current karaoke subtitle line onto one image frame."""
    img = get_base_img(img_path, img_cache)
    if evt is None:
        return img

    segs = parse_karaoke(evt[2])
    if not segs:
        return img

    img_w, img_h = cfg.img_size
    n_highlighted = sum(1 for (s, _e, _text) in segs if elapsed_cs >= s)

    draw = ImageDraw.Draw(img)
    font, seg_lines = fit_karaoke_layout(draw, segs, cfg)

    line_texts = ["".join(t for _, _, t in line) for line in seg_lines]
    line_bboxes = [draw.textbbox((0, 0), txt, font=font) for txt in line_texts]

    line_gap = int(getattr(font, "size", 20) * 0.3)
    block_h = sum(b[3] - b[1] for b in line_bboxes) + line_gap * (len(seg_lines) - 1)
    y = img_h - block_h - cfg.margin_v

    idx = 0
    for line, _txt, bbox in zip(seg_lines, line_texts, line_bboxes, strict=True):
        total_w = bbox[2] - bbox[0]
        cur_x = (img_w - total_w) // 2
        for _s, _e, seg_text in line:
            color = cfg.primary if idx < n_highlighted else cfg.secondary
            draw.text(
                (cur_x, y),
                seg_text,
                font=font,
                fill=color,
                stroke_width=cfg.outline,
                stroke_fill=cfg.outline_color,
            )
            seg_bbox = draw.textbbox((0, 0), seg_text, font=font)
            cur_x += seg_bbox[2] - seg_bbox[0]
            idx += 1
        y += (bbox[3] - bbox[1]) + line_gap

    return img


def collect_transition_times(events, total_dur):
    """Collect every time at which the drawn state can change."""
    transitions = {0.0, total_dur}
    for start, end, text in events:
        transitions.add(start)
        transitions.add(end)
        for seg_cs_s, seg_cs_e, _ in parse_karaoke(text):
            transitions.add(start + seg_cs_s / 100.0)
            transitions.add(start + seg_cs_e / 100.0)
    return sorted(transitions)


def build_concat_lines(
    transitions,
    timeline,
    events,
    cfg,
    frames_dir,
    img_cache=None,
):
    """Draw the distinct subtitle states and return the lines for ffmpeg concat."""
    if img_cache is None:
        img_cache = {}
    concat_lines = []
    rendered_cache = {}
    last_frame_path = None

    for i in range(len(transitions) - 1):
        seg_start = transitions[i]
        seg_end = transitions[i + 1]
        dur = seg_end - seg_start
        if dur < 1e-6:
            continue

        mid = (seg_start + seg_end) / 2
        img_file = img_at(timeline, mid)
        evt = event_at(events, mid)

        elapsed_cs = (mid - evt[0]) * 100 if evt else 0
        n_hl = 0
        if evt:
            segs = parse_karaoke(evt[2])
            n_hl = sum(1 for (s, _e, _text) in segs if elapsed_cs >= s)

        state_key = (img_file, evt[0] if evt else None, n_hl)

        if state_key not in rendered_cache:
            base = os.path.splitext(os.path.basename(img_file))[0]
            evt_start = str(evt[0] if evt else "x").replace(".", "_")
            frame_path = os.path.join(frames_dir, f"frame_{base}_{evt_start}_{n_hl}.png")
            img = render_frame(img_file, evt, elapsed_cs, img_cache, cfg)
            img.save(frame_path)
            rendered_cache[state_key] = frame_path
            print(f"  Rendered: {os.path.basename(frame_path)}")

        frame_path = rendered_cache[state_key]
        last_frame_path = frame_path
        concat_lines.append(f"file '{os.path.abspath(frame_path)}'")
        concat_lines.append(f"duration {dur:.6f}")

    if last_frame_path:
        concat_lines.append(f"file '{os.path.abspath(last_frame_path)}'")

    return concat_lines


def run_ffmpeg(concat_txt, audio, output):
    """Encode the rendered frames and the audio into the final MP4."""
    print("\nRunning ffmpeg...")
    return subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_txt,
            "-i",
            audio,
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-pix_fmt",
            "yuv420p",
            "-shortest",
            output,
        ],
    )


def main():
    """Load the input material, draw the frames with subtitles and write out the video."""
    parser = argparse.ArgumentParser(description="Burn ASS karaoke subtitles onto PNG images and create MP4.")
    parser.add_argument("audio", help="Input audio file (mp3)")
    parser.add_argument("keyframes", help="Input keyframes zip file")
    parser.add_argument("output", nargs="?", default="output.mp4", help="Output MP4 file")
    parser.add_argument("--subs", dest="subs_override", help="Override subtitles file (ass)")
    args = parser.parse_args()

    if args.subs_override and not os.path.exists(args.subs_override):
        print(f"Error: 指定された字幕ファイルが見つかりません: {args.subs_override}", file=sys.stderr)
        sys.exit(1)

    with tempfile.TemporaryDirectory(prefix="burn_subs_") as work_dir:
        print(f"Extracting {args.keyframes} -> {work_dir}")
        with zipfile.ZipFile(args.keyframes) as z:
            safe_extract(z, work_dir)

        inputs_txt = os.path.join(work_dir, "inputs.txt")
        images_with_durations = read_images_with_durations(inputs_txt)
        print(f"Images: {[f for f, _ in images_with_durations]}")

        first_img_path = os.path.join(work_dir, images_with_durations[0][0])
        with Image.open(first_img_path) as probe:
            img_w, img_h = probe.size
        print(f"Image size: {img_w}x{img_h}")

        subtitle_file = args.subs_override or os.path.join(work_dir, "subtitles.ass")
        subs_raw = pysubs2.load(subtitle_file)

        play_res_y = int(subs_raw.info.get("PlayResY", "1080"))
        scale = img_h / play_res_y

        style = subs_raw.styles.get("Karaoke") or list(subs_raw.styles.values())[0]
        font_size = int(style.fontsize * scale)
        margin_v = int(style.marginv * scale)
        outline = max(1, int(style.outline * scale))
        primary = color_to_rgb(style.primarycolor)
        secondary = color_to_rgb(style.secondarycolor)
        outline_color = color_to_rgb(style.outlinecolor)

        print(f"Font size: {font_size}, MarginV: {margin_v}, Outline: {outline}")
        print(f"Primary: {primary}, Secondary: {secondary}")

        font = load_font(font_size)
        cfg = RenderConfig(
            font=font,
            img_size=(img_w, img_h),
            margin_v=margin_v,
            outline=outline,
            primary=primary,
            secondary=secondary,
            outline_color=outline_color,
        )
        timeline, total_dur = build_timeline(work_dir, images_with_durations)
        events = [(e.start / 1000.0, e.end / 1000.0, e.text) for e in subs_raw]
        transitions = collect_transition_times(events, total_dur)
        print(f"Total transition segments: {len(transitions) - 1}")

        frames_dir = os.path.join(work_dir, "frames")
        os.makedirs(frames_dir, exist_ok=True)
        concat_lines = build_concat_lines(transitions, timeline, events, cfg, frames_dir)

        concat_txt = os.path.join(frames_dir, "concat.txt")
        with open(concat_txt, "w", encoding="utf-8") as f:
            f.write("\n".join(concat_lines))

        result = run_ffmpeg(concat_txt, args.audio, args.output)

    if result.returncode == 0:
        size_mb = os.path.getsize(args.output) / 1024 / 1024
        print(f"Done! {args.output} ({size_mb:.1f} MB)")
    else:
        print(f"ffmpeg failed (exit code {result.returncode})")
        sys.exit(1)


if __name__ == "__main__":
    main()
