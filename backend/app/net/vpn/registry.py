"""Driver lookup. The only place that maps a profile's kind to an implementation.

Adding FortiClient-with-posture (ROADMAP.md entry 1) means adding one entry here
and one new module. If a future driver needs anything beyond ``VpnDriver``'s four
methods, that is the signal the seam was drawn in the wrong place.
"""

from __future__ import annotations

from app.enums import VpnKind
from app.net.runner import Runner
from app.net.vpn.base import VpnDriver
from app.net.vpn.direct import DirectDriver
from app.net.vpn.fortinet import FortinetDriver
from app.net.vpn.globalprotect import GlobalProtectDriver
from app.net.vpn.wireguard import WireGuardDriver

DRIVERS: dict[VpnKind, type[VpnDriver]] = {
    VpnKind.NONE: DirectDriver,
    VpnKind.FORTINET: FortinetDriver,
    VpnKind.GLOBALPROTECT: GlobalProtectDriver,
    VpnKind.WIREGUARD: WireGuardDriver,
}


def driver_for(kind: VpnKind, runner: Runner) -> VpnDriver:
    try:
        return DRIVERS[kind](runner)
    except KeyError as exc:
        raise ValueError(f"no VPN driver registered for {kind!r}") from exc
