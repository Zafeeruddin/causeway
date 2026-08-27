"""WireGuard via wg-quick.

Unlike the SSL-VPNs there is no dial to watch: the config either loads or it
does not, and there are no certificates and no interactive prompts. Health is a
handshake age rather than a live process, because wg-quick exits immediately.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import time

from app.enums import VpnKind
from app.net.vpn.base import VpnConfig, VpnDriver, VpnError, VpnStatus

#: A peer with no handshake in this long is not carrying traffic any more.
STALE_HANDSHAKE_SECONDS = 180


class WireGuardDriver(VpnDriver):
    kind = VpnKind.WIREGUARD
    iface_prefixes = ("wg",)

    def __init__(self, runner, iface: str = "wg-cam") -> None:  # noqa: ANN001
        super().__init__(runner)
        self.iface = iface
        self._config_path: str | None = None

    async def dial(self, cfg: VpnConfig, *, timeout: float = 45.0) -> VpnStatus:
        if not cfg.wg_config.strip():
            raise VpnError("this WireGuard profile has no interface configuration")

        fd, path = tempfile.mkstemp(prefix=f"{self.iface}-", suffix=".conf")
        os.write(fd, cfg.wg_config.encode())
        os.close(fd)
        os.chmod(path, 0o600)
        self._config_path = path

        result = await self.runner.run(["wg-quick", "up", path], timeout=timeout)
        if not result.ok:
            self._log_tail.extend(result.output.splitlines())
            raise VpnError(f"wg-quick refused the configuration.\n{self.log_tail}")

        iface = await self._find_interface()
        return VpnStatus(
            up=True,
            detail="interface up",
            interface=iface,
            tunnel_ip=await self._interface_ip(iface) if iface else None,
        )

    async def health(self) -> VpnStatus:
        result = await self.runner.run(["wg", "show", "all", "latest-handshakes"], timeout=5.0)
        if not result.ok:
            return VpnStatus(up=False, detail="wg show failed")
        stamps = [int(m) for m in re.findall(r"\t(\d+)$", result.stdout, re.M)]
        if not stamps or max(stamps) == 0:
            return VpnStatus(up=False, detail="no handshake yet")
        age = time.time() - max(stamps)
        if age > STALE_HANDSHAKE_SECONDS:
            return VpnStatus(up=False, detail=f"last handshake {age:.0f}s ago")
        iface = await self._find_interface()
        return VpnStatus(up=True, detail=f"handshake {age:.0f}s ago", interface=iface)

    async def hangup(self) -> None:
        if self._config_path:
            await self.runner.run(["wg-quick", "down", self._config_path], timeout=20.0)
            with contextlib.suppress(OSError):
                os.unlink(self._config_path)
            self._config_path = None
        await super().hangup()
