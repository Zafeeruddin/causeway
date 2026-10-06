"""The browser-playable copy of a recording.

These cameras send H.265 and a recording is a stream copy, so the archive holds
bytes no browser on an operator's machine can decode. A second copy is made on
request, beside the original, which is never touched.
"""

from __future__ import annotations

import pytest

from app.agent.playback import is_playback_key, playback_key_for
from app.net.runner import ProcResult
from app.recorder.accel import Accel
from app.recorder.ffmpeg import HVC1, PLAYBACK_FILE, concat_argv, playback_argv
from app.recorder.shipper import video_tag_for


def test_the_copy_sits_beside_the_original():
    """Same recording, same prefix. Retention sweeps a recording by prefix, so
    a copy filed anywhere else would outlive the thing it belongs to."""
    original = "teams/acme/2026/09/20/rec-1/rtsp/session.mp4"
    assert playback_key_for(original) == f"teams/acme/2026/09/20/rec-1/rtsp/{PLAYBACK_FILE}"


def test_the_copy_is_told_apart_from_what_the_camera_sent():
    assert is_playback_key(f"teams/m/2026/09/20/r/rtsp/{PLAYBACK_FILE}")
    assert not is_playback_key("teams/m/2026/09/20/r/rtsp/session.mp4")
    assert not is_playback_key("teams/m/2026/09/20/r/gaps.json")


def test_the_card_decodes_as_well_as_encodes():
    """The source is H.265, which is the expensive half. Decoding on the CPU
    and encoding on the card would drag every frame across the bus."""
    argv = playback_argv("in.mp4", "out.mp4", Accel.NVIDIA)
    assert "h264_nvenc" in argv
    assert argv[argv.index("-hwaccel") + 1] == "cuda"
    # ffmpeg applies input options to the input that follows them.
    assert argv.index("-hwaccel") < argv.index("-i")


def test_without_a_card_it_still_produces_something_playable():
    argv = playback_argv("in.mp4", "out.mp4", Accel.CPU)
    assert "libx264" in argv
    assert "-hwaccel" not in argv


def test_the_copy_plays_before_it_has_all_arrived():
    """faststart puts the index at the front. Without it the browser fetches
    the whole recording before it shows a frame, which for a 900 second
    capture is the difference between watching and waiting."""
    for accel in (Accel.NVIDIA, Accel.CPU):
        argv = playback_argv("in.mp4", "out.mp4", accel)
        assert argv[argv.index("-movflags") + 1] == "+faststart"


def test_the_copy_keeps_audio_where_the_preview_drops_it():
    """The live preview discards audio -- nothing listens to a monitor view.
    This is a file somebody may download, so it keeps what was recorded."""
    assert "-an" not in playback_argv("in.mp4", "out.mp4", Accel.NVIDIA)


def test_full_range_pixels_are_normalised():
    """These cameras send yuvj420p. Some decoders render it with visibly wrong
    levels, which looks like a washed-out camera rather than a format quirk."""
    argv = playback_argv("in.mp4", "out.mp4", Accel.NVIDIA)
    assert argv[argv.index("-pix_fmt") + 1] == "yuv420p"


# ---- the container tag -------------------------------------------------


class Probe:
    """An ffprobe that says what the test wants it to say."""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self._stdout = stdout
        self._returncode = returncode

    def wrap(self, argv):
        return list(argv)

    async def run(self, argv, **_):
        return ProcResult(self._returncode, self._stdout, "")

    async def spawn(self, argv, **_):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_hevc_is_relabelled_so_apple_devices_will_play_it():
    """ffmpeg's default hev1 produces a correct file that VLC plays and that
    Safari, QuickTime and every iPhone refuse without saying why."""
    assert await video_tag_for(Probe("hevc\n"), "run-000/seg-00000.ts") == HVC1


@pytest.mark.asyncio
async def test_h264_keeps_the_tag_it_already_had():
    """avc1 is what everything expects. Forcing hvc1 here would mislabel a
    stream that was never the problem."""
    assert await video_tag_for(Probe("h264\n"), "run-000/seg-00000.ts") == ""


@pytest.mark.asyncio
async def test_a_failed_probe_costs_the_tag_and_not_the_recording():
    """Whatever stopped the probe must not stop the shipping. Without a tag the
    file is exactly what it used to be, which is no worse than before."""
    assert await video_tag_for(Probe("", returncode=1), "run-000/seg-00000.ts") == ""


def test_the_tag_is_applied_only_when_asked_for():
    assert "-tag:v" not in concat_argv("list.txt", "out.mp4")
    argv = concat_argv("list.txt", "out.mp4", video_tag=HVC1)
    assert argv[argv.index("-tag:v") + 1] == HVC1
    # Still a stream copy: relabelling is not re-encoding.
    assert argv[argv.index("-c") + 1] == "copy"
