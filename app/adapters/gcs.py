from __future__ import annotations

import datetime
import json
import mimetypes
import os
from functools import lru_cache

import google.auth
from google.auth.transport import requests as google_requests
from google.cloud import storage


def download(uri: str, local_path: str) -> None:
    """GCS上のオブジェクトをローカルパスへダウンロードする。"""
    bucket_name, blob_name = _parse(uri)
    os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
    _get_client().bucket(bucket_name).blob(blob_name).download_to_filename(local_path)


def upload(local_path: str, uri: str) -> None:
    """ローカルファイルを指定されたGCS URIへアップロードする。"""
    bucket_name, blob_name = _parse(uri)
    blob = _get_client().bucket(bucket_name).blob(blob_name)
    content_type, _ = mimetypes.guess_type(local_path)
    blob.upload_from_filename(local_path, content_type=content_type or "application/octet-stream")


def save_json(data: dict, uri: str) -> None:
    """dictをJSON形式でGCSへ保存する。"""
    bucket_name, blob_name = _parse(uri)
    blob = _get_client().bucket(bucket_name).blob(blob_name)
    blob.upload_from_string(json.dumps(data, ensure_ascii=False), content_type="application/json")


def load_json(uri: str) -> dict:
    """GCS上のJSONファイルをdictとして読み込む。"""
    bucket_name, blob_name = _parse(uri)
    blob = _get_client().bucket(bucket_name).blob(blob_name)
    return json.loads(blob.download_as_text())


def list_job_metadata(output_prefix: str) -> list[dict]:
    """output_prefix配下のmeta.jsonを並列取得し、作成日時の降順で返す。"""
    from concurrent.futures import ThreadPoolExecutor

    bucket_name, prefix = _parse(output_prefix.rstrip("/") + "/")
    bucket = _get_client().bucket(bucket_name)
    target_blobs = [b for b in bucket.list_blobs(prefix=prefix) if b.name.endswith("/meta.json")]

    def _download(blob):
        try:
            data = json.loads(blob.download_as_text())
            if not data.get("job_id"):
                # blob.name 例: lyric-video/output/<job_id>/meta.json
                data["job_id"] = blob.name.split("/")[-2]
            return data
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        raw = executor.map(_download, target_blobs)

    results = [r for r in raw if r is not None]
    return sorted(results, key=lambda x: x.get("created_at", ""), reverse=True)


def delete(uri: str) -> None:
    """GCS上のオブジェクトを削除する。存在しない場合は何もしない。"""
    bucket_name, blob_name = _parse(uri)
    blob = _get_client().bucket(bucket_name).blob(blob_name)
    blob.delete(if_generation_match=None)


def generate_signed_url(uri: str, service_account_email: str, expiration_hours: int = 1) -> str:
    """GCSオブジェクトへの時限アクセスURLを生成する。"""
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(google_requests.Request())
    bucket_name, blob_name = _parse(uri)
    blob = _get_client().bucket(bucket_name).blob(blob_name)
    return blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(hours=expiration_hours),
        method="GET",
        service_account_email=service_account_email,
        access_token=credentials.token,
    )


def _parse(uri: str) -> tuple[str, str]:
    """GCS URIをバケット名とオブジェクト名に分解する。"""
    if not uri.startswith("gs://"):
        raise ValueError(f"not a GCS URI: {uri!r}")
    path = uri[len("gs://"):]
    bucket, _, blob = path.partition("/")
    if not bucket or not blob:
        raise ValueError(f"invalid GCS URI: {uri!r}")
    return bucket, blob


@lru_cache(maxsize=1)
def _get_client() -> storage.Client:
    """再利用可能なGoogle Cloud Storageクライアントを取得する。"""
    return storage.Client()
