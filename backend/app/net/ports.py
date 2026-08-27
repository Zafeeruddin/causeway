"""Local port leases for SSH forwards.

Two cameras must never be handed the same local port, and a port must not be
reused while a dying ffmpeg still holds a socket on it. The pool tracks leases
in memory and confirms with the kernel before handing one out -- the bind check
is what catches ports taken by something outside this process entirely.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)


class NoPortsAvailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Lease:
    port: int
    owner: str
    target: str


@dataclass
class PortPool:
    low: int = 20000
    high: int = 20099
    _leases: dict[int, Lease] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def lease(self, owner: str, target: str) -> Lease:
        """Reserve the lowest free port. ``owner`` is a camera source id."""
        async with self._lock:
            for port in range(self.low, self.high + 1):
                if port in self._leases or not _is_free(port):
                    continue
                lease = Lease(port=port, owner=owner, target=target)
                self._leases[port] = lease
                log.info("port.leased", port=port, owner=owner, target=target)
                return lease
        raise NoPortsAvailable(
            f"every port in {self.low}-{self.high} is in use. "
            "Raise TUNNEL_PORT_MAX or stop some recordings."
        )

    async def release(self, port: int) -> None:
        async with self._lock:
            if self._leases.pop(port, None) is not None:
                log.info("port.released", port=port)

    async def release_owner(self, owner: str) -> None:
        async with self._lock:
            for port in [p for p, lease in self._leases.items() if lease.owner == owner]:
                self._leases.pop(port, None)
                log.info("port.released", port=port, owner=owner)

    def get(self, owner: str) -> Lease | None:
        return next((lease for lease in self._leases.values() if lease.owner == owner), None)

    @property
    def in_use(self) -> int:
        return len(self._leases)


def _is_free(port: int) -> bool:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True
