"""The eight gates.

"Connect" is not one operation. When it fails, the person looking at the
dashboard needs to know which hop failed and what to do about it -- not a
spinner and not a stack trace. Every connection attempt walks this ladder and
reports each rung as it passes, skips or fails.

Gates the profile's reachability mode does not use are SKIPPED, not silently
omitted: a direct-mode camera visibly runs gates 7 and 8 only, which is how you
tell "we didn't need a VPN" apart from "the VPN check never ran".
"""

from __future__ import annotations

import abc
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from app.enums import GateStatus, ReachMode, SourceKind
from app.gates.probes import StreamInfo, stream_probe, tcp_probe
from app.net.runner import Runner
from app.net.ssh import JumpHost, SshError, TunnelManager
from app.net.vpn.base import (
    InteractionRequired,
    TrustPromptRequired,
    VpnConfig,
    VpnDriver,
    VpnError,
)
from app.security.redaction import StreamUrl

log = structlog.get_logger(__name__)


# ---- inputs ------------------------------------------------------------


@dataclass(slots=True)
class ProfileSpec:
    """A connection profile with its secrets already resolved.

    Deliberately not the ORM row: the gates are pure enough to unit-test with a
    fake runner and no database.
    """

    id: str
    name: str
    mode: ReachMode
    vpn: VpnConfig
    jump: JumpHost | None = None
    #: Endpoint that confirms this VPN account is whitelisted. Skipped when unset.
    whitelist_url: str | None = None


@dataclass(slots=True)
class SourceSpec:
    id: str
    kind: SourceKind
    url: StreamUrl
    #: Address as seen from the far side of the tunnel.
    host: str = ""
    port: int = 0


@dataclass(slots=True)
class GateResult:
    key: str
    index: int
    title: str
    status: GateStatus
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    @property
    def blocking(self) -> bool:
        return self.status in (GateStatus.FAILED, GateStatus.BLOCKED)


@dataclass
class GateContext:
    """Carried down the ladder. Later gates read what earlier ones established."""

    profile: ProfileSpec
    runner: Runner
    driver: VpnDriver
    tunnels: TunnelManager
    source: SourceSpec | None = None

    tunnel_ip: str | None = None
    local_port: int | None = None
    stream: StreamInfo | None = None
    #: Set when the dial stops for a human. Read by the certificate gate.
    interaction: InteractionRequired | None = None
    vpn_error: VpnError | None = None


Emit = Callable[[GateResult], Awaitable[None]]


# ---- gates -------------------------------------------------------------


class Gate(abc.ABC):
    key: str
    title: str
    index: int
    #: Gates that need a specific camera source, rather than just the profile.
    source_scoped: bool = False

    def applies(self, ctx: GateContext) -> bool:
        return True

    def skip_reason(self, ctx: GateContext) -> str:
        return "not needed for this connection"

    @abc.abstractmethod
    async def check(self, ctx: GateContext) -> GateResult: ...

    # -- helpers for subclasses --

    def passed(self, message: str, **detail: Any) -> GateResult:
        return self._result(GateStatus.PASSED, message, detail)

    def failed(self, message: str, **detail: Any) -> GateResult:
        return self._result(GateStatus.FAILED, message, detail)

    def blocked(self, message: str, **detail: Any) -> GateResult:
        return self._result(GateStatus.BLOCKED, message, detail)

    def skipped(self, message: str) -> GateResult:
        return self._result(GateStatus.SKIPPED, message, {})

    def _result(self, status: GateStatus, message: str, detail: dict[str, Any]) -> GateResult:
        return GateResult(
            key=self.key,
            index=self.index,
            title=self.title,
            status=status,
            message=message,
            detail=detail,
        )


class VpnDialGate(Gate):
    key, title, index = "vpn_dial", "VPN dial", 1

    def applies(self, ctx: GateContext) -> bool:
        return ctx.profile.mode.has_vpn

    def skip_reason(self, ctx: GateContext) -> str:
        return "this profile has no VPN hop"

    async def check(self, ctx: GateContext) -> GateResult:
        try:
            status = await ctx.driver.dial(ctx.profile.vpn)
        except InteractionRequired as exc:
            # We reached the gateway and it spoke to us; it just wants a decision
            # from a person first. That is the certificate gate's problem, not a
            # dial failure -- so this rung passes and the next one blocks.
            ctx.interaction = exc
            return self.passed("reached the gateway", awaiting_interaction=True)
        except VpnError as exc:
            ctx.vpn_error = exc
            return self.failed(exc.user_message, log_tail=ctx.driver.log_tail[-2000:])

        ctx.tunnel_ip = status.tunnel_ip
        return self.passed(
            f"tunnel up on {status.interface or 'the VPN interface'}",
            tunnel_ip=status.tunnel_ip,
            interface=status.interface,
        )


class CertificateTrustGate(Gate):
    """The dialog FortiClient shows on the desktop, asked once and remembered.

    A blocked result is not an error: it carries the host and fingerprint so the
    dashboard can show Accept / Deny, and accepting pins the digest on the
    profile for every future dial.
    """

    key, title, index = "cert_trust", "Certificate trust", 2

    def applies(self, ctx: GateContext) -> bool:
        return ctx.profile.mode.has_vpn

    def skip_reason(self, ctx: GateContext) -> str:
        return "this profile has no VPN hop"

    async def check(self, ctx: GateContext) -> GateResult:
        prompt = ctx.interaction
        if isinstance(prompt, TrustPromptRequired):
            return self.blocked(
                prompt.user_message,
                action="accept_certificate",
                host=prompt.host,
                fingerprint=prompt.fingerprint,
                algorithm=prompt.algorithm,
                reason=prompt.reason,
            )
        if prompt is not None:
            return self.blocked(prompt.user_message, action="unknown")
        if ctx.profile.vpn.trusted_cert:
            return self.passed(
                "certificate matches the pinned fingerprint",
                fingerprint=ctx.profile.vpn.trusted_cert,
            )
        return self.passed("the gateway certificate validated against the system trust store")


class WhitelistGate(Gate):
    """Confirms the VPN account is allowed through, from inside the tunnel.

    Runs against the tunnel's own source address, so a pass here means the main
    server saw us as whitelisted -- not that the URL happens to be reachable.
    """

    key, title, index = "whitelist", "Whitelist check", 3

    def applies(self, ctx: GateContext) -> bool:
        return bool(ctx.profile.whitelist_url)

    def skip_reason(self, ctx: GateContext) -> str:
        return "no whitelist endpoint is configured for this profile"

    async def check(self, ctx: GateContext) -> GateResult:
        url = ctx.profile.whitelist_url or ""
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            return self.failed(
                f"could not reach the whitelist check at {url} - {type(exc).__name__}",
                tunnel_ip=ctx.tunnel_ip,
            )

        if response.status_code in (401, 403):
            return self.failed(
                "Connected, but this account is not whitelisted yet. "
                f"Tunnel IP {ctx.tunnel_ip or 'unknown'} - send that to infra.",
                tunnel_ip=ctx.tunnel_ip,
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            return self.failed(
                f"the whitelist check answered {response.status_code}",
                status_code=response.status_code,
            )
        return self.passed("this account is whitelisted", tunnel_ip=ctx.tunnel_ip)


class JumpRouteGate(Gate):
    key, title, index = "jump_route", "Route to the jump host", 4

    def applies(self, ctx: GateContext) -> bool:
        return ctx.profile.mode.has_jump

    def skip_reason(self, ctx: GateContext) -> str:
        return "this profile reaches cameras without a jump host"

    async def check(self, ctx: GateContext) -> GateResult:
        jump = ctx.profile.jump
        assert jump is not None
        probe = await tcp_probe(ctx.runner, jump.host, jump.port)
        if probe.open:
            return self.passed(f"{jump.host}:{jump.port} is reachable")
        if ctx.profile.mode.has_vpn:
            return self.failed(
                f"The VPN is up but {jump.host} is not routable. "
                "Split-tunnel settings may be excluding the camera subnet.",
                detail_text=probe.detail,
            )
        return self.failed(f"{jump.host}:{jump.port} is not reachable.", detail_text=probe.detail)


class SshAuthGate(Gate):
    key, title, index = "ssh_auth", "SSH authentication", 5

    def applies(self, ctx: GateContext) -> bool:
        return ctx.profile.mode.has_jump

    def skip_reason(self, ctx: GateContext) -> str:
        return "this profile reaches cameras without a jump host"

    async def check(self, ctx: GateContext) -> GateResult:
        jump = ctx.profile.jump
        assert jump is not None
        try:
            await ctx.tunnels.open_master(jump, ctx.runner)
        except SshError as exc:
            return self.failed(exc.user_message, detail_text=str(exc)[:300])
        return self.passed(f"connected as {jump.username} using {jump.auth} authentication")


class PortForwardGate(Gate):
    key, title, index = "port_forward", "Port forward", 6
    source_scoped = True

    def applies(self, ctx: GateContext) -> bool:
        return ctx.profile.mode.has_jump and ctx.source is not None

    def skip_reason(self, ctx: GateContext) -> str:
        if ctx.source is None:
            return "no camera selected yet"
        return "this camera is reached without a forward"

    async def check(self, ctx: GateContext) -> GateResult:
        jump, source = ctx.profile.jump, ctx.source
        assert jump is not None and source is not None
        try:
            lease = await ctx.tunnels.forward(jump, ctx.runner, source.id, source.host, source.port)
        except SshError as exc:
            return self.failed(exc.user_message, detail_text=str(exc)[:300])
        ctx.local_port = lease.port
        return self.passed(
            f"127.0.0.1:{lease.port} forwards to {source.host}:{source.port}",
            local_port=lease.port,
            target=lease.target,
        )


class CameraReachableGate(Gate):
    key, title, index = "camera_reachable", "Camera reachable", 7
    source_scoped = True

    def applies(self, ctx: GateContext) -> bool:
        return ctx.source is not None and ctx.source.kind is SourceKind.RTSP

    def skip_reason(self, ctx: GateContext) -> str:
        if ctx.source is None:
            return "no camera selected yet"
        return "HLS sources are checked by their manifest instead"

    async def check(self, ctx: GateContext) -> GateResult:
        source = ctx.source
        assert source is not None
        host, port = ("127.0.0.1", ctx.local_port) if ctx.local_port else (source.host, source.port)
        probe = await tcp_probe(ctx.runner, host, port)
        if probe.open:
            return self.passed(f"{source.host}:{source.port} accepts connections")
        via = " through the tunnel" if ctx.local_port else ""
        return self.failed(
            f"Cannot reach {source.host}:{source.port}{via} - the camera is off, "
            "or it is on a network this path does not see.",
            detail_text=probe.detail,
        )


class StreamHandshakeGate(Gate):
    """The last rung, and the only one that proves video actually arrives."""

    key, title, index = "stream_handshake", "Stream handshake", 8
    source_scoped = True

    def applies(self, ctx: GateContext) -> bool:
        return ctx.source is not None

    def skip_reason(self, ctx: GateContext) -> str:
        return "no camera selected yet"

    async def check(self, ctx: GateContext) -> GateResult:
        source = ctx.source
        assert source is not None
        url = source.url
        if ctx.local_port:
            url = url.with_host("127.0.0.1", ctx.local_port)

        info = await stream_probe(ctx.runner, url, rtsp=source.kind is SourceKind.RTSP)
        ctx.stream = info
        if not info.ok:
            return self.failed(info.detail, url=str(url))
        summary = " ".join(
            part
            for part in (info.codec, info.resolution, f"@ {info.fps}fps" if info.fps else "")
            if part
        )
        return self.passed(
            summary or "stream ok",
            codec=info.codec,
            resolution=info.resolution,
            fps=info.fps,
            audio_codec=info.audio_codec,
        )


#: Order is the contract. The UI numbers rungs from this list.
LADDER: list[Gate] = [
    VpnDialGate(),
    CertificateTrustGate(),
    WhitelistGate(),
    JumpRouteGate(),
    SshAuthGate(),
    PortForwardGate(),
    CameraReachableGate(),
    StreamHandshakeGate(),
]


async def run_ladder(
    ctx: GateContext,
    *,
    emit: Emit | None = None,
    gates: list[Gate] | None = None,
    stop_on_failure: bool = True,
) -> list[GateResult]:
    """Walk the ladder, reporting each rung as it resolves.

    Stops at the first blocking rung by default -- there is no point probing a
    camera through a tunnel that never opened, and the noise buries the one
    result that matters.
    """
    results: list[GateResult] = []
    for gate in gates or LADDER:
        if not gate.applies(ctx):
            result = gate.skipped(gate.skip_reason(ctx))
        else:
            started = time.monotonic()
            try:
                result = await gate.check(ctx)
            except Exception as exc:  # noqa: BLE001 - a gate must never crash the ladder
                log.exception("gate.crashed", gate=gate.key)
                result = gate.failed(f"{gate.title} could not complete: {type(exc).__name__}")
            result.duration_ms = int((time.monotonic() - started) * 1000)

        results.append(result)
        if emit is not None:
            await emit(result)
        log.info("gate", gate=gate.key, status=str(result.status), message=result.message)

        if stop_on_failure and result.blocking:
            for remaining in (gates or LADDER)[len(results) :]:
                pending = remaining._result(GateStatus.PENDING, "not reached", {})
                results.append(pending)
                if emit is not None:
                    await emit(pending)
            break

    return results


def connect_gates() -> list[Gate]:
    """Rungs that describe the profile itself -- what the heartbeat re-checks."""
    return [gate for gate in LADDER if not gate.source_scoped]


def source_gates() -> list[Gate]:
    """Rungs that need a specific camera."""
    return [gate for gate in LADDER if gate.source_scoped]
