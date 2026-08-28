"""Building ffmpeg command lines, and reading back what ffmpeg says.

Three decisions in here shape everything downstream.

**Segments are MPEG-TS, not MP4.** An MP4 only becomes readable when its moov
atom is written, and that happens when the muxer exits cleanly -- exactly what
does not happen when a tunnel dies mid-write. A TS segment is a stream of
self-describing packets: whatever reached the disk plays. The session is
concatenated into an MP4 at the end, where there is a clean exit to rely on.

**Durations come from ffmpeg's own segment list, not from our arithmetic.** The
segment muxer appends one CSV line per segment *as it seals it*. That gives an
accurate duration and, more usefully, a heartbeat: a segment list that stops
growing is a stream that stopped arriving, even while the socket is still open
and ffmpeg is still running. A socket timeout does not catch that case, and it
is the one cameras actually produce.

**No socket-timeout options.** ``-stimeout`` was renamed ``-timeout`` for RTSP
between ffmpeg 5 and 6, and the wrong one is either ignored or fatal depending
on the build. The supervisor's watchdog covers the same ground for every version
and catches the stalled-but-connected case as well, so the option is not used.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.enums import SourceKind
from app.security.redaction import StreamUrl

#: Written by the segment muxer, one line per sealed segment.
SEGMENT_LIST = "segments.csv"
SEGMENT_PATTERN = "seg-%05d.ts"
SEGMENT_GLOB = "seg-*.ts"
CONCAT_LIST = "concat.txt"
SESSION_FILE = "session.mp4"


@dataclass(frozen=True, slots=True)
class SealedSegment:
    """One line of the segment list: a chunk ffmpeg has closed and flushed."""

    filename: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return round(max(self.end - self.start, 0.0), 3)


def capture_argv(
    url: StreamUrl,
    kind: SourceKind,
    out_dir: Path | str,
    *,
    seconds: float,
    segment_seconds: int,
) -> list[str]:
    """Capture ``url`` into sealed segments under ``out_dir``.

    ``seconds`` is the wall-clock budget left for this run, so a capture that
    resumes after an outage asks for the remainder rather than starting the
    clock again.
    """
    out_dir = Path(out_dir)
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning"]

    if kind is SourceKind.RTSP:
        # ssh -L forwards TCP only. UDP RTSP through a tunnel connects and then
        # delivers nothing at all, so the transport is pinned rather than
        # negotiated -- the same reason the probe pins it.
        argv += ["-rtsp_transport", "tcp"]

    argv += [
        # Cameras with a drifting or restarting RTP clock otherwise produce
        # segments the concat step refuses to join.
        "-fflags",
        "+genpts",
        "-i",
        url.expose(),
        "-t",
        f"{max(seconds, 1.0):.3f}",
        # Audio is optional and copied only when the container can carry it;
        # G.711 from a camera intercom would otherwise ride in mpegts as an
        # unrecognised private stream.
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-segment_format",
        "mpegts",
        "-reset_timestamps",
        "1",
        "-segment_list",
        str(out_dir / SEGMENT_LIST),
        "-segment_list_type",
        "csv",
        str(out_dir / SEGMENT_PATTERN),
    ]
    return argv


def publish_argv(url: StreamUrl, kind: SourceKind, target: str) -> list[str]:
    """Republish a live source to MediaMTX, without re-encoding.

    This runs *inside* the profile's namespace, because that is the only place
    the camera exists -- and it reaches MediaMTX back out over the namespace's
    control route, which is the one thing that route is for. ``target`` must
    therefore be an address, never a name: Docker's resolver lives on the
    container's own loopback and is not visible from inside a namespace, so a
    hostname here fails to resolve in a way that looks like the camera is down.

    Copying rather than transcoding keeps a preview close to free; a browser
    that cannot play the camera's codec is a real possibility, and the honest
    answer to that is a message rather than fifteen silent transcodes.
    """
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning"]
    if kind is SourceKind.RTSP:
        argv += ["-rtsp_transport", "tcp"]
    argv += [
        "-fflags",
        "+genpts",
        "-i",
        url.expose(),
        "-c",
        "copy",
        "-f",
        "rtsp",
        # The push leg is pinned to TCP for the same reason the pull leg is.
        "-rtsp_transport",
        "tcp",
        target,
    ]
    return argv


def concat_argv(list_file: Path | str, output: Path | str) -> list[str]:
    """Join sealed segments into one MP4 without re-encoding video."""
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        # AAC arrives from mpegts in ADTS framing, which MP4 cannot hold.
        "-bsf:a",
        "aac_adtstoasc",
        "-movflags",
        "+faststart",
        "-y",
        str(output),
    ]


def concat_list(segments: list[Path | str]) -> str:
    """The concat demuxer's input file. Single quotes are its escape character."""
    return "".join(f"file '{str(path).replace(chr(39), chr(39) * 3)}'\n" for path in segments)


def parse_segment_list(text: str) -> list[SealedSegment]:
    """Read ``segments.csv``. Partial trailing lines are ignored -- the file is
    read while ffmpeg is still appending to it."""
    sealed: list[SealedSegment] = []
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) != 3 or not parts[0]:
            continue
        try:
            sealed.append(SealedSegment(parts[0], float(parts[1]), float(parts[2])))
        except ValueError:
            continue
    return sealed


#: Substrings in ffmpeg's diagnostics, mapped to what a person should do about
#: them, and to the layer the supervisor should suspect first.
_EXIT_TABLE: list[tuple[str, str, str]] = [
    ("401", "the camera rejected these credentials", "camera"),
    ("unauthorized", "the camera rejected these credentials", "camera"),
    ("404", "the camera has no stream at that path", "camera"),
    ("connection refused", "nothing is listening on the far end of the tunnel", "ssh"),
    ("administratively prohibited", "the jump host refused to open the forward", "ssh"),
    ("no route to host", "the camera's network is not reachable on this path", "vpn"),
    ("network is unreachable", "the camera's network is not reachable on this path", "vpn"),
    ("connection timed out", "the camera stopped answering", "camera"),
    ("connection reset", "the connection to the camera was reset", "camera"),
    ("end of file", "the camera closed the stream", "camera"),
    ("immediate exit requested", "the recorder stopped this capture", "unknown"),
    ("invalid data found", "the camera sent something that is not a stream", "camera"),
    ("server returned 5", "the stream server returned an error", "camera"),
]


@dataclass(frozen=True, slots=True)
class ExitReason:
    message: str
    #: Layer to suspect: vpn, ssh, camera or unknown. Confirmed by probing, not
    #: trusted on its own -- ffmpeg reports what its socket saw, and a dropped
    #: VPN and a dropped camera look identical from there.
    suspect: str = "unknown"


def explain_exit(returncode: int, stderr: str) -> ExitReason:
    text = stderr.lower()
    for needle, message, suspect in _EXIT_TABLE:
        if needle in text:
            return ExitReason(message, suspect)
    if returncode in (-9, -15, 137, 143):
        return ExitReason("the capture was stopped by the recorder", "unknown")
    tail = stderr.strip().splitlines()
    if tail:
        return ExitReason(tail[-1][:200], "unknown")
    return ExitReason(f"ffmpeg exited {returncode} without saying why", "unknown")
