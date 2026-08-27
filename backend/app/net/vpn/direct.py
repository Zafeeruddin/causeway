"""No VPN.

A real driver rather than a null check, so "we already have access to these
cameras" travels through exactly the same code path as every other mode. The
alternative -- ``if profile.vpn:`` scattered through the gate ladder, the tunnel
manager and the recorder -- is how the easy case ends up broken.
"""

from __future__ import annotations

from app.enums import VpnKind
from app.net.vpn.base import VpnConfig, VpnDriver, VpnStatus


class DirectDriver(VpnDriver):
    kind = VpnKind.NONE

    async def dial(self, cfg: VpnConfig, *, timeout: float = 45.0) -> VpnStatus:
        return VpnStatus(up=True, detail="no VPN hop on this profile")

    async def health(self) -> VpnStatus:
        return VpnStatus(up=True, detail="no VPN hop on this profile")

    async def hangup(self) -> None:
        return None
