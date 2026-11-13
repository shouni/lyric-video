from __future__ import annotations

import datetime
import json
import mimetypes
import os
from contextlib import contextmanager
from functools import lru_cache

import google.auth
from google.auth.transport import requests as google_requests
from google.cloud import storage


def download(uri: str, local_path: str) -> None:
    """Download an object on GCS to a local path."""
    bucket_name, blob_name = _parse(uri)
    os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
    _get_client().bucket(bucket_name).blob(blob_name).download_to_filename(local_path)


def upload(local_path: str, uri: str) -> None:
    """Upload a local file to the given GCS URI."""
    bucket_name, blob_name = _parse(uri)
    blob = _get_client().bucket(bucket_name).blob(blob_name)
    content_type, _ = mimetypes.guess_type(local_path)
    blob.upload_from_filename(local_path, content_type=content_type or "application/octet-stream")


def save_json(data: dict, uri: str) -> None:
    """Save a dict to GCS as JSON."""
    bucket_name, blob_name = _parse(uri)
    blob = _get_client().bucket(bucket_name).blob(blob_name)
    blob.upload_from_string(json.dumps(data, ensure_ascii=False), content_type="application/json")


def load_json(uri: str) -> dict:
    """Load a JSON file on GCS as a dict."""
    bucket_name, blob_name = _parse(uri)
    blob = _get_client().bucket(bucket_name).blob(blob_name)
    return json.loads(blob.download_as_text())


def list_job_metadata(output_prefix: str) -> list[dict]:
    """Fetch the meta.json files under output_prefix in parallel and return them newest first by creation time."""
    from concurrent.futures import ThreadPoolExecutor

    bucket_name, prefix = _parse(output_prefix.rstrip("/") + "/")
    bucket = _get_client().bucket(bucket_name)
    target_blobs = [b for b in bucket.list_blobs(prefix=prefix) if b.name.endswith("/meta.json")]

    def _download(blob):
        try:
            data = json.loads(blob.download_as_text())
            if not data.get("job_id"):
                # Example blob.name: lyric-video/output/<job_id>/meta.json
                data["job_id"] = blob.name.split("/")[-2]
            return data
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        raw = executor.map(_download, target_blobs)

    results = [r for r in raw if r is not None]
    return sorted(results, key=lambda x: x.get("created_at", ""), reverse=True)


@contextmanager
def open_blob(uri: str):
    """Open an object on GCS as a seekable stream."""
    bucket_name, blob_name = _parse(uri)
    blob = _get_client().bucket(bucket_name).blob(blob_name)
    with blob.open("rb") as f:
        yield f


def delete(uri: str) -> None:
    """Delete an object on GCS. Raises NotFound if it does not exist; the caller logs it and carries on."""
    bucket_name, blob_name = _parse(uri)
    blob = _get_client().bucket(bucket_name).blob(blob_name)
    blob.delete(if_generation_match=None)


def generate_signed_url(
    uri: str, service_account_email: str, expiration_hours: int = 1, filename: str | None = None
) -> str:
    """Generate a time-limited access URL for a GCS object."""
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(google_requests.Request())
    bucket_name, blob_name = _parse(uri)
    blob = _get_client().bucket(bucket_name).blob(blob_name)
    kwargs: dict = {
        "version": "v4",
        "expiration": datetime.timedelta(hours=expiration_hours),
        "method": "GET",
        "service_account_email": service_account_email,
        "access_token": credentials.token,
    }
    if filename:
        kwargs["response_disposition"] = f'attachment; filename="{filename}"'
    return blob.generate_signed_url(**kwargs)


def _parse(uri: str) -> tuple[str, str]:
    """Split a GCS URI into a bucket name and an object name."""
    if not uri.startswith("gs://"):
        raise ValueError(f"not a GCS URI: {uri!r}")
    path = uri[len("gs://") :]
    bucket, _, blob = path.partition("/")
    if not bucket or not blob:
        raise ValueError(f"invalid GCS URI: {uri!r}")
    return bucket, blob


@lru_cache(maxsize=1)
def _get_client() -> storage.Client:
    """Get a reusable Google Cloud Storage client."""
    return storage.Client()
