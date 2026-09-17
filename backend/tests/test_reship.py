"""Recovering a recording whose upload failed.

The live deployment's largest failure category by far: the camera was reached,
the segments were sealed and joined, and only the PUT failed. The footage is
still on the work volume, so the recording is recoverable rather than lost.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.recorder.shipper import reship
from app.storage.client import StorageError, StoredObject

pytestmark = pytest.mark.asyncio

STARTED = datetime(2026, 9, 12, 4, 18, tzinfo=UTC)


def _size(path: Path | str) -> int:
    """Sync on purpose: the real store does this on a worker thread."""
    return Path(path).stat().st_size


class FakeStore:
    """An object store that keeps what it was given, or refuses everything."""

    def __init__(self, *, reachable: bool = True) -> None:
        self.reachable = reachable
        self.puts: list[tuple[str, str]] = []

    async def put_file(self, path: Path | str, key: str) -> StoredObject:
        if not self.reachable:
            raise StorageError("could not reach the gateway")
        size = _size(path)
        self.puts.append((str(path), key))
        return StoredObject(key=key, bytes=size, content_type="video/mp4")


def _session_on_disk(root: Path, *, kind: str = "rtsp") -> Path:
    directory = root / "rec-1"
    (directory / kind).mkdir(parents=True)
    (directory / kind / "session.mp4").write_bytes(b"video")
    (directory / "gaps.json").write_text("{}")
    return directory


async def test_reship_sends_the_session_already_joined_on_disk(tmp_path):
    """The expensive work is done by the time the upload fails. Recovery is a
    PUT, not a recapture -- and not a second 600-second concatenation either."""
    directory = _session_on_disk(tmp_path)
    store = FakeStore()

    result = await reship(
        directory,
        team_slug="mofa",
        recording_id="rec-1",
        started_at=STARTED,
        store=store,
    )

    assert result.ok
    keys = [key for _, key in store.puts]
    # The same keys the agent would have written, so a recovered recording is
    # indistinguishable from one that shipped first time.
    assert "teams/mofa/2026/09/12/rec-1/rtsp/session.mp4" in keys
    assert "teams/mofa/2026/09/12/rec-1/gaps.json" in keys


async def test_reship_carries_the_stores_own_words_when_it_is_still_down(tmp_path):
    """Retrying into an outage must say the store is still unreachable, not
    invent a new reason."""
    directory = _session_on_disk(tmp_path)

    result = await reship(
        directory,
        team_slug="mofa",
        recording_id="rec-1",
        started_at=STARTED,
        store=FakeStore(reachable=False),
    )

    assert not result.ok
    assert "could not be reached" in "; ".join(result.failures)


async def test_reship_says_when_there_is_nothing_left_to_send(tmp_path):
    """A directory whose segments have gone is not an upload failure; there is
    nothing to upload, and the message has to distinguish the two."""
    directory = tmp_path / "rec-1"
    (directory / "rtsp").mkdir(parents=True)

    result = await reship(
        directory,
        team_slug="mofa",
        recording_id="rec-1",
        started_at=STARTED,
        store=FakeStore(),
    )

    assert not result.ok
    assert "no segments left" in "; ".join(result.failures)


async def test_reship_sends_both_sources_when_both_were_captured(tmp_path):
    """One camera, two feeds. A session where both recorded must recover both,
    or the comparison view loses the half it is meant to compare against."""
    directory = _session_on_disk(tmp_path)
    (directory / "hls").mkdir()
    (directory / "hls" / "session.mp4").write_bytes(b"other")
    store = FakeStore()

    result = await reship(
        directory,
        team_slug="mofa",
        recording_id="rec-1",
        started_at=STARTED,
        store=store,
    )

    assert result.ok
    keys = [key for _, key in store.puts]
    assert "teams/mofa/2026/09/12/rec-1/rtsp/session.mp4" in keys
    assert "teams/mofa/2026/09/12/rec-1/hls/session.mp4" in keys


async def test_a_missing_sidecar_does_not_fail_the_recovery(tmp_path):
    """gaps.json is written before the upload that failed, so it is normally
    there. Its absence is not worth losing the footage over."""
    directory = tmp_path / "rec-1"
    (directory / "rtsp").mkdir(parents=True)
    (directory / "rtsp" / "session.mp4").write_bytes(b"video")
    store = FakeStore()

    result = await reship(
        directory,
        team_slug="mofa",
        recording_id="rec-1",
        started_at=STARTED,
        store=store,
    )

    assert result.ok
    assert [key for _, key in store.puts] == ["teams/mofa/2026/09/12/rec-1/rtsp/session.mp4"]
