"""The agent's side of the command bus.

Each handler is the same call the API used to make in its own process, run in
the process that can actually make it. Nothing new happens here -- that is the
point of the split: the connect flow, the gate ladder and the certificate pin
are one implementation, reached from two places.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.agent.playback import PlaybackRenditions
from app.enums import SourceKind
from app.models import Camera, CameraSource, ConnectionProfile
from app.services.connections import ConnectionService
from app.services.gateway import outcome_payload, preview_payload
from app.services.preview import PreviewManager

log = structlog.get_logger(__name__)


class UnknownCommand(RuntimeError):
    def __init__(self, name: str) -> None:
        self.user_message = f"the agent does not know the command {name!r}"
        super().__init__(self.user_message)


class Missing(RuntimeError):
    """The row the command names is gone. Says so in the words the person who
    pressed the button will read."""

    def __init__(self, what: str) -> None:
        self.user_message = f"that {what} no longer exists"
        super().__init__(self.user_message)


class ConnectionCommands:
    def __init__(
        self,
        *,
        connections: ConnectionService,
        sessions: async_sessionmaker[AsyncSession],
        status: Callable[[], dict[str, Any]] | None = None,
        previews: PreviewManager | None = None,
        playback: PlaybackRenditions | None = None,
    ) -> None:
        self.connections = connections
        self._sessions = sessions
        self._status = status or (lambda: {})
        self.previews = previews
        self.playback = playback

    async def handle(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if name == "agent.ping":
            return {"up": True, **self._status()}
        handlers = {
            "profile.connect": self._connect,
            "profile.disconnect": self._disconnect,
            "profile.trust": self._trust,
            "camera.test_source": self._test_source,
            "preview.start": self._preview_start,
            "preview.stop": self._preview_stop,
            "preview.list": self._preview_list,
            "recording.make_playable": self._make_playable,
        }
        handler = handlers.get(name)
        if handler is None:
            raise UnknownCommand(name)
        return await handler(payload)

    # ---- handlers ------------------------------------------------------

    async def _make_playable(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Start a browser-playable copy of a recording, and answer at once.

        The encode outlives this reply on purpose: the command bus waits
        seconds and re-encoding a recording takes longer than that.
        """
        if self.playback is None:
            raise UnknownCommand("recording.make_playable")
        return await self.playback.request(
            str(payload.get("recording_id") or ""), force=bool(payload.get("force"))
        )

    async def _connect(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._sessions() as db:
            profile = await self._profile(db, payload)
            outcome = await self.connections.connect(db, profile)
            await db.commit()
            return outcome_payload(outcome)

    async def _disconnect(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._sessions() as db:
            profile = await self._profile(db, payload)
            await self.connections.disconnect(db, profile)
            await db.commit()
            return {}

    async def _trust(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Pin the certificate and commit it here.

        The dial that reads the pin happens in a separate command, so the write
        has to be visible before that command starts -- which it cannot be if it
        is still inside the API request's open transaction.
        """
        async with self._sessions() as db:
            profile = await self._profile(db, payload)
            await self.connections.accept_certificate(
                db, profile, payload["fingerprint"], payload.get("user_id", "")
            )
            await db.commit()
            return {}

    async def _test_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._sessions() as db:
            profile = await self._profile(db, payload)
            camera = await db.get(Camera, payload["camera_id"])
            source = await db.get(CameraSource, payload["source_id"])
            if camera is None or source is None:
                raise Missing("camera")
            outcome = await self.connections.test_source(db, profile, camera, source)
            await db.commit()
            return outcome_payload(outcome)

    # ---- preview -------------------------------------------------------

    async def _preview_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.previews is None:
            raise RuntimeError("this agent does not run previews")
        async with self._sessions() as db:
            camera = await db.get(
                Camera, payload["camera_id"], options=[selectinload(Camera.sources)]
            )
            if camera is None:
                raise Missing("camera")
            kind = SourceKind(payload["source_kind"]) if payload.get("source_kind") else None
            info = await self.previews.start(db, camera, payload["user_id"], kind)
            await db.commit()
            return preview_payload(info)

    async def _preview_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.previews is not None:
            await self.previews.stop(
                payload["preview_id"],
                payload.get("viewer"),
                user_id=payload.get("user_id"),
            )
        return {}

    async def _preview_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.previews is None:
            return {"previews": []}
        infos = await self.previews.list(payload.get("user_id"))
        return {"previews": [preview_payload(info) for info in infos]}

    async def _profile(self, db: AsyncSession, payload: dict[str, Any]) -> ConnectionProfile:
        profile = await db.get(ConnectionProfile, payload["profile_id"])
        if profile is None:
            raise Missing("connection profile")
        return profile
