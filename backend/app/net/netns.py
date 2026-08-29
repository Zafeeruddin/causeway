"""One network namespace per connection profile.

This is what lets three teams hold three different VPNs on one host, and what
makes the kill-switch real: inside the namespace the default route belongs to
the tunnel, so a process cannot fall back to the host's normal egress when the
VPN drops -- it just fails, which is what we want.

Two kinds of route exist inside each namespace:

  default via the VPN's tun device    -- everything camera-bound
  <control_cidrs> via the veth pair   -- Postgres, Redis, and anything else we
                                         operate that must not travel the tunnel

The control routes are added first and survive the VPN replacing the default,
because they are more specific. That ordering is the whole trick; if a control
route were a default too, a dropped VPN would silently reroute camera traffic
onto the host network.

**Uploads still do not happen in here, even though the storage is local.**
Versity is on our own network, so a route would work -- ``control_cidrs`` is a
list precisely so the storage subnet can be added when we have its address.
Recorders nonetheless write segments to the shared work volume and a shipper
outside every namespace uploads them, for two reasons that survive the storage
moving on-network:

* An upload inside a namespace dies with the tunnel and with the namespace. The
  recorder should be able to tear its namespace down the moment recording ends,
  while a 900 MB upload is still in flight.
* Camera networks are RFC1918 and so is ours. A customer VPN that advertises
  10.0.0.0/8 will collide with a storage host at 10.x.x.x, and the resulting
  failure looks like a storage outage rather than a routing one.

So add the storage subnet to ``control_cidrs`` if something in the namespace
genuinely needs it -- but a failing upload is not that something.

Needs CAP_NET_ADMIN. When it is unavailable (local dev without root) the
manager reports ``available = False`` and callers fall back to LocalRunner --
which is correct for direct mode and unsafe for anything else, so
:meth:`ensure` refuses rather than degrading silently.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass

import structlog

from app.net.runner import CommandFailed, LocalRunner, NetnsRunner, Runner

log = structlog.get_logger(__name__)

#: /30 blocks carved out of here, one per active namespace, for the veth pairs.
DEFAULT_VETH_POOL = ipaddress.IPv4Network("10.201.0.0/16")


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
        control_cidrs: str | list[str] = "172.16.0.0/12",
        pool: ipaddress.IPv4Network = DEFAULT_VETH_POOL,
        runner: Runner | None = None,
        inside_runner: Callable[[str], Runner] | None = None,
    ) -> None:
        self.prefix = prefix
        self.control_cidrs = (
            [c.strip() for c in control_cidrs.split(",") if c.strip()]
            if isinstance(control_cidrs, str)
            else list(control_cidrs)
        )
        self._pool = pool
        self._host = runner or LocalRunner()
        # How commands are run *inside* a namespace. Injectable so the wiring
        # can be tested without CAP_NET_ADMIN, and so a future driver that owns
        # its namespace differently can supply its own.
        self._inside = inside_runner or NetnsRunner
        self._active: dict[str, Namespace] = {}
        self._next_block = 0
        #: Why the kernel last refused to create a namespace here, if it has.
        self._refused: str | None = None
        #: Addresses reachable from each namespace that are not control plane.
        self._allowed: dict[str, set[str]] = {}

    # ---- capability ----------------------------------------------------

    @property
    def available(self) -> bool:
        """True when this process can actually manipulate namespaces.

        Being root is necessary and not sufficient: creating a namespace mounts,
        and a container can hold every capability while a mediation layer still
        refuses the mount. That cannot be settled without trying, so this stays
        a guess until a refusal proves it wrong -- after which it reports the
        truth rather than repeating the guess.
        """
        if self._refused is not None:
            return False
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
                self._refused
                or "network namespaces need CAP_NET_ADMIN. Run the agent container with "
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

        try:
            await self._run(["ip", "netns", "add", ns.name])
        except CommandFailed as exc:
            raise self._refusal(exc) from exc
        try:
            await self._wire(ns)
        except Exception:
            await self.destroy(profile_id)
            raise

        self._active[name] = ns
        log.info("netns.created", namespace=ns.name, ns_ip=str(ns.ns_ip))
        return ns

    def _refusal(self, exc: CommandFailed) -> NetnsUnavailable:
        """Turn a refused ``ip netns add`` into something a person can act on.

        Creating a namespace is a mount, and more than one thing can refuse it.
        The two refusals read alike as a failed command and mean different
        things: ``Operation not permitted`` is the missing capability, while
        ``Permission denied`` is a mediation layer that the capability does not
        satisfy. Reported as "CommandFailed" they are indistinguishable, and the
        person is left to guess which of the two they are looking at.
        """
        detail = exc.result.stderr.strip().splitlines()[-1][:200] if exc.result.stderr else str(exc)
        self._refused = (
            f"this container cannot create network namespaces: {detail}. "
            "Creating one is a mount, so the agent needs "
            "cap_add: [NET_ADMIN, SYS_ADMIN] and security_opt: [apparmor:unconfined] -- "
            "NET_ADMIN on its own is not enough. Direct-mode profiles work regardless."
        )
        log.error("netns.refused", namespace=self.prefix, detail=detail)
        return NetnsUnavailable(self._refused)

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

        # Control-plane traffic only. Deliberately NOT default routes: when the
        # VPN comes up it installs the default, and camera traffic must never be
        # able to fall back here.
        for cidr in self.control_cidrs:
            await inside.run(["ip", "route", "add", cidr, "via", str(ns.host_ip)], check=True)

        await self._enable_forwarding()
        await self._nat(ns, "-A")

    async def _enable_forwarding(self) -> None:
        """Turn on IPv4 forwarding, or confirm someone already did.

        The veth pair reaches the control plane through the agent's own stack,
        so forwarding has to be on. A container cannot turn it on: /proc/sys is
        mounted read-only, and ``sysctl -w`` is refused there whatever
        capabilities it holds. What the container runtime *can* do is set it at
        creation -- ``sysctls:`` in compose does exactly that -- so arriving here
        with it already on is the normal case in the deployment we ship, and
        treating the failed write as fatal breaks a correctly configured host.
        Writing is for an agent run outside a container.
        """
        try:
            await self._host.run(
                ["sysctl", "-w", "net.ipv4.ip_forward=1"], check=True, timeout=15.0
            )
            return
        except CommandFailed as exc:
            result = await self._host.run(["sysctl", "-n", "net.ipv4.ip_forward"], timeout=15.0)
            if result.stdout.strip() != "1":
                raise NetnsUnavailable(
                    "IPv4 forwarding is off and this container cannot turn it on "
                    "(/proc/sys is read-only). Set it on the agent service with "
                    "sysctls: {net.ipv4.ip_forward: 1}, or enable it on the host."
                ) from exc
            log.debug("netns.forwarding_already_on")

    async def allow_host(self, profile_id: str, host: str) -> list[str]:
        """Let one host outside the control plane be reached from the namespace.

        The namespace has no default route on purpose: camera traffic must never
        be able to fall back to the host's network when the tunnel drops. But
        the VPN client has to reach its gateway *before* there is a tunnel, and
        the gateway is on the public internet. Without this the dial fails with
        "connect: Network is unreachable" -- which reads like the gateway is
        down, when what is missing is the one route out.

        So the gateway, and only the gateway, gets a host route. Everything else
        still has nowhere to go until the VPN installs the default itself.
        """
        name = self.ns_name(profile_id)
        ns = self._active.get(name)
        if ns is None or not host:
            return []

        addresses = await self._resolve(host)
        allowed = self._allowed.setdefault(name, set())
        inside = self._inside(name)
        for address in addresses:
            if address in allowed:
                continue
            # `replace` rather than `add`: a gateway that has moved should end up
            # with one correct route, not a second one and an error.
            await inside.run(
                ["ip", "route", "replace", f"{address}/32", "via", str(ns.host_ip)], check=True
            )
            await self._masquerade(ns, f"{address}/32", "-A", skip_if_present=True)
            allowed.add(address)
            log.info("netns.host_allowed", namespace=name, host=host, address=address)
        return addresses

    async def _resolve(self, host: str) -> list[str]:
        """Addresses for ``host``, resolved out here rather than in the namespace.

        The namespace has no DNS -- that is the point of it -- so a gateway
        named rather than numbered can only be resolved by the agent itself.
        """
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            pass
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, None, family=socket.AF_INET)
        except OSError as exc:
            raise NetnsUnavailable(
                f"the VPN gateway {host!r} could not be resolved: {exc}"
            ) from exc
        return sorted({str(info[4][0]) for info in infos})

    async def destroy(self, profile_id: str) -> None:
        name = self.ns_name(profile_id)
        ns = self._active.pop(name, None)
        if ns is not None:
            for address in self._allowed.pop(name, set()):
                await self._masquerade(ns, f"{address}/32", "-D", ignore_errors=True)
            await self._nat(ns, "-D", ignore_errors=True)
            await self._run(["ip", "link", "del", ns.host_if], ignore_errors=True)
        await self._run(["ip", "netns", "del", name], ignore_errors=True)
        log.info("netns.destroyed", namespace=name)

    async def destroy_all(self) -> None:
        for name in list(self._active):
            await self.destroy(name.removeprefix(f"{self.prefix}-"))

    # ---- helpers -------------------------------------------------------

    async def _nat(self, ns: Namespace, op: str, *, ignore_errors: bool = False) -> None:
        for cidr in self.control_cidrs:
            await self._masquerade(ns, cidr, op, ignore_errors=ignore_errors)

    async def _masquerade(
        self,
        ns: Namespace,
        destination: str,
        op: str,
        *,
        ignore_errors: bool = False,
        skip_if_present: bool = False,
    ) -> None:
        rule = [
            "POSTROUTING",
            "-s",
            f"{ns.ns_ip}/32",
            "-d",
            destination,
            "-j",
            "MASQUERADE",
        ]
        if skip_if_present:
            # An agent restart forgets which rules it added; the namespace and
            # its rules survive. Asking iptables is the only way to know.
            present = await self._host.run(
                ["iptables", "-t", "nat", "-C", *rule], check=False, timeout=15.0
            )
            if present.returncode == 0:
                return
        await self._run(["iptables", "-t", "nat", op, *rule], ignore_errors=ignore_errors)

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
