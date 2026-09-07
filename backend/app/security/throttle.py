"""Attempt limiting for the endpoints that take a password.

A sliding window of failure timestamps per key, held in this process. Not
distributed, and deliberately so: this deployment is one API container, Redis is
not always on the critical path for a request, and a limiter that fails open
because Redis blinked is worse than one that only counts what it saw. If the API
is ever run with replicas this becomes a shared counter -- until then, saying it
is per-process is honest and the numbers below are sized for one.

Two windows, because they stop different attacks and one cannot do both:

* **Per address** stops somebody working through a password list against one
  account, or through an account list from one machine.
* **Per account** stops the same list spread across many addresses, which is
  what a credential-stuffing run looks like and what a per-address limit misses
  entirely.

Only failures are counted and a success clears the key, so an ordinary person
mistyping their password twice and then getting it right never approaches
either limit.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock

from fastapi import Request


@dataclass(frozen=True, slots=True)
class Limit:
    """``attempts`` failures inside ``window`` seconds, then locked out."""

    attempts: int
    window: float

    def __post_init__(self) -> None:
        if self.attempts < 1 or self.window <= 0:
            raise ValueError("a limit needs at least one attempt and a positive window")


class Throttle:
    """Counts failures per key and says when a key has had enough.

    Threadsafe because FastAPI runs sync dependencies in a worker thread and a
    deque is not atomic across the read-modify-write this does.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def retry_after(self, key: str, limit: Limit, *, now: float | None = None) -> float:
        """Seconds until ``key`` may try again. ``0`` means it may try now."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._prune(key, limit, now)
            if not hits:
                # Nothing left in the window. Drop the key rather than leaving
                # an empty deque behind for every address that ever mistyped a
                # password: this dict otherwise grows for the life of the
                # process and never gives anything back.
                self._hits.pop(key, None)
                return 0.0
            if len(hits) < limit.attempts:
                return 0.0
            # The oldest hit in the window is the one whose expiry unblocks them.
            return max(0.0, hits[0] + limit.window - now)

    def record(self, key: str, limit: Limit, *, now: float | None = None) -> None:
        """Count one hit against ``key``.

        What counts is the caller's decision and it differs by endpoint: a
        sign-in counts only failures, so getting your password right is never
        rationed, while a reset request counts every one, because there is no
        such thing as a failed one to tell apart.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            self._prune(key, limit, now).append(now)

    def clear(self, *keys: str) -> None:
        """Forget these keys. Called on a success, so a good login resets both
        windows -- otherwise one person's bad afternoon locks them out of a
        password they now remember."""
        with self._lock:
            for key in keys:
                self._hits.pop(key, None)

    def _prune(self, key: str, limit: Limit, now: float) -> deque[float]:
        """Drop hits that have aged out. Returns the live deque, still stored."""
        hits = self._hits[key]
        cutoff = now - limit.window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return hits


def client_address(request: Request, *, trusted_hops: int) -> str:
    """The caller's address, as far as it can be trusted.

    ``X-Forwarded-For`` is a header, which means the client writes it, which
    means reading it blindly hands every attacker an unlimited supply of
    identities and turns the limiter off. It is only consulted when the operator
    has said how many proxies actually sit in front (``TRUSTED_PROXY_HOPS``),
    and then only the entry that many places from the right -- the last hop your
    own proxy appended, not the first one a client invented.
    """
    direct = request.client.host if request.client else "unknown"
    if trusted_hops <= 0:
        return direct

    forwarded = request.headers.get("x-forwarded-for", "")
    chain = [part.strip() for part in forwarded.split(",") if part.strip()]
    if not chain:
        return direct
    index = len(chain) - trusted_hops
    return chain[index] if 0 <= index < len(chain) else chain[0]
