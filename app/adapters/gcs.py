from __future__ import annotations

import os
from functools import lru_cache

from google.cloud import storage


def _parse(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"not a GCS URI: {uri!r}")
    path = uri[len("gs://"):]
    bucket, _, blob = path.partition("/")
    if not bucket or not blob:
        raise ValueError(f"invalid GCS URI: {uri!r}")
    return bucket, blob


@lru_cache(maxsize=1)
def _get_client() -> storage.Client:
    return storage.Client()


def download(uri: str, local_path: str) -> None:
    bucket_name, blob_name = _parse(uri)
    os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
    _get_client().bucket(bucket_name).blob(blob_name).download_to_filename(local_path)


def upload(local_path: str, uri: str) -> None:
    bucket_name, blob_name = _parse(uri)
    _get_client().bucket(bucket_name).blob(blob_name).upload_from_filename(local_path)
