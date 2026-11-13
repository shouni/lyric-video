from __future__ import annotations

import logging

from app.adapters import gcs

logger = logging.getLogger(__name__)


def meta_uri(output_prefix: str, job_id: str) -> str:
    """Build the GCS URI of a job's meta.json. Returns an empty string if output_prefix is not set."""
    if not output_prefix:
        return ""
    return f"{output_prefix.rstrip('/')}/{job_id}/meta.json"


def load(uri: str) -> dict:
    """Load meta.json. Raises an exception if it does not exist or is corrupt."""
    return gcs.load_json(uri)


def save(meta: dict, uri: str) -> None:
    """Save the whole meta.json."""
    gcs.save_json(meta, uri)


def update(uri: str | None, updates: dict) -> bool:
    """Apply a partial update to an existing meta.json.

    meta.json is metadata for showing progress, and video generation and the YouTube upload
    themselves should continue even if updating it fails, so this does not raise an exception.
    It only logs a warning and returns False.
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
