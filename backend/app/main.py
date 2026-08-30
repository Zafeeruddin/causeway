"""Application entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.api.routes import admin, auth, cameras, profiles, recordings, ws
from app.config import settings
from app.enums import ReachMode, SourceKind, SshAuth, VpnKind
from app.net.netns import NetnsManager
from app.net.ports import PortPool
from app.net.ssh import TunnelManager
from app.net.vpn.base import ppp_available
from app.services.commands import CommandError, command_bus
from app.services.connections import ConnectionService
from app.services.events import event_bus
from app.services.gateway import RemoteGateway, RemotePreview
from app.services.preview import PreviewError, PreviewManager

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
    app.state.netns = None
    app.state.ports = None
    reaper: asyncio.Task | None = None

    if cfg.connect_mode == "agent":
        # Namespaces belong to a container. This process cannot create one, and
        # could not use a forward the agent opened even if it could, so it does
        # not pretend to own any of it -- it asks. ROADMAP entry 11.
        app.state.gateway = RemoteGateway()
        app.state.previews = RemotePreview()
        log.info("started", env=cfg.app_env, connect_mode="agent")
    else:
        app.state.netns = NetnsManager(prefix=cfg.netns_prefix, control_cidrs=cfg.control_cidrs)
        app.state.ports = PortPool(low=cfg.tunnel_port_min, high=cfg.tunnel_port_max)
        app.state.tunnels = TunnelManager(control_dir=cfg.ssh_control_dir, pool=app.state.ports)
        app.state.gateway = ConnectionService(netns=app.state.netns, tunnels=app.state.tunnels)
        log.info(
            "started",
            env=cfg.app_env,
            connect_mode="inproc",
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
        if reaper is not None:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
        if isinstance(app.state.previews, PreviewManager):
            await app.state.previews.stop_all()
        await event_bus().close()
        await command_bus().close()
        if app.state.netns is not None:
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

    for module in (auth, admin, profiles, cameras, recordings):
        app.include_router(module.router)
    app.include_router(ws.router)

    @app.exception_handler(ValueError)
    async def _value_error(request: Request, exc: ValueError) -> JSONResponse:
        """Domain errors carry messages written for people; surface them as 422
        rather than letting them become an opaque 500."""
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": str(exc)}
        )

    @app.exception_handler(PreviewError)
    async def _preview_error(request: Request, exc: PreviewError) -> JSONResponse:
        """A preview that cannot start is a state of the world, not a bad
        request: the camera is down, or every slot is taken."""
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"detail": exc.user_message}
        )

    @app.exception_handler(CommandError)
    async def _command_error(request: Request, exc: CommandError) -> JSONResponse:
        """The agent is unreachable, slow, or refused. None of those are the
        caller's fault, and all of them read better than a 500."""
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": exc.user_message}
        )

    @app.get("/api/health")
    async def health() -> JSONResponse:
        # Reported from the wiring rather than the setting: what this process
        # can actually do is the question being asked.
        body: dict = {
            "status": "ok",
            "connect_mode": "inproc" if app.state.netns is not None else "agent",
        }
        body.update(await _network_status(app))
        return JSONResponse(body)

    @app.get("/api/capabilities")
    async def capabilities() -> dict:
        """What this deployment can actually do -- the dashboard greys out the rest."""
        return {
            "reach_modes": [m.value for m in ReachMode],
            "vpn_kinds": [k.value for k in VpnKind],
            "ssh_auth": [a.value for a in SshAuth],
            "source_kinds": [k.value for k in SourceKind],
            **await _network_status(app),
            "max_streams_per_user": settings().max_streams_per_user,
            "max_concurrent_users": settings().max_concurrent_users,
            "record_default_seconds": settings().record_default_seconds,
            "record_max_seconds": settings().record_max_seconds,
        }

    return app


async def _reap_previews(previews: PreviewManager) -> None:
    interval = settings().preview_reap_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            await previews.reap()
        except Exception:  # noqa: BLE001 - a bad sweep must not end the loop
            log.exception("preview.reap_failed")


async def _network_status(app: FastAPI) -> dict:
    """Whether a VPN profile can be dialled at all, asked of whichever process
    would have to do the dialling."""
    if app.state.netns is not None:
        return {
            "netns_available": app.state.netns.available,
            "ppp_available": ppp_available(),
            "ports_in_use": app.state.ports.in_use,
        }
    reported = await app.state.gateway.ping()
    return {
        "netns_available": bool(reported.get("netns_available")),
        "ppp_available": bool(reported.get("ppp_available", True)),
        "agent": reported,
    }


app = create_app()
