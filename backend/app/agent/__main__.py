"""``python -m app.agent`` -- the recorder process.

Shutdown is graceful on purpose. SIGTERM tells every live session to wind up,
which finalizes and ships what is already on disk; killing ffmpeg outright would
leave the segments on the work volume with no row pointing at them. Compose's
default 10-second stop grace is not enough for that, which is why the service
sets its own.
"""

from __future__ import annotations

import asyncio
import signal

import structlog

from app.agent.worker import Agent
from app.config import settings
from app.db import sessionmaker
from app.main import configure_logging
from app.net.netns import NetnsManager
from app.net.ports import PortPool
from app.net.ssh import TunnelManager
from app.services.connections import ConnectionService
from app.services.events import event_bus
from app.services.preview import PreviewManager

log = structlog.get_logger(__name__)


def build_agent() -> Agent:
    cfg = settings()
    netns = NetnsManager(prefix=cfg.netns_prefix, control_cidrs=cfg.control_cidrs)
    pool = PortPool(low=cfg.tunnel_port_min, high=cfg.tunnel_port_max)
    tunnels = TunnelManager(control_dir=cfg.ssh_control_dir, pool=pool)
    connections = ConnectionService(netns=netns, tunnels=tunnels)
    return Agent(
        connections=connections,
        previews=PreviewManager(connections=connections, sessions=sessionmaker()),
    )


async def main() -> None:
    configure_logging()
    agent = build_agent()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, agent.stop)

    if not agent.connections.netns.available:
        log.warning(
            "agent.netns_unavailable",
            hint="VPN profiles need CAP_NET_ADMIN on this container. "
            "Direct-mode profiles record regardless.",
        )

    try:
        await agent.run()
    finally:
        await agent.connections.netns.destroy_all()
        await event_bus().close()


if __name__ == "__main__":
    asyncio.run(main())
