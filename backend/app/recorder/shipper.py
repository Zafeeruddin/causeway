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
