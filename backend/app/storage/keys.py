"""Object key layout.

Team first, because it is the scope boundary: a per-team prefix means access can
be restricted, usage can be accounted, and a whole team's recordings can be
found or removed without scanning the bucket. Date next, so listing a month is
cheap and the oldest-first retention sweep walks keys in roughly the order it
wants to delete them.

    teams/<team-slug>/2026/08/28/<recording-id>/rtsp/session.mp4
    teams/<team-slug>/2026/08/28/<recording-id>/hls/session.mp4
    teams/<team-slug>/2026/08/28/<recording-id>/gaps.json

The per-source directory is the source's own kind, so the two feeds of one
camera sit side by side under the same recording and a comparison view needs no
index to find them.
"""

from __future__ import annotations

from datetime import datetime

from app.enums import SourceKind

ROOT = "teams"


def recording_prefix(team_slug: str, recording_id: str, started_at: datetime) -> str:
    return f"{ROOT}/{team_slug}/{started_at:%Y/%m/%d}/{recording_id}"


def source_key(
    team_slug: str, recording_id: str, started_at: datetime, kind: SourceKind, filename: str
) -> str:
    return f"{recording_prefix(team_slug, recording_id, started_at)}/{kind.value}/{filename}"


def sidecar_key(team_slug: str, recording_id: str, started_at: datetime, filename: str) -> str:
    """gaps.json and anything else that describes the session as a whole."""
    return f"{recording_prefix(team_slug, recording_id, started_at)}/{filename}"


def team_prefix(team_slug: str) -> str:
    return f"{ROOT}/{team_slug}/"


def content_type_for(filename: str) -> str:
    if filename.endswith(".mp4"):
        return "video/mp4"
    if filename.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if filename.endswith(".ts"):
        return "video/mp2t"
    if filename.endswith(".json"):
        return "application/json"
    return "application/octet-stream"
