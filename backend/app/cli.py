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
from app.models import Base, Recording, Team, TeamMember, User
from app.security.reset import RESET_TTL_SECONDS, issue_reset
from app.services.maintenance import MaintenanceFlag
from app.services.reship import recoverable, reship_recording
from app.storage.client import StorageError, object_store


async def init_db() -> None:
    async with engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print(f"schema created: {len(Base.metadata.tables)} tables")


async def create_admin(email: str, password: str | None, name: str) -> None:
    await create_user(email, password, name, Role.SUPERADMIN, teams=())


async def create_user(
    email: str,
    password: str | None,
    name: str,
    role: Role,
    teams: tuple[str, ...] = (),
) -> None:
    """Create one account at any tier, optionally in some teams.

    The team argument matters most for demo accounts: a demo user with no team
    can see nothing, so an account created for a public demo and left teamless
    signs in successfully to an empty dashboard, which reads as a broken
    deployment rather than a missing membership.
    """
    password = password or getpass.getpass("password: ")
    minimum = settings().min_password_length
    if len(password) < minimum:
        sys.exit(f"password must be at least {minimum} characters")

    async with session() as db:
        existing = await db.execute(select(User).where(User.email == email.lower()))
        if existing.scalar_one_or_none() is not None:
            sys.exit(f"{email} already has an account")

        found = []
        for slug in teams:
            row = await db.execute(select(Team).where(Team.slug == slug))
            team = row.scalar_one_or_none()
            if team is None:
                sys.exit(f"no team with slug {slug!r}")
            found.append(team)

        user = User(
            email=email.lower(),
            display_name=name or email.split("@")[0],
            password_hash=hash_password(password),
            role=role,
        )
        db.add(user)
        await db.flush()
        for team in found:
            db.add(TeamMember(team_id=team.id, user_id=user.id))

    where = f" in {', '.join(teams)}" if teams else ""
    print(f"{role.value} created: {email}{where}")
    if role is Role.DEMO:
        print(
            "this account can watch cameras and change nothing -- not even its "
            "own password, which is the point of the tier"
        )


async def set_password(email: str, password: str | None) -> None:
    """Set somebody's password from the machine. The recovery path of last resort.

    Exists because the ways back into an account through the API both need
    something the operator may not have: a working sign-in, or an administrator
    who can still get in. When the only superadmin has lost the password, this
    is what is left.
    """
    password = password or getpass.getpass("new password: ")
    minimum = settings().min_password_length
    if len(password) < minimum:
        sys.exit(f"password must be at least {minimum} characters")

    async with session() as db:
        result = await db.execute(select(User).where(User.email == email.lower()))
        user = result.scalar_one_or_none()
        if user is None:
            sys.exit(f"no account {email}")
        user.password_hash = hash_password(password)
    print(f"password set: {email}")


async def reset_link(email: str, base_url: str) -> None:
    """Print a one-time reset link, for a deployment with no outbound mail."""
    async with session() as db:
        result = await db.execute(select(User).where(User.email == email.lower()))
        user = result.scalar_one_or_none()
        if user is None:
            sys.exit(f"no account {email}")
        if not user.may_write:
            sys.exit("demo accounts have a fixed password")
        token = issue_reset(user.id, user.password_hash)

    base = (base_url or settings().public_base_url).strip().rstrip("/")
    if not base:
        sys.exit("give --base-url, or set PUBLIC_BASE_URL")
    print(f"{base}/reset?token={token}")
    print(f"valid for {RESET_TTL_SECONDS // 60} minutes, once")


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


async def reship_failed(ids: tuple[str, ...], every: bool, dry_run: bool) -> None:
    """Send recordings whose capture worked but whose upload did not.

    The store being unreachable at shipping time leaves the footage on the work
    volume rather than destroying it, so a storage outage costs a delay instead
    of the recording -- but only if somebody sends it afterwards.
    """
    async with session() as db:
        if every:
            targets = [recording.id for recording in await recoverable(db)]
        else:
            targets = []
            for recording_id in ids:
                if await db.get(Recording, recording_id) is None:
                    print(f"{recording_id}: no such recording")
                    continue
                targets.append(recording_id)

    if not targets:
        print("nothing to send")
        return

    if dry_run:
        for recording_id in targets:
            print(f"{recording_id}  would be sent")
        print(f"{len(targets)} recording(s) would be sent")
        return

    sent = left = 0
    for recording_id in targets:
        # One session, one transaction, one recording. A backlog is recovered
        # one at a time, and neither a refusal nor an unexpected error on any of
        # them may strand the ones queued behind it -- which is precisely what a
        # single shared session did the first time this ran against a real
        # backlog, where one duplicate key ended the run with ten left to go.
        try:
            async with session() as db:
                recording = await db.get(Recording, recording_id)
                if recording is None:
                    left += 1
                    print(f"{recording_id}  disappeared before it could be sent")
                    continue
                outcome = await reship_recording(db, recording)
        except Exception as exc:  # noqa: BLE001 - one failure must not end the batch
            left += 1
            print(f"{recording_id}  {type(exc).__name__}: {exc}")
            continue

        if outcome.ok:
            sent += 1
            print(f"{recording_id}  sent, {outcome.bytes / 1024 / 1024:.1f} MB")
        else:
            left += 1
            print(f"{recording_id}  {outcome.reason}")
    print(f"{sent} recovered, {left} still failed")


async def maintenance(action: str, note: str) -> None:
    """Put the maintenance screen up, take it down, or say which it is.

    The flag lives in Redis, so this takes effect at once and survives the api
    container being replaced -- which is the point, since it is meant to be up
    *during* the replacement.
    """
    flag = MaintenanceFlag()
    if action == "on":
        current = await flag.turn_on(note)
        print(f"maintenance: on ({current.note})")
    elif action == "off":
        await flag.turn_off()
        print("maintenance: off")
    else:
        current = await flag.current()
        print(f"maintenance: on ({current.note})" if current.on else "maintenance: off")


def main() -> None:
    parser = argparse.ArgumentParser(prog="cam", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the schema")

    admin = sub.add_parser("create-admin", help="create a superadmin account")
    admin.add_argument("email")
    admin.add_argument("--password", default=None, help="prompted for if omitted")
    admin.add_argument("--name", default="")

    user = sub.add_parser("create-user", help="create an account at any tier")
    user.add_argument("email")
    user.add_argument("--role", default=Role.VIEWER.value, choices=[r.value for r in Role])
    user.add_argument("--password", default=None, help="prompted for if omitted")
    user.add_argument("--name", default="")
    user.add_argument("--team", action="append", default=[], metavar="SLUG", help="repeatable")

    pw = sub.add_parser("set-password", help="set an account's password directly")
    pw.add_argument("email")
    pw.add_argument("--password", default=None, help="prompted for if omitted")

    link = sub.add_parser("reset-link", help="print a one-time password reset link")
    link.add_argument("email")
    link.add_argument("--base-url", default="", help="defaults to PUBLIC_BASE_URL")

    team = sub.add_parser("create-team", help="create a team")
    team.add_argument("name")
    team.add_argument("slug")
    team.add_argument("--member", default=None, help="email of a user to add")

    sub.add_parser("check-storage", help="verify the Versity gateway and bucket")

    rs = sub.add_parser("reship", help="upload recordings whose capture worked but upload failed")
    rs.add_argument("ids", nargs="*", help="recording ids; omit and pass --all instead")
    rs.add_argument(
        "--all",
        action="store_true",
        dest="every",
        help="every failed recording that still has its files",
    )
    rs.add_argument("--dry-run", action="store_true", help="say what would be sent")

    maint = sub.add_parser("maintenance", help="show or set the maintenance screen")
    maint.add_argument("action", choices=["on", "off", "status"])
    maint.add_argument("--note", default="", help="what the screen should tell people")

    args = parser.parse_args()
    if args.command == "init-db":
        asyncio.run(init_db())
    elif args.command == "create-admin":
        asyncio.run(create_admin(args.email, args.password, args.name))
    elif args.command == "create-user":
        asyncio.run(
            create_user(args.email, args.password, args.name, Role(args.role), tuple(args.team))
        )
    elif args.command == "set-password":
        asyncio.run(set_password(args.email, args.password))
    elif args.command == "reset-link":
        asyncio.run(reset_link(args.email, args.base_url))
    elif args.command == "create-team":
        asyncio.run(create_team(args.name, args.slug, args.member))
    elif args.command == "check-storage":
        asyncio.run(check_storage())
    elif args.command == "reship":
        asyncio.run(reship_failed(tuple(args.ids), args.every, args.dry_run))
    elif args.command == "maintenance":
        asyncio.run(maintenance(args.action, args.note))


if __name__ == "__main__":
    main()
