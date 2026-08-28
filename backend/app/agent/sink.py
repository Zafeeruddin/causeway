"""Writing a live recording down: rows for the record, events for the screen.

Every state change is written and published in the same call, so the dashboard
and the database cannot disagree about what happened. The published payload is
the same shape the REST endpoint returns, which is what lets the recordings page
merge a live update into its list without a refetch.

Sessions are opened per call and closed immediately. A recorder that held one
open for its whole run would keep a pooled connection for five minutes and block
every other write behind it if the stream stalled.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.enums import RecordingState
from app.models import Gap, Recording, Segment
from app.recorder.session import GapRecord, SegmentRecord
from app.services.events import Event, EventBus, event_bus

log = structlog.get_logger(__name__)


class DbSink:
    """The :class:`~app.recorder.session.SessionSink` the agent actually uses."""

    def __init__(
        self,
        *,
        recording_id: str,
        team_id: str,
        sessions: async_sessionmaker[AsyncSession],
        bus: EventBus | None = None,
    ) -> None:
        self.recording_id = recording_id
        self.team_id = team_id
        self._sessions = sessions
        self._bus = bus or event_bus()
        #: Local gap key -> row id, so a gap can be closed after it is opened.
        self._gap_rows: dict[str, str] = {}

    # ---- SessionSink ---------------------------------------------------

    async def state(self, state: RecordingState, detail: str) -> None:
        async with self._sessions() as db:
            recording = await db.get(Recording, self.recording_id)
            if recording is None:
                return
            recording.state = state
            if state is RecordingState.RECORDING and recording.started_at is None:
                recording.started_at = datetime.now(UTC)
            if detail:
                # The failure column doubles as "why this recording looks odd",
                # so a recovery message belongs there too -- cleared when the
                # stream comes back.
                recording.failure_reason = detail if state is RecordingState.RECOVERING else ""
            await db.commit()
            await self._publish(db, recording)

    async def segment(self, segment: SegmentRecord) -> None:
        async with self._sessions() as db:
            db.add(
                Segment(
                    recording_id=self.recording_id,
                    source_kind=segment.source_kind,
                    sequence=segment.sequence,
                    path=segment.path,
                    started_at=segment.started_at,
                    duration_seconds=segment.duration_seconds,
                    bytes=segment.bytes,
                )
            )
            recording = await db.get(Recording, self.recording_id)
            if recording is not None:
                # Progress the UI can watch. Recomputed exactly at finalize; this
                # is the running total, not the final accounting.
                recording.captured_seconds = round(
                    recording.captured_seconds + segment.duration_seconds, 3
                )
                recording.total_bytes += segment.bytes
            await db.commit()
            if recording is not None:
                await self._publish(db, recording)

    async def gap_opened(self, gap: GapRecord) -> None:
        async with self._sessions() as db:
            row = Gap(
                recording_id=self.recording_id,
                source_kind=gap.source_kind,
                started_at=gap.started_at,
                cause=gap.cause,
                detail=gap.detail,
                redial_attempts=gap.redial_attempts,
            )
            db.add(row)
            await db.flush()
            self._gap_rows[gap.key] = row.id
            await db.commit()

    async def gap_closed(self, gap: GapRecord) -> None:
        row_id = self._gap_rows.get(gap.key)
        if row_id is None:
            return
        async with self._sessions() as db:
            row = await db.get(Gap, row_id)
            if row is None:
                return
            row.ended_at = gap.ended_at
            row.seconds = gap.seconds
            row.redial_attempts = gap.redial_attempts
            recording = await db.get(Recording, self.recording_id)
            if recording is not None:
                recording.gap_seconds = round(recording.gap_seconds + gap.seconds, 3)
            await db.commit()

    # ---- publishing ----------------------------------------------------

    async def _publish(self, db: AsyncSession, recording: Recording) -> None:
        await self._bus.publish(
            Event(type="recording", team_id=self.team_id, payload=payload_for(recording))
        )


def payload_for(recording: Recording) -> dict:
    """The wire shape of a recording. Mirrors ``RecordingOut``."""
    return {
        "id": recording.id,
        "team_id": recording.team_id,
        "camera_id": recording.camera_id,
        "state": str(recording.state),
        "requested_seconds": recording.requested_seconds,
        "started_at": _iso(recording.started_at),
        "finished_at": _iso(recording.finished_at),
        "captured_seconds": recording.captured_seconds,
        "gap_seconds": recording.gap_seconds,
        "total_bytes": recording.total_bytes,
        "failure_reason": recording.failure_reason,
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
