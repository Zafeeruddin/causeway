"""Fakes that let the whole network stack be tested without a network."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from app.net.runner import ProcResult


def reader_for(lines: list[str]) -> asyncio.StreamReader:
    stream = asyncio.StreamReader()
    for line in lines:
        stream.feed_data((line.rstrip("\n") + "\n").encode())
    stream.feed_eof()
    return stream


class FakeStdin:
    def __init__(self) -> None:
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeProcess:
    """Stands in for asyncio.subprocess.Process."""

    def __init__(
        self,
        stderr: list[str] | None = None,
        stdout: list[str] | None = None,
        returncode: int | None = None,
    ) -> None:
        self.stderr = reader_for(stderr or [])
        self.stdout = reader_for(stdout or [])
        self.stdin = FakeStdin()
        self.returncode = returncode
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


@dataclass
class FakeRunner:
    """Serves canned results by substring match and hands back a scripted process."""

    name: str = "fake"
    results: dict[str, ProcResult] = field(default_factory=dict)
    process: FakeProcess | None = None
    calls: list[list[str]] = field(default_factory=list)
    default: ProcResult = field(default_factory=lambda: ProcResult(0, "", ""))

    def wrap(self, argv):
        return list(argv)

    async def run(self, argv, *, timeout=30.0, stdin=None, env=None, check=False):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for needle, result in self.results.items():
            if needle in joined:
                return result
        return self.default

    async def spawn(self, argv, *, env=None):
        self.calls.append(list(argv))
        assert self.process is not None, "no scripted process for this test"
        return self.process


@pytest.fixture
def link_show_ppp() -> ProcResult:
    """`ip -o link show` with a pppd tunnel present."""
    return ProcResult(
        0,
        "1: lo: <LOOPBACK,UP> mtu 65536\n"
        "2: eth0: <BROADCAST,MULTICAST,UP> mtu 1500\n"
        "3: ppp0: <POINTOPOINT,MULTICAST,NOARP,UP> mtu 1354\n",
        "",
    )


@pytest.fixture
def addr_show() -> ProcResult:
    return ProcResult(
        0, "3: ppp0    inet 10.212.134.88 peer 10.212.134.1/32 scope global ppp0\n", ""
    )
