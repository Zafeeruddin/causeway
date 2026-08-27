"""The seam between "run a command" and "run a command inside a VPN's network".

Every component that touches the network -- VPN clients, ssh, the TCP and RTSP
probes, ffmpeg -- takes a :class:`Runner` instead of calling subprocess itself.
Direct-mode profiles get a :class:`LocalRunner`; profiles with a VPN get a
:class:`NetnsRunner` bound to that profile's namespace. Nothing downstream has
to know which it got.

It is also what makes the whole stack testable: :class:`RecordingRunner` captures
argv without executing anything.
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import structlog

from app.security.redaction import redact

log = structlog.get_logger(__name__)


class CommandFailed(RuntimeError):
    def __init__(self, argv: Sequence[str], result: ProcResult) -> None:
        self.argv = list(argv)
        self.result = result
        super().__init__(
            f"{shlex.join(argv[:2])} exited {result.returncode}: {result.stderr[:400]}"
        )


class CommandTimeout(RuntimeError):
    pass


@dataclass(slots=True)
class ProcResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """stdout and stderr together -- most VPN clients report on stderr."""
        return f"{self.stdout}\n{self.stderr}".strip()


@runtime_checkable
class Runner(Protocol):
    name: str

    def wrap(self, argv: Sequence[str]) -> list[str]:
        """Return ``argv`` as it will actually be executed."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = 30.0,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
        check: bool = False,
    ) -> ProcResult: ...

    async def spawn(
        self, argv: Sequence[str], *, env: dict[str, str] | None = None
    ) -> asyncio.subprocess.Process:
        """Start a long-lived process (a VPN client, an ffmpeg) and return it."""


class _BaseRunner:
    name = "base"

    def wrap(self, argv: Sequence[str]) -> list[str]:
        return list(argv)

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = 30.0,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
        check: bool = False,
    ) -> ProcResult:
        full = self.wrap(argv)
        log.debug("exec", runner=self.name, argv=redact(shlex.join(full)))
        proc = await asyncio.create_subprocess_exec(
            *full,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode() if stdin is not None else None), timeout
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise CommandTimeout(f"{shlex.join(full[:3])} exceeded {timeout}s") from exc

        result = ProcResult(
            returncode=proc.returncode or 0,
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
        )
        if check and not result.ok:
            raise CommandFailed(full, result)
        return result

    async def spawn(
        self, argv: Sequence[str], *, env: dict[str, str] | None = None
    ) -> asyncio.subprocess.Process:
        full = self.wrap(argv)
        log.debug("spawn", runner=self.name, argv=redact(shlex.join(full)))
        return await asyncio.create_subprocess_exec(
            *full,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )


class LocalRunner(_BaseRunner):
    """Runs in whatever namespace this process is already in. Direct mode."""

    name = "local"


class NetnsRunner(_BaseRunner):
    """Runs inside a named network namespace via ``ip netns exec``.

    Requires CAP_NET_ADMIN and a namespace already created by
    :class:`~app.net.netns.NetnsManager`.
    """

    name = "netns"

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace

    def wrap(self, argv: Sequence[str]) -> list[str]:
        return ["ip", "netns", "exec", self.namespace, *argv]


@dataclass
class RecordingRunner(_BaseRunner):
    """Test double. Records every argv and replays canned results."""

    name: str = "recording"
    calls: list[list[str]] = field(default_factory=list)
    results: dict[str, ProcResult] = field(default_factory=dict)
    default: ProcResult = field(default_factory=lambda: ProcResult(0, "", ""))

    def wrap(self, argv: Sequence[str]) -> list[str]:
        return list(argv)

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = 30.0,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
        check: bool = False,
    ) -> ProcResult:
        self.calls.append(list(argv))
        joined = shlex.join(argv)
        for needle, result in self.results.items():
            if needle in joined:
                if check and not result.ok:
                    raise CommandFailed(argv, result)
                return result
        if check and not self.default.ok:
            raise CommandFailed(argv, self.default)
        return self.default

    async def spawn(
        self, argv: Sequence[str], *, env: dict[str, str] | None = None
    ) -> asyncio.subprocess.Process:  # pragma: no cover - not used in unit tests
        raise NotImplementedError("RecordingRunner cannot spawn long-lived processes")
