"""The ladder's job is to tell you *which hop* failed, and to not run hops that
this profile does not have."""

from __future__ import annotations

from app.enums import GateStatus, ReachMode, SourceKind, SshAuth, VpnKind
from app.gates.ladder import (
    GateContext,
    ProfileSpec,
    SourceSpec,
    connect_gates,
    run_ladder,
    source_gates,
)
from app.net.runner import ProcResult
from app.net.ssh import JumpHost, TunnelManager
from app.net.vpn.base import VpnConfig
from app.net.vpn.registry import driver_for
from app.security.redaction import StreamUrl
from tests.conftest import FakeProcess, FakeRunner
from tests.test_vpn_drivers import DIGEST, FORTI_CERT_OUTPUT, FORTI_SUCCESS_OUTPUT

FFPROBE_OK = ProcResult(
    0,
    '{"streams":[{"codec_type":"video","codec_name":"h264","width":1920,"height":1080,'
    '"avg_frame_rate":"25/1"}],"format":{}}',
    "",
)
TCP_OPEN = ProcResult(0, "open\n", "")
TCP_SHUT = ProcResult(1, "closed: ConnectionRefusedError: [Errno 111] Connection refused\n", "")


class NamespacedRunner:
    """A runner bound to a namespace, the way a VPN profile's runner is.

    It records what would *actually* have been executed, ``ip netns exec`` and
    all. That is the point: the bug this guards against was invisible in the
    unwrapped argv, because the command was right and only the network it ran in
    was wrong.
    """

    name = "netns"

    def __init__(self, namespace: str, inner: FakeRunner) -> None:
        self.namespace = namespace
        self.inner = inner
        self.executed: list[list[str]] = []

    def wrap(self, argv) -> list[str]:
        return ["ip", "netns", "exec", self.namespace, *argv]

    async def run(self, argv, **kwargs):
        self.executed.append(self.wrap(argv))
        return await self.inner.run(argv, **kwargs)

    async def spawn(self, argv, **kwargs):
        self.executed.append(self.wrap(argv))
        return await self.inner.spawn(argv, **kwargs)


def _source() -> SourceSpec:
    return SourceSpec(
        id="src-1",
        kind=SourceKind.RTSP,
        url=StreamUrl.build("rtsp://10.20.30.42:554/Streaming/Channels/101", "admin", "hunter2"),
        host="10.20.30.42",
        port=554,
    )


def _ctx(mode: ReachMode, runner: FakeRunner, **kw) -> GateContext:
    profile = ProfileSpec(
        id="p1",
        name="test",
        mode=mode,
        vpn=VpnConfig(
            kind=VpnKind.FORTINET if mode.has_vpn else VpnKind.NONE, gateway="vpn.example.com"
        ),
        jump=JumpHost(host="10.20.30.71", username="ops", auth=SshAuth.PASSWORD, password="pw")
        if mode.has_jump
        else None,
        **kw,
    )
    return GateContext(
        profile=profile,
        runner=runner,
        driver=driver_for(profile.vpn.kind, runner),
        # No runner on the manager: the ladder hands it the one it is using,
        # which is the whole point -- see test_the_ssh_layer_runs_where_the_vpn_is.
        tunnels=TunnelManager(control_dir="/tmp/cam-test-ctl"),
    )


async def test_direct_mode_runs_two_gates_and_visibly_skips_the_rest():
    runner = FakeRunner(results={"socket": TCP_OPEN, "ffprobe": FFPROBE_OK}, default=TCP_OPEN)
    ctx = _ctx(ReachMode.DIRECT, runner)
    ctx.source = _source()

    results = await run_ladder(ctx)
    by_key = {r.key: r for r in results}

    assert by_key["vpn_dial"].status is GateStatus.SKIPPED
    assert by_key["cert_trust"].status is GateStatus.SKIPPED
    assert by_key["jump_route"].status is GateStatus.SKIPPED
    assert by_key["ssh_auth"].status is GateStatus.SKIPPED
    assert by_key["port_forward"].status is GateStatus.SKIPPED
    assert by_key["camera_reachable"].status is GateStatus.PASSED
    assert by_key["stream_handshake"].status is GateStatus.PASSED
    # Skipped is reported, never omitted -- "didn't need it" must be
    # distinguishable from "never ran".
    assert len(results) == 8


async def test_skipped_gates_say_why():
    runner = FakeRunner(results={"socket": TCP_OPEN, "ffprobe": FFPROBE_OK}, default=TCP_OPEN)
    ctx = _ctx(ReachMode.DIRECT, runner)
    ctx.source = _source()
    results = {r.key: r for r in await run_ladder(ctx)}
    assert "no VPN hop" in results["vpn_dial"].message
    assert "without a jump host" in results["jump_route"].message


async def test_certificate_prompt_blocks_gate_two_and_carries_the_fingerprint():
    """The dial reached the gateway, so gate 1 passes; gate 2 asks the question."""
    runner = FakeRunner(process=FakeProcess(stderr=FORTI_CERT_OUTPUT))
    ctx = _ctx(ReachMode.VPN_ONLY, runner)

    results = {r.key: r for r in await run_ladder(ctx, gates=connect_gates())}

    assert results["vpn_dial"].status is GateStatus.PASSED
    cert = results["cert_trust"]
    assert cert.status is GateStatus.BLOCKED
    assert cert.detail["action"] == "accept_certificate"
    assert cert.detail["fingerprint"] == DIGEST
    assert cert.detail["host"] == "vpn.example.com"
    # A blocked rung stops the ladder; later rungs are pending, not failed.
    assert results["whitelist"].status is GateStatus.PENDING


async def test_a_pinned_certificate_passes_gate_two(link_show_ppp, addr_show):
    runner = FakeRunner(
        process=FakeProcess(stderr=FORTI_SUCCESS_OUTPUT),
        results={"link show": link_show_ppp, "addr show": addr_show},
    )
    ctx = _ctx(ReachMode.VPN_ONLY, runner)
    ctx.profile.vpn.trusted_cert = DIGEST

    results = {r.key: r for r in await run_ladder(ctx, gates=connect_gates())}
    assert results["vpn_dial"].status is GateStatus.PASSED
    assert results["cert_trust"].status is GateStatus.PASSED
    assert "pinned" in results["cert_trust"].message


async def test_an_unreachable_camera_names_the_camera_not_the_stack():
    runner = FakeRunner(results={"socket": TCP_SHUT}, default=TCP_SHUT)
    ctx = _ctx(ReachMode.DIRECT, runner)
    ctx.source = _source()

    results = {r.key: r for r in await run_ladder(ctx, gates=source_gates())}
    failure = results["camera_reachable"]
    assert failure.status is GateStatus.FAILED
    assert "10.20.30.42:554" in failure.message
    assert "the camera is off" in failure.message


async def test_a_camera_that_answers_but_sends_no_video_fails_the_last_gate():
    no_video = ProcResult(0, '{"streams":[{"codec_type":"audio","codec_name":"aac"}]}', "")
    runner = FakeRunner(results={"socket": TCP_OPEN, "ffprobe": no_video}, default=TCP_OPEN)
    ctx = _ctx(ReachMode.DIRECT, runner)
    ctx.source = _source()

    results = {r.key: r for r in await run_ladder(ctx, gates=source_gates())}
    assert results["camera_reachable"].status is GateStatus.PASSED
    assert results["stream_handshake"].status is GateStatus.FAILED
    assert "no video track" in results["stream_handshake"].message


async def test_rtsp_probes_are_pinned_to_tcp():
    """ssh -L forwards TCP only; a UDP RTSP probe would connect and deliver nothing."""
    runner = FakeRunner(results={"socket": TCP_OPEN, "ffprobe": FFPROBE_OK}, default=TCP_OPEN)
    ctx = _ctx(ReachMode.DIRECT, runner)
    ctx.source = _source()
    await run_ladder(ctx, gates=source_gates())

    ffprobe = next(c for c in runner.calls if c[0] == "ffprobe")
    assert "-rtsp_transport" in ffprobe
    assert ffprobe[ffprobe.index("-rtsp_transport") + 1] == "tcp"


async def test_the_stream_gate_reports_what_the_camera_is_actually_sending():
    runner = FakeRunner(results={"socket": TCP_OPEN, "ffprobe": FFPROBE_OK}, default=TCP_OPEN)
    ctx = _ctx(ReachMode.DIRECT, runner)
    ctx.source = _source()
    results = {r.key: r for r in await run_ladder(ctx, gates=source_gates())}

    handshake = results["stream_handshake"]
    assert handshake.detail["codec"] == "h264"
    assert handshake.detail["resolution"] == "1920x1080"
    assert handshake.detail["fps"] == 25.0


async def test_hls_sources_skip_the_tcp_gate_and_still_probe_the_manifest():
    runner = FakeRunner(results={"ffprobe": FFPROBE_OK}, default=TCP_OPEN)
    ctx = _ctx(ReachMode.DIRECT, runner)
    ctx.source = SourceSpec(
        id="src-hls",
        kind=SourceKind.HLS,
        url=StreamUrl.build("https://cdn.example/live/cam1/index.m3u8"),
    )
    results = {r.key: r for r in await run_ladder(ctx, gates=source_gates())}

    assert results["camera_reachable"].status is GateStatus.SKIPPED
    assert results["stream_handshake"].status is GateStatus.PASSED
    ffprobe = next(c for c in runner.calls if c[0] == "ffprobe")
    assert "-rtsp_transport" not in ffprobe


async def test_a_crashing_gate_does_not_take_the_ladder_down():
    class Boom(FakeRunner):
        async def run(self, argv, **kw):
            raise RuntimeError("kernel panic")

    ctx = _ctx(ReachMode.DIRECT, Boom())
    ctx.source = _source()
    results = {r.key: r for r in await run_ladder(ctx, gates=source_gates())}
    assert results["camera_reachable"].status is GateStatus.FAILED
    assert "could not complete" in results["camera_reachable"].message


async def test_gates_are_emitted_live_as_they_resolve():
    seen: list[tuple[str, GateStatus]] = []
    runner = FakeRunner(results={"socket": TCP_OPEN, "ffprobe": FFPROBE_OK}, default=TCP_OPEN)
    ctx = _ctx(ReachMode.DIRECT, runner)
    ctx.source = _source()

    async def emit(result):
        seen.append((result.key, result.status))

    await run_ladder(ctx, emit=emit)
    assert [k for k, _ in seen] == [
        "vpn_dial",
        "cert_trust",
        "whitelist",
        "jump_route",
        "ssh_auth",
        "port_forward",
        "camera_reachable",
        "stream_handshake",
    ]


async def test_the_ssh_layer_runs_inside_the_profiles_namespace(link_show_ppp, addr_show):
    """Regression, and the shape of the whole bug.

    A jump host behind a VPN is only routable from inside that profile's
    namespace, and ``ssh -L`` binds 127.0.0.1 in whatever namespace it ran in --
    which has to be the one ffmpeg dials from. Running the ssh layer on a
    manager-wide runner satisfied neither, and looked like a credentials
    problem at gate 5.
    """
    inner = FakeRunner(
        process=FakeProcess(stderr=FORTI_SUCCESS_OUTPUT),
        results={
            "link show": link_show_ppp,
            "addr show": addr_show,
            "-O check": ProcResult(255, "", "No ControlPath specified"),
            "ffprobe": FFPROBE_OK,
        },
        default=TCP_OPEN,
    )
    runner = NamespacedRunner("cam-abc12345", inner)
    ctx = _ctx(ReachMode.VPN_JUMP, runner)
    ctx.profile.vpn.trusted_cert = DIGEST
    ctx.source = _source()

    results = {r.key: r for r in await run_ladder(ctx)}

    assert results["ssh_auth"].status is GateStatus.PASSED
    assert results["port_forward"].status is GateStatus.PASSED

    ssh_calls = [c for c in runner.executed if "ssh" in c or "sshpass" in c]
    assert ssh_calls, "the ssh layer never ran on this profile's runner at all"
    for call in ssh_calls:
        assert call[:4] == ["ip", "netns", "exec", "cam-abc12345"], call


async def test_the_forward_is_bound_by_the_same_runner_ffmpeg_will_use(link_show_ppp, addr_show):
    """A port bound on one namespace's loopback is not there on another's."""
    inner = FakeRunner(
        process=FakeProcess(stderr=FORTI_SUCCESS_OUTPUT),
        results={
            "link show": link_show_ppp,
            "addr show": addr_show,
            "-O check": ProcResult(255, "", "No ControlPath specified"),
            "ffprobe": FFPROBE_OK,
        },
        default=TCP_OPEN,
    )
    runner = NamespacedRunner("cam-abc12345", inner)
    ctx = _ctx(ReachMode.VPN_JUMP, runner)
    ctx.profile.vpn.trusted_cert = DIGEST
    ctx.source = _source()

    await run_ladder(ctx)

    forward = next(c for c in runner.executed if "-O" in c and "forward" in c)
    assert forward[:4] == ["ip", "netns", "exec", "cam-abc12345"]
    # And the stream probe that follows dials the port that forward just bound.
    assert ctx.local_port is not None
    probe = next(c for c in runner.executed if "ffprobe" in c)
    assert f"127.0.0.1:{ctx.local_port}" in " ".join(probe)
