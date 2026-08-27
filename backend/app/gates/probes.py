"""Network probes, all executed through a :class:`Runner`.

Everything here has to work inside a VPN's network namespace, which rules out
opening sockets from this process -- the socket would be created in the wrong
namespace and quietly test the wrong network. So each probe shells out through
the runner instead, and the runner decides whether that means plain exec or
``ip netns exec``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import structlog

from app.net.runner import Runner
from app.security.redaction import StreamUrl

log = structlog.get_logger(__name__)

#: Inlined rather than shipped as a file so it survives ``ip netns exec``
#: without depending on the working directory.
_TCP_SCRIPT = """
import socket, sys
host, port, timeout = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
try:
    with socket.create_connection((host, port), timeout):
        print("open")
except Exception as exc:
    print(f"closed: {type(exc).__name__}: {exc}")
    sys.exit(1)
"""


@dataclass(slots=True)
class TcpResult:
    open: bool
    detail: str = ""


@dataclass(slots=True)
class StreamInfo:
    """What a probe learned about a stream. Drives the camera tile's readout."""

    ok: bool
    detail: str = ""
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    audio_codec: str | None = None

    @property
    def resolution(self) -> str | None:
        return f"{self.width}x{self.height}" if self.width and self.height else None


async def tcp_probe(runner: Runner, host: str, port: int, *, timeout: float = 5.0) -> TcpResult:
    result = await runner.run(
        ["python3", "-c", _TCP_SCRIPT, host, str(port), str(timeout)],
        timeout=timeout + 5.0,
    )
    if result.ok:
        return TcpResult(open=True)
    return TcpResult(open=False, detail=result.stdout.strip() or result.stderr.strip()[:200])


async def stream_probe(
    runner: Runner,
    url: StreamUrl,
    *,
    rtsp: bool = True,
    timeout: float = 12.0,
) -> StreamInfo:
    """Handshake with a stream and read back what it is actually sending.

    Proves more than a TCP connect: a camera that accepts the connection, answers
    DESCRIBE and then sends no video is a real and common failure, and this is
    where it gets caught rather than three minutes into a recording.
    """
    argv = ["ffprobe", "-v", "error", "-hide_banner"]
    if rtsp:
        # ssh -L forwards TCP only. UDP RTSP through the tunnel connects and
        # then delivers nothing, so the transport is pinned here, not guessed.
        argv += ["-rtsp_transport", "tcp"]
    argv += [
        "-timeout",
        str(int(timeout * 1_000_000)),
        "-i",
        url.expose(),
        "-show_streams",
        "-show_format",
        "-of",
        "json",
    ]

    result = await runner.run(argv, timeout=timeout + 8.0)
    if not result.ok:
        return StreamInfo(ok=False, detail=_explain_ffprobe(result.stderr))

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return StreamInfo(ok=False, detail="the stream answered but sent nothing we could read")

    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        return StreamInfo(
            ok=False,
            detail="connected, but the stream carries no video track - check the stream path",
        )

    return StreamInfo(
        ok=True,
        detail="stream ok",
        codec=video.get("codec_name"),
        width=video.get("width"),
        height=video.get("height"),
        fps=_parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        audio_codec=audio.get("codec_name") if audio else None,
    )


def _parse_fps(value: str | None) -> float | None:
    if not value or "/" not in value:
        return None
    num, _, den = value.partition("/")
    try:
        return round(int(num) / int(den), 2) if int(den) else None
    except (ValueError, ZeroDivisionError):
        return None


def _explain_ffprobe(stderr: str) -> str:
    """Turn ffmpeg's diagnostics into something a person can act on."""
    text = stderr.lower()
    table = [
        ("401", "the camera rejected these credentials"),
        ("unauthorized", "the camera rejected these credentials"),
        ("404", "no stream at that path - check the stream URL"),
        ("not found", "no stream at that path - check the stream URL"),
        ("connection refused", "nothing is listening on that port"),
        ("timed out", "the camera accepted nothing in time - it may be off or unreachable"),
        ("immediate exit requested", "the probe timed out waiting for a first frame"),
        ("453", "the camera is out of session slots - something else is already streaming it"),
        ("not enough bandwidth", "the camera is out of session slots"),
        ("invalid data found", "the stream is not in a format we can read"),
        ("protocol not found", "that URL scheme is not supported"),
    ]
    for needle, message in table:
        if needle in text:
            return message
    first = next((line for line in stderr.splitlines() if line.strip()), "")
    return first.strip()[:200] or "the stream could not be opened"
