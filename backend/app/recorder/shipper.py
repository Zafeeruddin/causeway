"""Turning a finished session into objects in the bucket.

Deliberately outside every network namespace. The segments were captured inside
one; the upload is not, for two reasons the README states and this module has to
honour: an upload started in a namespace dies when the tunnel does, and a
customer VPN advertising ``10.0.0.0/8`` will happily swallow a storage host at
``10.x.x.x``. So concatenation uses a plain runner and the upload uses the
in-process S3 client, both in the agent's own network.

Nothing is deleted from the work volume until the object it came from is in the
bucket. A failed upload leaves the segments where they are, which is the whole
reason they were written to disk first.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import structlog

from app.enums import SourceKind
from app.net.runner import LocalRunner, Runner
from app.recorder.ffmpeg import CONCAT_LIST, SESSION_FILE, concat_argv, concat_list
from app.recorder.session import CaptureReport, SessionReport
from app.storage.client import ObjectStore, StorageError, object_store
from app.storage.keys import content_type_for, sidecar_key, source_key

log = structlog.get_logger(__name__)

GAPS_FILE = "gaps.json"

#: Joining a few hundred sealed segments is a stream copy, but a slow work
#: volume can still make it take a while.
CONCAT_TIMEOUT = 600.0


@dataclass(slots=True)
class ShippedObject:
    key: str
    bytes: int
    content_type: str
    #: None for session-level sidecars, which belong to no single source.
    source_kind: SourceKind | None = None


@dataclass
class ShipResult:
    objects: list[ShippedObject] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return sum(obj.bytes for obj in self.objects)

    @property
    def ok(self) -> bool:
        return any(obj.source_kind is not None for obj in self.objects)


def gaps_document(report: SessionReport, *, requested_seconds: int) -> dict:
    """The sidecar that explains the file.

    It travels with the recording rather than living only in our database,
    because the person who downloads a short file a month from now is the person
    who needs to know why it is short.
    """
    return {
        "recording_id": report.recording_id,
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "requested_seconds": requested_seconds,
        "captured_seconds": report.captured_seconds,
        "gap_seconds": report.gap_seconds,
        "stopped_early": report.stopped_early,
        "sources": [
            {
                "kind": capture.source_kind.value,
                "segments": len(capture.segments),
                "captured_seconds": capture.captured_seconds,
                "gap_seconds": capture.gap_seconds,
                "failure": capture.failure,
                "gaps": [
                    {
                        "started_at": gap.started_at.isoformat(),
                        "ended_at": gap.ended_at.isoformat() if gap.ended_at else None,
                        "seconds": gap.seconds,
                        "cause": gap.cause,
                        "detail": gap.detail,
                        "redial_attempts": gap.redial_attempts,
                    }
                    for gap in capture.gaps
                ],
            }
            for capture in report.captures
        ],
    }


async def ship(
    report: SessionReport,
    *,
    team_slug: str,
    requested_seconds: int,
    store: ObjectStore | None = None,
    runner: Runner | None = None,
) -> ShipResult:
    """Concatenate, upload, and say what landed.

    One source failing does not stop the other: a session where RTSP recorded
    and HLS did not is worth keeping, and the sidecar says which is which.
    """
    store = store or object_store()
    runner = runner or LocalRunner()
    result = ShipResult()

    for capture in report.captures:
        if not capture.segments:
            continue
        try:
            path = await _join(capture, runner)
        except RuntimeError as exc:
            result.failures.append(f"{capture.source_kind.value}: {exc}")
            log.error("ship.join_failed", kind=capture.source_kind.value, error=str(exc))
            continue
        key = source_key(
            team_slug, report.recording_id, report.started_at, capture.source_kind, SESSION_FILE
        )
        shipped, failure = await _upload(store, path, key, capture.source_kind)
        if shipped is None:
            # The storage layer's own words. "upload failed" tells the person
            # reading it nothing they can act on; "the recording store could not
            # be reached" tells them where to look.
            result.failures.append(f"{capture.source_kind.value}: {failure}")
            continue
        result.objects.append(shipped)

    sidecar = await _write_sidecar(report, requested_seconds)
    shipped, _ = await _upload(
        store,
        sidecar,
        sidecar_key(team_slug, report.recording_id, report.started_at, GAPS_FILE),
        None,
    )
    if shipped is not None:
        result.objects.append(shipped)

    return result


async def reship(
    directory: Path,
    *,
    team_slug: str,
    recording_id: str,
    started_at: datetime,
    store: ObjectStore | None = None,
    runner: Runner | None = None,
) -> ShipResult:
    """Send a session whose files are already sitting on the work volume.

    A recording that failed because the store could not be reached has usually
    done all of the expensive work already: the segments are sealed, ffmpeg has
    joined them into ``session.mp4`` and the sidecar is written. Only the PUT
    failed. Recovering it is an upload, not a recapture -- so this sends what is
    on disk rather than rebuilding a SessionReport that stopped existing when
    the process that held it exited.

    Joining is repeated only when ``session.mp4`` is absent, which means the
    failure landed early enough to interrupt the concatenation.
    """
    store = store or object_store()
    runner = runner or LocalRunner()
    result = ShipResult()

    for kind in SourceKind:
        source_dir = directory / kind.value
        if not await asyncio.to_thread(source_dir.is_dir):
            continue
        session = source_dir / SESSION_FILE
        if not await asyncio.to_thread(session.exists):
            try:
                session = await _rejoin(source_dir, runner)
            except RuntimeError as exc:
                result.failures.append(f"{kind.value}: {exc}")
                log.error("reship.join_failed", kind=kind.value, error=str(exc))
                continue
        key = source_key(team_slug, recording_id, started_at, kind, SESSION_FILE)
        shipped, failure = await _upload(store, session, key, kind)
        if shipped is None:
            result.failures.append(f"{kind.value}: {failure}")
            continue
        result.objects.append(shipped)

    # The sidecar was written before the upload that failed, so it is normally
    # already here. It is not worth failing a recovery over if it is not.
    sidecar = directory / GAPS_FILE
    if await asyncio.to_thread(sidecar.exists):
        shipped, _ = await _upload(
            store, sidecar, sidecar_key(team_slug, recording_id, started_at, GAPS_FILE), None
        )
        if shipped is not None:
            result.objects.append(shipped)

    return result


async def discard(directory: Path | str) -> None:
    """Remove a session's work directory. Only ever called once its objects are
    in the bucket."""
    await asyncio.to_thread(shutil.rmtree, Path(directory), ignore_errors=True)


# ---- internals ----------------------------------------------------------


async def _join(capture: CaptureReport, runner: Runner) -> Path:
    """Join one source's segments into a single MP4."""
    ordered = sorted(capture.segments, key=lambda s: s.sequence)
    list_file = capture.directory / CONCAT_LIST
    output = capture.directory / SESSION_FILE
    await asyncio.to_thread(
        list_file.write_text, concat_list([segment.path for segment in ordered])
    )

    result = await runner.run(concat_argv(list_file, output), timeout=CONCAT_TIMEOUT)
    exists = await asyncio.to_thread(output.exists)
    if not result.ok or not exists:
        raise RuntimeError(
            result.output.strip().splitlines()[-1][:200]
            if result.output.strip()
            else f"ffmpeg exited {result.returncode} while joining segments"
        )
    return output


async def _rejoin(source_dir: Path, runner: Runner) -> Path:
    """Join the raw segments of one source when ``session.mp4`` is not there.

    Ordered lexically rather than by a database row: segments are named
    ``run-NNN/seg-NNNNN.ts`` precisely so that lexical order is capture order,
    including across the redials that open a new run directory.
    """
    found = await asyncio.to_thread(lambda: sorted(str(p) for p in source_dir.glob("run-*/seg-*")))
    if not found:
        raise RuntimeError("no segments left on the work volume")
    list_file = source_dir / CONCAT_LIST
    output = source_dir / SESSION_FILE
    await asyncio.to_thread(list_file.write_text, concat_list(found))

    result = await runner.run(concat_argv(list_file, output), timeout=CONCAT_TIMEOUT)
    exists = await asyncio.to_thread(output.exists)
    if not result.ok or not exists:
        raise RuntimeError(
            result.output.strip().splitlines()[-1][:200]
            if result.output.strip()
            else f"ffmpeg exited {result.returncode} while joining segments"
        )
    return output


async def _write_sidecar(report: SessionReport, requested_seconds: int) -> Path:
    document = gaps_document(report, requested_seconds=requested_seconds)
    # One level above the per-source directories: the sidecar describes the
    # session, not a source.
    directory = report.captures[0].directory.parent if report.captures else Path(".")
    path = directory / GAPS_FILE
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_text, json.dumps(document, indent=2))
    return path


async def _upload(
    store: ObjectStore, path: Path, key: str, kind: SourceKind | None
) -> tuple[ShippedObject | None, str]:
    """Upload one file. Returns what landed, or why it did not."""
    try:
        stored = await store.put_file(path, key)
    except StorageError as exc:
        log.error("ship.upload_failed", key=key, error=str(exc))
        return None, exc.user_message
    log.info("ship.uploaded", key=key, bytes=stored.bytes)
    return ShippedObject(
        key=stored.key,
        bytes=stored.bytes,
        content_type=stored.content_type or content_type_for(path.name),
        source_kind=kind,
    ), ""
