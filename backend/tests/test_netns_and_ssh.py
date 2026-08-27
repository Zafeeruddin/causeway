"""The namespace wiring and the ssh argv are both places where a subtle mistake
produces a system that looks connected and carries nothing."""

from __future__ import annotations

import pytest

from app.enums import SshAuth
from app.net.netns import NetnsManager, NetnsUnavailable
from app.net.ports import NoPortsAvailable, PortPool
from app.net.runner import LocalRunner, NetnsRunner, ProcResult
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


async def test_the_control_route_is_specific_and_never_a_default(monkeypatch):
    """If the control route were a default, a dropped VPN would silently reroute
    camera traffic onto the host network instead of failing."""
    monkeypatch.setenv("CAM_FORCE_NETNS", "1")
    runner = FakeRunner()
    manager = NetnsManager(
        prefix="cam", control_cidr="172.28.0.0/16", runner=runner, inside_runner=lambda _: runner
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


#: Make the fake report "no master yet", so open_master actually dials.
NO_MASTER = {"-O check": ProcResult(255, "", "No ControlPath specified")}


async def test_ssh_password_never_reaches_argv():
    """A password in argv is visible in /proc and in any process listing on the
    host. sshpass reads it from the environment instead."""
    runner = FakeRunner(results=dict(NO_MASTER))
    manager = TunnelManager(control_dir="/tmp/cam-test-ctl", runner=runner)
    jump = JumpHost(host="10.0.0.71", username="ops", auth=SshAuth.PASSWORD, password="hunter2")

    await manager.open_master(jump)

    for call in runner.calls:
        assert "hunter2" not in " ".join(call)
    assert any(call[0] == "sshpass" and "-e" in call for call in runner.calls)


async def test_forward_failure_is_not_silent():
    """Without ExitOnForwardFailure ssh reports success and forwards nothing."""
    runner = FakeRunner(results=dict(NO_MASTER))
    manager = TunnelManager(control_dir="/tmp/cam-test-ctl", runner=runner)
    jump = JumpHost(host="h", username="ops", auth=SshAuth.KEY, private_key="k")
    await manager.open_master(jump)

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
    manager = TunnelManager(control_dir="/tmp/cam-test-ctl", runner=runner)
    jump = JumpHost(host="10.0.0.71", username="ops", auth=SshAuth.PASSWORD, password="x")

    with pytest.raises(SshAuthFailed) as caught:
        await manager.open_master(jump)
    assert "key authentication" in caught.value.user_message


async def test_a_forward_reuses_the_lease_it_already_has():
    """Two probes of one camera must not burn two ports."""
    runner = FakeRunner()
    manager = TunnelManager(control_dir="/tmp/cam-test-ctl", runner=runner)
    jump = JumpHost(host="h", username="ops")

    first = await manager.forward(jump, "src-1", "10.0.0.42", 554)
    second = await manager.forward(jump, "src-1", "10.0.0.42", 554)
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
