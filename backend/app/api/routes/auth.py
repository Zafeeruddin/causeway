from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, current_principal, issue_session, verify_password
from app.api.schemas import LoginRequest, Me, TeamBrief
from app.config import settings
from app.db import Principal, get_session
from app.models import Team, TeamMember, User

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=Me)
async def login(
    body: LoginRequest, response: Response, db: AsyncSession = Depends(get_session)
) -> Me:
    result = await db.execute(select(User).where(User.email == body.email.lower().strip()))
    user = result.scalar_one_or_none()

    # Same message either way: which half was wrong is not the caller's business.
    if user is None or not user.is_active or not verify_password(user.password_hash, body.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Email or password is incorrect.")

    user.last_login_at = datetime.now(UTC)
    await db.flush()

    response.set_cookie(
        COOKIE_NAME,
        issue_session(user.id),
        httponly=True,
        samesite="lax",
        secure=not settings().is_dev,
        max_age=12 * 3600,
        path="/",
    )
    return await _me(db, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/me", response_model=Me)
async def me(
    principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)
) -> Me:
    user = await db.get(User, principal.user_id)
    assert user is not None
    return await _me(db, user)


async def _me(db: AsyncSession, user: User) -> Me:
    if user.is_admin:
        rows = await db.execute(select(Team))
    else:
        rows = await db.execute(select(Team).join(TeamMember).where(TeamMember.user_id == user.id))
    return Me(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        teams=[TeamBrief.model_validate(t) for t in rows.scalars().all()],
    )
