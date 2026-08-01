"""adapters 配下（GCS URI 解析 / job_store / YouTube URL 解析）のテスト。"""

import pytest

from app.adapters import gcs, job_store
from app.adapters.youtube import extract_video_id


class TestParseGcsUri:
    def test_splits_bucket_and_object(self):
        assert gcs._parse("gs://bucket/path/to/obj.mp4") == ("bucket", "path/to/obj.mp4")

    @pytest.mark.parametrize("uri", ["https://example.com/a", "bucket/obj", "", "gs://bucket", "gs://bucket/"])
    def test_rejects_invalid_uris(self, uri):
        with pytest.raises(ValueError):
            gcs._parse(uri)


class TestMetaUri:
    def test_builds_uri(self):
        assert job_store.meta_uri("gs://b/lyric-video/output", "job-1") == "gs://b/lyric-video/output/job-1/meta.json"

    def test_tolerates_trailing_slash(self):
        assert job_store.meta_uri("gs://b/out/", "job-1") == "gs://b/out/job-1/meta.json"

    def test_returns_empty_without_prefix(self):
        assert job_store.meta_uri("", "job-1") == ""


class TestJobStoreUpdate:
    def test_merges_into_existing_meta(self, monkeypatch):
        saved = {}
        monkeypatch.setattr(job_store, "load", lambda uri: {"status": "queued", "job_id": "j"})
        monkeypatch.setattr(job_store, "save", lambda meta, uri: saved.update({"meta": meta, "uri": uri}))
        assert job_store.update("gs://b/j/meta.json", {"status": "complete"}) is True
        assert saved["meta"] == {"status": "complete", "job_id": "j"}

    def test_creates_meta_when_missing(self, monkeypatch):
        saved = {}

        def _raise(uri):
            raise FileNotFoundError(uri)

        monkeypatch.setattr(job_store, "load", _raise)
        monkeypatch.setattr(job_store, "save", lambda meta, uri: saved.update({"meta": meta}))
        job_store.update("gs://b/j/meta.json", {"status": "running"})
        assert saved["meta"] == {"status": "running"}

    def test_is_noop_without_uri(self, monkeypatch):
        monkeypatch.setattr(job_store, "save", lambda meta, uri: pytest.fail("should not save"))
        assert job_store.update("", {"status": "x"}) is False
        assert job_store.update(None, {"status": "x"}) is False

    def test_swallows_save_errors(self, monkeypatch):
        monkeypatch.setattr(job_store, "load", lambda uri: {})

        def _raise(meta, uri):
            raise RuntimeError("GCS down")

        monkeypatch.setattr(job_store, "save", _raise)
        assert job_store.update("gs://b/j/meta.json", {"status": "x"}) is False


class TestExtractVideoId:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.youtube.com/watch?v=abc123", "abc123"),
            ("https://youtube.com/watch?v=abc123&t=10", "abc123"),
            ("https://youtu.be/abc123", "abc123"),
            ("https://example.com/watch?v=abc123", ""),
            ("https://www.youtube.com/shorts/abc123", ""),
            ("", ""),
        ],
    )
    def test_extracts_id(self, url, expected):
        assert extract_video_id(url) == expected
