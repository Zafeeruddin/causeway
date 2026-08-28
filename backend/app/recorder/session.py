"""Capturing one recording: segments, outages, and the record of both.

The promise this module keeps is the one in the README -- *a dropped link costs
the seconds it lasted, not the whole recording*. It keeps it by never treating a
capture as a single long-running process. A capture is a sequence of runs
against a deadline: a run ends, the supervisor works out which layer went away,
reopens the path, and starts the next run with the remaining budget. What was
already sealed on disk is never at risk.

The deadline is wall clock, not captured time. Someone recording five minutes of
a camera wants the five minutes that just happened; extending the session to
make up for an outage would hand back footage of a different five minutes and
quietly double the storage the admission check was sized against. The outage is
recorded as a gap instead, which is the honest answer to "why is this file
short".

Nothing here touches the database. The session reports what happened through a
:class:`SessionSink`, which is what lets the whole supervisor be tested against
a scripted ffmpeg with no camera, no tunnel and no Postgres.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import structlog

from app.enums import RecordingState, SourceKind
from app.net.runner import Runner
from app.recorder.ffmpeg import (
    SEGMENT_GLOB,
    SEGMENT_LIST,
    ExitReason,
    capture_argv,
    explain_exit,
    parse_segment_list,
)
from app.security.redaction import StreamUrl

log = structlog.get_logger(__name__)

#: Redial ladder after a drop, in seconds, then held at the last value. The
#: dashboard's socket uses the same shape so the two do not look out of step.
BACKOFF = (1, 2, 4, 8, 15)

#: Consecutive runs that capture nothing before we stop trying. A camera that
#: refuses credentials will refuse them for the whole session, and burning the
#: full duration on it delays the failure the user needs to see.
MAX_EMPTY_RUNS = 5

#: A run shorter than this is not worth starting; the deadline has effectively
#: arrived.
MIN_RUN_SECONDS = 2.0

#: How long past its own ``-t`` budget ffmpeg gets before it is killed.
RUN_GRACE_SECONDS = 20.0

#: No sealed segment for this many segment-lengths means the stream stalled.
#: This is the case a socket timeout misses: the connection is open, ffmpeg is
#: running, and no frames are arriving.
STALL_FACTOR = 3

#: Lines of ffmpeg stderr kept for the failure message.
STDERR_LINES = 25

_POLL_SECONDS = 1.0


# ---- what a capture needs, and what it reports -------------------------


@dataclass(slots=True)
class OpenPath:
    """A stream URL that is ready to be handed to ffmpeg, and the runner that
    can reach it -- namespaced for a VPN profile, plain for a direct one."""

    url: StreamUrl
    runner: Runner
    detail: str = ""


class SourcePath(Protocol):
    """Everything the supervisor needs to reach one camera source, and to work
    out which hop failed when it can no longer reach it."""

    source_id: str
    kind: SourceKind

    async def open(self) -> OpenPath:
        """Establish the path -- dial, forward, whatever this source needs."""

    async def diagnose(self) -> tuple[str, str]:
        """Which layer is down right now: ``(cause, detail)``.

        ``cause`` is one of ``vpn``, ``ssh``, ``camera``, ``unknown``.
        """

    async def close(self) -> None: ...


@dataclass(slots=True)
class SegmentRecord:
    source_kind: SourceKind
    sequence: int
    path: str
    started_at: datetime
    duration_seconds: float
    bytes: int


@dataclass
class GapRecord:
    """An outage, from the moment the stream stopped to the moment it resumed.

    Held open while the supervisor redials, so a gap that is still open is
    visible in the UI as it happens rather than after the fact.
    """

    source_kind: SourceKind
    started_at: datetime
    ended_at: datetime | None = None
    seconds: float = 0.0
    cause: str = "unknown"
    detail: str = ""
    redial_attempts: int = 0
    key: str = field(default_factory=lambda: str(uuid4()))

    def close(self, at: datetime) -> None:
        self.ended_at = at
        self.seconds = round(max((at - self.started_at).total_seconds(), 0.0), 3)


@dataclass
class CaptureReport:
    source_kind: SourceKind
    directory: Path
    segments: list[SegmentRecord] = field(default_factory=list)
    gaps: list[GapRecord] = field(default_factory=list)
    failure: str = ""

    @property
    def captured_seconds(self) -> float:
        return round(sum(s.duration_seconds for s in self.segments), 3)

    @property
    def gap_seconds(self) -> float:
        return round(sum(g.seconds for g in self.gaps), 3)

    @property
    def bytes(self) -> int:
        return sum(s.bytes for s in self.segments)

    @property
    def ok(self) -> bool:
        return bool(self.segments)


@dataclass
class SessionReport:
    recording_id: str
    started_at: datetime
    finished_at: datetime
    captures: list[CaptureReport]
    stopped_early: bool = False

    @property
    def ok(self) -> bool:
        return any(c.ok for c in self.captures)

    @property
    def captured_seconds(self) -> float:
        """The session's own length, not the sum of its sources.

        Two sources of the same camera cover the same stretch of wall clock;
        adding them would report ten minutes for a five-minute session.
        """
        return max((c.captured_seconds for c in self.captures), default=0.0)

    @property
    def gap_seconds(self) -> float:
        return max((c.gap_seconds for c in self.captures), default=0.0)

    @property
    def bytes(self) -> int:
        return sum(c.bytes for c in self.captures)

    @property
    def failure_reason(self) -> str:
        failures = [f"{c.source_kind.value}: {c.failure}" for c in self.captures if c.failure]
        return "; ".join(failures)


class SessionSink(Protocol):
    """Where a session reports what it is doing. The worker implements this
    against the database and the event bus; tests collect into a list."""

    async def state(self, state: RecordingState, detail: str) -> None: ...
    async def segment(self, segment: SegmentRecord) -> None: ...
    async def gap_opened(self, gap: GapRecord) -> None: ...
    async def gap_closed(self, gap: GapRecord) -> None: ...


class NullSink:
    """Collects instead of persisting. The default, and what tests assert on."""

    def __init__(self) -> None:
        self.states: list[tuple[RecordingState, str]] = []
        self.segments: list[SegmentRecord] = []
        self.gaps: list[GapRecord] = []

    async def state(self, state: RecordingState, detail: str) -> None:
        self.states.append((state, detail))

    async def segment(self, segment: SegmentRecord) -> None:
        self.segments.append(segment)

    async def gap_opened(self, gap: GapRecord) -> None:
        self.gaps.append(gap)

    async def gap_closed(self, gap: GapRecord) -> None:
        return None


Now = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


# ---- one source ---------------------------------------------------------


class SourceCapture:
    """Keeps one source recording until its deadline, across as many outages as
    the deadline leaves room for."""

    def __init__(
        self,
        *,
        path: SourcePath,
        directory: Path,
        deadline: datetime,
        segment_seconds: int,
        sink: SessionSink | None = None,
        now: Now | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self.path = path
        self.directory = Path(directory)
        self.deadline = deadline
        self.segment_seconds = segment_seconds
        self.sink: SessionSink = sink or NullSink()
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep

        self.report = CaptureReport(source_kind=path.kind, directory=self.directory)
        self._sequence = 0
        self._sealed_in_run = 0
        self._stopping = False
        self._process: asyncio.subprocess.Process | None = None

    # ---- lifecycle -----------------------------------------------------

    def stop(self) -> None:
        """Wind up at the next opportunity, keeping what is already on disk."""
        self._stopping = True
        self._terminate()

    def _terminate(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()

    async def run(self) -> CaptureReport:
        await asyncio.to_thread(self.directory.mkdir, parents=True, exist_ok=True)
        gap: GapRecord | None = None
        empty_runs = 0
        run_index = 0
        last_reason = ExitReason("the capture never started")

        try:
            while not self._stopping:
                remaining = (self.deadline - self._now()).total_seconds()
                if remaining < MIN_RUN_SECONDS:
                    break

                try:
                    opened = await self.path.open()
                except Exception as exc:  # noqa: BLE001 - any failure to open is a retry
                    last_reason = ExitReason(_message_for(exc), "unknown")
                    log.warning("capture.open_failed", source=self.path.source_id, error=str(exc))
                    empty_runs += 1
                    if empty_runs >= MAX_EMPTY_RUNS:
                        break
                    gap = await self._widen(gap, last_reason)
                    if not await self._backoff(gap, empty_runs):
                        break
                    continue

                if gap is not None:
                    await self._close_gap(gap)
                    gap = None
                await self.sink.state(RecordingState.RECORDING, opened.detail or "capturing")

                # Re-read the clock: opening the path may have meant a 45-second
                # VPN dial, and the budget ffmpeg is given has to be what is left
                # after it rather than what was left before it.
                remaining = (self.deadline - self._now()).total_seconds()
                if remaining < MIN_RUN_SECONDS:
                    break

                before = len(self.report.segments)
                run_dir = self.directory / f"run-{run_index:03d}"
                run_index += 1
                last_reason = await self._capture_once(opened, run_dir, remaining)
                captured_anything = len(self.report.segments) > before

                empty_runs = 0 if captured_anything else empty_runs + 1
                if self._stopping:
                    break
                if (self.deadline - self._now()).total_seconds() < MIN_RUN_SECONDS:
                    break
                if empty_runs >= MAX_EMPTY_RUNS:
                    break

                # The deadline has not arrived, so the stream went away.
                gap = await self._widen(gap, last_reason)
                if not await self._backoff(gap, empty_runs):
                    break
        finally:
            self._terminate()
            await self.path.close()

        if gap is not None:
            # Still open when the deadline arrived: the outage ran to the end.
            await self._close_gap(gap)

        if not self.report.segments:
            self.report.failure = last_reason.message
        return self.report

    # ---- one run of ffmpeg ---------------------------------------------

    async def _capture_once(self, opened: OpenPath, run_dir: Path, seconds: float) -> ExitReason:
        await asyncio.to_thread(run_dir.mkdir, parents=True, exist_ok=True)
        self._sealed_in_run = 0
        argv = capture_argv(
            opened.url,
            self.path.kind,
            run_dir,
            seconds=seconds,
            segment_seconds=self.segment_seconds,
        )

        started = self._now()
        process = await opened.runner.spawn(argv)
        self._process = process
        tail: deque[str] = deque(maxlen=STDERR_LINES)

        drainer = asyncio.create_task(_drain(process, tail))
        watcher = asyncio.create_task(self._watch(process, run_dir, started))
        waiter = asyncio.create_task(process.wait())
        try:
            returncode = await asyncio.wait_for(waiter, timeout=seconds + RUN_GRACE_SECONDS)
        except TimeoutError:
            # ffmpeg ignored its own -t. Nothing here waits on a process that
            # will not exit.
            log.warning("capture.overran", source=self.path.source_id, budget=seconds)
            process.kill()
            returncode = await waiter
        finally:
            for task in (watcher, drainer):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self._process = None

        await self._seal(run_dir, started, final=True)
        reason = explain_exit(returncode or 0, "\n".join(tail))
        log.info(
            "capture.run_ended",
            source=self.path.source_id,
            returncode=returncode,
            segments=len(self.report.segments),
            reason=reason.message,
        )
        return reason

    async def _watch(
        self, process: asyncio.subprocess.Process, run_dir: Path, started: datetime
    ) -> None:
        """Seal segments as ffmpeg closes them, and kill a stream that stalls.

        The stall check is the reason this polls rather than waiting on the
        process: a camera that stops sending frames leaves ffmpeg alive and
        silent, and only the segment list stops moving.
        """
        stall_after = self.segment_seconds * STALL_FACTOR
        last_progress = self._now()
        while True:
            await self._sleep(_POLL_SECONDS)
            sealed = await self._seal(run_dir, started)
            now = self._now()
            if sealed:
                last_progress = now
                continue
            if (now - last_progress).total_seconds() >= stall_after:
                log.warning("capture.stalled", source=self.path.source_id, seconds=int(stall_after))
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                return

    async def _seal(self, run_dir: Path, run_started: datetime, *, final: bool = False) -> int:
        """Turn newly closed segment files into records. Returns how many.

        The directory is read in one hop off the event loop: this runs every
        second for every source of every live recording, and a stalled NFS mount
        under the work volume must not be able to stop the other recordings.
        """
        scan = await asyncio.to_thread(_scan_run, run_dir)
        sealed = parse_segment_list(scan.listing)

        new = 0
        for entry in sealed[self._sealed_in_run :]:
            await self._add_segment(
                run_dir / entry.filename,
                scan.sizes.get(entry.filename, 0),
                run_started + timedelta(seconds=entry.start),
                entry.duration,
            )
            new += 1
        self._sealed_in_run = len(sealed)

        if final:
            # Whatever ffmpeg was mid-way through when it died is still playable
            # -- that is why segments are mpegts -- so it is kept rather than
            # discarded. Its length is what is left of the run's wall clock.
            named = {entry.filename for entry in sealed}
            elapsed = (self._now() - run_started).total_seconds()
            accounted = sum(entry.duration for entry in sealed)
            for name, size in sorted(scan.sizes.items()):
                if name in named or size == 0:
                    continue
                await self._add_segment(
                    run_dir / name,
                    size,
                    run_started + timedelta(seconds=accounted),
                    round(max(elapsed - accounted, 0.0), 3),
                )
                new += 1
        return new

    async def _add_segment(
        self, path: Path, size: int, started_at: datetime, duration: float
    ) -> None:
        record = SegmentRecord(
            source_kind=self.path.kind,
            sequence=self._sequence,
            path=str(path),
            started_at=started_at,
            duration_seconds=duration,
            bytes=size,
        )
        self._sequence += 1
        self.report.segments.append(record)
        await self.sink.segment(record)

    # ---- outages -------------------------------------------------------

    async def _widen(self, gap: GapRecord | None, reason: ExitReason) -> GapRecord:
        """Open a gap, or keep widening the one already open.

        A second failure inside the same outage is the same outage. Opening a
        new gap per attempt would turn one dropped VPN into five, and the count
        of attempts is already on the gap.
        """
        if gap is not None:
            return gap

        cause, detail = await self._attribute(reason)
        gap = GapRecord(
            source_kind=self.path.kind,
            started_at=self._now(),
            cause=cause,
            detail=detail or reason.message,
        )
        self.report.gaps.append(gap)
        await self.sink.gap_opened(gap)
        await self.sink.state(RecordingState.RECOVERING, f"{reason.message} ({cause})")
        log.warning("capture.gap_opened", source=self.path.source_id, cause=cause)
        return gap

    async def _attribute(self, reason: ExitReason) -> tuple[str, str]:
        """Name the layer that went away.

        ffmpeg only ever reports what its own socket saw, and a dropped VPN, a
        dropped SSH master and an unplugged camera all look the same from there.
        So the path is probed for a second opinion, and ffmpeg's guess is the
        fallback rather than the answer.
        """
        try:
            cause, detail = await self.path.diagnose()
        except Exception as exc:  # noqa: BLE001 - diagnosis must not end a recording
            return reason.suspect, f"{reason.message} (diagnosis failed: {exc})"
        if cause and cause != "unknown":
            return cause, detail or reason.message
        return reason.suspect, detail or reason.message

    async def _close_gap(self, gap: GapRecord) -> None:
        gap.close(self._now())
        await self.sink.gap_closed(gap)
        log.info("capture.gap_closed", source=self.path.source_id, seconds=gap.seconds)

    async def _backoff(self, gap: GapRecord, attempt: int) -> bool:
        """Wait before redialling. False when the deadline arrives first."""
        delay = BACKOFF[min(attempt, len(BACKOFF)) - 1] if attempt else BACKOFF[0]
        remaining = (self.deadline - self._now()).total_seconds()
        if remaining <= delay + MIN_RUN_SECONDS:
            return False
        gap.redial_attempts += 1
        await self._sleep(delay)
        return not self._stopping


@dataclass(slots=True)
class _RunScan:
    """One run directory as it is on disk right now."""

    listing: str
    sizes: dict[str, int]


def _scan_run(run_dir: Path) -> _RunScan:
    """Read the segment list and the segment sizes together. Synchronous by
    design -- the caller runs it on a worker thread."""
    listing = run_dir / SEGMENT_LIST
    text = listing.read_text() if listing.exists() else ""
    sizes: dict[str, int] = {}
    for path in run_dir.glob(SEGMENT_GLOB):
        try:
            sizes[path.name] = path.stat().st_size
        except OSError:
            continue
    return _RunScan(listing=text, sizes=sizes)


async def _drain(process: asyncio.subprocess.Process, tail: deque[str]) -> None:
    """Keep reading stderr. A full pipe stops ffmpeg mid-recording, so this runs
    for the life of the process whether or not anyone reads the result."""
    if process.stderr is None:
        return
    while True:
        line = await process.stderr.readline()
        if not line:
            return
        tail.append(line.decode(errors="replace").rstrip())


def _message_for(exc: Exception) -> str:
    return getattr(exc, "user_message", None) or str(exc) or type(exc).__name__


# ---- the whole recording ------------------------------------------------


class RecordingSession:
    """One recording: every source of one camera, captured concurrently against
    a shared deadline."""

    def __init__(
        self,
        *,
        recording_id: str,
        paths: Sequence[SourcePath],
        work_dir: Path | str,
        requested_seconds: int,
        segment_seconds: int,
        sink: SessionSink | None = None,
        now: Now | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self.recording_id = recording_id
        self.paths = paths
        self.work_dir = Path(work_dir) / recording_id
        self.requested_seconds = requested_seconds
        self.segment_seconds = segment_seconds
        self.sink: SessionSink = sink or NullSink()
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep
        self._captures: list[SourceCapture] = []
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True
        for capture in self._captures:
            capture.stop()

    async def run(self) -> SessionReport:
        started = self._now()
        deadline = started + timedelta(seconds=self.requested_seconds)
        self._captures = [
            SourceCapture(
                path=path,
                directory=self.work_dir / path.kind.value,
                deadline=deadline,
                segment_seconds=self.segment_seconds,
                sink=self.sink,
                now=self._now,
                sleep=self._sleep,
            )
            for path in self.paths
        ]
        if self._stopping:
            for capture in self._captures:
                capture.stop()

        await self.sink.state(RecordingState.RECORDING, "capture started")
        reports = await asyncio.gather(
            *(capture.run() for capture in self._captures), return_exceptions=True
        )

        captures: list[CaptureReport] = []
        for capture, result in zip(self._captures, reports, strict=True):
            if isinstance(result, BaseException):
                log.exception("capture.crashed", source=capture.path.source_id, error=str(result))
                capture.report.failure = capture.report.failure or _message_for(
                    result if isinstance(result, Exception) else Exception(str(result))
                )
                captures.append(capture.report)
            else:
                captures.append(result)

        return SessionReport(
            recording_id=self.recording_id,
            started_at=started,
            finished_at=self._now(),
            captures=captures,
            stopped_early=self._stopping,
        )
