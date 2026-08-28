"""Operational commands.

Schema is created directly from the models for now. The schema is new and still
churning weekly; Alembic earns its keep once it stops. ROADMAP.md entry 10.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.api.auth import hash_password
from app.config import settings
from app.db import engine, session
from app.enums import Role
from app.models import Base, Team, TeamMember, User
from app.storage.client import StorageError, object_store


async def init_db() -> None:
    async with engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print(f"schema created: {len(Base.metadata.tables)} tables")


async def create_admin(email: str, password: str | None, name: str) -> None:
    password = password or getpass.getpass("password: ")
    if len(password) < 10:
        sys.exit("password must be at least 10 characters")

    async with session() as db:
        existing = await db.execute(select(User).where(User.email == email.lower()))
        if existing.scalar_one_or_none() is not None:
            sys.exit(f"{email} already has an account")
        db.add(
            User(
                email=email.lower(),
                display_name=name or email.split("@")[0],
                password_hash=hash_password(password),
                role=Role.ADMIN,
            )
        )
    print(f"admin created: {email}")


async def create_team(name: str, slug: str, member_email: str | None) -> None:
    async with session() as db:
        team = Team(name=name, slug=slug)
        db.add(team)
        await db.flush()
        if member_email:
            result = await db.execute(select(User).where(User.email == member_email.lower()))
            user = result.scalar_one_or_none()
            if user is None:
                sys.exit(f"no user {member_email}")
            db.add(TeamMember(team_id=team.id, user_id=user.id))
        print(f"team created: {name} ({slug})")


async def check_storage() -> None:
    """Confirm the gateway is reachable and the bucket exists before anyone
    records into it and finds out the hard way."""
    cfg = settings()
    store = object_store()
    print(f"endpoint : {store.endpoint_url}")
    print(f"bucket   : {store.bucket}")
    print(f"region   : {cfg.s3_region}")
    print(f"style    : {cfg.s3_addressing_style}")
    if not cfg.s3_secret_key:
        sys.exit("AWS_SECRET_ACCESS_KEY is not set - see deploy/versity-setup.md")
    try:
        await store.check()
    except StorageError as exc:
        sys.exit(f"FAILED: {exc}")
    print("ok: bucket reachable and writable by these credentials")


def main() -> None:
    parser = argparse.ArgumentParser(prog="cam", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the schema")

    admin = sub.add_parser("create-admin", help="create an admin account")
    admin.add_argument("email")
    admin.add_argument("--password", default=None, help="prompted for if omitted")
    admin.add_argument("--name", default="")

    team = sub.add_parser("create-team", help="create a team")
    team.add_argument("name")
    team.add_argument("slug")
    team.add_argument("--member", default=None, help="email of a user to add")

    sub.add_parser("check-storage", help="verify the Versity gateway and bucket")

    args = parser.parse_args()
    if args.command == "init-db":
        asyncio.run(init_db())
    elif args.command == "create-admin":
        asyncio.run(create_admin(args.email, args.password, args.name))
    elif args.command == "create-team":
        asyncio.run(create_team(args.name, args.slug, args.member))
    elif args.command == "check-storage":
        asyncio.run(check_storage())


if __name__ == "__main__":
    main()
