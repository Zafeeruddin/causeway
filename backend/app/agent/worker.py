"""The agent's main loop.

This process is the only one that touches the network the cameras live on. It
holds the VPN clients, the SSH masters and every ffmpeg, which is why it is the
only container with ``NET_ADMIN`` -- namespaces cannot be shared across
containers, so an API process could not use a tunnel this one opened even if it
were allowed to create one.

Work arrives as rows, not as messages. The API writes a queued recording and
returns; this loop claims it with a conditional update, so a second agent
against the same database takes different work rather than the same work twice,
and a crash mid-recording leaves a row that can be reasoned about instead of a
message that was already acknowledged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, cast

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.agent.commands import ConnectionCommands
from app.agent.sink import DbSink, payload_for
from app.config import settings
from app.db import sessionmaker
from app.enums import ProfileState, RecordingState, SourceKind
from app.models import Camera, ConnectionProfile, Recording, StorageObject, Team
from app.net.runner import LocalRunner, Runner
from app.recorder.session import RecordingSession, SessionReport
from app.recorder.shipper import ShipResult, discard, ship
from app.services.commands import CommandBus, command_bus
from app.services.connections import ConnectionService
from app.services.events import Event, EventBus, event_bus
from app.services.paths import ProfileSourcePath
from app.services.preview import PreviewManager
from app.storage.client import ObjectStore, StorageError, object_store
from app.storage.retention import (
    DELETION_NOTICE_DAYS,
    Candidate,
    StoragePolicy,
    plan_sweep,
)

log = structlog.get_logger(__name__)


class Agent:
    def __init__(
        self,
        *,
        connections: ConnectionService,
        sessions: async_sessionmaker[AsyncSession] | None = None,
        store: ObjectStore | None = None,
        bus: EventBus | None = None,
        concurrency: int | None = None,
        runner: Runner | None = None,
        commands: CommandBus | None = None,
        previews: PreviewManager | None = None,
    ) -> None:
        cfg = settings()
        self.connections = connections
        self._sessions = sessions or sessionmaker()
        self._store = store
        #: Joins segments into the session file. Deliberately a plain runner:
        #: the join reads the work volume and must not inherit a namespace that
        #: is about to be torn down with the tunnel.
        self._runner = runner or LocalRunner()
        self._bus = bus or event_bus()
        self.concurrency = concurrency or cfg.max_streams_per_user * cfg.max_concurrent_users
        self._commands = commands
        self.previews = previews
        self._running: dict[str, RecordingSession] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stopping = asyncio.Event()

    @property
    def commands(self) -> CommandBus:
        if self._commands is None:
            self._commands = command_bus()
        return self._commands

    @property
    def store(self) -> ObjectStore:
        if self._store is None:
            self._store = object_store()
        return self._store

    # ---- lifecycle -----------------------------------------------------

    async def run(self) -> None:
        cfg = settings()
        log.info(
            "agent.started",
            concurrency=self.concurrency,
            work_dir=cfg.work_dir,
            netns_available=self.connections.netns.available,
        )
        loops = [
            asyncio.create_task(self._claim_loop(), name="claim"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._retention_loop(), name="retention"),
            asyncio.create_task(self._command_loop(), name="commands"),
            asyncio.create_task(self._preview_loop(), name="previews"),
        ]
        try:
            await self._stopping.wait()
        finally:
            for task in loops:
                task.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            if self.previews is not None:
                await self.previews.stop_all()
            await self._drain()
            log.info("agent.stopped")

    def stop(self) -> None:
        """Stop taking work and wind up what is running.

        Recordings in flight are told to stop rather than killed: the segments
        already on disk are still worth shipping, and a session that stops early
        is a short recording, not a lost one.
        """
        log.info("agent.stopping", recordings=len(self._running))
        self._stopping.set()
        for session in list(self._running.values()):
            session.stop()

    async def _drain(self) -> None:
        if not self._tasks:
            return
        await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    # ---- serving the API -----------------------------------------------

    async def _command_loop(self) -> None:
        """Answer the API's connect, disconnect, trust and test requests.

        Reconnects rather than exiting: Redis restarting must not leave an
        otherwise healthy agent that records fine and answers nothing, which
        looks from the dashboard like the agent is gone.
        """
        handler = ConnectionCommands(
            connections=self.connections,
            sessions=self._sessions,
            status=self.status,
            previews=self.previews,
        ).handle
        while not self._stopping.is_set():
            try:
                await self.commands.serve(handler)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("agent.command_loop_failed")
                await asyncio.sleep(settings().agent_poll_seconds)

    def status(self) -> dict:
        """What the agent says about itself when the API asks."""
        return {
            "netns_available": self.connections.netns.available,
            "recordings": len(self._tasks),
            "capacity": self.concurrency,
            "previews": self.previews.count if self.previews else 0,
        }

    async def _preview_loop(self) -> None:
        """Drop previews nobody is watching.

        A preview holds a camera session open, and cameras cap those hard --
        a tab left open on someone's second monitor is a session a recording
        cannot have.
        """
        if self.previews is None:
            return
        interval = settings().preview_reap_seconds
        while not self._stopping.is_set():
            await asyncio.sleep(interval)
            try:
                await self.previews.reap()
            except Exception:  # noqa: BLE001
                log.exception("agent.preview_reap_failed")

    # ---- claiming ------------------------------------------------------

    async def _claim_loop(self) -> None:
        poll = settings().agent_poll_seconds
        while not self._stopping.is_set():
            try:
                for recording_id in await self.claim(self.concurrency - len(self._tasks)):
                    task = asyncio.create_task(self._run_recording(recording_id))
                    self._tasks[recording_id] = task
                    task.add_done_callback(partial(self._forget, recording_id))
            except Exception:  # noqa: BLE001 - a bad poll must not end the loop
                log.exception("agent.claim_failed")
            await asyncio.sleep(poll)

    def _forget(self, recording_id: str, task: asyncio.Task) -> None:
        self._tasks.pop(recording_id, None)
        self._running.pop(recording_id, None)
        # Nothing awaits these tasks, so an exception that reaches here is one
        # nobody would ever see: asyncio only reports it when the task is
        # collected, and by then the row it belonged to is long forgotten.
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error("recording.task_crashed", recording=recording_id, exc_info=exc)

    async def claim(self, slots: int) -> list[str]:
        """Take up to ``slots`` queued recordings, atomically.

        The conditional update is the claim: whichever agent's UPDATE matches
        the row first gets it, and the other's matches nothing.
        """
        if slots <= 0:
            return []
        claimed: list[str] = []
        async with self._sessions() as db:
            rows = await db.execute(
                select(Recording.id)
                .where(Recording.state == RecordingState.QUEUED)
                .order_by(Recording.created_at)
                .limit(slots)
            )
            for recording_id in rows.scalars().all():
                result = cast(
                    "CursorResult[Any]",
                    await db.execute(
                        update(Recording)
                        .where(
                            Recording.id == recording_id,
                            Recording.state == RecordingState.QUEUED,
                        )
                        .values(state=RecordingState.RECORDING, started_at=datetime.now(UTC))
                    ),
                )
                if result.rowcount == 1:
                    claimed.append(recording_id)
            await db.commit()
        for recording_id in claimed:
            log.info("recording.claimed", recording=recording_id)
        return claimed

    # ---- running one recording -----------------------------------------

    async def _run_recording(self, recording_id: str) -> None:
        cfg = settings()
        try:
            plan = await self._plan(recording_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("recording.plan_failed", recording=recording_id)
            await self._fail(recording_id, _message_for(exc))
            return

        sink = DbSink(
            recording_id=recording_id,
            team_id=plan.team_id,
            sessions=self._sessions,
            bus=self._bus,
        )
        session = RecordingSession(
            recording_id=recording_id,
            paths=plan.paths,
            work_dir=cfg.work_dir,
            requested_seconds=plan.requested_seconds,
            segment_seconds=cfg.record_segment_seconds,
            sink=sink,
        )
        self._running[recording_id] = session

        try:
            report = await session.run()
        except Exception as exc:  # noqa: BLE001 - a crashed session is a failed one
            log.exception("recording.crashed", recording=recording_id)
            await self._fail(recording_id, _message_for(exc))
            return

        try:
            await self.finalize(recording_id, plan, report, sink, session.work_dir)
        except Exception as exc:  # noqa: BLE001 - the row is what matters here
            # finalize is the last thing that touches this row. Letting it raise
            # leaves the recording in FINALIZING with no reason on it, forever,
            # and the task dies where nobody is looking -- so the failure has to
            # be written down here even when we do not know what it was.
            log.exception("recording.finalize_crashed", recording=recording_id)
            await self._fail(recording_id, _message_for(exc))

    async def _plan(self, recording_id: str) -> _Plan:
        async with self._sessions() as db:
            recording = await db.get(Recording, recording_id)
            if recording is None:
                raise RuntimeError("this recording no longer exists")
            camera = await db.get(
                Camera, recording.camera_id, options=[selectinload(Camera.sources)]
            )
            if camera is None:
                raise RuntimeError("the camera for this recording has been deleted")
            team = await db.get(Team, recording.team_id)
            profile = await db.get(ConnectionProfile, camera.profile_id)
            if profile is None:
                raise RuntimeError("the connection profile for this camera has been deleted")
            if not camera.sources:
                raise RuntimeError("this camera has no sources to record")

            paths = [
                ProfileSourcePath(
                    source_id=source.id,
                    kind=SourceKind(source.kind),
                    profile_id=profile.id,
                    connections=self.connections,
                    sessions=self._sessions,
                )
                for source in camera.sources
            ]
            return _Plan(
                team_id=recording.team_id,
                team_slug=team.slug if team else recording.team_id,
                requested_seconds=recording.requested_seconds,
                paths=paths,
            )

    async def finalize(
        self,
        recording_id: str,
        plan: _Plan,
        report: SessionReport,
        sink: DbSink,
        work_dir: Path,
    ) -> None:
        """Join, upload, account, and only then delete the work directory."""
        if not report.ok:
            await self._fail(recording_id, report.failure_reason or "nothing was captured")
            await discard(work_dir)
            return

        await sink.state(RecordingState.FINALIZING, "")
        try:
            shipped = await ship(
                report,
                team_slug=plan.team_slug,
                requested_seconds=plan.requested_seconds,
                store=self.store,
                runner=self._runner,
            )
        except StorageError as exc:
            log.error("recording.ship_failed", recording=recording_id, error=str(exc))
            await self._fail(recording_id, exc.user_message)
            return

        if not shipped.ok:
            # The segments are still on the work volume -- deleting them here
            # would destroy the only copy of footage the gateway never got --
            # and the person reading this is the one who decides what to do
            # about that, so the message says so rather than leaving them to
            # guess.
            detail = "; ".join(shipped.failures) or "the recording could not be uploaded"
            await self._fail(
                recording_id,
                f"{detail.rstrip('.')}. "
                "The recording is still on the work volume and was not deleted.",
            )
            return

        await self._record_objects(recording_id, plan, report, shipped)
        await discard(work_dir)
        log.info(
            "recording.complete",
            recording=recording_id,
            captured=report.captured_seconds,
            gaps=report.gap_seconds,
            bytes=shipped.bytes,
        )

    async def _record_objects(
        self, recording_id: str, plan: _Plan, report: SessionReport, shipped: ShipResult
    ) -> None:
        async with self._sessions() as db:
            for obj in shipped.objects:
                db.add(
                    StorageObject(
                        team_id=plan.team_id,
                        recording_id=recording_id,
                        source_kind=obj.source_kind,
                        s3_key=obj.key,
                        content_type=obj.content_type,
                        bytes=obj.bytes,
                    )
                )
            recording = await db.get(Recording, recording_id)
            if recording is not None:
                recording.state = (
                    RecordingState.CANCELLED if report.stopped_early else RecordingState.COMPLETE
                )
                recording.finished_at = report.finished_at
                recording.captured_seconds = report.captured_seconds
                recording.gap_seconds = report.gap_seconds
                recording.total_bytes = shipped.bytes
                # A session where one of two sources failed is still complete;
                # the reason says which half is missing.
                recording.failure_reason = "; ".join(shipped.failures) or report.failure_reason
            await db.commit()
            if recording is not None:
                await self._bus.publish(
                    Event(type="recording", team_id=plan.team_id, payload=payload_for(recording))
                )

    async def _fail(self, recording_id: str, reason: str) -> None:
        async with self._sessions() as db:
            recording = await db.get(Recording, recording_id)
            if recording is None:
                return
            recording.state = RecordingState.FAILED
            recording.finished_at = datetime.now(UTC)
            recording.failure_reason = reason[:500]
            await db.commit()
            await self._bus.publish(
                Event(
                    type="recording",
                    team_id=recording.team_id,
                    payload=payload_for(recording),
                )
            )
        log.error("recording.failed", recording=recording_id, reason=reason)

    # ---- connection health ---------------------------------------------

    async def _heartbeat_loop(self) -> None:
        interval = settings().heartbeat_seconds
        while not self._stopping.is_set():
            await asyncio.sleep(interval)
            try:
                await self._heartbeat()
            except Exception:  # noqa: BLE001
                log.exception("agent.heartbeat_failed")

    async def _heartbeat(self) -> None:
        """Re-check the connections this process believes are up.

        Only this process's own connections: a profile marked up by an agent
        that has since died is not ours to contradict, and marking it down from
        here would race with the agent that owns it.
        """
        live = self.connections.live_profile_ids
        if not live:
            return
        async with self._sessions() as db:
            rows = await db.execute(select(ConnectionProfile).where(ConnectionProfile.id.in_(live)))
            for profile in rows.scalars().all():
                if profile.state != ProfileState.UP:
                    continue
                if not await self.connections.health(profile):
                    await self.connections.mark_degraded(
                        db, profile, "the tunnel stopped answering its health check"
                    )
            await db.commit()

    # ---- retention -----------------------------------------------------

    async def _retention_loop(self) -> None:
        interval = settings().retention_sweep_seconds
        while not self._stopping.is_set():
            await asyncio.sleep(interval)
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001
                log.exception("agent.retention_failed")

    async def sweep(self) -> None:
        """The only thing that ever deletes a recording.

        Versity has no lifecycle rules, so if this stops running nothing expires
        and the admission check is left holding the whole ceiling on its own.
        """
        policy = StoragePolicy.from_settings()
        async with self._sessions() as db:
            used = int(
                (
                    await db.execute(
                        select(func.coalesce(func.sum(StorageObject.bytes), 0)).where(
                            StorageObject.deleted_at.is_(None)
                        )
                    )
                ).scalar_one()
            )
            rows = await db.execute(select(StorageObject).where(StorageObject.deleted_at.is_(None)))
            objects = {obj.id: obj for obj in rows.scalars().all()}
            plan = plan_sweep(
                [
                    Candidate(
                        id=obj.id,
                        key=obj.s3_key,
                        bytes=obj.bytes,
                        created_at=obj.created_at,
                        recording_id=obj.recording_id,
                        eligible_for_deletion_at=obj.eligible_for_deletion_at,
                    )
                    for obj in objects.values()
                ],
                used,
                policy,
            )

            if plan.mark_eligible:
                eligible_at = datetime.now(UTC) + timedelta(days=DELETION_NOTICE_DAYS)
                for candidate in plan.mark_eligible:
                    objects[candidate.id].eligible_for_deletion_at = eligible_at

            if plan.delete:
                try:
                    await self.store.delete([c.key for c in plan.delete])
                except StorageError as exc:
                    log.error("retention.delete_failed", error=str(exc))
                    await db.commit()
                    return
                now = datetime.now(UTC)
                for candidate in plan.delete:
                    objects[candidate.id].deleted_at = now

            await db.commit()

        if plan.delete or plan.mark_eligible:
            log.info(
                "retention.swept",
                deleted=len(plan.delete),
                flagged=len(plan.mark_eligible),
                freed=plan.freed_bytes,
                reason=plan.reason,
            )


@dataclass(slots=True)
class _Plan:
    """What one recording needs, resolved once before the session starts."""

    team_id: str
    team_slug: str
    requested_seconds: int
    paths: list[ProfileSourcePath]


def _message_for(exc: BaseException) -> str:
    return getattr(exc, "user_message", None) or str(exc) or type(exc).__name__
