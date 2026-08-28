"""Cameras: add one, paste a block, upload a CSV, and test what you added."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth import current_principal
from app.api.deps import NOT_FOUND, gateway, load_camera, previews
from app.api.schemas import (
    CameraCreate,
    CameraOut,
    GateResultOut,
    ImportIssueOut,
    ImportPreview,
    ImportRequest,
    ImportResponse,
    PreviewOut,
    PreviewStart,
)
from app.config import settings
from app.db import Principal, get_session, scoped_select
from app.models import Camera, CameraSource, ConnectionProfile
from app.security.secrets import secrets_backend
from app.services.audit import record
from app.services.camera_import import (
    InvalidStreamUrl,
    ParsedCamera,
    parse_csv,
    parse_pasted,
    parse_source_url,
)
from app.services.gateway import ConnectionGateway
from app.services.preview import PreviewGateway, PreviewInfo, whep_url

router = APIRouter(prefix="/api/cameras", tags=["cameras"])

#: Uploading a whole NVR export by mistake should fail fast, not fill the disk.
MAX_CSV_BYTES = 2 * 1024 * 1024


@router.get("", response_model=list[CameraOut])
async def list_cameras(
    db: AsyncSession = Depends(get_session), principal: Principal = Depends(current_principal)
) -> list[CameraOut]:
    rows = await db.execute(
        scoped_select(Camera, principal).options(selectinload(Camera.sources)).order_by(Camera.name)
    )
    return [CameraOut.model_validate(c) for c in rows.scalars().all()]


@router.post("", response_model=CameraOut, status_code=status.HTTP_201_CREATED)
async def create_camera(
    body: CameraCreate,
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> CameraOut:
    await _check_profile(db, principal, body.team_id, body.profile_id)

    camera = Camera(
        team_id=body.team_id, profile_id=body.profile_id, name=body.name, location=body.location
    )
    secrets = secrets_backend()
    for entry in body.sources:
        try:
            parsed = parse_source_url(entry.url)
        except InvalidStreamUrl as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        if parsed.kind is not entry.kind:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"That URL is a {parsed.kind.value} stream, not {entry.kind.value}.",
            )
        password = entry.password or parsed.password
        camera.sources.append(
            CameraSource(
                kind=parsed.kind,
                url=parsed.url,
                host=parsed.host,
                port=parsed.port,
                username=entry.username or parsed.username,
                password_ref=await secrets.put(password, hint="camera") if password else None,
                uses_profile_path=entry.uses_profile_path,
            )
        )

    db.add(camera)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"This team already has a camera named {body.name!r}."
        ) from exc

    await db.refresh(camera, ["sources"])
    await record(
        db,
        principal,
        "camera.create",
        "camera",
        camera.id,
        {"sources": [s.kind for s in camera.sources]},
        team_id=camera.team_id,
    )
    return CameraOut.model_validate(camera)


@router.post("/import", response_model=ImportResponse)
async def import_pasted(
    body: ImportRequest,
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> ImportResponse:
    """One stream per line, optionally prefixed with a name."""
    await _check_profile(db, principal, body.team_id, body.profile_id)
    report = parse_pasted(body.text)
    return await _persist(db, principal, body.team_id, body.profile_id, body.dry_run, report)


@router.post("/import/csv", response_model=ImportResponse)
async def import_csv(
    team_id: str,
    profile_id: str,
    dry_run: bool = False,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> ImportResponse:
    await _check_profile(db, principal, team_id, profile_id)

    content = await file.read(MAX_CSV_BYTES + 1)
    if len(content) > MAX_CSV_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"That file is larger than {MAX_CSV_BYTES // 1024 // 1024} MB.",
        )

    report = parse_csv(content)
    return await _persist(db, principal, team_id, profile_id, dry_run, report)


@router.post("/{camera_id}/test", response_model=list[GateResultOut])
async def test_camera(
    camera: Camera = Depends(load_camera),
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
    service: ConnectionGateway = Depends(gateway),
) -> list[GateResultOut]:
    """Run the source gates for every source on this camera."""
    profile = await db.get(ConnectionProfile, camera.profile_id)
    if profile is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This camera has no connection profile.")

    results: list[GateResultOut] = []
    for source in camera.sources:
        outcome = await service.test_source(db, profile, camera, source)
        results.extend(GateResultOut(**asdict(g)) for g in outcome.results)

    await record(
        db,
        principal,
        "camera.test",
        "camera",
        camera.id,
        {"sources": len(camera.sources)},
        team_id=camera.team_id,
    )
    return results


def _preview_out(info: PreviewInfo) -> PreviewOut:
    return PreviewOut(
        id=info.id,
        camera_id=info.camera_id,
        source_kind=info.source_kind,
        path=info.path,
        whep_url=whep_url(settings().preview_public_base, info.path),
        started_at=info.started_at,
        expires_at=info.expires_at,
        viewers=info.viewers,
    )


@router.post("/{camera_id}/preview", response_model=PreviewOut)
async def start_preview(
    body: PreviewStart | None = None,
    camera: Camera = Depends(load_camera),
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
    previewer: PreviewGateway = Depends(previews),
) -> PreviewOut:
    """Start watching this camera live.

    The stream is copied, never transcoded, and it stops on its own once nobody
    is watching -- a preview holds a camera session open, and cameras cap those
    hard.
    """
    kind = body.source_kind if body else None
    info = await previewer.start(db, camera, principal.user_id, kind)
    await record(
        db,
        principal,
        "camera.preview",
        "camera",
        camera.id,
        {"source_kind": info.source_kind.value},
        team_id=camera.team_id,
    )
    return _preview_out(info)


@router.delete("/{camera_id}/preview/{preview_id}", status_code=status.HTTP_204_NO_CONTENT)
async def stop_preview(
    preview_id: str,
    camera: Camera = Depends(load_camera),
    principal: Principal = Depends(current_principal),
    previewer: PreviewGateway = Depends(previews),
) -> None:
    """Stop one of your own previews.

    Scoped to the caller's own: two people watching one camera share a stream,
    and closing one tab must not blank the other person's screen.
    """
    mine = {info.id for info in await previewer.list(principal.user_id)}
    if preview_id not in mine:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    await previewer.stop(preview_id)


@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(
    camera: Camera = Depends(load_camera),
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> None:
    await db.delete(camera)
    await db.flush()
    await record(db, principal, "camera.delete", "camera", camera.id, team_id=camera.team_id)


# ---- helpers -----------------------------------------------------------


async def _check_profile(
    db: AsyncSession, principal: Principal, team_id: str, profile_id: str
) -> ConnectionProfile:
    if not principal.may_see(team_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    profile = await db.get(ConnectionProfile, profile_id)
    if profile is None or profile.team_id != team_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "That connection profile belongs to a different team.",
        )
    return profile


async def _persist(
    db: AsyncSession,
    principal: Principal,
    team_id: str,
    profile_id: str,
    dry_run: bool,
    report,
) -> ImportResponse:
    """Write the parsed cameras, skipping any whose name already exists.

    A name clash is reported as a duplicate rather than failing the whole
    import: re-importing a spreadsheet after adding two rows should add two
    cameras, not refuse the file.
    """
    previews = [
        ImportPreview(
            name=c.name,
            location=c.location,
            sources=[
                {"kind": s.kind, "url": s.url, "host": s.host, "port": s.port} for s in c.sources
            ],
        )
        for c in report.cameras
    ]
    response = ImportResponse(
        summary=report.summary,
        cameras=previews,
        duplicates=[ImportIssueOut(**asdict(d)) for d in report.duplicates],
        rejected=[ImportIssueOut(**asdict(r)) for r in report.rejected],
        dry_run=dry_run,
    )
    if dry_run:
        return response

    existing = await db.execute(select(Camera.name).where(Camera.team_id == team_id))
    taken = {name.lower() for name in existing.scalars().all()}

    secrets = secrets_backend()
    created = 0
    for parsed in report.cameras:
        if parsed.name.lower() in taken:
            response.duplicates.append(
                ImportIssueOut(line=0, value=parsed.name, reason="a camera with this name exists")
            )
            continue
        taken.add(parsed.name.lower())
        db.add(await _to_model(secrets, team_id, profile_id, parsed))
        created += 1

    await db.flush()
    response.created = created
    response.summary = (
        f"{created} added"
        + (f", {len(response.duplicates)} duplicate" if response.duplicates else "")
        + (f", {len(response.rejected)} rejected" if response.rejected else "")
    )

    await record(
        db,
        principal,
        "camera.import",
        "camera",
        None,
        {"created": created, "rejected": len(response.rejected)},
        team_id=team_id,
    )
    return response


async def _to_model(secrets, team_id: str, profile_id: str, parsed: ParsedCamera) -> Camera:
    camera = Camera(
        team_id=team_id,
        profile_id=profile_id,
        name=parsed.name,
        location=parsed.location,
    )
    for source in parsed.sources:
        camera.sources.append(
            CameraSource(
                kind=source.kind,
                url=source.url,
                host=source.host,
                port=source.port,
                username=source.username,
                password_ref=(
                    await secrets.put(source.password, hint="camera") if source.password else None
                ),
                # An HLS playlist is usually served from somewhere reachable
                # without the tunnel, even when the camera's RTSP is not.
                uses_profile_path=source.kind.value != "hls",
            )
        )
    return camera
