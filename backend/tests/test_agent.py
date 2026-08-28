"""The agent: claiming work, writing it down, and clearing space.

These run against the same SQLite schema the API tests use, so the claim query
and the accounting are exercised as SQL rather than as intent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agent.sink import DbSink
from app.agent.worker import Agent, _Plan
from app.enums import ReachMode, RecordingState, SourceKind
from app.models import (
    Camera,
    CameraSource,
    ConnectionProfile,
    Gap,
    Recording,
    Segment,
    StorageObject,
    Team,
    User,
)
from app.recorder.session import GapRecord, RecordingSession, SegmentRecord
from app.services.events import NullBus
from app.storage.retention import StoragePolicy
from tests.test_recorder import Clock, FakePath, JoiningRunner, Run, ScriptedFfmpeg

GB = 1024**3
NOW = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)


@pytest.fixture
async def fixture_ids(sessions):
    """One team, one camera with two sources, one queued recording."""
    async with sessions() as db:
        team = Team(name="ACME", slug="acme")
        user = User(email="qa@example.com", password_hash="x")
        db.add_all([team, user])
        await db.flush()
        profile = ConnectionProfile(team_id=team.id, name="ACME VPN", mode=ReachMode.DIRECT)
        db.add(profile)
        await db.flush()
        camera = Camera(team_id=team.id, profile_id=profile.id, name="Gate 1")
        db.add(camera)
        await db.flush()
        db.add_all(
            [
                CameraSource(
                    camera_id=camera.id,
                    kind=SourceKind.RTSP,
                    url="rtsp://10.0.0.4:554/s1",
                    host="10.0.0.4",
                    port=554,
                ),
                CameraSource(
                    camera_id=camera.id,
                    kind=SourceKind.HLS,
                    url="https://hls.example/s1.m3u8",
                    uses_profile_path=False,
                ),
            ]
        )
        recording = Recording(
            team_id=team.id,
            camera_id=camera.id,
            requested_by=user.id,
            requested_seconds=60,
            state=RecordingState.QUEUED,
        )
        db.add(recording)
        await db.commit()
        return {
            "team": team.id,
            "camera": camera.id,
            "profile": profile.id,
            "recording": recording.id,
        }


def build_agent(sessions, bus=None, **kwargs) -> Agent:
    return Agent(
        connections=None,  # not reached by these tests
        sessions=sessions,
        bus=bus or NullBus(),
        runner=JoiningRunner(),
        **kwargs,
    )


# ---- claiming -----------------------------------------------------------


async def test_a_queued_recording_is_claimed_exactly_once(sessions, fixture_ids):
    first, second = build_agent(sessions), build_agent(sessions)

    claimed = await first.claim(10)
    also = await second.claim(10)

    assert claimed == [fixture_ids["recording"]]
    assert also == []


async def test_claiming_marks_the_recording_started(sessions, fixture_ids):
    await build_agent(sessions).claim(10)
    async with sessions() as db:
        recording = await db.get(Recording, fixture_ids["recording"])
        assert recording.state == RecordingState.RECORDING
        assert recording.started_at is not None


async def test_a_full_agent_claims_nothing(sessions, fixture_ids):
    assert await build_agent(sessions).claim(0) == []
    async with sessions() as db:
        recording = await db.get(Recording, fixture_ids["recording"])
        assert recording.state == RecordingState.QUEUED


# ---- writing a live recording down --------------------------------------


async def test_the_sink_writes_segments_and_publishes_progress(sessions, fixture_ids):
    bus = NullBus()
    sink = DbSink(
        recording_id=fixture_ids["recording"],
        team_id=fixture_ids["team"],
        sessions=sessions,
        bus=bus,
    )

    await sink.state(RecordingState.RECORDING, "capture started")
    await sink.segment(
        SegmentRecord(
            source_kind=SourceKind.RTSP,
            sequence=0,
            path="/w/seg-00000.ts",
            started_at=NOW,
            duration_seconds=10.0,
            bytes=2048,
        )
    )

    async with sessions() as db:
        recording = await db.get(Recording, fixture_ids["recording"])
        segments = (await db.execute(select(Segment))).scalars().all()

    assert len(segments) == 1
    assert recording.captured_seconds == 10.0
    assert recording.total_bytes == 2048

    payload = bus.events[-1].payload
    assert bus.events[-1].type == "recording"
    assert payload["state"] == "recording"
    # The recordings page merges this straight into its row.
    assert set(payload) >= {"id", "camera_id", "state", "captured_seconds", "gap_seconds"}


async def test_a_gap_is_written_when_it_opens_and_closed_when_it_ends(sessions, fixture_ids):
    sink = DbSink(
        recording_id=fixture_ids["recording"],
        team_id=fixture_ids["team"],
        sessions=sessions,
        bus=NullBus(),
    )
    gap = GapRecord(
        source_kind=SourceKind.RTSP, started_at=NOW, cause="vpn", detail="the tunnel dropped"
    )

    await sink.gap_opened(gap)
    async with sessions() as db:
        open_gap = (await db.execute(select(Gap))).scalar_one()
        assert open_gap.ended_at is None
        assert open_gap.cause == "vpn"

    gap.close(NOW + timedelta(seconds=12))
    await sink.gap_closed(gap)
    async with sessions() as db:
        closed = (await db.execute(select(Gap))).scalar_one()
        recording = await db.get(Recording, fixture_ids["recording"])
        assert closed.seconds == 12.0
        assert recording.gap_seconds == 12.0


# ---- finishing ----------------------------------------------------------


class FakeStore:
    def __init__(self) -> None:
        self.uploads: list[str] = []
        self.deleted: list[str] = []

    async def put_file(self, path, key):
        from app.storage.client import StoredObject
        from app.storage.keys import content_type_for

        self.uploads.append(key)
        return StoredObject(key=key, bytes=1024, content_type=content_type_for(Path(path).name))

    async def delete(self, keys):
        self.deleted.extend(keys)
        return len(keys)


async def _session_report(tmp_path, recording_id: str):
    clock = Clock(NOW)
    runner = ScriptedFfmpeg([Run(sealed=[("seg-00000.ts", 0, 10)], elapsed=60)], clock)
    session = RecordingSession(
        recording_id=recording_id,
        paths=[FakePath(runner)],
        work_dir=tmp_path,
        requested_seconds=60,
        segment_seconds=10,
        now=clock.now,
        sleep=clock.sleep,
    )
    return await session.run(), session.work_dir


async def test_finishing_records_every_object_and_frees_the_work_volume(
    sessions, fixture_ids, tmp_path
):
    store = FakeStore()
    agent = build_agent(sessions, store=store)
    report, work_dir = await _session_report(tmp_path, fixture_ids["recording"])
    plan = _Plan(team_id=fixture_ids["team"], team_slug="acme", requested_seconds=60, paths=[])
    sink = DbSink(
        recording_id=fixture_ids["recording"],
        team_id=fixture_ids["team"],
        sessions=sessions,
        bus=NullBus(),
    )

    await agent.finalize(fixture_ids["recording"], plan, report, sink, work_dir)

    async with sessions() as db:
        recording = await db.get(Recording, fixture_ids["recording"])
        objects = (await db.execute(select(StorageObject))).scalars().all()

    assert recording.state == RecordingState.COMPLETE
    assert recording.captured_seconds == 10.0
    assert recording.total_bytes == 2048  # session.mp4 + gaps.json, 1024 each
    # The sidecar is a row like any other, so retention will sweep it too.
    assert {obj.source_kind for obj in objects} == {SourceKind.RTSP, None}
    assert not work_dir.exists()


async def test_a_failed_upload_keeps_the_segments(sessions, fixture_ids, tmp_path):
    class BrokenStore(FakeStore):
        async def put_file(self, path, key):
            from app.storage.client import StorageError

            raise StorageError("the gateway is not answering")

    agent = build_agent(sessions, store=BrokenStore())
    report, work_dir = await _session_report(tmp_path, fixture_ids["recording"])
    plan = _Plan(team_id=fixture_ids["team"], team_slug="acme", requested_seconds=60, paths=[])
    sink = DbSink(
        recording_id=fixture_ids["recording"],
        team_id=fixture_ids["team"],
        sessions=sessions,
        bus=NullBus(),
    )

    await agent.finalize(fixture_ids["recording"], plan, report, sink, work_dir)

    async with sessions() as db:
        recording = await db.get(Recording, fixture_ids["recording"])
    assert recording.state == RecordingState.FAILED
    assert work_dir.exists()


async def test_a_session_that_captured_nothing_fails_with_the_reason(
    sessions, fixture_ids, tmp_path
):
    agent = build_agent(sessions, store=FakeStore())
    clock = Clock(NOW)
    session = RecordingSession(
        recording_id=fixture_ids["recording"],
        paths=[FakePath(ScriptedFfmpeg([], clock), open_failures=99)],
        work_dir=tmp_path,
        requested_seconds=60,
        segment_seconds=10,
        now=clock.now,
        sleep=clock.sleep,
    )
    report = await session.run()
    plan = _Plan(team_id=fixture_ids["team"], team_slug="acme", requested_seconds=60, paths=[])
    sink = DbSink(
        recording_id=fixture_ids["recording"],
        team_id=fixture_ids["team"],
        sessions=sessions,
        bus=NullBus(),
    )

    await agent.finalize(fixture_ids["recording"], plan, report, sink, session.work_dir)

    async with sessions() as db:
        recording = await db.get(Recording, fixture_ids["recording"])
    assert recording.state == RecordingState.FAILED
    assert "VPN rejected" in recording.failure_reason


# ---- retention ----------------------------------------------------------


async def _fill(sessions, team_id: str, recording_id: str, *, count: int, size: int) -> None:
    async with sessions() as db:
        for i in range(count):
            db.add(
                StorageObject(
                    team_id=team_id,
                    recording_id=recording_id,
                    source_kind=SourceKind.RTSP,
                    s3_key=f"teams/acme/2026/08/{i:02d}/rec/rtsp/session.mp4",
                    bytes=size,
                    created_at=NOW - timedelta(days=count - i),
                )
            )
        await db.commit()


async def test_the_sweep_deletes_oldest_first_and_only_past_the_threshold(
    sessions, fixture_ids, monkeypatch
):
    store = FakeStore()
    agent = build_agent(sessions, store=store)
    await _fill(sessions, fixture_ids["team"], fixture_ids["recording"], count=10, size=10 * GB)

    monkeypatch.setattr(
        StoragePolicy,
        "from_settings",
        classmethod(lambda cls: StoragePolicy(60 * GB, 90 * GB, 98 * GB)),
    )
    await agent.sweep()

    async with sessions() as db:
        rows = (await db.execute(select(StorageObject))).scalars().all()
    live = [r for r in rows if r.deleted_at is None]

    # 100 GB used, swept down to the 60 GB warn line rather than just under 90.
    assert len(store.deleted) == 4
    assert sum(r.bytes for r in live) == 60 * GB
    assert store.deleted[0].endswith("2026/08/00/rec/rtsp/session.mp4")


async def test_the_sweep_only_warns_below_the_collection_threshold(
    sessions, fixture_ids, monkeypatch
):
    store = FakeStore()
    agent = build_agent(sessions, store=store)
    await _fill(sessions, fixture_ids["team"], fixture_ids["recording"], count=7, size=10 * GB)

    monkeypatch.setattr(
        StoragePolicy,
        "from_settings",
        classmethod(lambda cls: StoragePolicy(60 * GB, 90 * GB, 98 * GB)),
    )
    await agent.sweep()

    async with sessions() as db:
        rows = (await db.execute(select(StorageObject))).scalars().all()

    assert store.deleted == []
    assert sum(1 for r in rows if r.eligible_for_deletion_at is not None) == 1


# ---- reaching a source --------------------------------------------------


def _connections(bus):
    from app.net.netns import NetnsManager
    from app.net.ssh import TunnelManager
    from app.security.secrets import MemoryBackend
    from app.services.connections import ConnectionService

    return ConnectionService(
        netns=NetnsManager(prefix="test"),
        tunnels=TunnelManager(control_dir="/tmp/cam-test-ctl"),
        secrets=MemoryBackend(),
        bus=bus,
    )


async def _source_of(sessions, kind: SourceKind) -> CameraSource:
    async with sessions() as db:
        rows = await db.execute(select(CameraSource).where(CameraSource.kind == kind))
        return rows.scalar_one()


async def test_a_direct_profile_needs_no_dial(sessions, fixture_ids):
    from app.agent.paths import ProfileSourcePath

    source = await _source_of(sessions, SourceKind.RTSP)
    path = ProfileSourcePath(
        source_id=source.id,
        kind=SourceKind.RTSP,
        profile_id=fixture_ids["profile"],
        connections=_connections(NullBus()),
        sessions=sessions,
    )

    opened = await path.open()

    # Direct mode gets a plain runner on purpose: demanding CAP_NET_ADMIN for a
    # profile with no tunnel would stop the easy case working at all.
    assert opened.runner.name == "local"
    assert str(opened.url) == "rtsp://10.0.0.4:554/s1"


async def test_a_source_off_the_profile_path_is_opened_without_the_tunnel(sessions, fixture_ids):
    """The HLS feed is usually reachable directly. Dialling for it would let an
    outage on the RTSP side take the comparison feed down too."""
    from app.agent.paths import ProfileSourcePath

    source = await _source_of(sessions, SourceKind.HLS)
    path = ProfileSourcePath(
        source_id=source.id,
        kind=SourceKind.HLS,
        profile_id=fixture_ids["profile"],
        connections=_connections(NullBus()),
        sessions=sessions,
    )

    opened = await path.open()

    assert opened.detail == "direct, no tunnel"
    async with sessions() as db:
        profile = await db.get(ConnectionProfile, fixture_ids["profile"])
        assert profile.state == "idle"  # nothing was dialled


async def test_diagnosis_blames_the_camera_when_nothing_is_listening(sessions, fixture_ids):
    from app.agent.paths import ProfileSourcePath

    source = await _source_of(sessions, SourceKind.HLS)
    async with sessions() as db:
        row = await db.get(CameraSource, source.id)
        row.host, row.port = "127.0.0.1", 1  # refused immediately, no waiting
        await db.commit()

    path = ProfileSourcePath(
        source_id=source.id,
        kind=SourceKind.HLS,
        profile_id=fixture_ids["profile"],
        connections=_connections(NullBus()),
        sessions=sessions,
    )

    cause, detail = await path.diagnose()
    assert cause == "camera"
    assert "127.0.0.1:1" in detail
