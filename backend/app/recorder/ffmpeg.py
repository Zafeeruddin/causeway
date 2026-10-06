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
from app.recorder.accel import Accel
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


#: Video codecs a browser will accept over WebRTC without help. H.264 is the
#: only one a camera is likely to send; the rest are here because a source that
#: already speaks them needs no more from us than one that speaks H.264.
BROWSER_CODECS = frozenset({"h264", "avc1", "vp8", "vp9", "av1"})


def needs_transcode(codec: str) -> bool:
    """Whether this source has to be re-encoded to be watchable in a browser.

    Only a codec we have positively identified and know is undeliverable earns
    the CPU. An unprobed source stays a copy: guessing wrong there spends a core
    on a stream that would have played, and the player says plainly when a codec
    turns out to be one it cannot decode.
    """
    return bool(codec) and codec.strip().lower() not in BROWSER_CODECS


def publish_argv(
    url: StreamUrl,
    kind: SourceKind,
    target: str,
    *,
    transcode: bool = False,
    accel: Accel = Accel.CPU,
) -> list[str]:
    """Republish a live source to MediaMTX.

    This runs *inside* the profile's namespace, because that is the only place
    the camera exists -- and it reaches MediaMTX back out over the namespace's
    control route, which is the one thing that route is for. ``target`` must
    therefore be an address, never a name: Docker's resolver lives on the
    container's own loopback and is not visible from inside a namespace, so a
    hostname here fails to resolve in a way that looks like the camera is down.

    Copying is the default and keeps a preview close to free. Re-encoding is
    reserved for sources a browser cannot decode at all -- an H.265 camera is
    otherwise recordable but unwatchable -- and is deliberately not the standing
    behaviour: at fifteen concurrent streams, transcoding the ones that did not
    need it is what would make the machine the limit.
    """
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning"]
    if kind is SourceKind.RTSP:
        argv += ["-rtsp_transport", "tcp"]
    if transcode and accel is Accel.NVIDIA:
        # Before -i, and only when re-encoding: this decodes into card memory,
        # so the encoder below reads frames that never crossed the PCIe bus.
        # Asking for it on a stream copy would decode a stream nothing decodes.
        argv += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    argv += [
        # ffmpeg's defaults are tuned for files of unknown shape: analyse up to
        # five seconds and five megabytes before emitting anything. A live
        # camera is neither unknown nor in a hurry to repeat itself, and that
        # analysis is pure latency in front of the first frame -- most of the
        # wait between pressing Preview and seeing a picture. Cutting it costs
        # nothing; it is strictly less work.
        "-analyzeduration",
        "1000000",
        "-probesize",
        "1000000",
        # nobuffer and low_delay stop the demuxer holding frames back to smooth
        # delivery, which is the wrong trade for a monitor view.
        "-fflags",
        "+genpts+nobuffer",
        "-flags",
        "low_delay",
        "-i",
        url.expose(),
    ]
    argv += _transcode_argv(accel) if transcode else ["-c", "copy"]
    argv += [
        "-f",
        "rtsp",
        # The push leg is pinned to TCP for the same reason the pull leg is.
        "-rtsp_transport",
        "tcp",
        target,
    ]
    return argv


#: The browser-playable copy of a recording, beside the original. The original
#: keeps the camera's own bytes; this one exists only so a browser can decode it.
PLAYBACK_FILE = "session-h264.mp4"


def playback_argv(src: Path | str, dst: Path | str, accel: Accel) -> list[str]:
    """Re-encode a finished recording into something every browser can decode.

    Deliberately not the preview encoder. That one is tuned for a live monitor
    view -- baseline profile, no B-frames, a keyframe every second, audio
    discarded -- because a viewer joining an established stream halfway through
    matters more than the size of anything. This is the opposite errand. The
    file is watched from its beginning, it is stored rather than thrown away a
    second later, and it is a copy somebody may download. So it keeps the audio,
    spends a slower preset on a smaller file, and puts the index at the front so
    playback starts without fetching the whole thing first.

    The pixel format is forced: these cameras send full-range ``yuvj420p``,
    which some decoders render with visibly wrong levels.
    """
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning", "-y"]
    if accel is Accel.NVIDIA:
        # Decode on the card as well as encode, so the frames never cross the
        # bus. The source here is usually HEVC, which is the expensive half.
        argv += ["-hwaccel", "cuda"]
    argv += ["-i", str(src)]
    if accel is Accel.NVIDIA:
        argv += [
            "-c:v",
            "h264_nvenc",
            # p2 rather than p4: the wait is what makes this feature tolerable,
            # and the difference in the picture is not visible on a camera feed
            # while the difference in time is minutes on a long recording.
            "-preset",
            "p2",
            "-profile:v",
            "high",
            "-rc",
            "vbr",
            "-cq",
            "23",
        ]
    else:
        argv += ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-crf", "23"]
    argv += [
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    return argv


def _transcode_argv(accel: Accel) -> list[str]:
    """Re-encode to what every browser can decode, as cheaply as it can be done.

    Same picture either way, and the same three choices behind it whichever
    encoder runs. This is a monitor view, not an archive -- the recording keeps
    the camera's own bytes and owes nothing to this path -- so the fastest
    preset wins over quality. The short keyframe interval is what lets a viewer
    joining an established stream see a picture in about a second rather than
    waiting out the camera's own interval, which on these is often several. And
    audio is dropped rather than re-encoded to Opus: nothing in the product
    listens to it, so encoding it would be spending the machine on silence.
    """
    if accel is Accel.NVIDIA:
        return [
            "-an",
            "-c:v",
            "h264_nvenc",
            # p1 is NVENC's fastest preset and ll its low-latency tuning: the
            # equivalent choice to ultrafast/zerolatency below.
            "-preset",
            "p1",
            "-tune",
            "ll",
            "-profile:v",
            "baseline",
            # B-frames buy compression at the cost of latency, which is the
            # wrong trade for a live view.
            "-bf",
            "0",
            "-g",
            "30",
            # No -pix_fmt here on purpose: the frames are already on the card in
            # NV12, and naming a pixel format would pull them back to host
            # memory and hand the saving straight back.
        ]
    return [
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        # Baseline: no B-frames, and the profile every decoder has.
        "-profile:v",
        "baseline",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "30",
    ]


#: The tag Safari and QuickTime will accept for HEVC in MP4. ffmpeg writes
#: ``hev1`` by default when stream-copying, and Safari silently refuses that --
#: same bytes, same codec, four characters between playing and not.
HVC1 = "hvc1"


def probe_codec_argv(path: Path | str) -> list[str]:
    """Ask what codec a file holds. Reads the header, not the file."""
    return [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]


def concat_argv(list_file: Path | str, output: Path | str, *, video_tag: str = "") -> list[str]:
    """Join sealed segments into one MP4 without re-encoding video.

    ``video_tag`` relabels the video stream without touching it. HEVC needs
    ``hvc1`` to play in Safari and on iOS; left at ffmpeg's default of ``hev1``
    the file is correct, plays in VLC, and is refused by every Apple decoder.
    It is not set blindly: forcing it onto H.264 would mislabel a stream that
    was already fine.
    """
    argv = [
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
    ]
    if video_tag:
        argv += ["-tag:v", video_tag]
    argv += ["-movflags", "+faststart", "-y", str(output)]
    return argv


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
