"""Teams and users.

Two audiences behind one router. A superadmin owns the deployment and sees all
of it. An admin owns their own teams and sees exactly those -- the same screen,
narrowed, rather than a second screen -- so every read here is scoped and every
write asks which team it is writing to.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import hash_password, require_admin, require_superadmin
from app.api.deps import deny_unless_team_admin
from app.api.schemas import (
    MembershipRequest,
    TeamCreate,
    TeamOut,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.db import Principal, get_session
from app.enums import Role
from app.models import Team, TeamMember, User
from app.services.audit import record

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/teams", response_model=list[TeamOut])
async def list_teams(
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> list[TeamOut]:
    """Every team for a superadmin; an admin's own teams for an admin."""
    counts = (
        select(TeamMember.team_id, func.count().label("n")).group_by(TeamMember.team_id).subquery()
    )
    query = select(Team, func.coalesce(counts.c.n, 0)).outerjoin(
        counts, Team.id == counts.c.team_id
    )
    if not principal.is_superadmin:
        # `or {""}` so an admin with no teams matches nothing rather than
        # producing an empty IN () that some backends read as "everything".
        query = query.where(Team.id.in_(principal.team_ids or {""}))
    rows = await db.execute(query)
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
    principal: Principal = Depends(require_superadmin),
) -> TeamOut:
    """Superadmin only: a team is the boundary every other permission is drawn
    against, so drawing a new one is not something a scoped account can do."""
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
async def list_users(
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> list[UserOut]:
    """Everyone, or everyone in the caller's teams.

    An admin manages membership of their own teams, so they need the people in
    them. They do not need the roster of the whole deployment, and handing it to
    them is how one team learns another team's staff list.
    """
    query = select(User).order_by(User.email)
    if not principal.is_superadmin:
        query = query.where(
            User.id.in_(
                select(TeamMember.user_id).where(TeamMember.team_id.in_(principal.team_ids or {""}))
            )
        )
    rows = await db.execute(query)
    return [UserOut.model_validate(u) for u in rows.scalars().all()]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> UserOut:
    """Create an account.

    A superadmin may create any role and need not place it in a team. An admin
    may create viewers only, and only into their own teams -- otherwise the
    quickest route to superadmin is to make one, and an account created into no
    team would be one its creator could no longer see.
    """
    if not principal.is_superadmin:
        if Role(body.role) is not Role.VIEWER:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Only a superadmin can create an account that is not a viewer.",
            )
        if not body.team_ids:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Choose the team this account belongs to.",
            )
    for team_id in body.team_ids:
        deny_unless_team_admin(principal, team_id)

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
    for team_id in body.team_ids:
        db.add(TeamMember(team_id=team_id, user_id=user.id))
    await db.flush()
    await record(
        db,
        principal,
        "user.create",
        "user",
        user.id,
        {"role": str(user.role), "teams": body.team_ids},
    )
    return UserOut.model_validate(user)


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    body: UserUpdate,
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> UserOut:
    """Change what an account is allowed to do.

    Three rules, each closing a way this becomes a privilege ladder:

    * Nobody edits their own role. Otherwise the last superadmin can demote
      themselves and lock the deployment out of its own administration, and an
      admin who could edit roles at all could promote themselves.
    * Only a superadmin creates or edits a superadmin -- in either direction.
    * An admin acts only on people who are in their own teams, and may set only
      the viewer and admin roles, matching what they may create.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user.")

    if user.id == principal.user_id and body.role is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot change your own role.")

    if not principal.is_superadmin:
        if Role(user.role) is Role.SUPERADMIN:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Only a superadmin can change this account."
            )
        if body.role is not None and Role(body.role) is Role.SUPERADMIN:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a superadmin can grant that role.")
        rows = await db.execute(select(TeamMember.team_id).where(TeamMember.user_id == user.id))
        shared = set(rows.scalars().all()) & principal.team_ids
        if not shared:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user.")

    changed: dict[str, object] = {}
    if body.role is not None:
        user.role = Role(body.role)
        changed["role"] = str(user.role)
    if body.is_active is not None:
        user.is_active = body.is_active
        changed["is_active"] = user.is_active
    if body.display_name is not None:
        user.display_name = body.display_name.strip()
        changed["display_name"] = user.display_name

    await db.flush()
    if changed:
        await record(db, principal, "user.update", "user", user.id, changed)
    return UserOut.model_validate(user)


@router.post("/teams/{team_id}/members", status_code=status.HTTP_204_NO_CONTENT)
async def add_member(
    team_id: str,
    body: MembershipRequest,
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> None:
    deny_unless_team_admin(principal, team_id)
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
    deny_unless_team_admin(principal, team_id)
    result = await db.execute(
        select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
    )
    membership = result.scalar_one_or_none()
    if membership is not None:
        await db.delete(membership)
        await db.flush()
        await record(db, principal, "team.remove_member", "team", team_id, {"user_id": user_id})
