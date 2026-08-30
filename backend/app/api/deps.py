"""Shared dependencies.

The loader functions here are the enforcement point for team scoping: a route
that fetches an object through one of them cannot forget the filter, because
there is no unscoped path to the object.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Path, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth import current_principal
from app.db import Principal, get_session
from app.models import Camera, ConnectionProfile, Recording
from app.services.gateway import ConnectionGateway
from app.services.preview import PreviewGateway

#: Deliberately the same message for "does not exist" and "not yours" -- telling
#: a member that a camera exists in another team is itself a disclosure.
NOT_FOUND = "Not found."


def gateway(request: Request) -> ConnectionGateway:
    """In-process or in the agent, decided once at startup. See
    :mod:`app.services.gateway`."""
    return request.app.state.gateway


def previews(request: Request) -> PreviewGateway:
    return request.app.state.previews


def deny_unless_team_admin(principal: Principal, team_id: str | None) -> None:
    """Refuse anyone who is not an administrator *of this team*.

    Two different refusals, on purpose. Someone who belongs to the team gets 403
    and a sentence: they know the thing exists, and "you cannot do that" is the
    useful answer. Someone outside it gets the same 404 everything else gives,
    because confirming which teams and objects exist is itself a disclosure.
    """
    if not principal.may_see(team_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    if not principal.may_administer_team(team_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This needs an administrator of this team.")


async def require_team(
    team_id: str,
    principal: Principal = Depends(current_principal),
) -> str:
    if not principal.may_see(team_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return team_id


async def load_profile(
    profile_id: str = Path(...),
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> ConnectionProfile:
    profile = await db.get(ConnectionProfile, profile_id)
    if profile is None or not principal.may_see(profile.team_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return profile


async def load_camera(
    camera_id: str = Path(...),
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> Camera:
    result = await db.execute(
        select(Camera).where(Camera.id == camera_id).options(selectinload(Camera.sources))
    )
    camera = result.scalar_one_or_none()
    if camera is None or not principal.may_see(camera.team_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return camera


async def load_recording(
    recording_id: str = Path(...),
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> Recording:
    recording = await db.get(Recording, recording_id)
    if recording is None or not principal.may_see(recording.team_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return recording
