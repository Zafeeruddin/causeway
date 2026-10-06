"""Making a recording playable in a browser.

These cameras send H.265. A recording is a stream copy -- the archive keeps the
camera's own bytes, which is the point of it -- and no browser on the operators'
machines can decode that. Until now the answer was "download it and open VLC",
which is a poor answer for a platform whose whole job is showing people footage.

So a second copy is made, on demand, beside the original: same recording, same
prefix, H.264. The original is never touched or replaced. Nothing is converted
until somebody actually asks to watch it, because most recordings are never
opened and re-encoding the estate on the chance that one might be is a great
deal of GPU spent on nothing.

This lives in the agent because the agent is where the card is: the API
container has no GPU at all.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.sink import payload_for
from app.config import settings
from app.models import Recording, StorageObject
from app.net.runner import LocalRunner, Runner
from app.recorder.accel import TranscodeBudget
from app.recorder.ffmpeg import PLAYBACK_FILE, needs_transcode, playback_argv, probe_codec_argv
from app.services.events import Event, EventBus, event_bus
from app.storage.client import ObjectStore, StorageError, object_store

log = structlog.get_logger(__name__)

#: Re-encoding an eight minute 1080p recording is seconds on a card and minutes
#: on a CPU. This is the ceiling for either.
CONVERT_TIMEOUT = 1800.0

#: Reading one header over HTTPS. The file is faststart, so this is a range
#: request for the front of it rather than a download.
PROBE_TIMEOUT = 30.0
PROBE_TTL = 600


def playback_key_for(original_key: str) -> str:
    """The playable copy sits beside the original, under the same recording."""
    return f"{original_key.rsplit('/', 1)[0]}/{PLAYBACK_FILE}"


def is_playback_key(key: str) -> bool:
    return key.rsplit("/", 1)[-1] == PLAYBACK_FILE


class PlaybackRenditions:
    """Converts recordings to H.264 on request, one at a time per recording."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        budget: TranscodeBudget | None = None,
        store: ObjectStore | None = None,
        runner: Runner | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._sessions = sessions
        self._budget = budget or TranscodeBudget()
        self._store = store
        self._runner = runner or LocalRunner()
        self._bus = bus
        #: Recordings being converted right now. Two people opening the same
        #: recording is the ordinary case, not the rare one, and converting it
        #: twice would spend the card twice to write the same object.
        self._running: dict[str, asyncio.Task] = {}

    @property
    def store(self) -> ObjectStore:
        # Resolved late: the agent builds this before the process has any
        # reason to have constructed an S3 client.
        if self._store is None:
            self._store = object_store()
        return self._store

    async def request(self, recording_id: str, *, force: bool = False) -> dict:
        """Start a conversion if one is needed, and say where it stands.

        Returns at once either way. The conversion outlives this call, because
        the command bus answers in seconds and an encode does not.

        ``force`` separates the two callers. A recording that has just finished
        asks without it, and an H.264 camera is then left alone rather than
        re-encoded into the same thing it already was. Somebody pressing the
        button in the dashboard asks with it, because their browser has already
        refused the file and "nothing to do here" would be a dead end.
        """
        async with self._sessions() as db:
            original, existing = await self._objects(db, recording_id)
            if original is None:
                return {"state": "unavailable", "detail": "this recording has no stored video."}
            if existing is not None:
                return {"state": "ready"}
            key = original.s3_key

        if recording_id in self._running:
            return {"state": "converting"}

        # Asked before the download, so a recording that needs nothing costs one
        # header read rather than fetching and re-encoding itself into a copy.
        if not force and not await self._needs_conversion(key):
            return {"state": "ready"}

        task = asyncio.create_task(self._convert(recording_id))
        self._running[recording_id] = task
        task.add_done_callback(lambda _: self._running.pop(recording_id, None))
        return {"state": "converting"}

    # ---- internals -----------------------------------------------------

    async def _needs_conversion(self, key: str) -> bool:
        """Whether a browser would refuse this recording as it stands.

        A probe that cannot answer says no, which matches how the rest of the
        product treats an unknown codec: an unprobed source is copied rather
        than re-encoded on a guess. The dashboard's button passes ``force`` and
        so never reaches here, which is what keeps that conservative default
        from becoming a dead end for the person who actually cannot watch.
        """
        try:
            url = await self.store.presign(key, expires=PROBE_TTL)
            result = await self._runner.run(probe_codec_argv(url), timeout=PROBE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - an unreadable header is not a failure
            log.info("playback.probe_error", key=key, error=str(exc))
            return False
        lines = (result.stdout or "").strip().splitlines()
        return needs_transcode(lines[0].strip().lower() if lines else "")

    async def _objects(
        self, db: AsyncSession, recording_id: str
    ) -> tuple[StorageObject | None, StorageObject | None]:
        rows = await db.execute(
            select(StorageObject).where(
                StorageObject.recording_id == recording_id,
                StorageObject.deleted_at.is_(None),
                StorageObject.source_kind.is_not(None),
            )
        )
        original = playable = None
        for obj in rows.scalars().all():
            if is_playback_key(obj.s3_key):
                playable = obj
            else:
                original = obj
        return original, playable

    async def _convert(self, recording_id: str) -> None:
        async with self._sessions() as db:
            original, existing = await self._objects(db, recording_id)
            if original is None or existing is not None:
                return
            key, team_id, kind = original.s3_key, original.team_id, original.source_kind

        directory = Path(settings().work_dir) / f"playback-{recording_id}"
        source = directory / "source.mp4"
        output = directory / PLAYBACK_FILE
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)

        try:
            await self._budget.reserve()
        except Exception as exc:  # noqa: BLE001 - at capacity is not an error worth a traceback
            log.info("playback.deferred", recording=recording_id, reason=str(exc))
            await self._cleanup(directory)
            return

        try:
            await self.store.get_file(key, source)
            result = await self._runner.run(
                playback_argv(source, output, self._budget.accel), timeout=CONVERT_TIMEOUT
            )
            if not result.ok or not await asyncio.to_thread(output.exists):
                detail = result.output.strip().splitlines()[-1][:200] if result.output else ""
                log.error("playback.encode_failed", recording=recording_id, detail=detail)
                return

            stored = await self.store.put_file(output, playback_key_for(key))
            async with self._sessions() as db:
                # Re-checked inside the write: a second agent, or a restart
                # that replayed the request, must not add a second row for the
                # key that was just overwritten.
                _, existing = await self._objects(db, recording_id)
                if existing is None:
                    db.add(
                        StorageObject(
                            team_id=team_id,
                            recording_id=recording_id,
                            source_kind=kind,
                            s3_key=stored.key,
                            content_type=stored.content_type,
                            bytes=stored.bytes,
                        )
                    )
                    await db.commit()
                recording = await db.get(Recording, recording_id)
            log.info("playback.ready", recording=recording_id, bytes=stored.bytes)
            if recording is not None:
                await self._publish(recording)
        except StorageError as exc:
            log.error("playback.storage_failed", recording=recording_id, error=str(exc))
        finally:
            await self._budget.release()
            await self._cleanup(directory)

    async def _publish(self, recording: Recording) -> None:
        bus = self._bus or event_bus()
        await bus.publish(
            Event(type="recording", team_id=recording.team_id, payload=payload_for(recording))
        )

    async def _cleanup(self, directory: Path) -> None:
        await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)
