"""Live events, fanned out over Redis.

Agents publish state; the dashboard reads it off a WebSocket. Nothing in the UI
polls a process directly, which is what makes "the VPN dropped at 14:02, the
recorder retried twice, the third succeeded" reconstructable rather than
inferred from whatever the page happened to be showing.

Every event is addressed to a team. A subscriber only ever gets channels for
teams it belongs to, so scoping holds on the socket as well as in the database.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis
import structlog

from app.config import settings

log = structlog.get_logger(__name__)


def team_channel(team_id: str) -> str:
    return f"cam:team:{team_id}"


@dataclass(slots=True)
class Event:
    type: str
    team_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> Event:
        data = json.loads(raw)
        return cls(**data)


class EventBus:
    def __init__(self, url: str | None = None) -> None:
        self._url = url or settings().redis_url
        self._client: redis.Redis | None = None

    async def client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.from_url(self._url, decode_responses=True)
        return self._client

    async def publish(self, event: Event) -> None:
        client = await self.client()
        await client.publish(team_channel(event.team_id), event.to_json())
        log.debug("event", type=event.type, team=event.team_id)

    async def subscribe(self, team_ids: set[str]) -> AsyncIterator[Event]:
        """Yield events for these teams until the caller stops consuming."""
        if not team_ids:
            return
        client = await self.client()
        pubsub = client.pubsub()
        await pubsub.subscribe(*[team_channel(t) for t in team_ids])
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30.0)
                if message is None:
                    # Idle. The socket layer sends its own keepalive.
                    continue
                try:
                    yield Event.from_json(message["data"])
                except (ValueError, TypeError):
                    log.warning("event.unparseable", raw=str(message.get("data"))[:200])
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


_bus: EventBus | None = None


def event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def set_event_bus(bus: EventBus) -> None:
    global _bus
    _bus = bus


class NullBus(EventBus):
    """Collects events instead of publishing them. For tests."""

    def __init__(self) -> None:  # noqa: D107
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    async def subscribe(self, team_ids: set[str]) -> AsyncIterator[Event]:
        for event in self.events:
            if event.team_id in team_ids:
                yield event
        await asyncio.sleep(0)

    async def close(self) -> None:
        return None
