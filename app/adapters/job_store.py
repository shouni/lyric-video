from __future__ import annotations

import logging

from app.adapters import gcs

logger = logging.getLogger(__name__)


def meta_uri(output_prefix: str, job_id: str) -> str:
    """ジョブのmeta.jsonのGCS URIを組み立てる。output_prefixが未設定なら空文字を返す。"""
    if not output_prefix:
        return ""
    return f"{output_prefix.rstrip('/')}/{job_id}/meta.json"


def load(uri: str) -> dict:
    """meta.jsonを読み込む。存在しない・壊れている場合は例外を送出する。"""
    return gcs.load_json(uri)


def save(meta: dict, uri: str) -> None:
    """meta.jsonを丸ごと保存する。"""
    gcs.save_json(meta, uri)


def update(uri: str | None, updates: dict) -> bool:
    """既存のmeta.jsonへ差分を適用する。

    meta.jsonは進捗表示用のメタデータであり、更新に失敗しても動画生成・YouTube投稿
    そのものは継続させたいため、例外は送出せず警告ログに留めて False を返す。
    """
    if not uri:
        return False
    try:
        try:
            meta = load(uri)
        except Exception:
            meta = {}
        meta.update(updates)
        save(meta, uri)
        return True
    except Exception as exc:
        logger.warning("Failed to update job metadata uri=%s: %s", uri, exc)
        return False
