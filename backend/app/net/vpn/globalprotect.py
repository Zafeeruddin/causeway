"""GlobalProtect and Palo Alto via openconnect.

One driver covers both: they are the same protocol and the same client flag.
Cisco AnyConnect would be a third value of ``--protocol`` if it ever appears.
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

#: "To trust this server in future, perhaps add this to your command line:
#:      --servercert sha256:abc123..."
_DIGEST = re.compile(r"--servercert\s+(sha(?:1|256):[0-9a-fA-F:]+)")
_CERT_FAIL = re.compile(r"certificate from vpn server.*failed verification", re.I)
_REASON = re.compile(r"^\s*Reason:\s*(.+)$", re.I)
_AUTH_FAIL = re.compile(r"login failed|authentication failed|failed to obtain", re.I)
_POSTURE = re.compile(r"host ?scan|endpoint (compliance|posture)|hip (check|report)", re.I)
_UP = re.compile(r"connected as [\d.]+|session authentication will expire|got legacy ipv4", re.I)


class GlobalProtectDriver(VpnDriver):
    kind = VpnKind.GLOBALPROTECT
    iface_prefixes = ("tun", "vpn")

    async def dial(self, cfg: VpnConfig, *, timeout: float = 45.0) -> VpnStatus:
        argv = [
            "openconnect",
            "--protocol=gp",
            "--user",
            cfg.username,
            "--passwd-on-stdin",
            "--non-inter",
            f"{cfg.gateway}:{cfg.port}",
        ]
        if cfg.trusted_cert:
            argv += ["--servercert", cfg.trusted_cert]
        argv += cfg.extra_args

        proc = await self.runner.spawn(argv)
        self._proc = proc
        assert proc.stdin is not None
        proc.stdin.write((cfg.password + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()

        reason = "certificate verification failed"

        def classify(line: str, tail: list[str]) -> VpnStatus | None:
            nonlocal reason
            if _UP.search(line):
                return VpnStatus(up=True, detail="tunnel up")
            if match := _REASON.match(line):
                reason = match.group(1).strip()
                return None
            if match := _DIGEST.search(line):
                algorithm, _, digest = match.group(1).partition(":")
                raise TrustPromptRequired(
                    host=cfg.gateway,
                    fingerprint=digest.lower(),
                    algorithm=algorithm.lower(),
                    reason=reason,
                )
            if _CERT_FAIL.search(line):
                return None
            if _POSTURE.search(line):
                raise PostureFailed(f"{line}\n{self.log_tail}")
            if _AUTH_FAIL.search(line):
                raise AuthFailed(line)
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
