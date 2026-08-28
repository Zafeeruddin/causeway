"""The live channel.

One socket per browser tab, subscribed to the teams that viewer belongs to.
Scoping is applied when the socket opens rather than per message, so there is no
path by which an event for another team reaches this connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from app.api.auth import COOKIE_NAME, read_session
from app.db import session
from app.models import TeamMember, User
from app.services.events import event_bus

log = structlog.get_logger(__name__)
router = APIRouter()

#: Proxies drop idle sockets; this keeps the connection warm.
KEEPALIVE_SECONDS = 25


@router.websocket("/api/ws")
async def live(socket: WebSocket) -> None:
    token = socket.cookies.get(COOKIE_NAME)
    user_id = read_session(token) if token else None
    if user_id is None:
        await socket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Sign in to continue.")
        return

    async with session() as db:
        user = await db.get(User, user_id)
        if user is None or not user.is_active:
            await socket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Account is inactive.")
            return
        if user.is_admin:
            rows = await db.execute(select(TeamMember.team_id).distinct())
        else:
            rows = await db.execute(select(TeamMember.team_id).where(TeamMember.user_id == user_id))
        team_ids = set(rows.scalars().all())

    await socket.accept()
    await socket.send_text(json.dumps({"type": "ready", "teams": sorted(team_ids)}))

    if not team_ids:
        # Nothing to subscribe to, but hold the socket open so the UI does not
        # show a connection error for an account with no team yet.
        await _idle(socket)
        return

    bus = event_bus()
    pump = asyncio.create_task(_pump(socket, bus, team_ids))
    keepalive = asyncio.create_task(_keepalive(socket))
    try:
        await asyncio.wait([pump, keepalive], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (pump, keepalive):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def _pump(socket: WebSocket, bus, team_ids: set[str]) -> None:
    try:
        async for event in bus.subscribe(team_ids):
            await socket.send_text(event.to_json())
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        log.exception("ws.pump_failed")


async def _keepalive(socket: WebSocket) -> None:
    while True:
        await asyncio.sleep(KEEPALIVE_SECONDS)
        try:
            await socket.send_text(json.dumps({"type": "ping"}))
        except (WebSocketDisconnect, RuntimeError):
            return


async def _idle(socket: WebSocket) -> None:
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        await _keepalive(socket)
