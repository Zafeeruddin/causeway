"""Recordings and downloads.

The admission check runs here, before anything is queued. On Versity there is no
bucket quota behind us, so this is the only place a recording that will not fit
gets stopped -- and stopping it here costs a message, while discovering it
mid-write costs a truncated file and overshoots anyway.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth import current_principal
from app.api.deps import load_recording
from app.api.schemas import (
    AdmissionOut,
    AlignmentOut,
    ComparisonOut,
    DownloadLink,
    GapMarkOut,
    RecordingCreate,
    RecordingOut,
    SpanOut,
    StorageUsage,
    TrackOut,
)
from app.config import settings
from app.db import Principal, get_session, scoped_select
from app.enums import RecordingState, SourceKind
from app.models import Camera, Gap, Recording, Segment, StorageObject
from app.services.audit import record as audit
from app.services.playback import build_spans, gap_marks, origin_of, window_seconds
from app.storage.client import StorageError, object_store
from app.storage.keys import download_name
from app.storage.retention import StoragePolicy, admit, estimate_bytes, usage_state

router = APIRouter(prefix="/api", tags=["recordings"])

DOWNLOAD_TTL = 3600

#: How closely the two feeds can honestly be lined up. The spans are exact, but
#: a span starts when ffmpeg started, not when the first frame arrived, and RTSP
#: and HLS do not buffer alike. Quoted to the user rather than hidden.
ALIGNMENT_ACCURACY = 2.0


@router.get("/recordings", response_model=list[RecordingOut])
async def list_recordings(
    limit: int = 100,
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> list[RecordingOut]:
    rows = await db.execute(
        scoped_select(Recording, principal).order_by(Recording.created_at.desc()).limit(limit)
    )
    return [RecordingOut.model_validate(r) for r in rows.scalars().all()]


@router.post("/recordings", response_model=list[RecordingOut], status_code=status.HTTP_202_ACCEPTED)
async def start_recording(
    body: RecordingCreate,
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> list[RecordingOut]:
    cfg = settings()
    if len(body.camera_ids) > cfg.max_streams_per_user:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"You can record at most {cfg.max_streams_per_user} cameras at once.",
        )

    rows = await db.execute(
        scoped_select(Camera, principal)
        .where(Camera.id.in_(body.camera_ids))
        .options(selectinload(Camera.sources))
    )
    cameras = list(rows.scalars().all())
    if len(cameras) != len(set(body.camera_ids)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")

    # Size the whole batch, not each recording in turn: five cameras that each
    # fit individually can still overrun the cap together.
    source_count = sum(
        len([s for s in c.sources if not body.sources or SourceKind(s.kind) in body.sources])
        for c in cameras
    )
    if source_count == 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "None of those cameras have a source of the kind you asked for.",
        )

    policy = StoragePolicy.from_settings()
    used = await _used_bytes(db)
    estimate = estimate_bytes(body.seconds, source_count=source_count)
    decision = admit(used, estimate, policy)
    if not decision.allowed:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, decision.reason)

    created: list[Recording] = []
    for camera in cameras:
        recording = Recording(
            team_id=camera.team_id,
            camera_id=camera.id,
            requested_by=principal.user_id,
            requested_seconds=body.seconds,
            state=RecordingState.QUEUED,
        )
        db.add(recording)
        created.append(recording)

    await db.flush()
    await audit(
        db,
        principal,
        "recording.start",
        "recording",
        None,
        {"cameras": len(cameras), "seconds": body.seconds, "estimated_bytes": estimate},
        team_id=cameras[0].team_id,
    )
    return [RecordingOut.model_validate(r) for r in created]


@router.post("/recordings/estimate", response_model=AdmissionOut)
async def estimate(
    body: RecordingCreate,
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> AdmissionOut:
    """What this recording would cost, and whether it would be allowed.

    The dashboard calls this as the duration slider moves, so the refusal is
    visible before anyone presses record.
    """
    rows = await db.execute(
        scoped_select(Camera, principal)
        .where(Camera.id.in_(body.camera_ids))
        .options(selectinload(Camera.sources))
    )
    cameras = list(rows.scalars().all())
    source_count = sum(len(c.sources) for c in cameras) or 1

    policy = StoragePolicy.from_settings()
    used = await _used_bytes(db)
    decision = admit(used, estimate_bytes(body.seconds, source_count=source_count), policy)
    return AdmissionOut(
        allowed=decision.allowed,
        reason=decision.reason,
        estimated_bytes=decision.estimated_bytes,
        headroom_bytes=decision.headroom_bytes,
    )


@router.get("/recordings/{recording_id}", response_model=RecordingOut)
async def get_recording(recording: Recording = Depends(load_recording)) -> RecordingOut:
    return RecordingOut.model_validate(recording)


@router.get("/recordings/{recording_id}/downloads", response_model=list[DownloadLink])
async def downloads(
    recording: Recording = Depends(load_recording),
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> list[DownloadLink]:
    """Presigned links, one per source.

    The browser fetches from Versity directly rather than streaming a gigabyte
    back through this process.
    """
    rows = await db.execute(
        select(StorageObject).where(
            StorageObject.recording_id == recording.id, StorageObject.deleted_at.is_(None)
        )
    )
    objects = list(rows.scalars().all())
    if not objects:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This recording has nothing stored yet."
            if recording.state != RecordingState.COMPLETE
            else "The files for this recording are gone - it may have been cleared by retention.",
        )

    # The camera and the moment, so the file is identifiable once it is out of
    # here and sitting in a downloads folder next to five others.
    camera = await db.get(Camera, recording.camera_id)
    started = recording.started_at or recording.created_at

    store = object_store()
    links: list[DownloadLink] = []
    for obj in objects:
        filename = download_name(
            camera.name if camera else "camera",
            started,
            SourceKind(obj.source_kind) if obj.source_kind else None,
            obj.s3_key,
        )
        try:
            url = await store.presign(obj.s3_key, expires=DOWNLOAD_TTL, filename=filename)
        except StorageError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.user_message) from exc
        links.append(
            DownloadLink(
                source_kind=SourceKind(obj.source_kind) if obj.source_kind else None,
                filename=filename,
                bytes=obj.bytes,
                url=url,
                expires_in=DOWNLOAD_TTL,
            )
        )

    await audit(
        db,
        principal,
        "recording.download",
        "recording",
        recording.id,
        {"files": len(links)},
        team_id=recording.team_id,
    )
    return links


@router.get("/recordings/{recording_id}/comparison", response_model=ComparisonOut)
async def comparison(
    recording: Recording = Depends(load_recording),
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> ComparisonOut:
    """Everything the side-by-side view needs, in one request.

    Both feeds, their gaps, and the timeline that maps a moment to a position in
    each file. The mapping is the part that matters: the session file is a
    concatenation of what was captured, so after an outage the two feeds sit at
    different media times for the same moment and seeking both to the same
    number silently compares the wrong frames.
    """
    rows = await db.execute(
        select(StorageObject).where(
            StorageObject.recording_id == recording.id,
            StorageObject.deleted_at.is_(None),
            StorageObject.source_kind.is_not(None),
        )
    )
    objects = list(rows.scalars().all())
    if not objects:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This recording has nothing to play yet."
            if recording.state != RecordingState.COMPLETE
            else "The files for this recording are gone - it may have been cleared by retention.",
        )

    segments = list(
        (await db.execute(select(Segment).where(Segment.recording_id == recording.id)))
        .scalars()
        .all()
    )
    origin = origin_of(segments)
    if origin is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This recording captured nothing that can be played."
        )

    gaps = list(
        (await db.execute(select(Gap).where(Gap.recording_id == recording.id))).scalars().all()
    )
    camera = await db.get(Camera, recording.camera_id)
    store = object_store()

    spans_by_kind = {
        kind: build_spans([s for s in segments if s.source_kind == kind], origin)
        for kind in {SourceKind(s.source_kind) for s in segments}
    }
    window = window_seconds(list(spans_by_kind.values()))

    # Raw feed first: the comparison reads left to right, camera then inference.
    order = {SourceKind.RTSP: 0, SourceKind.HLS: 1}
    playable = [(SourceKind(obj.source_kind), obj) for obj in objects if obj.source_kind]
    tracks: list[TrackOut] = []
    for kind, obj in sorted(playable, key=lambda pair: order.get(pair[0], 9)):
        spans = spans_by_kind.get(kind, [])
        try:
            # No attachment disposition here: these URLs go into a <video>, and
            # the download button is the other endpoint.
            url = await store.presign(obj.s3_key, expires=DOWNLOAD_TTL)
        except StorageError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.user_message) from exc
        marks = gap_marks([g for g in gaps if g.source_kind == kind], origin, window=window)
        tracks.append(
            TrackOut(
                source_kind=kind,
                url=url,
                expires_in=DOWNLOAD_TTL,
                bytes=obj.bytes,
                captured_seconds=round(sum(span.seconds for span in spans), 3),
                gap_seconds=round(sum(mark.seconds for mark in marks), 3),
                starts_at=spans[0].wall_start if spans else 0.0,
                spans=[SpanOut(**asdict(span)) for span in spans],
                gaps=[GapMarkOut(**asdict(mark)) for mark in marks],
            )
        )

    return ComparisonOut(
        recording_id=recording.id,
        camera_id=recording.camera_id,
        camera_name=camera.name if camera else "",
        state=RecordingState(recording.state),
        requested_seconds=recording.requested_seconds,
        origin=origin,
        window_seconds=window,
        tracks=tracks,
        alignment=AlignmentOut(
            accuracy_seconds=ALIGNMENT_ACCURACY,
            note=(
                "Aligned by wall clock, to about "
                f"{ALIGNMENT_ACCURACY:.0f} seconds. Each feed's own outages are "
                "accounted for. What is not: the delay the inference pipeline adds "
                "before it publishes a segment. Use the trim control to take that out."
            ),
        ),
    )


@router.get("/storage/usage", response_model=StorageUsage)
async def storage_usage(
    db: AsyncSession = Depends(get_session), principal: Principal = Depends(current_principal)
) -> StorageUsage:
    policy = StoragePolicy.from_settings()
    used = await _used_bytes(db)
    state = usage_state(used, policy)

    rows = await db.execute(
        select(StorageObject.team_id, func.coalesce(func.sum(StorageObject.bytes), 0))
        .where(StorageObject.deleted_at.is_(None))
        .group_by(StorageObject.team_id)
    )
    by_team = {team: total for team, total in rows.all() if principal.may_see(team)}

    messages = {
        "ok": "",
        "warning": "The oldest recordings are marked for deletion in 7 days unless space frees up.",
        "collecting": "Oldest recordings are being removed to make room.",
        "full": "Storage is full. New recordings are refused until space frees up.",
    }
    return StorageUsage(
        used_bytes=used,
        warn_bytes=policy.warn_bytes,
        gc_bytes=policy.gc_bytes,
        hard_bytes=policy.hard_bytes,
        state=state,
        message=messages[state],
        by_team=by_team,
    )


async def _used_bytes(db: AsyncSession) -> int:
    """Total live bytes, from our own accounting.

    Deliberately not a bucket listing: the admission check runs on every
    recording, and it must not depend on a round trip that walks every object.
    Drift between this and the gateway is reconciled by a separate job.
    """
    result = await db.execute(
        select(func.coalesce(func.sum(StorageObject.bytes), 0)).where(
            StorageObject.deleted_at.is_(None)
        )
    )
    return int(result.scalar_one())
