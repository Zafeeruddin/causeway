"""Getting one camera source into a state ffmpeg can read, over and over.

A recording outlives any single connection, so this is deliberately not "the
tunnel we opened at the start". Every time the supervisor reopens a path it
re-reads the profile, re-checks that the connection is genuinely up rather than
merely marked up, and re-establishes the forward. That is what makes a redial
after a dropped VPN identical to the first dial, and it is why the recorder has
no reconnection logic of its own.

Database sessions here are short and per-call. Holding one open for the length
of a five-minute recording would pin a connection from the pool for the whole
session and make every state write wait behind it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.enums import SourceKind
from app.gates.probes import tcp_probe
from app.models import CameraSource, ConnectionProfile
from app.net.runner import LocalRunner
from app.recorder.session import OpenPath
from app.services.connections import ConnectionService

log = structlog.get_logger(__name__)


class PathUnavailable(RuntimeError):
    """The source cannot be reached right now. Carries the gate's own wording --
    the person reading it should see "the VPN rejected these credentials", not
    "connect returned False"."""

    def __init__(self, message: str, cause: str = "unknown") -> None:
        self.user_message = message
        self.cause = cause
        super().__init__(message)


class ProfileSourcePath:
    """A camera source reached through its team's connection profile."""

    def __init__(
        self,
        *,
        source_id: str,
        kind: SourceKind,
        profile_id: str,
        connections: ConnectionService,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self.source_id = source_id
        self.kind = kind
        self.profile_id = profile_id
        self._connections = connections
        self._sessions = sessions

    @asynccontextmanager
    async def _load(self):
        async with self._sessions() as db:
            profile = await db.get(ConnectionProfile, self.profile_id)
            source = await db.get(CameraSource, self.source_id)
            if profile is None or source is None:
                raise PathUnavailable("this camera or its connection profile has been deleted")
            try:
                yield db, profile, source
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    # ---- SourcePath ----------------------------------------------------

    async def open(self) -> OpenPath:
        async with self._load() as (db, profile, source):
            spec = await self._connections.build_source_spec(source)

            # A source that does not travel the profile's path -- the HLS feed
            # is usually reachable without the tunnel -- is opened from here.
            # Dialling a VPN it does not need would make an outage on the RTSP
            # side take the HLS side down with it.
            if not source.uses_profile_path:
                return OpenPath(url=spec.url, runner=LocalRunner(), detail="direct, no tunnel")

            if not await self._connections.health(profile):
                outcome = await self._connections.connect(db, profile)
                if not outcome.ok:
                    blocking = outcome.blocked_on or outcome.failed_at
                    raise PathUnavailable(
                        blocking.message if blocking else "the connection could not be opened",
                        _cause_for_gate(blocking.key if blocking else ""),
                    )

            runner = await self._connections.runner_for(profile)
            url = spec.url
            if profile.reach_mode.has_jump:
                jump = (await self._connections.build_spec(profile)).jump
                assert jump is not None
                lease = await self._connections.tunnels.forward(
                    jump, source.id, source.host, source.port
                )
                url = url.with_host("127.0.0.1", lease.port)
            return OpenPath(url=url, runner=runner, detail=f"via {profile.name}")

    async def diagnose(self) -> tuple[str, str]:
        """Which hop went away, asked of the hops themselves.

        Ordered outermost first: a dropped VPN takes the SSH master and the
        camera probe down with it, so reporting the camera as the cause would be
        true and useless.
        """
        async with self._load() as (_db, profile, source):
            if not source.uses_profile_path:
                probe = await tcp_probe(LocalRunner(), source.host, source.port)
                if not probe.open:
                    return "camera", f"{source.host}:{source.port} is not answering"
                return "unknown", "the source is reachable but stopped sending video"

            if profile.reach_mode.has_vpn and not await self._connections.health(profile):
                return "vpn", f"the VPN tunnel for {profile.name} is down"

            if profile.reach_mode.has_jump:
                jump = (await self._connections.build_spec(profile)).jump
                assert jump is not None
                if not await self._connections.tunnels.master_alive(jump):
                    return "ssh", f"the SSH connection to {jump.host} dropped"

            try:
                runner = await self._connections.runner_for(profile)
            except Exception as exc:  # noqa: BLE001 - no namespace means no path
                return "vpn", f"the connection's namespace is gone: {exc}"

            lease = self._connections.tunnels.pool.get(source.id)
            host, port = ("127.0.0.1", lease.port) if lease else (source.host, source.port)
            probe = await tcp_probe(runner, host, port)
            if not probe.open:
                return "camera", f"{source.host}:{source.port} stopped accepting connections"
            return "unknown", "the path is up; the camera stopped sending video"

    async def close(self) -> None:
        """Forwards are left open on purpose.

        A forward is leased per camera source, not per recording, so two
        recordings of the same camera share one. Cancelling it here would cut
        the other recording. Forwards are released when the profile disconnects,
        which is also when the port pool is reclaimed.
        """
        return None


def _cause_for_gate(key: str) -> str:
    if key in ("vpn_dial", "certificate_trust", "whitelist"):
        return "vpn"
    if key in ("jump_route", "ssh_auth", "port_forward"):
        return "ssh"
    if key in ("camera_reachable", "stream_handshake"):
        return "camera"
    return "unknown"
