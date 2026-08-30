"""Local accounts. Argon2 hashes, signed session cookies, no third party."""

from __future__ import annotations

import hmac
import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from hashlib import sha256

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import Principal, get_session
from app.enums import Role
from app.models import TeamMember, User

COOKIE_NAME = "cam_session"
SESSION_TTL = 12 * 3600

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    return True


def issue_session(user_id: str) -> str:
    payload = json.dumps({"sub": user_id, "exp": int(time.time()) + SESSION_TTL}).encode()
    body = urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{_sign(body)}"


def read_session(token: str) -> str | None:
    body, _, signature = token.partition(".")
    if not signature or not hmac.compare_digest(signature, _sign(body)):
        return None
    try:
        payload = json.loads(urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload.get("sub")


def _sign(body: str) -> str:
    mac = hmac.new(settings().app_secret_key.encode(), body.encode(), sha256)
    return urlsafe_b64encode(mac.digest()).decode().rstrip("=")


async def current_principal(
    cam_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: AsyncSession = Depends(get_session),
) -> Principal:
    if not cam_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue.")
    user_id = read_session(cam_session)
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session has expired.")

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This account is no longer active.")

    rows = await db.execute(select(TeamMember.team_id).where(TeamMember.user_id == user_id))
    return Principal(user_id=user.id, role=Role(user.role), team_ids=set(rows.scalars().all()))


async def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    """Administers *something*. Which team is the route's question, not this one.

    A route that ends here and does not then check the team it is acting on has
    granted one team's admin authority over another's, which is the failure this
    split exists to make hard to write by accident.
    """
    if not principal.may_administer:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This action needs an administrator account."
        )
    return principal


async def require_superadmin(principal: Principal = Depends(current_principal)) -> Principal:
    if not principal.is_superadmin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a superadmin can do this.")
    return principal
