"""The namespace wiring and the ssh argv are both places where a subtle mistake
produces a system that looks connected and carries nothing."""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from app.enums import SshAuth
from app.net.netns import NetnsManager, NetnsUnavailable
from app.net.ports import NoPortsAvailable, PortPool
from app.net.runner import CommandFailed, LocalRunner, NetnsRunner, ProcResult
from app.net.ssh import JumpHost, SshAuthFailed, TunnelManager
from tests.conftest import FakeRunner


def test_netns_runner_prefixes_every_command():
    assert NetnsRunner("cam-abc").wrap(["ffmpeg", "-i", "x"]) == [
        "ip",
        "netns",
        "exec",
        "cam-abc",
        "ffmpeg",
        "-i",
        "x",
    ]
    assert LocalRunner().wrap(["ffmpeg", "-i", "x"]) == ["ffmpeg", "-i", "x"]


async def test_namespace_brings_loopback_up_before_anything_else(monkeypatch):
    """ssh -L binds 127.0.0.1 and ffmpeg dials it. A namespace with lo down
    forwards nothing, and says nothing about why."""
    monkeypatch.setenv("CAM_FORCE_NETNS", "1")
    runner = FakeRunner()
    manager = NetnsManager(prefix="cam", runner=runner, inside_runner=lambda _: runner)

    await manager.ensure("abcdef12-3456")

    flat = [" ".join(c) for c in runner.calls]
    lo_up = next(i for i, c in enumerate(flat) if c == "ip link set lo up")
    veth = next(i for i, c in enumerate(flat) if "link add veth-h" in c)
    assert lo_up < veth, "loopback must come up before the veth pair is wired"


async def test_multiple_control_networks_each_get_their_own_specific_route(monkeypatch):
    """Storage on our own network can be added here -- as a route, never a default."""
    monkeypatch.setenv("CAM_FORCE_NETNS", "1")
    runner = FakeRunner()
    manager = NetnsManager(
        prefix="cam",
        control_cidrs="172.28.0.0/16,10.90.0.0/24",
        runner=runner,
        inside_runner=lambda _: runner,
    )
    await manager.ensure("abcdef12-3456")

    routes = [" ".join(c) for c in runner.calls if "route add" in " ".join(c)]
    assert any("172.28.0.0/16" in r for r in routes)
    assert any("10.90.0.0/24" in r for r in routes)
    assert not any("default" in r for r in routes)


async def test_the_control_route_is_specific_and_never_a_default(monkeypatch):
    """If the control route were a default, a dropped VPN would silently reroute
    camera traffic onto the host network instead of failing."""
    monkeypatch.setenv("CAM_FORCE_NETNS", "1")
    runner = FakeRunner()
    manager = NetnsManager(
        prefix="cam", control_cidrs="172.28.0.0/16", runner=runner, inside_runner=lambda _: runner
    )

    await manager.ensure("abcdef12-3456")

    routes = [" ".join(c) for c in runner.calls if "route add" in " ".join(c)]
    assert routes, "expected a control route"
    assert all("172.28.0.0/16" in r for r in routes)
    assert not any("default" in r for r in routes)


async def test_a_namespace_without_privileges_refuses_rather_than_degrading(monkeypatch):
    monkeypatch.delenv("CAM_FORCE_NETNS", raising=False)
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    with pytest.raises(NetnsUnavailable, match="CAP_NET_ADMIN"):
        await NetnsManager(runner=FakeRunner()).ensure("abc")


async def test_the_vpn_gateway_gets_a_route_out_but_nothing_else_does(monkeypatch):
    """The namespace has no default route on purpose, and the VPN client still
    has to reach its gateway to build the tunnel that supplies one. Without this
    the dial fails "connect: Network is unreachable", which reads like the
    gateway is down rather than unrouted."""
    monkeypatch.setenv("CAM_FORCE_NETNS", "1")
    #: iptables -C exits non-zero when the rule is not there yet.
    runner = FakeRunner(results={"nat -C": ProcResult(1, "", "")})
    manager = NetnsManager(
        prefix="cam", control_cidrs="172.16.0.0/12", runner=runner, inside_runner=lambda _: runner
    )
    await manager.ensure("abcdef12-3456")
    runner.calls.clear()

    assert await manager.allow_host("abcdef12-3456", "82.197.58.159") == ["82.197.58.159"]

    flat = [" ".join(c) for c in runner.calls]
    assert any("route replace 82.197.58.159/32 via" in c for c in flat)
    assert any(
        "iptables -t nat -A POSTROUTING" in c and "-d 82.197.58.159/32" in c for c in flat
    ), "the gateway needs NAT as well as a route"
    assert not any("route" in c and "default" in c for c in flat), "never a default route"


async def test_a_gateway_already_routed_is_not_routed_twice(monkeypatch):
    monkeypatch.setenv("CAM_FORCE_NETNS", "1")
    runner = FakeRunner(results={"nat -C": ProcResult(1, "", "")})
    manager = NetnsManager(prefix="cam", runner=runner, inside_runner=lambda _: runner)
    await manager.ensure("abcdef12-3456")
    await manager.allow_host("abcdef12-3456", "82.197.58.159")
    runner.calls.clear()

    await manager.allow_host("abcdef12-3456", "82.197.58.159")

    assert runner.calls == []


async def test_a_gateway_that_does_not_resolve_says_so(monkeypatch):
    """A named gateway can only be resolved out here -- the namespace has no
    DNS, which is the point of it."""
    monkeypatch.setenv("CAM_FORCE_NETNS", "1")
    runner = FakeRunner()
    manager = NetnsManager(prefix="cam", runner=runner, inside_runner=lambda _: runner)
    await manager.ensure("abcdef12-3456")

    async def cannot_resolve(*_args, **_kwargs):
        raise OSError("Name or service not known")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", cannot_resolve)
    with pytest.raises(NetnsUnavailable, match="could not be resolved"):
        await manager.allow_host("abcdef12-3456", "vpn.example.invalid")


class RefusingRunner(FakeRunner):
    """A container that is root and still cannot mount."""

    def __init__(self, stderr: str) -> None:
        super().__init__()
        self.stderr = stderr

    async def run(self, argv, *, timeout=30.0, stdin=None, env=None, check=False):
        await super().run(argv, timeout=timeout, stdin=stdin, env=env, check=check)
        if "netns add" in " ".join(argv):
            raise CommandFailed(argv, ProcResult(1, "", self.stderr))
        return self.default


async def test_a_refused_mount_says_what_is_missing_rather_than_that_a_command_failed(monkeypatch):
    """`ip netns add` mounts, and the two ways that is refused read alike.
    Surfaced as CommandFailed they tell the person nothing they can act on."""
    monkeypatch.setenv("CAM_FORCE_NETNS", "1")
    runner = RefusingRunner("mount --make-shared /run/netns failed: Operation not permitted")
    manager = NetnsManager(prefix="cam", runner=runner, inside_runner=lambda _: runner)

    with pytest.raises(NetnsUnavailable) as caught:
        await manager.ensure("abcdef12-3456")

    assert "mount --make-shared" in str(caught.value)
    assert "SYS_ADMIN" in str(caught.value)


async def test_a_container_that_has_been_refused_stops_claiming_it_can(monkeypatch):
    """Root in a container is a guess, and health reports it as a fact. Once the
    kernel has said no, the guess has been settled and must not come back."""
    monkeypatch.setenv("CAM_FORCE_NETNS", "1")
    monkeypatch.setattr("os.geteuid", lambda: 0)
    runner = RefusingRunner("mount --make-shared /run/netns failed: Permission denied")
    manager = NetnsManager(prefix="cam", runner=runner, inside_runner=lambda _: runner)
    assert manager.available

    with pytest.raises(NetnsUnavailable):
        await manager.ensure("abcdef12-3456")

    assert not manager.available
    # And the second attempt repeats the reason rather than the generic hint.
    with pytest.raises(NetnsUnavailable, match="Permission denied"):
        await manager.ensure("beefcafe-0000")


class ReadOnlySysctlRunner(FakeRunner):
    """A container with /proc/sys mounted read-only, which is every container."""

    def __init__(self, current: str) -> None:
        super().__init__()
        self.current = current

    async def run(self, argv, *, timeout=30.0, stdin=None, env=None, check=False):
        await super().run(argv, timeout=timeout, stdin=stdin, env=env, check=check)
        joined = " ".join(argv)
        if "sysctl -w" in joined:
            raise CommandFailed(
                argv, ProcResult(1, "", 'sysctl: permission denied on key "net.ipv4.ip_forward"')
            )
        if "sysctl -n" in joined:
            return ProcResult(0, self.current, "")
        return self.default


async def test_forwarding_already_on_is_not_a_failure(monkeypatch):
    """compose sets ip_forward at container creation, which is the only way a
    container can set it at all. Insisting on writing it ourselves fails on
    exactly the deployments that had it right."""
    monkeypatch.setenv("CAM_FORCE_NETNS", "1")
    runner = ReadOnlySysctlRunner("1\n")
    manager = NetnsManager(prefix="cam", runner=runner, inside_runner=lambda _: runner)

    ns = await manager.ensure("abcdef12-3456")

    assert ns.name == "cam-abcdef12"
    assert any("iptables" in " ".join(c) for c in runner.calls), "NAT should still be applied"


async def test_forwarding_that_is_off_and_unwritable_says_where_to_set_it(monkeypatch):
    monkeypatch.setenv("CAM_FORCE_NETNS", "1")
    runner = ReadOnlySysctlRunner("0\n")
    manager = NetnsManager(prefix="cam", runner=runner, inside_runner=lambda _: runner)

    with pytest.raises(NetnsUnavailable, match="ip_forward"):
        await manager.ensure("abcdef12-3456")


#: Make the fake report "no master yet", so open_master actually dials.
NO_MASTER = {"-O check": ProcResult(255, "", "No ControlPath specified")}


async def test_ssh_password_never_reaches_argv():
    """A password in argv is visible in /proc and in any process listing on the
    host. sshpass reads it from the environment instead."""
    runner = FakeRunner(results=dict(NO_MASTER))
    manager = TunnelManager(control_dir="/tmp/cam-test-ctl")
    jump = JumpHost(host="10.0.0.71", username="ops", auth=SshAuth.PASSWORD, password="hunter2")

    await manager.open_master(jump, runner)

    for call in runner.calls:
        assert "hunter2" not in " ".join(call)
    assert any(call[0] == "sshpass" and "-e" in call for call in runner.calls)


async def test_a_control_socket_left_by_a_dead_master_is_removed_first(tmp_path):
    """The control directory is a volume and outlives the process; the master is
    a child process and does not. ssh will not bind a ControlPath that already
    exists -- it disables multiplexing and connects anyway, so the dial succeeds
    and every later `-O forward` is refused against a socket nobody is holding.
    """
    runner = FakeRunner(results=dict(NO_MASTER))
    manager = TunnelManager(control_dir=str(tmp_path))
    jump = JumpHost(host="10.0.0.71", username="ops", auth=SshAuth.KEY, private_key="k")
    stale = pathlib.Path(manager.control_path(jump, runner))
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.touch()  # noqa: ASYNC240

    await manager.open_master(jump, runner)

    assert not stale.exists(), "ssh would have refused to bind this and multiplexing would be off"  # noqa: ASYNC240


async def test_a_master_that_answers_is_left_alone(tmp_path):
    """`-O check` succeeding means something is behind the socket. Removing it
    would tear down forwards that are carrying video right now."""
    runner = FakeRunner()  # every command succeeds, including -O check
    manager = TunnelManager(control_dir=str(tmp_path))
    jump = JumpHost(host="10.0.0.71", username="ops", auth=SshAuth.KEY, private_key="k")
    live = pathlib.Path(manager.control_path(jump, runner))
    live.parent.mkdir(parents=True, exist_ok=True)
    live.touch()  # noqa: ASYNC240

    await manager.open_master(jump, runner)

    assert live.exists()  # noqa: ASYNC240
    assert not any("-fNT" in call for call in runner.calls), "it redialled a live master"


async def test_forward_failure_is_not_silent():
    """Without ExitOnForwardFailure ssh reports success and forwards nothing."""
    runner = FakeRunner(results=dict(NO_MASTER))
    manager = TunnelManager(control_dir="/tmp/cam-test-ctl")
    jump = JumpHost(host="h", username="ops", auth=SshAuth.KEY, private_key="k")
    await manager.open_master(jump, runner)

    master_call = next(c for c in runner.calls if "-fNT" in c)
    assert "ExitOnForwardFailure=yes" in master_call
    assert "ServerAliveInterval=15" in master_call


async def test_ssh_rejection_says_what_to_do_about_it():
    runner = FakeRunner(
        results={
            **NO_MASTER,
            "-fNT": ProcResult(255, "", "ops@10.0.0.71: Permission denied (publickey,password)."),
        }
    )
    manager = TunnelManager(control_dir="/tmp/cam-test-ctl")
    jump = JumpHost(host="10.0.0.71", username="ops", auth=SshAuth.PASSWORD, password="x")

    with pytest.raises(SshAuthFailed) as caught:
        await manager.open_master(jump, runner)
    assert "key authentication" in caught.value.user_message


async def test_a_forward_reuses_the_lease_it_already_has():
    """Two probes of one camera must not burn two ports."""
    runner = FakeRunner()
    manager = TunnelManager(control_dir="/tmp/cam-test-ctl")
    jump = JumpHost(host="h", username="ops")

    first = await manager.forward(jump, runner, "src-1", "10.0.0.42", 554)
    second = await manager.forward(jump, runner, "src-1", "10.0.0.42", 554)
    assert first.port == second.port
    assert manager.pool.in_use == 1


async def test_the_port_pool_reports_exhaustion_with_the_fix_in_it():
    pool = PortPool(low=20000, high=20001)
    await pool.lease("a", "x:1")
    await pool.lease("b", "x:2")
    with pytest.raises(NoPortsAvailable, match="TUNNEL_PORT_MAX"):
        await pool.lease("c", "x:3")


async def test_releasing_by_owner_frees_the_port():
    pool = PortPool(low=20050, high=20060)
    lease = await pool.lease("src-9", "cam:554")
    await pool.release_owner("src-9")
    assert pool.in_use == 0
    assert (await pool.lease("src-10", "cam:554")).port == lease.port


async def test_connecting_a_vpn_profile_hands_ssh_the_profiles_namespace(sessions):
    """The wiring the ladder's own tests cannot see.

    Production builds exactly one TunnelManager, shared by every profile, so the
    runner has to arrive with the call. It used to arrive from the manager
    instead -- a LocalRunner -- and the ssh layer dialled a jump host that is
    only reachable through the VPN from a namespace that has no VPN in it.
    """
    from app.enums import ReachMode, VpnKind
    from app.models import ConnectionProfile, Team
    from app.net.netns import Namespace
    from app.security.secrets import MemoryBackend
    from app.services.connections import ConnectionService
    from app.services.events import NullBus

    inside = FakeRunner(default=ProcResult(0, "open\n", ""))
    seen: dict[str, object] = {}

    class RecordingTunnels(TunnelManager):
        async def open_master(self, jump, runner, *, timeout=25.0):
            seen["master"] = runner

    class StubNetns(NetnsManager):
        async def ensure(self, profile_id: str) -> Namespace:
            seen["namespace"] = self.ns_name(profile_id)
            return _FakeNamespace(inside)

    service = ConnectionService(
        netns=StubNetns(prefix="cam"),
        tunnels=RecordingTunnels(control_dir="/tmp/cam-test-ctl"),
        secrets=MemoryBackend(),
        bus=NullBus(),
    )

    async with sessions() as db:
        team = Team(name="ACME", slug="acme")
        db.add(team)
        await db.flush()
        profile = ConnectionProfile(
            team_id=team.id,
            name="ACME via the VM",
            mode=ReachMode.VPN_JUMP,
            vpn_kind=VpnKind.NONE,
            jump_host="10.20.30.71",
            jump_username="ops",
        )
        db.add(profile)
        await db.flush()

        outcome = await service.connect(db, profile)

    assert outcome.ok, outcome.results
    assert seen["master"] is inside, "ssh ran outside the profile's namespace"


class _FakeNamespace:
    """Stands in for a created namespace without needing CAP_NET_ADMIN."""

    def __init__(self, runner) -> None:
        self._runner = runner

    @property
    def runner(self):
        return self._runner
