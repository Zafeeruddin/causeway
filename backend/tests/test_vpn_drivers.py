"""Driver tests are written against real client output, verbatim.

If openfortivpn or openconnect ever change their wording these tests are the
thing that catches it -- a mis-parsed certificate prompt would otherwise show up
as a silent, unexplained dial failure.
"""

from __future__ import annotations

import pytest

from app.enums import VpnKind
from app.net.runner import ProcResult
from app.net.vpn.base import (
    AuthFailed,
    HostMisconfigured,
    PostureFailed,
    TrustPromptRequired,
    VpnConfig,
    VpnError,
)
from app.net.vpn.direct import DirectDriver
from app.net.vpn.fortinet import FortinetDriver
from app.net.vpn.globalprotect import GlobalProtectDriver
from app.net.vpn.registry import driver_for
from tests.conftest import FakeProcess, FakeRunner

DIGEST = "e0f8c1b0a6d3f4159c2b7d8e0a1f2b3c4d5e6f708192a3b4c5d6e7f80912a3b4"

# Verbatim from openfortivpn 1.21 when the gateway certificate is not pinned.
FORTI_CERT_OUTPUT = [
    "INFO:   Connected to gateway.",
    "ERROR:  Gateway certificate validation failed, and the certificate digest is not in",
    "ERROR:  the local whitelist. If you trust it, rerun with:",
    f"ERROR:      --trusted-cert {DIGEST}",
    "ERROR:  Could not log out.",
]

FORTI_SUCCESS_OUTPUT = [
    "INFO:   Connected to gateway.",
    "INFO:   Authenticated.",
    "INFO:   Remote gateway has allocated a VPN.",
    "INFO:   Interface ppp0 is UP.",
    "INFO:   Tunnel is up and running.",
]

FORTI_AUTH_FAIL_OUTPUT = [
    "INFO:   Connected to gateway.",
    "ERROR:  Could not authenticate to gateway. Please check the password, client certificate, etc.",
]

# Verbatim from openconnect 9.x against an untrusted GlobalProtect portal.
GP_CERT_OUTPUT = [
    'Certificate from VPN server "vpn.example.com" failed verification.',
    "Reason: signer not found",
    "To trust this server in future, perhaps add this to your command line:",
    f"    --servercert sha256:{DIGEST}",
]

GP_SUCCESS_OUTPUT = [
    "POST https://vpn.example.com/ssl-vpn/login.esp",
    "Got legacy IPv4 config",
    "Connected as 10.44.2.19, using SSL",
]


async def test_fortinet_certificate_prompt_becomes_a_question_not_an_error():
    runner = FakeRunner(process=FakeProcess(stderr=FORTI_CERT_OUTPUT))
    driver = FortinetDriver(runner)

    with pytest.raises(TrustPromptRequired) as caught:
        await driver.dial(VpnConfig(kind=VpnKind.FORTINET, gateway="vpn.example.com"), timeout=5)

    prompt = caught.value
    assert prompt.fingerprint == DIGEST
    # The GUI shows SHA-1; openfortivpn pins SHA-256. We must report what we pin.
    assert prompt.algorithm == "sha256"
    assert prompt.host == "vpn.example.com"
    assert DIGEST in prompt.user_message


#: What a namespace with no VPN in it looks like. The kernel puts tunl0 there
#: the moment the namespace exists.
EMPTY_NAMESPACE = ProcResult(
    0,
    "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\\    link/loopback\n"
    "2: tunl0@NONE: <NOARP> mtu 1480 state DOWN\\    link/ipip 0.0.0.0\n"
    "4: veth-n-abc@if5: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\\    link/ether\n",
    "",
)


async def test_a_namespace_with_no_tunnel_in_it_is_not_healthy():
    """tunl0 is a kernel placeholder that starts with "tun" and exists in every
    namespace. Matching it reports a tunnel that was never dialled, and
    SourcePath.open trusts health to decide whether to redial -- so the SSH
    forward fails against a master nobody opened, three layers from the cause."""
    runner = FakeRunner(results={"link show": EMPTY_NAMESPACE})

    status = await FortinetDriver(runner).health()

    assert not status.up
    assert status.interface is None


async def test_a_tunnel_that_is_down_does_not_count_as_one():
    down = ProcResult(0, "3: ppp0: <POINTOPOINT,MULTICAST,NOARP> mtu 1354 state DOWN\n", "")
    assert not (await FortinetDriver(FakeRunner(results={"link show": down})).health()).up


async def test_the_real_tunnel_is_found_past_the_placeholder(addr_show):
    """tunl0 is listed before ppp0, so whichever is matched first is whichever
    the filter lets through."""
    both = ProcResult(
        0,
        "2: tunl0@NONE: <NOARP> mtu 1480 state DOWN\\    link/ipip\n"
        "3: ppp0: <POINTOPOINT,MULTICAST,NOARP,UP> mtu 1354\\    link/ppp\n",
        "",
    )
    runner = FakeRunner(results={"link show": both, "addr show": addr_show})

    status = await FortinetDriver(runner).health()

    assert status.up
    assert status.interface == "ppp0"
    assert status.tunnel_ip == "10.212.134.88"


async def test_fortinet_successful_dial(link_show_ppp, addr_show):
    runner = FakeRunner(
        process=FakeProcess(stderr=FORTI_SUCCESS_OUTPUT),
        results={"link show": link_show_ppp, "addr show": addr_show},
    )
    driver = FortinetDriver(runner)
    status = await driver.dial(
        VpnConfig(kind=VpnKind.FORTINET, gateway="vpn.example.com", username="u", password="p"),
        timeout=5,
    )
    assert status.up
    assert status.interface == "ppp0"
    assert status.tunnel_ip == "10.212.134.88"


async def test_fortinet_password_goes_over_stdin_never_argv():
    proc = FakeProcess(stderr=FORTI_AUTH_FAIL_OUTPUT)
    runner = FakeRunner(process=proc)
    with pytest.raises(AuthFailed):
        await FortinetDriver(runner).dial(
            VpnConfig(kind=VpnKind.FORTINET, gateway="g", username="u", password="hunter2"),
            timeout=5,
        )
    argv = runner.calls[0]
    assert "hunter2" not in " ".join(argv)
    # There is no flag for this: openfortivpn takes a password on argv (-p) or
    # off stdin, and argv is readable by every process on the host.
    assert not {"-p", "--password"} & set(argv)
    assert not any(a.startswith("--password") for a in argv)
    assert proc.stdin.written == b"hunter2\n"


async def test_fortinet_pins_the_certificate_once_accepted():
    runner = FakeRunner(process=FakeProcess(stderr=FORTI_SUCCESS_OUTPUT))
    driver = FortinetDriver(runner)
    await driver.dial(VpnConfig(kind=VpnKind.FORTINET, gateway="g", trusted_cert=DIGEST), timeout=5)
    assert f"--trusted-cert={DIGEST}" in runner.calls[0]


async def test_fortinet_reports_posture_enforcement_distinctly():
    """ROADMAP entry 1: this is the symptom that says 'you need a different driver'."""
    runner = FakeRunner(
        process=FakeProcess(stderr=["ERROR:  Endpoint compliance check required by gateway."])
    )
    with pytest.raises(PostureFailed):
        await FortinetDriver(runner).dial(VpnConfig(kind=VpnKind.FORTINET, gateway="g"), timeout=5)


async def test_globalprotect_certificate_prompt_carries_the_gateway_reason():
    runner = FakeRunner(process=FakeProcess(stderr=GP_CERT_OUTPUT))
    with pytest.raises(TrustPromptRequired) as caught:
        await GlobalProtectDriver(runner).dial(
            VpnConfig(kind=VpnKind.GLOBALPROTECT, gateway="vpn.example.com"), timeout=5
        )
    assert caught.value.fingerprint == DIGEST
    assert caught.value.reason == "signer not found"


async def test_globalprotect_and_paloalto_share_one_protocol_flag():
    runner = FakeRunner(process=FakeProcess(stderr=GP_SUCCESS_OUTPUT))
    await GlobalProtectDriver(runner).dial(
        VpnConfig(kind=VpnKind.GLOBALPROTECT, gateway="g", username="u", password="p"), timeout=5
    )
    assert "--protocol=gp" in runner.calls[0]


async def test_direct_mode_is_a_driver_not_a_null_check():
    driver = driver_for(VpnKind.NONE, FakeRunner())
    assert isinstance(driver, DirectDriver)
    assert (await driver.dial(VpnConfig(kind=VpnKind.NONE))).up
    assert (await driver.health()).up
    await driver.hangup()


async def test_a_missing_ppp_line_discipline_names_the_module(monkeypatch):
    """The host, not the profile. Credentials were accepted and the gateway
    allocated a tunnel; pppd then could not build it. Unclassified this arrives
    as "an immediately fatal error of some kind occurred", which sends the
    reader through capabilities, credentials and the gateway in turn."""
    runner = FakeRunner(
        process=FakeProcess(
            stderr=[
                "INFO:   Authenticated.",
                "INFO:   Remote gateway has allocated a VPN.",
                "Couldn't set tty to PPP discipline: Operation not permitted",
                "ERROR:  pppd: An immediately fatal error of some kind occurred",
            ]
        )
    )
    with pytest.raises(HostMisconfigured) as caught:
        await FortinetDriver(runner).dial(VpnConfig(kind=VpnKind.FORTINET, gateway="g"), timeout=5)

    assert "ppp_async" in caught.value.user_message
    assert "authenticated correctly" in caught.value.user_message


def test_ppp_availability_is_read_from_the_line_disciplines(tmp_path, monkeypatch):
    """/dev/ppp existing is ppp_generic, which is usually built in. The
    discipline pppd needs comes from ppp_async, and checking the device instead
    passes on a host where every dial will fail."""
    from app.net.vpn import base

    present = tmp_path / "with"
    present.write_text("n_tty       0\nppp         3\nn_null     27\n")
    monkeypatch.setattr(base, "LDISCS", present)
    assert base.ppp_available() is True

    absent = tmp_path / "without"
    absent.write_text("n_tty       0\nn_null     27\n")
    monkeypatch.setattr(base, "LDISCS", absent)
    assert base.ppp_available() is False

    # Nothing to read is not evidence of absence; refusing to try would be worse.
    monkeypatch.setattr(base, "LDISCS", tmp_path / "missing")
    assert base.ppp_available() is True


async def test_a_client_that_dies_without_speaking_is_reported_as_such():
    runner = FakeRunner(process=FakeProcess(stderr=[]))
    with pytest.raises(Exception, match="stopped without connecting"):
        await FortinetDriver(runner).dial(VpnConfig(kind=VpnKind.FORTINET, gateway="g"), timeout=5)


async def test_a_client_that_rejects_its_own_arguments_says_so(monkeypatch):
    """A wrong flag and an unreachable gateway both end as "the client exited".
    Reported as "The VPN connection failed." they are indistinguishable, and the
    reader goes hunting for a network problem that is not there."""
    runner = FakeRunner(
        process=FakeProcess(
            stderr=[
                "openfortivpn: unrecognized option '--password-on-stdin'",
                "Usage: openfortivpn [<host>[:<port>]] [-u <user>] [-p <pass>]",
            ]
        )
    )
    with pytest.raises(VpnError) as caught:
        await FortinetDriver(runner).dial(VpnConfig(kind=VpnKind.FORTINET, gateway="g"), timeout=5)

    assert "unrecognized option" in caught.value.user_message


async def test_the_reason_comes_from_the_client_not_from_us():
    """openfortivpn prefixes its complaints; the prefix is noise to the reader."""
    runner = FakeRunner(
        process=FakeProcess(stderr=["INFO:   Connected to gateway.", "ERROR:  Operation canceled"])
    )
    with pytest.raises(VpnError) as caught:
        await FortinetDriver(runner).dial(VpnConfig(kind=VpnKind.FORTINET, gateway="g"), timeout=5)

    assert caught.value.user_message.endswith("Operation canceled")
