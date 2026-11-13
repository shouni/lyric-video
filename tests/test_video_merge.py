"""Tests for the input validation of video_merge and the wrapping logic for karaoke lines."""

import pytest
from PIL import Image, ImageDraw

from app import video_merge
from app.burn_subs import load_font


class TestSafeSec:
    def test_accepts_numeric_strings(self):
        assert video_merge._safe_sec("12.5", "start_sec") == 12.5

    def test_rejects_non_numeric(self):
        with pytest.raises(ValueError, match="invalid start_sec"):
            video_merge._safe_sec("abc", "start_sec")

    def test_rejects_none(self):
        with pytest.raises(ValueError, match="invalid start_sec"):
            video_merge._safe_sec(None, "start_sec")

    def test_rejects_negative(self):
        with pytest.raises(ValueError, match="out of plausible range"):
            video_merge._safe_sec(-1, "start_sec")

    def test_rejects_absurdly_large(self):
        with pytest.raises(ValueError, match="out of plausible range"):
            video_merge._safe_sec(99999, "start_sec")


class TestEscapeFfmpegFilterPath:
    def test_escapes_backslash_colon_and_quote(self):
        assert video_merge._escape_ffmpeg_filter_path(r"C:\tmp\a'b.ass") == r"C\:\\tmp\\a\'b.ass"

    def test_leaves_plain_paths_untouched(self):
        assert video_merge._escape_ffmpeg_filter_path("/tmp/subs.ass") == "/tmp/subs.ass"


class TestWrapKaraokeText:
    @pytest.fixture
    def draw(self):
        return ImageDraw.Draw(Image.new("RGB", (1, 1)))

    @pytest.fixture
    def font(self):
        font = load_font(40)
        if getattr(font, "size", 0) != 40:
            pytest.skip("スケーラブルなフォントが見つからない環境ではスキップ")
        return font

    def karaoke(self, text):
        return "".join(rf"{{\k10}}{ch}" for ch in text)

    def test_returns_text_unchanged_without_k_tags(self, draw, font):
        assert video_merge._wrap_karaoke_text("プレーン", draw, font, 1000) == "プレーン"

    def test_keeps_short_line_on_one_row(self, draw, font):
        text = self.karaoke("短い")
        assert video_merge._wrap_karaoke_text(text, draw, font, 10000) == text

    def test_wraps_long_japanese_line_without_spaces(self, draw, font):
        wrapped = video_merge._wrap_karaoke_text(self.karaoke("あ" * 30), draw, font, 200)
        assert r"\N" in wrapped
        # It wraps without dropping the \k tags.
        assert wrapped.count(r"\k10") == 30

    def test_never_exceeds_max_width_per_line(self, draw, font):
        max_width = 200
        wrapped = video_merge._wrap_karaoke_text(self.karaoke("あ" * 30), draw, font, max_width)
        for line in wrapped.split(r"\N"):
            plain = video_merge._KARAOKE_TOKEN.sub(lambda m: m.group(2), line)
            assert draw.textlength(plain, font=font) <= max_width

    def test_prefers_word_boundaries_for_spaced_text(self, draw, font):
        tokens = "".join(rf"{{\k10}}{w} " for w in ["alpha", "beta", "gamma", "delta"])
        wrapped = video_merge._wrap_karaoke_text(tokens, draw, font, 200)
        for line in wrapped.split(r"\N"):
            plain = video_merge._KARAOKE_TOKEN.sub(lambda m: m.group(2), line)
            # It does not split in the middle of a word.
            assert plain.strip().split(" ")[0] in ("alpha", "beta", "gamma", "delta")

    def test_preserves_all_characters(self, draw, font):
        original = "あ" * 25
        wrapped = video_merge._wrap_karaoke_text(self.karaoke(original), draw, font, 150)
        plain = video_merge._KARAOKE_TOKEN.sub(lambda m: m.group(2), wrapped).replace(r"\N", "")
        assert plain == original
