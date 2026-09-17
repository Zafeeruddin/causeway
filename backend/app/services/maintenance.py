"""Whether somebody is working on this deployment right now.

A redeploy replaces the api and web containers. For the few seconds that takes,
whoever is signed in watches requests fail with nothing to explain them, and
whoever arrives meets a blank page. Neither says "come back in a minute", which
is the only thing either of them can act on.

The flag lives in Redis rather than in the environment for two reasons. Turning
maintenance *on* must not itself be an outage -- an environment variable would
mean recreating the api container in order to announce that the api container is
about to be recreated. And Redis is the one service a redeploy leaves running,
so the answer survives the swap it is describing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import redis.asyncio as redis
import structlog

from app.config import settings

log = structlog.get_logger(__name__)

#: One key, read on every health check.
KEY = "cam:maintenance"

#: What the screen says when whoever raised it did not say anything.
DEFAULT_NOTE = "Causeway is being updated."

#: A health check sits on the loading screen's critical path. Redis is local and
#: answers in microseconds; anything slower is treated as "nobody is deploying"
#: rather than kept waiting.
TIMEOUT = 2.0


@dataclass(frozen=True, slots=True)
class Maintenance:
    on: bool
    note: str = ""

    def public(self) -> dict[str, str]:
        """What an unauthenticated loading screen is allowed to see: the note an
        operator wrote for exactly this purpose, and nothing else."""
        return {"note": self.note}


class MaintenanceFlag:
    def __init__(self, *, url: str | None = None, timeout: float = TIMEOUT) -> None:
        self._url = url or settings().redis_url
        self._timeout = timeout
        self._client: redis.Redis | None = None

    async def client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.from_url(self._url, decode_responses=True)
        return self._client

    async def current(self) -> Maintenance:
        """Off unless Redis says otherwise.

        Every failure here reads as off, on purpose: a flag that cannot be read
        is not a reason to put a maintenance screen in front of a deployment
        that is working. The cost of being wrong in this direction is that a
        deploy goes unannounced; the other direction locks everyone out of a
        healthy system because Redis blinked.
        """
        try:
            raw = await asyncio.wait_for(self._read(), self._timeout)
        except Exception as exc:  # noqa: BLE001 - see docstring; never block on this
            log.debug("maintenance.unreadable", error=str(exc))
            return Maintenance(on=False)
        if raw is None:
            return Maintenance(on=False)
        return Maintenance(on=True, note=raw or DEFAULT_NOTE)

    async def _read(self) -> str | None:
        client = await self.client()
        return await client.get(KEY)

    # Setting the flag does *not* swallow failures the way reading it does. An
    # operator who runs `cam maintenance on` and is told it worked will go on to
    # replace the containers; if the write failed, they need to hear it now.

    async def turn_on(self, note: str = "") -> Maintenance:
        client = await self.client()
        stored = note.strip() or DEFAULT_NOTE
        await client.set(KEY, stored)
        log.info("maintenance.on", note=stored)
        return Maintenance(on=True, note=stored)

    async def turn_off(self) -> None:
        client = await self.client()
        await client.delete(KEY)
        log.info("maintenance.off")
