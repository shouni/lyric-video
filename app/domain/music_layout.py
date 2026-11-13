"""Object layout of the ap-music bucket.

The layout is decided by ap-music (a private repository). The canonical source is `internal/domain/storage_layout.go` in
that repository. This file copies those rules, and **these are not values this app may decide**.
When the ap-music side changes, this follows.

    music/<job_id>/audio.mp3      audio             (AudioPath)
    music/<job_id>/recipe.json    recipe            (RecipePath)
    music/<job_id>/master.wav     for distribution  (MasterPath)
    jobs/<job_id>.json            job status        (JobStatusPath, not kept alongside the artifacts)

It used to be a flat layout directly under the bucket, `gs://ap-music/<job_id>.mp3`.
Even after ap-music moved to "gather one job's artifacts under a prefix", this app alone stayed on
the old rule and could no longer fetch its source material (fixed on 2026-08-06).
ap-mv resolves recipe.json by the same rule (handler_recipe.go).
"""

from __future__ import annotations

# File extension of the audio. ap-music's AudioPath takes the extension as an argument, but
# lyric-video always reads mp3 (master.wav is for distribution and is not used for compositing).
AUDIO_EXTENSION = ".mp3"


def music_job_prefix(bucket: str, job_id: str) -> str:
    """Return the GCS prefix that holds the artifacts of one music job."""
    return f"gs://{bucket}/music/{job_id}/"


def music_audio_uri(bucket: str, job_id: str) -> str:
    """Return the GCS URI of the song's audio file."""
    return f"{music_job_prefix(bucket, job_id)}audio{AUDIO_EXTENSION}"


def music_recipe_uri(bucket: str, job_id: str) -> str:
    """Return the GCS URI of the song's recipe JSON.

    It is placed under the same prefix as the audio, but the file name is recipe.json, not audio.json.
    Swapping the extension does not find it.
    """
    return f"{music_job_prefix(bucket, job_id)}recipe.json"
