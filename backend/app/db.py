"""Database session handling, and the one place team scoping is enforced.

Filtering by team in request handlers is how a hole gets left: someone adds an
endpoint, forgets the filter, and it works fine in testing because the tester is
an admin. :func:`scoped` makes the filter the default and an unscoped query the
thing you have to ask for explicitly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.enums import Role

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(settings().database_url, pool_pre_ping=True, future=True)
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(engine(), expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as sess:
        try:
            yield sess
            await sess.commit()
        except Exception:
            await sess.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session() as sess:
        yield sess


T = TypeVar("T")


class Principal:
    """Who is asking. Carries the role and team set so scoping needs no lookup.

    Two questions get asked of this all over the API and they are not the same
    one: *may they see it* and *may they change it*. A viewer sees every camera
    in their teams and may change none of them, so a single "is this yours"
    predicate cannot answer both -- which is exactly the mistake that puts a
    delete button in front of someone who should not have one.
    """

    def __init__(self, user_id: str, role: Role, team_ids: set[str]) -> None:
        self.user_id = user_id
        self.role = role
        self.team_ids = team_ids

    @property
    def is_superadmin(self) -> bool:
        return self.role is Role.SUPERADMIN

    @property
    def may_administer(self) -> bool:
        """Whether they administer anything anywhere. Not a licence over a team."""
        return self.role.may_administer

    def may_see(self, team_id: str | None) -> bool:
        return self.is_superadmin or (team_id is not None and team_id in self.team_ids)

    def may_administer_team(self, team_id: str | None) -> bool:
        """Membership is not enough: a viewer belongs to the team too."""
        return self.is_superadmin or (self.may_administer and self.may_see(team_id))


def scoped(stmt: Select[Any], model: Any, principal: Principal) -> Select[Any]:
    """Restrict a query to what ``principal`` is allowed to see.

    A superadmin sees everything. Everyone else sees the union of their teams,
    and someone with no teams sees nothing -- which is the correct answer, and
    the reason this returns a false predicate rather than the unfiltered query.
    """
    if principal.is_superadmin:
        return stmt
    if not principal.team_ids:
        return stmt.where(model.team_id.is_(None) & model.team_id.is_not(None))
    return stmt.where(model.team_id.in_(principal.team_ids))


def scoped_select(model: Any, principal: Principal) -> Select[Any]:
    return scoped(select(model), model, principal)
