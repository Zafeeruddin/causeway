"""Turning a stored connection profile into a live connection.

This is the bridge between the database and the gate ladder: it resolves sealed
credentials into a :class:`ProfileSpec`, picks the right runner for the profile's
reachability mode, walks the gates, and writes every rung back as a
:class:`GateRun` row while streaming the same results to the dashboard.

Plaintext credentials exist only for the duration of one attempt, inside one
:class:`ProfileSpec`, and are never returned to a caller.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import GateStatus, ProfileState, ReachMode, SourceKind, SshAuth, VpnKind
from app.gates.ladder import (
    GateContext,
    GateResult,
    ProfileSpec,
    SourceSpec,
    connect_gates,
    run_ladder,
    source_gates,
)
from app.models import Camera, CameraSource, ConnectionProfile, GateRun
from app.net.netns import NetnsManager, NetnsUnavailable
from app.net.runner import LocalRunner, NetnsRunner, Runner
from app.net.ssh import JumpHost, TunnelManager
from app.net.vpn.base import VpnConfig, VpnDriver
from app.net.vpn.registry import driver_for
from app.security.redaction import StreamUrl
from app.security.secrets import SecretsBackend, secrets_backend
from app.services.events import Event, EventBus, event_bus

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class ConnectOutcome:
    attempt_id: str
    results: list[GateResult]
    state: ProfileState

    @property
    def ok(self) -> bool:
        return self.state is ProfileState.UP

    @property
    def blocked_on(self) -> GateResult | None:
        return next((r for r in self.results if r.status is GateStatus.BLOCKED), None)

    @property
    def failed_at(self) -> GateResult | None:
        return next((r for r in self.results if r.status is GateStatus.FAILED), None)


class ConnectionService:
    """Owns the live side of connection profiles.

    One instance per process. It holds the namespaces, the VPN drivers and the
    SSH masters, so it is also the thing that knows whether a profile is
    genuinely up rather than merely marked up in the database.
    """

    def __init__(
        self,
        *,
        netns: NetnsManager,
        tunnels: TunnelManager,
        secrets: SecretsBackend | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.netns = netns
        self.tunnels = tunnels
        self.secrets = secrets or secrets_backend()
        self.bus = bus or event_bus()
        self._drivers: dict[str, VpnDriver] = {}
        self._runners: dict[str, Runner] = {}

    # ---- spec building -------------------------------------------------

    async def _reveal(self, ref: str | None) -> str:
        return await self.secrets.get(ref) if ref else ""

    async def build_spec(self, profile: ConnectionProfile) -> ProfileSpec:
        """Resolve a stored profile into dial parameters. Plaintext lives here
        and nowhere else."""
        vpn = VpnConfig(
            kind=VpnKind(profile.vpn_kind),
            gateway=profile.vpn_gateway,
            port=profile.vpn_port,
            username=profile.vpn_username,
            password=await self._reveal(profile.vpn_password_ref),
            realm=profile.vpn_realm,
            trusted_cert=profile.trusted_cert,
            wg_config=await self._reveal(profile.vpn_config_ref),
        )

        jump = None
        if profile.reach_mode.has_jump:
            jump = JumpHost(
                host=profile.jump_host,
                port=profile.jump_port,
                username=profile.jump_username,
                auth=SshAuth(profile.jump_auth),
                password=await self._reveal(profile.jump_password_ref),
                private_key=await self._reveal(profile.jump_key_ref),
            )

        return ProfileSpec(
            id=profile.id,
            name=profile.name,
            mode=profile.reach_mode,
            vpn=vpn,
            jump=jump,
            whitelist_url=profile.whitelist_url,
        )

    async def build_source_spec(self, source: CameraSource) -> SourceSpec:
        return SourceSpec(
            id=source.id,
            kind=SourceKind(source.kind),
            url=StreamUrl.build(
                source.url, source.username, await self._reveal(source.password_ref)
            ),
            host=source.host,
            port=source.port,
        )

    # ---- runners -------------------------------------------------------

    async def runner_for(self, profile: ConnectionProfile) -> Runner:
        """A namespaced runner for VPN profiles, a plain one for direct mode.

        Direct mode deliberately does not get a namespace: there is no tunnel to
        isolate, and demanding CAP_NET_ADMIN for it would stop the easy case
        working on a host that cannot provide it.
        """
        if not profile.reach_mode.has_vpn:
            return LocalRunner()
        if profile.id not in self._runners:
            namespace = await self.netns.ensure(profile.id)
            self._runners[profile.id] = namespace.runner
        # Outside the cache: the gateway is profile data and can be edited, and
        # a namespace that outlived an agent restart has the route without us
        # knowing it. allow_host is a no-op once the address is already in.
        await self.netns.allow_host(profile.id, profile.vpn_gateway)
        return self._runners[profile.id]

    def driver_for_profile(self, profile: ConnectionProfile, runner: Runner) -> VpnDriver:
        if profile.id not in self._drivers:
            self._drivers[profile.id] = driver_for(VpnKind(profile.vpn_kind), runner)
        return self._drivers[profile.id]

    # ---- the connect flow ----------------------------------------------

    async def connect(self, db: AsyncSession, profile: ConnectionProfile) -> ConnectOutcome:
        attempt_id = str(uuid.uuid4())
        await self._set_state(db, profile, ProfileState.CONNECTING, "walking the gates")

        try:
            runner = await self.runner_for(profile)
        except NetnsUnavailable as exc:
            # Before the first rung, so there is no gate to hang this on and the
            # ladder shows eight "waiting" rows. Without this line the agent log
            # says only that a connect arrived, and the reason lives in a column
            # nobody thinks to read.
            log.error("connect.setup_failed", profile=profile.id, reason=str(exc))
            await self._set_state(db, profile, ProfileState.FAILED, str(exc))
            return ConnectOutcome(attempt_id, [], ProfileState.FAILED)

        spec = await self.build_spec(profile)
        ctx = GateContext(
            profile=spec,
            runner=runner,
            driver=self.driver_for_profile(profile, runner),
            tunnels=self.tunnels,
        )

        results = await run_ladder(
            ctx, emit=self._emitter(db, profile, attempt_id), gates=connect_gates()
        )

        state = self._state_from(results)
        detail = self._detail_from(results)
        if state is ProfileState.UP:
            profile.tunnel_ip = ctx.tunnel_ip
            profile.namespace = self.netns.ns_name(profile.id) if spec.mode.has_vpn else None
            profile.last_connected_at = datetime.now(UTC)
        await self._set_state(db, profile, state, detail)

        return ConnectOutcome(attempt_id, results, state)

    async def test_source(
        self, db: AsyncSession, profile: ConnectionProfile, camera: Camera, source: CameraSource
    ) -> ConnectOutcome:
        """Run the source-scoped gates for one camera source.

        The profile's own hops are assumed already walked -- that is what makes
        this the *source* ladder -- but "already walked" is a claim about a
        process, not about a row. An agent that restarted holds no VPN and no
        SSH master while the profile still reads ``up``, and the first rung here
        opens a forward on a master that is not there. Recording and preview
        redial through ``SourcePath.open`` when that happens; testing a camera
        has to do the same or it reports a broken forward for a connection
        nobody has made yet.
        """
        attempt_id = str(uuid.uuid4())
        needs_profile = source.uses_profile_path and profile.reach_mode.has_vpn
        if needs_profile and not await self.health(profile):
            outcome = await self.connect(db, profile)
            if not outcome.ok:
                # The profile's own failure, reported as the profile's -- not as
                # whichever source rung noticed it second.
                return outcome
        runner = await self.runner_for(profile)
        spec = await self.build_spec(profile)
        source_spec = await self.build_source_spec(source)

        # A source that does not travel the profile's path -- an HLS URL that is
        # reachable directly while the camera's RTSP is not -- is probed from
        # here rather than from inside the tunnel.
        if not source.uses_profile_path:
            runner = LocalRunner()
            spec = ProfileSpec(id=spec.id, name=spec.name, mode=ReachMode.DIRECT, vpn=spec.vpn)

        ctx = GateContext(
            profile=spec,
            runner=runner,
            driver=self.driver_for_profile(profile, runner),
            tunnels=self.tunnels,
            source=source_spec,
        )
        results = await run_ladder(
            ctx,
            emit=self._emitter(db, profile, attempt_id, source_id=source.id),
            gates=source_gates(),
        )

        await self._record_probe(db, source, ctx, results)
        state = ProfileState.UP if all(not r.blocking for r in results) else ProfileState.DEGRADED
        return ConnectOutcome(attempt_id, results, state)

    async def accept_certificate(
        self, db: AsyncSession, profile: ConnectionProfile, fingerprint: str, user_id: str
    ) -> None:
        """Pin the gateway certificate the user just approved.

        Stored per profile, so the question is asked once. A gateway that later
        presents a different certificate fails gate 1 and prompts again -- which
        is the entire value of pinning it.
        """
        profile.trusted_cert = fingerprint.lower()
        profile.trusted_cert_accepted_by = user_id
        profile.trusted_cert_accepted_at = datetime.now(UTC)
        await db.flush()
        # The driver holds a dead process from the refused dial; drop it so the
        # next connect builds a fresh one with the pinned certificate.
        self._drivers.pop(profile.id, None)

    async def disconnect(self, db: AsyncSession, profile: ConnectionProfile) -> None:
        driver = self._drivers.pop(profile.id, None)
        if driver is not None:
            await driver.hangup()
        if profile.reach_mode.has_jump:
            spec = await self.build_spec(profile)
            if spec.jump is not None:
                # The master lives in this profile's namespace, so it has to be
                # closed from there -- and before the namespace goes away.
                await self.tunnels.close_master(spec.jump, self._known_runner(profile))
        self._runners.pop(profile.id, None)
        if profile.reach_mode.has_vpn:
            await self.netns.destroy(profile.id)
        profile.tunnel_ip = None
        profile.namespace = None
        await self._set_state(db, profile, ProfileState.IDLE, "disconnected")

    def _known_runner(self, profile: ConnectionProfile) -> Runner:
        """The runner this profile was connected with, without creating anything.

        ``runner_for`` would build the namespace back to tear it down. If the
        namespace is already gone the ssh call simply fails, which is the right
        outcome for a connection that is not there any more.
        """
        cached = self._runners.get(profile.id)
        if cached is not None:
            return cached
        if profile.reach_mode.has_vpn:
            return NetnsRunner(self.netns.ns_name(profile.id))
        return LocalRunner()

    @property
    def live_profile_ids(self) -> set[str]:
        """Profiles this process is actually holding a connection for.

        The database says what state a profile is in; this says whether *we* are
        the process that put it there. Only the owner may mark it degraded.
        """
        return set(self._drivers)

    async def mark_degraded(
        self, db: AsyncSession, profile: ConnectionProfile, detail: str
    ) -> None:
        """Record that a profile we believed was up no longer is.

        Separate from ``disconnect``: the tunnel may come back on the next
        redial, and tearing the namespace down would take every recording still
        using it with us.
        """
        if profile.state == ProfileState.UP:
            await self._set_state(db, profile, ProfileState.DEGRADED, detail)

    async def health(self, profile: ConnectionProfile) -> bool:
        driver = self._drivers.get(profile.id)
        if driver is None:
            return False
        return (await driver.health()).up

    # ---- persistence and events ----------------------------------------

    def _emitter(
        self,
        db: AsyncSession,
        profile: ConnectionProfile,
        attempt_id: str,
        *,
        source_id: str | None = None,
    ):
        async def emit(result: GateResult) -> None:
            db.add(
                GateRun(
                    profile_id=profile.id,
                    source_id=source_id,
                    attempt_id=attempt_id,
                    gate_key=result.key,
                    gate_index=result.index,
                    status=result.status,
                    message=result.message,
                    detail=result.detail,
                    duration_ms=result.duration_ms,
                )
            )
            await db.flush()
            await self.bus.publish(
                Event(
                    type="gate",
                    team_id=profile.team_id,
                    payload={
                        "profile_id": profile.id,
                        "source_id": source_id,
                        "attempt_id": attempt_id,
                        "key": result.key,
                        "index": result.index,
                        "title": result.title,
                        "status": str(result.status),
                        "message": result.message,
                        "detail": result.detail,
                        "duration_ms": result.duration_ms,
                    },
                )
            )

        return emit

    async def _record_probe(
        self, db: AsyncSession, source: CameraSource, ctx: GateContext, results: list[GateResult]
    ) -> None:
        failure = next((r for r in results if r.status is GateStatus.FAILED), None)
        source.last_probe_at = datetime.now(UTC)
        source.last_probe_ok = failure is None
        source.last_probe_detail = failure.message if failure else "ok"
        if ctx.stream and ctx.stream.ok:
            source.codec = ctx.stream.codec
            source.width = ctx.stream.width
            source.height = ctx.stream.height
            source.fps = ctx.stream.fps
        await db.flush()

    async def _set_state(
        self, db: AsyncSession, profile: ConnectionProfile, state: ProfileState, detail: str
    ) -> None:
        profile.state = state
        profile.state_detail = detail
        await db.flush()
        await self.bus.publish(
            Event(
                type="profile_state",
                team_id=profile.team_id,
                payload={
                    "profile_id": profile.id,
                    "state": str(state),
                    "detail": detail,
                    "tunnel_ip": profile.tunnel_ip,
                },
            )
        )

    @staticmethod
    def _state_from(results: list[GateResult]) -> ProfileState:
        if any(r.status is GateStatus.BLOCKED for r in results):
            return ProfileState.NEEDS_INTERACTION
        if any(r.status is GateStatus.FAILED for r in results):
            return ProfileState.FAILED
        return ProfileState.UP

    @staticmethod
    def _detail_from(results: list[GateResult]) -> str:
        blocking = next((r for r in results if r.blocking), None)
        return blocking.message if blocking else "all gates passed"
