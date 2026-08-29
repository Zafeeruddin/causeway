"""The recorder, tested without a camera, a tunnel or a storage gateway.

The scripted ffmpeg here is the point: every interesting case in a recorder is a
failure part-way through, and those are the ones that never happen on demand
against a real camera.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.enums import RecordingState, SourceKind
from app.net.runner import ProcResult
from app.recorder.ffmpeg import (
    SEGMENT_LIST,
    capture_argv,
    concat_list,
    explain_exit,
    parse_segment_list,
)
from app.recorder.session import (
    MAX_EMPTY_RUNS,
    NullSink,
    OpenPath,
    RecordingSession,
    SourceCapture,
)
from app.recorder.shipper import GAPS_FILE, gaps_document, ship
from app.security.redaction import StreamUrl
from tests.conftest import FakeProcess

START = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
URL = StreamUrl.build("rtsp://cam-12.local:554/stream1", "viewer", "s3cret")


# ---- test doubles -------------------------------------------------------


class Clock:
    """Wall clock the test moves by hand. Sleeping advances it, which is what
    lets a five-minute recording run in a millisecond."""

    def __init__(self, start: datetime = START) -> None:
        self.at = start

    def now(self) -> datetime:
        return self.at

    async def sleep(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)
        await asyncio.sleep(0)


@dataclass
class Run:
    """One scripted ffmpeg invocation."""

    #: (filename, start, end) written to the segment list as sealed.
    sealed: list[tuple[str, float, float]] = field(default_factory=list)
    #: A segment file ffmpeg was mid-way through when it died.
    leftover: str | None = None
    returncode: int = 0
    stderr: list[str] = field(default_factory=list)
    #: Wall-clock seconds this run consumed.
    elapsed: float = 0.0


class ScriptedFfmpeg:
    """A runner whose spawned processes leave behind the files a real ffmpeg
    would have written."""

    name = "scripted"

    def __init__(self, runs: list[Run], clock: Clock) -> None:
        self.runs = list(runs)
        self.clock = clock
        self.calls: list[list[str]] = []

    def wrap(self, argv):
        return list(argv)

    async def run(self, argv, *, timeout=30.0, stdin=None, env=None, check=False):
        self.calls.append(list(argv))
        return ProcResult(0, "", "")

    async def spawn(self, argv, *, env=None):
        self.calls.append(list(argv))
        run = self.runs.pop(0) if self.runs else Run(returncode=1, stderr=["Connection refused"])
        out_dir = Path(argv[-1]).parent

        lines = []
        for name, start, end in run.sealed:
            (out_dir / name).write_bytes(b"x" * 1024)
            lines.append(f"{name},{start:.6f},{end:.6f}")
        if lines:
            (out_dir / SEGMENT_LIST).write_text("\n".join(lines) + "\n")
        if run.leftover:
            (out_dir / run.leftover).write_bytes(b"y" * 512)

        self.clock.at += timedelta(seconds=run.elapsed)
        return FakeProcess(stderr=run.stderr, returncode=run.returncode)


class FakePath:
    source_id = "src-1"
    kind = SourceKind.RTSP

    def __init__(
        self,
        runner,
        *,
        diagnosis: tuple[str, str] = ("camera", "the camera stopped answering"),
        open_failures: int = 0,
        open_script: list[bool] | None = None,
    ) -> None:
        self.runner = runner
        self.diagnosis = diagnosis
        self.open_failures = open_failures
        #: True to succeed, False to fail, in order. Overrides open_failures.
        self.open_script = list(open_script) if open_script else None
        self.opens = 0
        self.closed = False

    async def open(self) -> OpenPath:
        self.opens += 1
        if self.open_script is not None:
            if not self.open_script.pop(0):
                raise RuntimeError("the VPN rejected these credentials")
        elif self.open_failures > 0:
            self.open_failures -= 1
            raise RuntimeError("the VPN rejected these credentials")
        return OpenPath(url=URL, runner=self.runner)

    async def diagnose(self) -> tuple[str, str]:
        return self.diagnosis

    async def close(self) -> None:
        self.closed = True


def build(tmp_path: Path, runs: list[Run], *, seconds: int = 60, **kwargs) -> tuple:
    clock = Clock()
    runner = ScriptedFfmpeg(runs, clock)
    path = FakePath(runner, **kwargs)
    sink = NullSink()
    capture = SourceCapture(
        path=path,
        directory=tmp_path / "rtsp",
        deadline=clock.now() + timedelta(seconds=seconds),
        segment_seconds=10,
        sink=sink,
        now=clock.now,
        sleep=clock.sleep,
    )
    return capture, path, sink, clock


# ---- command lines ------------------------------------------------------


def test_rtsp_capture_is_pinned_to_tcp():
    argv = capture_argv(URL, SourceKind.RTSP, "/w", seconds=42.0, segment_seconds=10)
    assert "-rtsp_transport" in argv and argv[argv.index("-rtsp_transport") + 1] == "tcp"


def test_hls_capture_does_not_ask_for_an_rtsp_transport():
    argv = capture_argv(URL, SourceKind.HLS, "/w", seconds=42.0, segment_seconds=10)
    assert "-rtsp_transport" not in argv


def test_capture_asks_only_for_the_time_that_is_left():
    argv = capture_argv(URL, SourceKind.RTSP, "/w", seconds=42.5, segment_seconds=10)
    assert argv[argv.index("-t") + 1] == "42.500"


def test_capture_writes_a_segment_list_it_can_read_back():
    argv = capture_argv(URL, SourceKind.RTSP, "/w", seconds=60, segment_seconds=10)
    assert "/w/segments.csv" in argv
    assert argv[-1] == "/w/seg-%05d.ts"


def test_credentials_never_reach_the_argv_representation():
    argv = capture_argv(URL, SourceKind.RTSP, "/w", seconds=60, segment_seconds=10)
    # They are in the URL handed to the subprocess, and nowhere else.
    assert sum("s3cret" in part for part in argv) == 1
    assert "s3cret" not in str(URL)


def test_segment_list_ignores_the_line_ffmpeg_is_still_writing():
    sealed = parse_segment_list("seg-00000.ts,0.000000,10.000000\nseg-00001.ts,10.00")
    assert [s.filename for s in sealed] == ["seg-00000.ts"]
    assert sealed[0].duration == 10.0


def test_concat_list_escapes_quotes_in_paths():
    assert concat_list(["/w/o'clock.ts"]) == "file '/w/o'''clock.ts'\n"


@pytest.mark.parametrize(
    ("stderr", "suspect"),
    [
        ("401 Unauthorized", "camera"),
        ("Connection refused", "ssh"),
        ("No route to host", "vpn"),
    ],
)
def test_exit_reasons_name_the_layer_to_suspect(stderr, suspect):
    assert explain_exit(1, stderr).suspect == suspect


# ---- a clean recording --------------------------------------------------


async def test_a_clean_run_records_every_sealed_segment(tmp_path):
    capture, path, sink, _ = build(
        tmp_path,
        [Run(sealed=[("seg-00000.ts", 0, 10), ("seg-00001.ts", 10, 20)], elapsed=60)],
    )
    report = await capture.run()

    assert [s.sequence for s in report.segments] == [0, 1]
    assert report.captured_seconds == 20.0
    assert report.gaps == []
    assert report.failure == ""
    assert path.opens == 1
    assert path.closed
    assert sink.segments == report.segments


async def test_segment_start_times_are_absolute(tmp_path):
    capture, *_ = build(tmp_path, [Run(sealed=[("seg-00000.ts", 0, 10)], elapsed=60)])
    report = await capture.run()
    assert report.segments[0].started_at == START


async def test_the_segment_ffmpeg_died_inside_is_kept(tmp_path):
    """mpegts is chosen so a half-written segment still plays. Throwing it away
    would give back the failure mode the format was picked to avoid."""
    capture, *_ = build(
        tmp_path,
        [Run(sealed=[("seg-00000.ts", 0, 10)], leftover="seg-00001.ts", returncode=1, elapsed=60)],
    )
    report = await capture.run()

    assert [Path(s.path).name for s in report.segments] == ["seg-00000.ts", "seg-00001.ts"]
    assert report.segments[1].bytes == 512


# ---- outages ------------------------------------------------------------


async def test_a_drop_costs_a_gap_and_not_the_recording(tmp_path):
    capture, path, sink, _ = build(
        tmp_path,
        [
            Run(
                sealed=[("seg-00000.ts", 0, 10)],
                returncode=1,
                stderr=["Connection reset by peer"],
                elapsed=15,
            ),
            Run(sealed=[("seg-00000.ts", 0, 10), ("seg-00001.ts", 10, 20)], elapsed=120),
        ],
        seconds=120,
    )
    report = await capture.run()

    assert len(report.segments) == 3
    assert [s.sequence for s in report.segments] == [0, 1, 2]
    # Segments from the second run live in their own directory, so the second
    # ffmpeg cannot overwrite what the first one sealed.
    assert len({Path(s.path).parent for s in report.segments}) == 2

    assert len(report.gaps) == 1
    gap = report.gaps[0]
    assert gap.cause == "camera"
    assert gap.ended_at is not None and gap.seconds > 0
    assert path.opens == 2


async def test_the_gap_names_the_hop_the_path_blames_not_the_one_ffmpeg_saw(tmp_path):
    """ffmpeg only ever sees its own socket. A dropped VPN and an unplugged
    camera look identical from there, so the path gets the deciding vote."""
    capture, *_ = build(
        tmp_path,
        [
            Run(returncode=1, stderr=["Connection refused"], elapsed=10),
            Run(sealed=[("seg-00000.ts", 0, 10)], elapsed=120),
        ],
        seconds=120,
        diagnosis=("vpn", "the VPN tunnel for ACME is down"),
    )
    report = await capture.run()

    assert explain_exit(1, "Connection refused").suspect == "ssh"
    assert report.gaps[0].cause == "vpn"


async def test_one_outage_is_one_gap_however_many_redials_it_takes(tmp_path):
    """Opening a gap per failed redial would turn one dropped VPN into four.
    The attempts belong on the gap, not instead of it."""
    capture, path, _sink, _ = build(
        tmp_path,
        [
            Run(sealed=[("seg-00000.ts", 0, 10)], returncode=1, elapsed=10),
            Run(sealed=[("seg-00000.ts", 0, 10)], elapsed=200),
        ],
        seconds=200,
        open_script=[True, False, False, True],
    )
    report = await capture.run()

    assert len(report.gaps) == 1
    assert report.gaps[0].redial_attempts == 3
    assert path.opens == 4
    assert len(report.segments) == 2


async def test_recovering_is_reported_while_the_gap_is_open(tmp_path):
    capture, _path, sink, _ = build(
        tmp_path,
        [
            Run(sealed=[("seg-00000.ts", 0, 10)], returncode=1, elapsed=15),
            Run(sealed=[("seg-00000.ts", 0, 10)], elapsed=120),
        ],
        seconds=120,
    )
    await capture.run()

    states = [state for state, _ in sink.states]
    assert RecordingState.RECOVERING in states
    assert states[-1] is RecordingState.RECORDING


async def test_a_camera_that_never_answers_fails_instead_of_burning_the_session(tmp_path):
    capture, path, _sink, clock = build(
        tmp_path,
        [],  # every spawn falls through to an immediate failure
        seconds=3600,
        open_failures=99,
    )
    report = await capture.run()

    assert report.segments == []
    assert "the VPN rejected these credentials" in report.failure
    assert path.opens == MAX_EMPTY_RUNS
    # And it gave up in seconds rather than sitting on the hour it was given.
    assert (clock.now() - START).total_seconds() < 60


async def test_the_deadline_is_wall_clock_so_an_outage_shortens_the_file(tmp_path):
    capture, *_ = build(
        tmp_path,
        [
            Run(sealed=[("seg-00000.ts", 0, 10)], returncode=1, elapsed=10),
            Run(sealed=[("seg-00000.ts", 0, 10)], elapsed=50),
        ],
        seconds=60,
    )
    report = await capture.run()

    assert report.captured_seconds == 20.0
    assert report.captured_seconds < 60


# ---- the whole session --------------------------------------------------


async def test_a_session_runs_every_source_of_the_camera(tmp_path):
    clock = Clock()
    runner = ScriptedFfmpeg(
        [
            Run(sealed=[("seg-00000.ts", 0, 10)], elapsed=0),
            Run(sealed=[("seg-00000.ts", 0, 10)], elapsed=60),
        ],
        clock,
    )
    rtsp = FakePath(runner)
    hls = FakePath(runner)
    hls.kind = SourceKind.HLS
    hls.source_id = "src-2"

    session = RecordingSession(
        recording_id="rec-1",
        paths=[rtsp, hls],
        work_dir=tmp_path,
        requested_seconds=60,
        segment_seconds=10,
        now=clock.now,
        sleep=clock.sleep,
    )
    report = await session.run()

    assert report.ok
    assert {c.source_kind for c in report.captures} == {SourceKind.RTSP, SourceKind.HLS}
    # Both sources cover the same stretch of wall clock, so the session's length
    # is the longer of the two -- never their sum.
    assert report.captured_seconds == 10.0
    assert report.bytes == 2048


# ---- shipping -----------------------------------------------------------


class FakeStore:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, int]] = []

    async def put_file(self, path, key):
        from app.storage.client import StoredObject
        from app.storage.keys import content_type_for

        size = Path(path).stat().st_size
        self.uploads.append((key, size))
        return StoredObject(key=key, bytes=size, content_type=content_type_for(Path(path).name))


class JoiningRunner:
    """Stands in for the concat pass: writes the file ffmpeg would have made."""

    name = "joining"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def wrap(self, argv):
        return list(argv)

    async def run(self, argv, *, timeout=30.0, stdin=None, env=None, check=False):
        self.calls.append(list(argv))
        Path(argv[-1]).write_bytes(b"joined")
        return ProcResult(0, "", "")

    async def spawn(self, argv, *, env=None):  # pragma: no cover - unused
        raise NotImplementedError


async def _one_session(tmp_path) -> object:
    clock = Clock()
    runner = ScriptedFfmpeg([Run(sealed=[("seg-00000.ts", 0, 10)], elapsed=60)], clock)
    session = RecordingSession(
        recording_id="rec-1",
        paths=[FakePath(runner)],
        work_dir=tmp_path,
        requested_seconds=60,
        segment_seconds=10,
        now=clock.now,
        sleep=clock.sleep,
    )
    return await session.run()


async def test_shipping_uploads_one_object_per_source_plus_the_sidecar(tmp_path):
    report = await _one_session(tmp_path)
    store, runner = FakeStore(), JoiningRunner()

    result = await ship(report, team_slug="acme", requested_seconds=60, store=store, runner=runner)

    keys = [key for key, _ in store.uploads]
    assert keys == [
        "teams/acme/2026/08/28/rec-1/rtsp/session.mp4",
        "teams/acme/2026/08/28/rec-1/gaps.json",
    ]
    assert result.ok
    assert [obj.source_kind for obj in result.objects] == [SourceKind.RTSP, None]


async def test_the_sidecar_says_why_a_file_is_short(tmp_path):
    clock = Clock()
    runner = ScriptedFfmpeg(
        [
            Run(sealed=[("seg-00000.ts", 0, 10)], returncode=1, elapsed=15),
            Run(sealed=[("seg-00000.ts", 0, 10)], elapsed=120),
        ],
        clock,
    )
    session = RecordingSession(
        recording_id="rec-1",
        paths=[FakePath(runner, diagnosis=("vpn", "the VPN tunnel is down"))],
        work_dir=tmp_path,
        requested_seconds=120,
        segment_seconds=10,
        now=clock.now,
        sleep=clock.sleep,
    )
    report = await session.run()
    document = gaps_document(report, requested_seconds=120)

    assert document["requested_seconds"] == 120
    assert document["captured_seconds"] == 20.0
    gaps = document["sources"][0]["gaps"]
    assert len(gaps) == 1
    assert gaps[0]["cause"] == "vpn"
    assert gaps[0]["ended_at"] is not None


async def test_segments_stay_on_disk_when_the_upload_fails(tmp_path):
    report = await _one_session(tmp_path)

    class BrokenStore(FakeStore):
        async def put_file(self, path, key):
            from app.storage.client import StorageError

            raise StorageError("the gateway is not answering")

    result = await ship(
        report, team_slug="acme", requested_seconds=60, store=BrokenStore(), runner=JoiningRunner()
    )

    assert not result.ok
    # The storage layer's own words, not "upload failed" -- the person reading
    # this needs to know where to look.
    assert "The recording store could not be reached." in result.failures[0]
    assert list((tmp_path / "rec-1" / "rtsp").glob("run-*/seg-*.ts"))


async def test_a_source_that_captured_nothing_is_not_uploaded(tmp_path):
    clock = Clock()
    runner = ScriptedFfmpeg([Run(sealed=[("seg-00000.ts", 0, 10)], elapsed=60)], clock)
    rtsp = FakePath(runner)
    hls = FakePath(runner, open_failures=99)
    hls.kind = SourceKind.HLS

    session = RecordingSession(
        recording_id="rec-1",
        paths=[rtsp, hls],
        work_dir=tmp_path,
        requested_seconds=60,
        segment_seconds=10,
        now=clock.now,
        sleep=clock.sleep,
    )
    report = await session.run()
    store = FakeStore()
    await ship(report, team_slug="acme", requested_seconds=60, store=store, runner=JoiningRunner())

    keys = [key for key, _ in store.uploads]
    assert "teams/acme/2026/08/28/rec-1/hls/session.mp4" not in keys
    assert any(key.endswith(GAPS_FILE) for key in keys)
