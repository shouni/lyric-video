"""Tests for the matching and ASS generation logic of align_subtitles.py (Whisper calls are not included).

They are paired with the test of the same name in the sibling repository lyric-video-maker.
"""

import zipfile

import pysubs2
import pytest

from app import align_subtitles as al


class FakeWord:
    def __init__(self, word, start, end):
        self.word = word
        self.start = start
        self.end = end


class FakeSegment:
    def __init__(self, words):
        self.words = words


def chars(*specs):
    return [{"char": c, "start": s, "end": e} for c, s, e in specs]


class TestWordsToChars:
    def test_distributes_word_duration_evenly(self):
        result = al.words_to_chars([FakeSegment([FakeWord("あい", 1.0, 2.0)])])
        assert result == [
            {"char": "あ", "start": 1.0, "end": 1.5},
            {"char": "い", "start": 1.5, "end": 2.0},
        ]

    def test_skips_segments_without_words(self):
        assert al.words_to_chars([FakeSegment([]), FakeSegment([FakeWord("  ", 0, 1)])]) == []


class TestPlainText:
    def test_strips_tags_and_line_breaks(self):
        assert al.plain_text(r"{\k30}歌\N詞{\i1}だ") == "歌詞だ"

    def test_returns_empty_for_tag_only_event(self):
        assert al.plain_text(r"{\pos(0,0)}") == ""


class TestLoadSourceSubs:
    def test_reads_ass_from_zip(self, tmp_path):
        subs = pysubs2.SSAFile()
        subs.append(pysubs2.SSAEvent(start=0, end=1000, text="歌詞"))
        zip_path = tmp_path / "keyframes.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            z.writestr("subtitles.ass", subs.to_string("ass"))
        assert al.extract_lyric_lines(al.load_source_subs(str(zip_path))) == ["歌詞"]

    def test_errors_when_zip_has_no_subtitles(self, tmp_path):
        zip_path = tmp_path / "keyframes.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            z.writestr("cut_01.png", "x")
        with pytest.raises(SystemExit, match="subtitles.ass"):
            al.load_source_subs(str(zip_path))

    def test_errors_when_missing(self, tmp_path):
        with pytest.raises(SystemExit, match="見つかりません"):
            al.load_source_subs(str(tmp_path / "nope.ass"))

    def test_txt_lyrics_become_karaoke_styled_events(self, tmp_path):
        path = tmp_path / "lyrics.txt"
        path.write_text("一行目\n\n二行目\n", encoding="utf-8")
        subs = al.load_source_subs(str(path))
        assert al.extract_lyric_lines(subs) == ["一行目", "二行目"]
        assert subs.styles["Karaoke"].fontsize == 64


class TestMapCharsToLines:
    def test_assigns_timings_per_line_ignoring_punctuation(self):
        whisper = chars(("あ", 0.0, 0.5), ("い", 0.5, 1.0), ("う", 1.0, 1.5), ("え", 1.5, 2.0))
        result = al.map_chars_to_lines(["あい、", "うえ"], whisper)
        assert [c["char"] for c in result[0]] == ["あ", "い"]
        assert [c["char"] for c in result[1]] == ["う", "え"]

    def test_raises_on_character_count_mismatch(self):
        with pytest.raises(ValueError, match="文字数が一致しません"):
            al.map_chars_to_lines(["あいう"], chars(("あ", 0.0, 0.5), ("い", 0.5, 1.0)))


class TestBuildKaraokeText:
    def test_assigns_k_tags_from_durations(self):
        assert al.build_karaoke_text("あい", chars(("あ", 0.0, 0.30), ("い", 0.30, 0.75))) == r"{\k30}あ{\k45}い"

    def test_punctuation_is_absorbed_by_previous_char(self):
        assert al.build_karaoke_text("あ、い", chars(("あ", 0.0, 0.2), ("い", 0.2, 0.4))) == r"{\k20}あ、{\k20}い"

    def test_leading_punctuation_gets_zero_duration(self):
        assert al.build_karaoke_text("「あ", chars(("あ", 0.0, 0.2))) == r"{\k0}「{\k20}あ"

    def test_missing_timings_fall_back_to_default_duration(self):
        expected = rf"{{\k20}}あ{{\k{al.DEFAULT_K_CS}}}い"
        assert al.build_karaoke_text("あい", chars(("あ", 0.0, 0.2))) == expected

    def test_never_emits_zero_duration_for_sung_char(self):
        assert al.build_karaoke_text("あ", chars(("あ", 0.0, 0.001))) == r"{\k1}あ"


class TestFillRepeatGaps:
    def test_extends_line_until_just_before_next(self):
        subs = pysubs2.SSAFile()
        subs.append(pysubs2.SSAEvent(start=0, end=1000, text="一"))
        subs.append(pysubs2.SSAEvent(start=5000, end=6000, text="二"))
        al.fill_repeat_gaps(subs)
        assert subs[0].end == 5000 - al.FILL_GAP_MARGIN_MS

    def test_leaves_small_gaps_untouched(self):
        subs = pysubs2.SSAFile()
        subs.append(pysubs2.SSAEvent(start=0, end=1000, text="一"))
        subs.append(pysubs2.SSAEvent(start=1050, end=2000, text="二"))
        al.fill_repeat_gaps(subs)
        assert subs[0].end == 1000


class TestTrimOverlaps:
    def _subs(self, events):
        subs = pysubs2.SSAFile()
        for start, end, text in events:
            subs.append(pysubs2.SSAEvent(start=start, end=end, text=text))
        return subs

    def test_cuts_line_end_at_next_start(self):
        subs = self._subs([(0, 1300, "一"), (1000, 2000, "二")])
        al.trim_overlaps(subs)
        assert subs[0].end == 1000
        assert subs[1].end == 2000

    def test_leaves_non_overlapping_lines_untouched(self):
        subs = self._subs([(0, 1000, "一"), (1000, 2000, "二")])
        al.trim_overlaps(subs)
        assert subs[0].end == 1000

    def test_keeps_line_when_next_starts_before_it(self):
        # Trimming a line that the next one has overtaken would make its display length 0 or less, so leave it as is.
        subs = self._subs([(1000, 2000, "一"), (500, 3000, "二")])
        al.trim_overlaps(subs)
        assert subs[0].end == 2000

    def test_ignores_events_without_text(self):
        subs = self._subs([(0, 1300, "一"), (500, 800, ""), (1000, 2000, "二")])
        al.trim_overlaps(subs)
        assert subs[0].end == 1000


class TestBuildAlignedSubs:
    def _orig(self, events):
        subs = pysubs2.SSAFile()
        subs.info["PlayResY"] = "1080"
        subs.styles["Karaoke"] = pysubs2.SSAStyle(fontsize=64)
        for start, end, text in events:
            subs.append(pysubs2.SSAEvent(start=start, end=end, text=text))
        return subs

    def test_keeps_original_start_when_lead_in_is_slightly_early(self):
        result = al.build_aligned_subs(self._orig([(1000, 4000, "あ")]), {0: chars(("あ", 2.5, 3.0))}, verbose=False)
        assert result[0].start == 1000

    def test_uses_whisper_start_when_original_is_far_too_early(self):
        result = al.build_aligned_subs(self._orig([(0, 9000, "あ")]), {0: chars(("あ", 5.0, 5.5))}, verbose=False)
        assert result[0].start == 5000

    def test_end_time_extends_by_tail(self):
        orig = self._orig([(0, 1000, "あ"), (8000, 9000, "い")])
        line_map = {0: chars(("あ", 1.0, 1.5)), 1: chars(("い", 6.0, 6.5))}
        result = al.build_aligned_subs(orig, line_map, verbose=False)
        assert result[1].end == 6500 + al.TAIL_MS

    def test_preserves_info_styles_and_untimed_events(self):
        orig = self._orig([(0, 1000, ""), (1000, 2000, "あ")])
        result = al.build_aligned_subs(orig, {0: chars(("あ", 1.0, 1.5))}, verbose=False)
        assert len(result) == 2
        assert result.info["PlayResY"] == "1080"
        assert "Karaoke" in result.styles

    def test_warns_about_lines_without_timings(self, capsys):
        orig = self._orig([(0, 1000, "あ"), (2000, 3000, "、")])
        al.build_aligned_subs(orig, {0: chars(("あ", 0.0, 0.5))}, verbose=False)
        assert "Warning" in capsys.readouterr().err

    def test_trims_overlap_with_the_next_line(self):
        # Line 1 ends at 1.0s + TAIL_MS = 1300ms, but line 2 starts at 1100ms.
        orig = self._orig([(0, 1000, "あ"), (1000, 2000, "い")])
        line_map = {0: chars(("あ", 0.5, 1.0)), 1: chars(("い", 1.1, 1.6))}
        result = al.build_aligned_subs(orig, line_map, verbose=False)
        assert result[0].end == 1100
