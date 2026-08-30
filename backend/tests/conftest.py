"""Fakes that let the whole network stack be tested without a network."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.auth import hash_password
from app.db import get_session
from app.enums import Role
from app.main import create_app
from app.models import Base, Team, TeamMember, User
from app.net.netns import NetnsManager
from app.net.ports import PortPool
from app.net.runner import ProcResult
from app.net.ssh import TunnelManager
from app.security.secrets import MemoryBackend, set_secrets_backend
from app.services.connections import ConnectionService
from app.services.events import NullBus, set_event_bus
from app.services.preview import PreviewManager


def reader_for(lines: list[str]) -> asyncio.StreamReader:
    stream = asyncio.StreamReader()
    for line in lines:
        stream.feed_data((line.rstrip("\n") + "\n").encode())
    stream.feed_eof()
    return stream


class FakeStdin:
    def __init__(self) -> None:
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeProcess:
    """Stands in for asyncio.subprocess.Process."""

    def __init__(
        self,
        stderr: list[str] | None = None,
        stdout: list[str] | None = None,
        returncode: int | None = None,
    ) -> None:
        self.stderr = reader_for(stderr or [])
        self.stdout = reader_for(stdout or [])
        self.stdin = FakeStdin()
        self.returncode = returncode
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


@dataclass
class FakeRunner:
    """Serves canned results by substring match and hands back a scripted process."""

    name: str = "fake"
    results: dict[str, ProcResult] = field(default_factory=dict)
    process: FakeProcess | None = None
    calls: list[list[str]] = field(default_factory=list)
    default: ProcResult = field(default_factory=lambda: ProcResult(0, "", ""))

    def wrap(self, argv):
        return list(argv)

    async def run(self, argv, *, timeout=30.0, stdin=None, env=None, check=False):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for needle, result in self.results.items():
            if needle in joined:
                return result
        return self.default

    async def spawn(self, argv, *, env=None):
        self.calls.append(list(argv))
        assert self.process is not None, "no scripted process for this test"
        return self.process


class FakeMediaMtx:
    """MediaMTX without the container. Paths are a dict; readiness is scripted."""

    def __init__(self, *, ready: bool = True, tracks: tuple[str, ...] = ("H264",)) -> None:
        self.paths: dict[str, int] = {}
        self.ready = ready
        self.tracks = tracks
        self.removed: list[str] = []

    async def add_path(self, name: str) -> None:
        self.paths[name] = 0

    async def remove_path(self, name: str) -> None:
        self.paths.pop(name, None)
        self.removed.append(name)

    async def state(self, name: str):
        from app.services.mediamtx import PathState

        if name not in self.paths:
            return None
        return PathState(name=name, ready=self.ready, readers=self.paths[name], tracks=self.tracks)

    async def states(self) -> dict:
        from app.services.mediamtx import PathState

        return {
            name: PathState(name=name, ready=self.ready, readers=readers, tracks=self.tracks)
            for name, readers in self.paths.items()
        }


@pytest.fixture
def link_show_ppp() -> ProcResult:
    """`ip -o link show` with a pppd tunnel present."""
    return ProcResult(
        0,
        "1: lo: <LOOPBACK,UP> mtu 65536\n"
        "2: eth0: <BROADCAST,MULTICAST,UP> mtu 1500\n"
        "3: ppp0: <POINTOPOINT,MULTICAST,NOARP,UP> mtu 1354\n",
        "",
    )


@pytest.fixture
def addr_show() -> ProcResult:
    return ProcResult(
        0, "3: ppp0    inet 10.212.134.88 peer 10.212.134.1/32 scope global ppp0\n", ""
    )


# ---- API test harness --------------------------------------------------


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def sessions(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def bus():
    b = NullBus()
    set_event_bus(b)
    return b


@pytest_asyncio.fixture
async def app(sessions, bus):
    set_secrets_backend(MemoryBackend())
    application = create_app()

    async def override():
        async with sessions() as sess:
            try:
                yield sess
                await sess.commit()
            except Exception:
                await sess.rollback()
                raise

    application.dependency_overrides[get_session] = override
    application.state.netns = NetnsManager(prefix="test")
    application.state.ports = PortPool(low=20000, high=20099)
    application.state.tunnels = TunnelManager(control_dir="/tmp/cam-test-ctl")
    application.state.gateway = ConnectionService(
        netns=application.state.netns,
        tunnels=application.state.tunnels,
        secrets=MemoryBackend(),
        bus=bus,
    )
    # No reaper task and no real MediaMTX: tests that exercise preview supply
    # their own manager, and the rest must not reach a network for it.
    application.state.previews = PreviewManager(
        connections=application.state.gateway,
        sessions=sessions,
        mediamtx=FakeMediaMtx(),
    )
    return application


@pytest_asyncio.fixture
async def seeded(sessions):
    """One of each role, and two teams to draw the boundary with.

    ``member`` and ``viewer`` are both in MOFA and differ only in role, which is
    the pair most of the permission tests need: same team, same cameras, one may
    change them and one may not.
    """
    async with sessions() as db:
        admin = User(
            email="admin@example.com",
            display_name="Admin",
            password_hash=hash_password("admin-password"),
            role=Role.SUPERADMIN,
        )
        member = User(
            email="qa@example.com",
            display_name="QA",
            password_hash=hash_password("member-password"),
            role=Role.ADMIN,
        )
        other = User(
            email="ops@example.com",
            display_name="Ops",
            password_hash=hash_password("other-password"),
            role=Role.ADMIN,
        )
        viewer = User(
            email="watch@example.com",
            display_name="Watcher",
            password_hash=hash_password("viewer-password"),
            role=Role.VIEWER,
        )
        mofa = Team(name="MOFA", slug="mofa")
        ops = Team(name="Ops", slug="ops")
        db.add_all([admin, member, other, viewer, mofa, ops])
        await db.flush()
        db.add_all(
            [
                TeamMember(team_id=mofa.id, user_id=member.id),
                TeamMember(team_id=ops.id, user_id=other.id),
                TeamMember(team_id=mofa.id, user_id=viewer.id),
            ]
        )
        await db.commit()
        return {
            "admin": admin.id,
            "member": member.id,
            "other": other.id,
            "viewer": viewer.id,
            "mofa": mofa.id,
            "ops": ops.id,
        }


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def as_admin(client, seeded):
    await client.post(
        "/api/auth/login", json={"email": "admin@example.com", "password": "admin-password"}
    )
    return client


@pytest_asyncio.fixture
async def as_member(client, seeded):
    await client.post(
        "/api/auth/login", json={"email": "qa@example.com", "password": "member-password"}
    )
    return client


@pytest_asyncio.fixture
async def as_viewer(client, seeded):
    """In MOFA, like ``as_member``, and allowed to do far less with it."""
    await client.post(
        "/api/auth/login", json={"email": "watch@example.com", "password": "viewer-password"}
    )
    return client


@pytest_asyncio.fixture
async def as_other(client, seeded):
    await client.post(
        "/api/auth/login", json={"email": "ops@example.com", "password": "other-password"}
    )
    return client
