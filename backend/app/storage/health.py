"""Is the object store there?

Causeway records into object storage and cannot do that job without it, so a
deployment whose store is down should say so on the first screen rather than
let someone sign in, open a camera and find out when the recording fails. Not
every deployment uses the same storage product, and some have none at all,
which is why this asks nothing more product-specific than HeadBucket on the
configured bucket, and can be switched off with ``STORAGE_HEALTH_CHECK``.

Three constraints shape it:

* **The answer is public.** ``/api/health`` is read before sign-in, so what goes
  out is only what a person could usefully repeat to an administrator: up or
  down, and the endpoint host. Never the bucket, and never whether it was the
  network or a credential that failed -- that distinction is worth a lot to an
  operator and nothing to anyone else, so it goes to the log.
* **Every page load asks.** One probe per TTL is shared by every caller, which
  matters most during an outage: a store that is already struggling should not
  get an extra request from every visitor refreshing a broken page.
* **It has to answer quickly.** The recording client retries three times and
  waits up to two minutes to read, which is right for an upload and wrong for a
  loading screen. The probe makes one attempt under its own ceiling, so a store
  that is not answering is reported unavailable in seconds.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import structlog

from app.config import settings
from app.storage.client import ObjectStore

log = structlog.get_logger(__name__)

StorageStatus = Literal["ok", "unavailable", "disabled"]

#: Anything that raises when the store is not usable.
Check = Callable[[], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class StorageHealth:
    status: StorageStatus
    #: The host a person can pass on -- ``s3.example.com``, not the full URL.
    endpoint: str

    def public(self) -> dict[str, str]:
        return {"status": self.status, "endpoint": self.endpoint}


def endpoint_label(url: str) -> str:
    """The part of an endpoint URL worth showing someone.

    Host and port only. A URL can carry credentials in its userinfo, and the
    scheme and path mean nothing to the person who has to pick up a phone.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    return f"{host}:{parts.port}" if host and parts.port else host


class StorageProbe:
    """Whether the object store is usable, asked at most once per TTL."""

    def __init__(
        self,
        *,
        check: Check | None = None,
        endpoint: str | None = None,
        enabled: bool | None = None,
        ttl: float | None = None,
        timeout: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        cfg = settings()
        self.enabled = cfg.storage_health_check if enabled is None else enabled
        self.endpoint = endpoint_label(cfg.s3_endpoint_url) if endpoint is None else endpoint
        self.ttl = cfg.storage_health_ttl_seconds if ttl is None else ttl
        self.timeout = cfg.storage_health_timeout_seconds if timeout is None else timeout
        self._check = check or self._head_bucket
        self._clock = clock
        self._store: ObjectStore | None = None
        self._cached: tuple[float, StorageHealth] | None = None
        self._lock = asyncio.Lock()

    async def current(self) -> StorageHealth:
        if not self.enabled:
            return StorageHealth("disabled", self.endpoint)
        fresh = self._fresh()
        if fresh is not None:
            return fresh
        # Callers arriving while a probe is out wait for its answer rather than
        # each sending their own.
        async with self._lock:
            fresh = self._fresh()
            if fresh is not None:
                return fresh
            result = await self._probe()
            self._cached = (self._clock(), result)
            return result

    def _fresh(self) -> StorageHealth | None:
        if self._cached is None:
            return None
        at, result = self._cached
        return result if self._clock() - at < self.ttl else None

    async def _probe(self) -> StorageHealth:
        previous = self._cached[1].status if self._cached else None
        try:
            await asyncio.wait_for(self._check(), timeout=self.timeout)
        except TimeoutError:
            reason = f"no answer within {self.timeout:g}s"
        except Exception as exc:  # noqa: BLE001 - every failure means the same thing to the page
            reason = str(exc) or type(exc).__name__
        else:
            if previous == "unavailable":
                log.info("storage.health.recovered", endpoint=self.endpoint)
            return StorageHealth("ok", self.endpoint)
        # Logged when it changes, not on every probe: an outage that lasts an
        # hour is one line and a recovery, not two hundred and forty.
        if previous != "unavailable":
            log.warning("storage.health.unavailable", endpoint=self.endpoint, reason=reason)
        return StorageHealth("unavailable", self.endpoint)

    def _probe_store(self) -> ObjectStore:
        if self._store is None:
            self._store = ObjectStore(
                connect_timeout=min(3.0, self.timeout),
                read_timeout=self.timeout,
                # One attempt. A retry inside the same ceiling only means the
                # first attempt had less time to succeed.
                retries={"total_max_attempts": 1, "mode": "standard"},
            )
        return self._store

    async def _head_bucket(self) -> None:
        await self._probe_store().check()
