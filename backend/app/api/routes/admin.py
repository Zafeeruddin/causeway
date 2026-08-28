"""Teams and users. Admin only."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import hash_password, require_admin
from app.api.schemas import MembershipRequest, TeamCreate, TeamOut, UserCreate, UserOut
from app.db import Principal, get_session
from app.models import Team, TeamMember, User
from app.services.audit import record

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/teams", response_model=list[TeamOut])
async def list_teams(db: AsyncSession = Depends(get_session)) -> list[TeamOut]:
    counts = (
        select(TeamMember.team_id, func.count().label("n")).group_by(TeamMember.team_id).subquery()
    )
    rows = await db.execute(
        select(Team, func.coalesce(counts.c.n, 0)).outerjoin(counts, Team.id == counts.c.team_id)
    )
    return [
        TeamOut(
            id=team.id,
            name=team.name,
            slug=team.slug,
            description=team.description,
            member_count=count,
        )
        for team, count in rows.all()
    ]


@router.post("/teams", response_model=TeamOut, status_code=status.HTTP_201_CREATED)
async def create_team(
    body: TeamCreate,
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> TeamOut:
    team = Team(name=body.name, slug=body.slug, description=body.description)
    db.add(team)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A team named {body.name!r} or {body.slug!r} already exists."
        ) from exc
    await record(db, principal, "team.create", "team", team.id, {"slug": team.slug})
    return TeamOut(id=team.id, name=team.name, slug=team.slug, description=team.description)


@router.get("/users", response_model=list[UserOut])
async def list_users(db: AsyncSession = Depends(get_session)) -> list[UserOut]:
    rows = await db.execute(select(User).order_by(User.email))
    return [UserOut.model_validate(u) for u in rows.scalars().all()]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> UserOut:
    user = User(
        email=body.email.lower().strip(),
        display_name=body.display_name or body.email.split("@")[0],
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "That email already has an account.") from exc
    await record(db, principal, "user.create", "user", user.id, {"role": str(user.role)})
    return UserOut.model_validate(user)


@router.post("/teams/{team_id}/members", status_code=status.HTTP_204_NO_CONTENT)
async def add_member(
    team_id: str,
    body: MembershipRequest,
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> None:
    if await db.get(Team, team_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such team.")
    if await db.get(User, body.user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user.")

    exists = await db.execute(
        select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == body.user_id)
    )
    if exists.scalar_one_or_none() is not None:
        return  # Idempotent: adding twice is not an error.

    db.add(TeamMember(team_id=team_id, user_id=body.user_id))
    await db.flush()
    await record(db, principal, "team.add_member", "team", team_id, {"user_id": body.user_id})


@router.delete("/teams/{team_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    team_id: str,
    user_id: str,
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> None:
    result = await db.execute(
        select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
    )
    membership = result.scalar_one_or_none()
    if membership is not None:
        await db.delete(membership)
        await db.flush()
        await record(db, principal, "team.remove_member", "team", team_id, {"user_id": user_id})
