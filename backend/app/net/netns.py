"""One network namespace per connection profile.

This is what lets three teams hold three different VPNs on one host, and what
makes the kill-switch real: inside the namespace the default route belongs to
the tunnel, so a process cannot fall back to the host's normal egress when the
VPN drops -- it just fails, which is what we want.

Two routes exist inside each namespace:

  default via the VPN's tun device   -- everything camera-bound
  <control_cidr> via the veth pair   -- Postgres, Redis, MinIO only

The control route is added first and survives the VPN replacing the default,
because it is more specific. That ordering is the whole trick; if the control
route were a default too, a dropped VPN would silently reroute camera traffic
onto the host network.

Needs CAP_NET_ADMIN. When it is unavailable (local dev without root) the
manager reports ``available = False`` and callers fall back to LocalRunner --
which is correct for direct mode and unsafe for anything else, so
:meth:`ensure` refuses rather than degrading silently.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Callable
from dataclasses import dataclass

import structlog

from app.net.runner import CommandFailed, LocalRunner, NetnsRunner, Runner

log = structlog.get_logger(__name__)

#: /30 blocks carved out of here, one per active namespace, for the veth pairs.
DEFAULT_VETH_POOL = ipaddress.ip_network("10.201.0.0/16")


class NetnsUnavailable(RuntimeError):
    """We need a namespace and cannot make one."""


@dataclass(frozen=True, slots=True)
class Namespace:
    name: str
    host_if: str
    peer_if: str
    host_ip: ipaddress.IPv4Address
    ns_ip: ipaddress.IPv4Address
    prefixlen: int

    @property
    def runner(self) -> Runner:
        return NetnsRunner(self.name)


class NetnsManager:
    """Creates, wires and tears down per-profile namespaces."""

    def __init__(
        self,
        *,
        prefix: str = "cam",
        control_cidr: str = "172.16.0.0/12",
        pool: ipaddress.IPv4Network = DEFAULT_VETH_POOL,
        runner: Runner | None = None,
        inside_runner: Callable[[str], Runner] | None = None,
    ) -> None:
        self.prefix = prefix
        self.control_cidr = control_cidr
        self._pool = pool
        self._host = runner or LocalRunner()
        # How commands are run *inside* a namespace. Injectable so the wiring
        # can be tested without CAP_NET_ADMIN, and so a future driver that owns
        # its namespace differently can supply its own.
        self._inside = inside_runner or NetnsRunner
        self._active: dict[str, Namespace] = {}
        self._next_block = 0

    # ---- capability ----------------------------------------------------

    @property
    def available(self) -> bool:
        """True when this process can actually manipulate namespaces."""
        if os.geteuid() == 0:
            return True
        # CAP_NET_ADMIN without root is possible; probing is cheaper than parsing caps.
        return os.environ.get("CAM_FORCE_NETNS") == "1"

    # ---- lifecycle -----------------------------------------------------

    def ns_name(self, profile_id: str) -> str:
        # Interface names cap at 15 chars, so the profile id is truncated hard.
        return f"{self.prefix}-{profile_id[:8]}"

    async def ensure(self, profile_id: str) -> Namespace:
        """Create the namespace for ``profile_id`` if it does not exist yet."""
        name = self.ns_name(profile_id)
        if name in self._active:
            return self._active[name]
        if not self.available:
            raise NetnsUnavailable(
                "network namespaces need CAP_NET_ADMIN. Run the agent container with "
                "cap_add: [NET_ADMIN], or use a direct-mode profile for local development."
            )

        block = self._take_block()
        host_ip, ns_ip = block[1], block[2]
        short = profile_id[:8]
        ns = Namespace(
            name=name,
            host_if=f"veth-h-{short}"[:15],
            peer_if=f"veth-n-{short}"[:15],
            host_ip=host_ip,
            ns_ip=ns_ip,
            prefixlen=block.prefixlen,
        )

        await self._run(["ip", "netns", "add", ns.name])
        try:
            await self._wire(ns)
        except Exception:
            await self.destroy(profile_id)
            raise

        self._active[name] = ns
        log.info("netns.created", namespace=ns.name, ns_ip=str(ns.ns_ip))
        return ns

    async def _wire(self, ns: Namespace) -> None:
        inside = self._inside(ns.name)

        # Loopback comes up down. ssh -L binds 127.0.0.1 and ffmpeg dials it,
        # so forgetting this produces a tunnel that connects and forwards nothing.
        await inside.run(["ip", "link", "set", "lo", "up"], check=True)

        await self._run(
            ["ip", "link", "add", ns.host_if, "type", "veth", "peer", "name", ns.peer_if]
        )
        await self._run(["ip", "link", "set", ns.peer_if, "netns", ns.name])
        await self._run(["ip", "addr", "add", f"{ns.host_ip}/{ns.prefixlen}", "dev", ns.host_if])
        await self._run(["ip", "link", "set", ns.host_if, "up"])

        await inside.run(
            ["ip", "addr", "add", f"{ns.ns_ip}/{ns.prefixlen}", "dev", ns.peer_if], check=True
        )
        await inside.run(["ip", "link", "set", ns.peer_if, "up"], check=True)

        # Control-plane traffic only. Deliberately NOT a default route: when the
        # VPN comes up it installs the default, and camera traffic must never be
        # able to fall back here.
        await inside.run(
            ["ip", "route", "add", self.control_cidr, "via", str(ns.host_ip)], check=True
        )

        await self._run(["sysctl", "-w", "net.ipv4.ip_forward=1"])
        await self._nat(ns, "-A")

    async def destroy(self, profile_id: str) -> None:
        name = self.ns_name(profile_id)
        ns = self._active.pop(name, None)
        if ns is not None:
            await self._nat(ns, "-D", ignore_errors=True)
            await self._run(["ip", "link", "del", ns.host_if], ignore_errors=True)
        await self._run(["ip", "netns", "del", name], ignore_errors=True)
        log.info("netns.destroyed", namespace=name)

    async def destroy_all(self) -> None:
        for name in list(self._active):
            await self.destroy(name.removeprefix(f"{self.prefix}-"))

    # ---- helpers -------------------------------------------------------

    async def _nat(self, ns: Namespace, op: str, *, ignore_errors: bool = False) -> None:
        await self._run(
            [
                "iptables",
                "-t",
                "nat",
                op,
                "POSTROUTING",
                "-s",
                f"{ns.ns_ip}/32",
                "-d",
                self.control_cidr,
                "-j",
                "MASQUERADE",
            ],
            ignore_errors=ignore_errors,
        )

    async def _run(self, argv: list[str], *, ignore_errors: bool = False) -> None:
        try:
            await self._host.run(argv, check=not ignore_errors, timeout=15.0)
        except CommandFailed:
            if not ignore_errors:
                raise

    def _take_block(self) -> ipaddress.IPv4Network:
        block = list(self._pool.subnets(new_prefix=30))[self._next_block]
        self._next_block += 1
        return block


def runner_for(namespace: Namespace | None) -> Runner:
    """The one place that decides local-vs-namespaced execution."""
    return namespace.runner if namespace is not None else LocalRunner()
