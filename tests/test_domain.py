"""domain 配下（Task / JobRecord / YouTubeTask / music_layout）のテスト。"""

import json

import pytest

from app.domain.job import JobRecord
from app.domain.music_layout import music_audio_uri, music_job_prefix, music_recipe_uri
from app.domain.task import Task, new_job_id
from app.domain.youtube_task import FIXED_HASHTAGS, FIXED_TAGS, MAX_YOUTUBE_TITLE_LENGTH, TITLE_SUFFIX, YouTubeTask


def valid_task(**overrides) -> Task:
    defaults = {
        "job_id": "lyric-20260101-000000-abcdef",
        "ap_mv_job_id": "mv-123",
        "music_job_id": "music-123",
        "output_prefix": "gs://my-bucket/lyric-video/output",
    }
    return Task(**{**defaults, **overrides})


class TestNewJobId:
    def test_has_prefix_timestamp_and_random_suffix(self):
        job_id = new_job_id("lyric")
        assert job_id.startswith("lyric-")
        assert len(job_id.split("-")) == 4  # lyric / YYYYMMDD / HHMMSS / rand

    def test_sanitizes_prefix(self):
        assert new_job_id("Lyric Video!").startswith("lyricvideo-")

    def test_falls_back_to_job_for_empty_prefix(self):
        assert new_job_id("!!!").startswith("job-")

    def test_is_accepted_by_validate(self):
        valid_task(job_id=new_job_id()).validate()


class TestTaskValidate:
    def test_accepts_valid_task(self):
        valid_task().validate(allowed_bucket="my-bucket")

    def test_rejects_bad_job_id(self):
        with pytest.raises(ValueError, match="invalid job_id"):
            valid_task(job_id="../etc/passwd").validate()

    def test_rejects_unknown_whisper_model(self):
        with pytest.raises(ValueError, match="invalid whisper_model"):
            valid_task(whisper_model="tiny").validate()

    def test_rejects_unknown_source_type(self):
        with pytest.raises(ValueError, match="invalid source_type"):
            valid_task(source_type="audio").validate()

    def test_requires_both_source_job_ids(self):
        with pytest.raises(ValueError) as exc:
            valid_task(ap_mv_job_id="", music_job_id="").validate()
        assert "ap_mv_job_id is required" in str(exc.value)
        assert "music_job_id is required" in str(exc.value)

    def test_rejects_output_prefix_outside_allowed_bucket(self):
        with pytest.raises(ValueError, match="must be within bucket"):
            valid_task(output_prefix="gs://other-bucket/out").validate(allowed_bucket="my-bucket")

    def test_rejects_non_gcs_subs_url(self):
        with pytest.raises(ValueError, match="subs_url must be a GCS URI"):
            valid_task(subs_url="https://example.com/a.ass").validate()

    def test_ignores_subs_url_for_video_source(self):
        valid_task(source_type="video", subs_url="https://example.com/a.ass").validate()

    def test_reports_all_errors_at_once(self):
        with pytest.raises(ValueError) as exc:
            Task(job_id="", whisper_model="tiny", source_type="x").validate()
        assert str(exc.value).count(";") >= 3

    def test_round_trips_through_json(self):
        task = valid_task()
        assert Task.from_json(task.to_json()) == task

    def test_from_json_accepts_bytes(self):
        task = valid_task()
        assert Task.from_json(task.to_json().encode()) == task

    def test_from_json_rejects_unknown_fields(self):
        with pytest.raises(TypeError):
            Task.from_json(json.dumps({"job_id": "x", "bogus": 1}))


class TestJobRecord:
    def test_from_task_starts_queued(self):
        record = JobRecord.from_task(valid_task())
        assert record.status == "queued"
        assert record.job_id == "lyric-20260101-000000-abcdef"
        assert record.output_uri == ""

    def test_from_dict_ignores_unknown_keys(self):
        record = JobRecord.from_dict(
            {"job_id": "a", "status": "complete", "created_at": "t", "youtube_url": "https://x", "unknown": 1}
        )
        assert record.job_id == "a"
        assert record.status == "complete"

    def test_from_task_fills_created_at_when_missing(self):
        record = JobRecord.from_task(valid_task(created_at=""))
        assert record.created_at


class TestYouTubeTaskTitle:
    def _task(self, title="曲名", **kw):
        return YouTubeTask(job_id="j", output_uri="gs://b/o.mp4", title=title, **kw)

    def test_appends_suffix(self):
        assert self._task().full_title() == "曲名" + TITLE_SUFFIX

    def test_does_not_double_append_suffix(self):
        already = "曲名" + TITLE_SUFFIX
        assert self._task(already).full_title() == already

    def test_truncates_to_youtube_limit(self):
        title = self._task("あ" * 200).full_title()
        assert len(title) == MAX_YOUTUBE_TITLE_LENGTH
        assert title.endswith(TITLE_SUFFIX)


class TestYouTubeTaskDescription:
    def _task(self, description=""):
        return YouTubeTask(job_id="j", output_uri="gs://b/o.mp4", title="t", description=description)

    def test_uses_hashtags_only_when_empty(self):
        assert self._task().full_description() == FIXED_HASHTAGS

    def test_appends_hashtags_after_blank_line(self):
        assert self._task("本文\n").full_description() == "本文\n\n" + FIXED_HASHTAGS


class TestYouTubeTaskTags:
    def _task(self, tags=""):
        return YouTubeTask(job_id="j", output_uri="gs://b/o.mp4", title="t", tags=tags)

    def test_adds_fixed_tags_in_order(self):
        assert self._task().tag_list() == FIXED_TAGS

    def test_keeps_user_tags_after_fixed_tags(self):
        assert self._task("夏, 応援ソング").tag_list() == [*FIXED_TAGS, "夏", "応援ソング"]

    def test_does_not_duplicate_existing_fixed_tags(self):
        tags = self._task("AI音楽,夏").tag_list()
        assert tags.count("AI音楽") == 1
        assert tags[-1] == "夏"

    def test_ignores_empty_entries(self):
        assert self._task(" , ,").tag_list() == FIXED_TAGS


class TestMusicLayout:
    """ap-music の配置は ap-comp の storage_layout.go が決めている。

    かつてバケット直下の <job_id>.mp3 だったものが music/<job_id>/audio.mp3 へ
    移り、このアプリだけ追随できていなかった。再発すると素材の取得が全滅する。
    """

    def test_job_prefix_groups_artifacts_under_the_job_id(self):
        assert music_job_prefix("ap-music", "comp-1") == "gs://ap-music/music/comp-1/"

    def test_audio_uri_is_not_flat_under_the_bucket(self):
        uri = music_audio_uri("ap-music", "comp-1")
        assert uri == "gs://ap-music/music/comp-1/audio.mp3"
        assert uri != "gs://ap-music/comp-1.mp3"

    def test_recipe_is_named_recipe_json_not_audio_json(self):
        # 拡張子の差し替えでは当たらないことが、この命名を明示する理由。
        assert music_recipe_uri("ap-music", "comp-1") == "gs://ap-music/music/comp-1/recipe.json"
        assert music_recipe_uri("ap-music", "comp-1") != music_audio_uri("ap-music", "comp-1")[: -len(".mp3")] + ".json"

    def test_audio_and_recipe_share_a_prefix(self):
        prefix = music_job_prefix("ap-music", "comp-1")
        assert music_audio_uri("ap-music", "comp-1").startswith(prefix)
        assert music_recipe_uri("ap-music", "comp-1").startswith(prefix)
