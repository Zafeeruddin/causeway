"""Where a connection actually gets made.

The dashboard asks the same four questions however the deployment is arranged --
connect this profile, disconnect it, trust this certificate, test this camera --
and there are exactly two places the answer can come from:

* **In process.** :class:`~app.services.connections.ConnectionService` itself,
  which is what a single-process dev run and the whole test suite use.
* **In the agent.** The compose stack, where namespaces, VPN clients, SSH
  masters and ffmpeg all live in one container and the API has no way to reach
  them except by asking.

Both satisfy :class:`ConnectionGateway`, and the choice is made once at startup
rather than per request, so no route has to know which deployment it is in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import GateStatus, ProfileState, SourceKind
from app.gates.ladder import GateResult
from app.models import Camera, CameraSource, ConnectionProfile
from app.services.commands import CommandBus, command_bus
from app.services.connections import ConnectOutcome
from app.services.preview import PreviewInfo

log = structlog.get_logger(__name__)


class ConnectionGateway(Protocol):
    """The four operations that touch the camera network."""

    async def connect(self, db: AsyncSession, profile: ConnectionProfile) -> ConnectOutcome: ...

    async def disconnect(self, db: AsyncSession, profile: ConnectionProfile) -> None: ...

    async def accept_certificate(
        self, db: AsyncSession, profile: ConnectionProfile, fingerprint: str, user_id: str
    ) -> None: ...

    async def test_source(
        self,
        db: AsyncSession,
        profile: ConnectionProfile,
        camera: Camera,
        source: CameraSource,
    ) -> ConnectOutcome: ...


# ---- wire format --------------------------------------------------------


def gate_payload(gate: GateResult) -> dict[str, Any]:
    return {
        "key": gate.key,
        "index": gate.index,
        "title": gate.title,
        "status": str(gate.status),
        "message": gate.message,
        "detail": gate.detail,
        "duration_ms": gate.duration_ms,
    }


def outcome_payload(outcome: ConnectOutcome) -> dict[str, Any]:
    return {
        "attempt_id": outcome.attempt_id,
        "state": str(outcome.state),
        "gates": [gate_payload(gate) for gate in outcome.results],
    }


def outcome_from_payload(payload: dict[str, Any]) -> ConnectOutcome:
    """Rebuild the outcome the routes already know how to render.

    Deliberately reconstructed rather than passed through as a dict: the routes
    were written against ``ConnectOutcome`` and should not gain a second shape
    just because the work happened in another process.
    """
    return ConnectOutcome(
        attempt_id=payload.get("attempt_id", ""),
        results=[
            GateResult(
                key=gate["key"],
                index=gate["index"],
                title=gate["title"],
                status=GateStatus(gate["status"]),
                message=gate.get("message", ""),
                detail=gate.get("detail") or {},
                duration_ms=gate.get("duration_ms", 0),
            )
            for gate in payload.get("gates", [])
        ],
        state=ProfileState(payload.get("state", ProfileState.FAILED)),
    )


# ---- the remote implementation ------------------------------------------


class RemoteGateway:
    """Hands the work to the agent and refreshes what the agent changed.

    Every call ends with a refresh because the agent writes profile state in its
    own transaction. Without it the route would answer from the row as it was
    before the connection it just made.
    """

    def __init__(self, bus: CommandBus | None = None) -> None:
        self._bus = bus or command_bus()

    async def connect(self, db: AsyncSession, profile: ConnectionProfile) -> ConnectOutcome:
        payload = await self._bus.call("profile.connect", {"profile_id": profile.id})
        await _refresh(db, profile)
        return outcome_from_payload(payload)

    async def disconnect(self, db: AsyncSession, profile: ConnectionProfile) -> None:
        await self._bus.call("profile.disconnect", {"profile_id": profile.id})
        await _refresh(db, profile)

    async def accept_certificate(
        self, db: AsyncSession, profile: ConnectionProfile, fingerprint: str, user_id: str
    ) -> None:
        """Pinned by the agent, not here.

        The pin has to be committed before the dial that reads it, and this
        request's transaction does not commit until it returns. Writing it here
        would mean the agent reconnecting against the certificate the user just
        rejected.
        """
        await self._bus.call(
            "profile.trust",
            {"profile_id": profile.id, "fingerprint": fingerprint, "user_id": user_id},
        )
        await _refresh(db, profile)

    async def test_source(
        self,
        db: AsyncSession,
        profile: ConnectionProfile,
        camera: Camera,
        source: CameraSource,
    ) -> ConnectOutcome:
        payload = await self._bus.call(
            "camera.test_source",
            {"profile_id": profile.id, "camera_id": camera.id, "source_id": source.id},
        )
        await _refresh(db, source)
        return outcome_from_payload(payload)

    async def ping(self) -> dict[str, Any]:
        """What the agent says about itself. Never raises: the health endpoint
        reports an absent agent, it does not fail with it."""
        try:
            return await self._bus.call("agent.ping")
        except Exception as exc:  # noqa: BLE001
            return {"up": False, "detail": getattr(exc, "user_message", str(exc))}


class RemotePreview:
    """Preview, asked of the agent.

    Starting one means spawning a process inside a network namespace, so unlike
    the rest of the API this has no in-process fallback worth having in compose:
    the API container could not do it if it tried.
    """

    def __init__(self, bus: CommandBus | None = None) -> None:
        self._bus = bus or command_bus()

    async def start(
        self,
        db: AsyncSession,
        camera: Camera,
        user_id: str,
        kind: SourceKind | None = None,
    ) -> PreviewInfo:
        payload = await self._bus.call(
            "preview.start",
            {
                "camera_id": camera.id,
                "user_id": user_id,
                "source_kind": kind.value if kind else None,
            },
        )
        return preview_from_payload(payload)

    async def stop(self, preview_id: str, user_id: str | None = None) -> None:
        await self._bus.call("preview.stop", {"preview_id": preview_id, "user_id": user_id})

    async def list(self, user_id: str | None = None) -> list[PreviewInfo]:
        payload = await self._bus.call("preview.list", {"user_id": user_id})
        return [preview_from_payload(item) for item in payload.get("previews", [])]


def preview_payload(info: PreviewInfo) -> dict[str, Any]:
    return {
        "id": info.id,
        "path": info.path,
        "camera_id": info.camera_id,
        "source_kind": info.source_kind.value,
        "started_at": info.started_at.isoformat(),
        "expires_at": info.expires_at.isoformat(),
        "viewers": info.viewers,
        "codec": info.codec,
    }


def preview_from_payload(payload: dict[str, Any]) -> PreviewInfo:
    return PreviewInfo(
        id=payload["id"],
        path=payload["path"],
        camera_id=payload["camera_id"],
        source_kind=SourceKind(payload["source_kind"]),
        started_at=datetime.fromisoformat(payload["started_at"]),
        expires_at=datetime.fromisoformat(payload["expires_at"]),
        viewers=payload.get("viewers", 0),
        codec=payload.get("codec", ""),
    )


async def _refresh(db: AsyncSession, instance: Any) -> None:
    try:
        await db.refresh(instance)
    except Exception:  # noqa: BLE001 - a row deleted underneath us is not this call's problem
        log.debug("gateway.refresh_skipped", instance=type(instance).__name__)
