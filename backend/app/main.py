"""Application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import settings
from app.enums import ReachMode, VpnKind
from app.net.netns import NetnsManager
from app.net.ports import PortPool
from app.net.ssh import TunnelManager

log = structlog.get_logger(__name__)


def configure_logging() -> None:
    logging.basicConfig(level=settings().log_level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer()
            if settings().is_dev
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings().log_level)
        ),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    cfg = settings()

    app.state.netns = NetnsManager(prefix=cfg.netns_prefix)
    app.state.ports = PortPool(low=cfg.tunnel_port_min, high=cfg.tunnel_port_max)
    app.state.tunnels = TunnelManager(control_dir=cfg.ssh_control_dir, pool=app.state.ports)

    log.info(
        "started",
        env=cfg.app_env,
        netns_available=app.state.netns.available,
        port_pool=f"{cfg.tunnel_port_min}-{cfg.tunnel_port_max}",
    )
    if not app.state.netns.available:
        log.warning(
            "netns.unavailable",
            hint="VPN profiles need CAP_NET_ADMIN. Direct-mode profiles work regardless.",
        )
    try:
        yield
    finally:
        await app.state.netns.destroy_all()
        log.info("stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Camera Tunnel Control Plane",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "netns_available": app.state.netns.available,
                "ports_in_use": app.state.ports.in_use,
            }
        )

    @app.get("/api/capabilities")
    async def capabilities() -> dict:
        """What this deployment can actually do -- the dashboard greys out the rest."""
        return {
            "reach_modes": [m.value for m in ReachMode],
            "vpn_kinds": [k.value for k in VpnKind],
            "netns_available": app.state.netns.available,
            "max_streams_per_user": settings().max_streams_per_user,
            "max_concurrent_users": settings().max_concurrent_users,
            "record_default_seconds": settings().record_default_seconds,
            "record_max_seconds": settings().record_max_seconds,
        }

    return app


app = create_app()
