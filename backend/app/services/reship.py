"""Recovering a recording whose capture worked and whose upload did not.

The store being unreachable at shipping time is the single largest cause of
failed recordings on the live deployment: the camera was reached, the segments
were sealed and joined, and then the last step -- the PUT -- failed, and the
whole session was marked ``failed``. The footage is not lost. It is on the work
volume, where the shipper deliberately leaves it.

This turns that into a recoverable state rather than a terminal one. It is the
same upload the agent would have done, run again later, against the same keys.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.enums import RecordingState
from app.models import Recording, StorageObject, Team
from app.recorder.shipper import discard, reship
from app.storage.client import ObjectStore, StorageError
from app.storage.retention import StoragePolicy, admit

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReshipOutcome:
    recording_id: str
    ok: bool
    bytes: int = 0
    reason: str = ""


def work_dir_for(recording_id: str) -> Path:
    return Path(settings().work_dir) / recording_id


async def has_files_on_disk(recording_id: str) -> bool:
    """Whether there is anything left to send.

    The work directory is removed as soon as a session's objects are in the
    bucket, so its presence is the answer -- no flag to keep in step with the
    filesystem, and no way for the two to disagree.
    """
    return await asyncio.to_thread(work_dir_for(recording_id).is_dir)


async def reship_recording(
    db: AsyncSession,
    recording: Recording,
    *,
    store: ObjectStore | None = None,
) -> ReshipOutcome:
    """Upload what is on disk for one failed recording and complete it.

    The caller's copy of the row is not trusted for the state check. Sessions
    here are built with ``expire_on_commit=False``, so an object held across a
    commit keeps whatever state it was loaded with -- and a stale ``failed``
    would send a recording that another caller has already recovered.
    """
    await db.refresh(recording)
    if recording.state != RecordingState.FAILED:
        return ReshipOutcome(
            recording.id, False, reason="only a failed recording can be sent again."
        )

    directory = work_dir_for(recording.id)
    if not await has_files_on_disk(recording.id):
        return ReshipOutcome(
            recording.id,
            False,
            reason="nothing is left on the work volume for this recording.",
        )

    # The bytes are known rather than estimated -- the files are right there --
    # but the ceiling still applies. Storage may have filled since this failed,
    # and discovering that mid-write is what the admission check exists to
    # prevent.
    size = await _bytes_on_disk(directory)
    used = await _used_bytes(db)
    decision = admit(used, size, StoragePolicy.from_settings())
    if not decision.allowed:
        return ReshipOutcome(recording.id, False, reason=decision.reason)

    team = await db.get(Team, recording.team_id)
    started = recording.started_at or recording.created_at
    try:
        result = await reship(
            directory,
            team_slug=team.slug if team else recording.team_id,
            recording_id=recording.id,
            started_at=started,
            store=store,
        )
    except StorageError as exc:
        return ReshipOutcome(recording.id, False, reason=exc.user_message)

    if not result.ok:
        detail = "; ".join(result.failures) or "the recording could not be uploaded"
        return ReshipOutcome(recording.id, False, reason=detail)

    # A previous attempt may have landed some of these before failing, and
    # s3_key is unique. Re-sending overwrites the object; re-inserting the row
    # would abort the transaction and lose the recovery.
    existing = set(
        (
            await db.execute(
                select(StorageObject.s3_key).where(StorageObject.recording_id == recording.id)
            )
        )
        .scalars()
        .all()
    )
    for obj in result.objects:
        if obj.key in existing:
            continue
        db.add(
            StorageObject(
                team_id=recording.team_id,
                recording_id=recording.id,
                source_kind=obj.source_kind,
                s3_key=obj.key,
                content_type=obj.content_type,
                bytes=obj.bytes,
            )
        )

    recording.state = RecordingState.COMPLETE
    recording.total_bytes = result.bytes
    recording.failure_reason = ""
    try:
        await db.commit()
    except IntegrityError:
        # s3_key is unique, and something has already recorded these objects --
        # a concurrent recovery, or a caller working from a stale copy of this
        # row. The upload writes the same keys with the same bytes either way,
        # so the footage is in the bucket and the recording is recovered; it
        # simply was not this caller who recorded it. Losing the batch over
        # that would strand every recording still queued behind it.
        await db.rollback()
        log.info("reship.already_recorded", recording=recording.id)
        fresh = await db.get(Recording, recording.id)
        if fresh is not None and fresh.state != RecordingState.COMPLETE:
            fresh.state = RecordingState.COMPLETE
            fresh.total_bytes = result.bytes
            fresh.failure_reason = ""
            await db.commit()
        await discard(directory)
        return ReshipOutcome(recording.id, True, bytes=result.bytes)

    # Only now: the work directory is the only copy until the objects are rows.
    await discard(directory)
    log.info("reship.complete", recording=recording.id, bytes=result.bytes)
    return ReshipOutcome(recording.id, True, bytes=result.bytes)


async def recoverable(db: AsyncSession) -> list[Recording]:
    """Failed recordings that still have their files, oldest first."""
    rows = await db.execute(
        select(Recording)
        .where(Recording.state == RecordingState.FAILED)
        .order_by(Recording.created_at)
    )
    found = []
    for recording in rows.scalars().all():
        if await has_files_on_disk(recording.id):
            found.append(recording)
    return found


async def _bytes_on_disk(directory: Path) -> int:
    def total() -> int:
        return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())

    return await asyncio.to_thread(total)


async def _used_bytes(db: AsyncSession) -> int:
    result = await db.execute(
        select(func.coalesce(func.sum(StorageObject.bytes), 0)).where(
            StorageObject.deleted_at.is_(None)
        )
    )
    return int(result.scalar_one())
