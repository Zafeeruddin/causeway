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
    PostureFailed,
    TrustPromptRequired,
    VpnConfig,
    VpnDriver,
    VpnStatus,
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
            "--password-on-stdin",
            # pppd would otherwise rewrite the container's /etc/resolv.conf.
            "--pppd-no-peerdns",
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
