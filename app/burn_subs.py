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

from PIL import Image, ImageDraw, ImageFont
import pysubs2


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
    """ZIP内のパスを検証し、展開先の外へ出るファイルを拒否して展開する。"""
    abs_target = os.path.abspath(target_dir) + os.sep
    for member in zip_file.infolist():
        dest = os.path.abspath(os.path.join(abs_target, member.filename))
        if not dest.startswith(abs_target):
            raise ValueError(f"Attempted path traversal in zip: {member.filename}")
    zip_file.extractall(target_dir)


def color_to_rgb(c):
    """pysubs2のASSカラーをPILで使えるRGBタプルへ変換する。"""
    return (c.r, c.g, c.b)


def parse_karaoke(raw_text):
    """ASSのカラオケ\\kタグをセンチ秒単位のテキスト区間へ分解する。"""
    segments = []
    cs = 0
    for m in re.finditer(r'\{\\k(\d+)\}([^{]*)', raw_text):
        k = int(m.group(1))
        text = m.group(2)
        segments.append((cs, cs + k, text))
        cs += k
    return segments


def read_images_with_durations(inputs_txt):
    """ffmpeg concat用の入力ファイルから画像名と表示秒数の組を読み取る。"""
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
    """画像ごとの表示秒数から絶対時刻のタイムラインを作成する。"""
    timeline = []
    t = 0.0
    for img, dur in images_with_durations:
        timeline.append((t, t + dur, os.path.join(work_dir, img)))
        t += dur
    return timeline, t


def img_at(timeline, t_sec):
    """指定秒数で表示されている画像パスを返す。"""
    for start, end, img in timeline:
        if start <= t_sec < end:
            return img
    return timeline[-1][2]


def event_at(events, t_sec):
    """指定秒数で有効な字幕イベントを返す。"""
    for start, end, text in events:
        if start <= t_sec < end:
            return (start, end, text)
    return None


def load_font(font_size):
    """日本語カラオケ文字を描画できるフォントを読み込む。"""
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
    """キャッシュしたRGB元画像のコピーを返す。"""
    if path not in img_cache:
        img_cache[path] = Image.open(path).convert("RGB")
    return img_cache[path].copy()


def render_frame(img_path, evt, elapsed_cs, img_cache, cfg):
    """1枚の画像フレームへ現在のカラオケ字幕行を描画する。"""
    img = get_base_img(img_path, img_cache)
    if evt is None:
        return img

    segs = parse_karaoke(evt[2])
    if not segs:
        return img

    img_w, img_h = cfg.img_size
    n_highlighted = sum(1 for (s, _e, _text) in segs if elapsed_cs >= s)

    draw = ImageDraw.Draw(img)
    full_text = "".join(t for _, _, t in segs)

    bbox = draw.textbbox((0, 0), full_text, font=cfg.font)
    total_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    x = (img_w - total_w) // 2
    y = img_h - text_h - cfg.margin_v

    cur_x = x
    for i, (_s, _e, seg_text) in enumerate(segs):
        color = cfg.primary if i < n_highlighted else cfg.secondary
        draw.text(
            (cur_x, y),
            seg_text,
            font=cfg.font,
            fill=color,
            stroke_width=cfg.outline,
            stroke_fill=cfg.outline_color,
        )
        seg_bbox = draw.textbbox((0, 0), seg_text, font=cfg.font)
        cur_x += seg_bbox[2] - seg_bbox[0]

    return img


def collect_transition_times(events, total_dur):
    """描画状態が変わり得る全ての時刻を収集する。"""
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
    """重複しない字幕状態を描画し、ffmpeg concat用の行を返す。"""
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
    """描画済みフレームと音声を最終MP4へエンコードする。"""
    print("\nRunning ffmpeg...")
    return subprocess.run(
        [
            "ffmpeg", "-y",
            "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", concat_txt,
            "-i", audio,
            "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-shortest",
            output,
        ],
    )


def main():
    """入力素材を読み込み、字幕付きフレームを描画して動画を書き出す。"""
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
