"""Fortinet SSL-VPN via openfortivpn.

The certificate handling here is the whole reason gate 2 exists. On first dial
openfortivpn refuses an unpinned gateway certificate and prints the digest it
wants; we turn that into a question for the user instead of an error, and pin
the answer on the profile.

Worth knowing: the FortiClient desktop dialog shows a **SHA-1** fingerprint,
while openfortivpn pins **SHA-256**. They are both correct and they never match.
We always take the digest from openfortivpn's own output rather than asking
anyone to transcribe what the GUI displayed.
"""

from __future__ import annotations

import re

from app.enums import VpnKind
from app.net.vpn.base import (
    AuthFailed,
    HostMisconfigured,
    PostureFailed,
    TrustPromptRequired,
    VpnConfig,
    VpnDriver,
    VpnStatus,
    ppp_available,
)

#: "ERROR:  Gateway certificate validation failed, and the certificate digest
#:  is not in the local whitelist. If you trust it, rerun with:
#:  ERROR:      --trusted-cert 1a2b3c..."
_DIGEST = re.compile(r"--trusted-cert[= ]([0-9a-f]{40,64})", re.I)
_CERT_FAIL = re.compile(r"certificate (validation|digest)|gateway certificate", re.I)
_AUTH_FAIL = re.compile(
    r"authentication failed|could not authenticate|login failed|invalid credentials", re.I
)
_POSTURE = re.compile(r"host ?check|endpoint (compliance|control)|registration required", re.I)
_UP = re.compile(r"tunnel is up and running", re.I)
#: pppd could not put its tty into PPP mode. Reads as a permissions problem and
#: is almost never one: the PPP line discipline is registered by ``ppp_async``,
#: and a container cannot load a kernel module. Left unclassified this arrives
#: as "pppd: An immediately fatal error of some kind occurred", which sends the
#: reader to look at capabilities, credentials and the gateway in turn.
_PPP_DISCIPLINE = re.compile(r"tty to ppp discipline|ppp discipline", re.I)


class FortinetDriver(VpnDriver):
    kind = VpnKind.FORTINET
    #: openfortivpn runs pppd, so the interface is pppN rather than tunN.
    iface_prefixes = ("ppp", "tun")

    async def dial(self, cfg: VpnConfig, *, timeout: float = 45.0) -> VpnStatus:
        argv = [
            "openfortivpn",
            f"{cfg.gateway}:{cfg.port}",
            "--username",
            cfg.username,
            # There is no flag for the password and we would not use one if
            # there were: -p puts it in argv, where every process on the host
            # can read it. Given no password openfortivpn prints "VPN account
            # password: " and reads stdin, which is what dial() writes to below.
            #
            # Both of these keep the tunnel out of the container's DNS. pppd
            # would otherwise hand over the gateway's nameservers and
            # openfortivpn would write them into /etc/resolv.conf -- which is
            # the container's own, so a dialled profile would take out name
            # resolution for postgres, redis and mediamtx at once. Cameras are
            # addressed by IP, so there is nothing to lose here.
            "--pppd-use-peerdns=0",
            "--set-dns=0",
            "--persistent=0",
        ]
        if cfg.realm:
            argv += ["--realm", cfg.realm]
        if cfg.trusted_cert:
            argv += [f"--trusted-cert={cfg.trusted_cert}"]
        argv += cfg.extra_args

        proc = await self.runner.spawn(argv)
        self._proc = proc
        assert proc.stdin is not None
        proc.stdin.write((cfg.password + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()

        def classify(line: str, tail: list[str]) -> VpnStatus | None:
            if _UP.search(line):
                return VpnStatus(up=True, detail="tunnel up")

            if match := _DIGEST.search(line):
                raise TrustPromptRequired(
                    host=cfg.gateway,
                    fingerprint=match.group(1).lower(),
                    algorithm="sha256",
                    reason="the gateway certificate is not in this profile's trust list",
                )
            if _CERT_FAIL.search(line) and not cfg.trusted_cert:
                # openfortivpn prints the explanation before the digest; keep
                # reading so the next line can carry the fingerprint.
                return None
            if _PPP_DISCIPLINE.search(line) or (not ppp_available() and "pppd" in line.lower()):
                raise HostMisconfigured(
                    "This machine cannot run pppd: the PPP line discipline is missing. "
                    "Load it on the host with `modprobe ppp_async` and keep it across "
                    "reboots by adding ppp_generic, ppp_async and ppp_deflate to "
                    "/etc/modules-load.d/. The VPN authenticated correctly -- only the "
                    "tunnel could not be built."
                )
            if _POSTURE.search(line):
                raise PostureFailed(f"{line}\n{self.log_tail}")
            if _AUTH_FAIL.search(line):
                raise AuthFailed(f"{line}")
            return None

        status = await self._read_until(proc, timeout=timeout, classify=classify)
        self._start_background_pump(proc)
        iface = await self._find_interface()
        return VpnStatus(
            up=True,
            detail=status.detail,
            interface=iface,
            tunnel_ip=await self._interface_ip(iface) if iface else None,
        )
