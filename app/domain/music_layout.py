"""ap-music バケットのオブジェクト配置。

配置を決めているのは ap-comp で、正典は同リポジトリの
`internal/domain/storage_layout.go`。ここはその規則を写したもので、
**このアプリが決めてよい値ではない**。ap-comp 側が変わったら追随する。

    music/<job_id>/audio.mp3      音声      (AudioPath)
    music/<job_id>/recipe.json    レシピ    (RecipePath)
    music/<job_id>/master.wav     配信用    (MasterPath)
    jobs/<job_id>.json            ジョブ状態 (JobStatusPath — 成果物と同居させない)

かつては `gs://ap-music/<job_id>.mp3` というバケット直下のフラットな配置だった。
ap-comp が「1 ジョブの成果物をプレフィックス配下へまとめる」形へ移したあとも
このアプリだけが旧規則のまま残り、素材を取得できなくなっていた（2026-08-06 に修正）。
ap-mv も同じ規則で recipe.json を解決している（handler_recipe.go）。
"""

from __future__ import annotations

# 音声ファイルの拡張子。ap-comp の AudioPath は拡張子を引数に取るが、
# lyric-video が読むのは常に mp3（master.wav は配信用で、合成には使わない）。
AUDIO_EXTENSION = ".mp3"


def music_job_prefix(bucket: str, job_id: str) -> str:
    """1 つの楽曲ジョブの成果物が収まる GCS プレフィックスを返す。"""
    return f"gs://{bucket}/music/{job_id}/"


def music_audio_uri(bucket: str, job_id: str) -> str:
    """楽曲の音声ファイルの GCS URI を返す。"""
    return f"{music_job_prefix(bucket, job_id)}audio{AUDIO_EXTENSION}"


def music_recipe_uri(bucket: str, job_id: str) -> str:
    """楽曲レシピ JSON の GCS URI を返す。

    音声と同じプレフィックスに置かれるが、ファイル名は audio.json ではなく
    recipe.json。拡張子の差し替えでは当たらない。
    """
    return f"{music_job_prefix(bucket, job_id)}recipe.json"
