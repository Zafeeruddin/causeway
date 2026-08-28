"""The comparison timeline.

One claim carries this whole feature: after an outage, the same moment sits at
different positions in the two files, because each file is a concatenation of
what that feed captured. Seeking both players to the same number would compare
the wrong frames and look exactly like the inference being wrong -- which is the
thing QA is using this view to judge.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.enums import ReachMode, RecordingState, SourceKind
from app.models import (
    Camera,
    CameraSource,
    ConnectionProfile,
    Gap,
    Recording,
    Segment,
    StorageObject,
    User,
)
from app.services.playback import (
    build_spans,
    gap_marks,
    media_time_at,
    origin_of,
    wall_time_at,
    window_seconds,
)
from app.storage.client import set_object_store

T0 = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)


def _segments(kind: SourceKind, plan: list[tuple[float, float]]) -> list[Segment]:
    """``plan`` is (seconds after T0, duration) per segment."""
    return [
        Segment(
            recording_id="rec-1",
            source_kind=kind,
            sequence=i,
            path=f"/w/{kind.value}/seg-{i:05d}.ts",
            started_at=T0 + timedelta(seconds=at),
            duration_seconds=duration,
            bytes=1024,
        )
        for i, (at, duration) in enumerate(plan)
    ]


#: RTSP dropped from 30s to 45s; HLS ran straight through.
RTSP_PLAN = [(0, 10.0), (10, 10.0), (20, 10.0), (45, 10.0), (55, 5.0)]
HLS_PLAN = [(0, 10.0), (10, 10.0), (20, 10.0), (30, 10.0), (40, 10.0), (50, 10.0)]


# ---- spans --------------------------------------------------------------


def test_an_uninterrupted_capture_is_one_span():
    spans = build_spans(_segments(SourceKind.HLS, HLS_PLAN), T0)
    assert len(spans) == 1
    assert spans[0].media_start == 0.0
    assert spans[0].seconds == 60.0


def test_an_outage_starts_a_new_span_without_a_hole_in_the_file():
    spans = build_spans(_segments(SourceKind.RTSP, RTSP_PLAN), T0)

    assert len(spans) == 2
    # The file has no gap in it: the second span continues where the first ended.
    assert spans[0].media_start == 0.0 and spans[0].seconds == 30.0
    assert spans[1].media_start == 30.0
    # The world does: it resumes fifteen seconds later.
    assert spans[1].wall_start == 45.0


def test_the_same_moment_is_in_a_different_place_in_each_file():
    """The whole reason the transport runs on wall clock."""
    rtsp = build_spans(_segments(SourceKind.RTSP, RTSP_PLAN), T0)
    hls = build_spans(_segments(SourceKind.HLS, HLS_PLAN), T0)

    assert media_time_at(hls, 50.0) == 50.0
    assert media_time_at(rtsp, 50.0) == 35.0  # fifteen seconds it never recorded


def test_a_moment_no_one_captured_is_nothing_rather_than_zero():
    rtsp = build_spans(_segments(SourceKind.RTSP, RTSP_PLAN), T0)
    assert media_time_at(rtsp, 35.0) is None
    assert media_time_at(rtsp, 30.0) is None
    assert media_time_at(rtsp, 29.999) is not None


def test_wall_and_media_time_are_inverses():
    spans = build_spans(_segments(SourceKind.RTSP, RTSP_PLAN), T0)
    for at in (0.0, 12.5, 29.9, 45.0, 59.0):
        media = media_time_at(spans, at)
        assert media is not None
        assert wall_time_at(spans, media) == pytest.approx(at, abs=0.001)


def test_the_window_covers_both_feeds_not_the_shorter_one():
    rtsp = build_spans(_segments(SourceKind.RTSP, [(0, 10.0)]), T0)
    hls = build_spans(_segments(SourceKind.HLS, HLS_PLAN), T0)
    assert window_seconds([rtsp, hls]) == 60.0


def test_a_zero_length_segment_is_not_a_span():
    assert build_spans(_segments(SourceKind.RTSP, [(0, 0.0)]), T0) == []


def test_the_origin_is_the_first_frame_anyone_captured():
    late = _segments(SourceKind.HLS, [(4, 10.0)])
    early = _segments(SourceKind.RTSP, [(0, 10.0)])
    assert origin_of(late + early) == T0


# ---- gaps on the timeline -----------------------------------------------


def test_a_gap_that_never_closed_runs_to_the_end_of_the_window():
    gap = Gap(
        recording_id="rec-1",
        source_kind=SourceKind.RTSP,
        started_at=T0 + timedelta(seconds=30),
        ended_at=None,
        seconds=0.0,
        cause="vpn",
        detail="the tunnel dropped",
    )
    marks = gap_marks([gap], T0, window=60.0)
    assert marks[0].wall_start == 30.0
    assert marks[0].seconds == 30.0
    assert marks[0].cause == "vpn"


# ---- the endpoint -------------------------------------------------------


class FakeStore:
    async def presign(self, key, *, expires=3600, filename=None):
        return f"https://s3.example.com/cam-recordings/{key}?signed=yes"


@pytest.fixture
def store():
    set_object_store(FakeStore())
    yield
    set_object_store(None)


@pytest.fixture
async def recorded(sessions, seeded):
    """A finished recording of a camera with both feeds, one of which dropped."""
    async with sessions() as db:
        user = await db.get(User, seeded["member"])
        profile = ConnectionProfile(team_id=seeded["acme"], name="Direct", mode=ReachMode.DIRECT)
        db.add(profile)
        await db.flush()
        camera = Camera(team_id=seeded["acme"], profile_id=profile.id, name="Gate 1")
        db.add(camera)
        await db.flush()
        db.add_all(
            [
                CameraSource(camera_id=camera.id, kind=SourceKind.RTSP, url="rtsp://cam/s1"),
                CameraSource(camera_id=camera.id, kind=SourceKind.HLS, url="https://h/s.m3u8"),
            ]
        )
        recording = Recording(
            team_id=seeded["acme"],
            camera_id=camera.id,
            requested_by=user.id,
            requested_seconds=60,
            state=RecordingState.COMPLETE,
            started_at=T0,
            captured_seconds=45.0,
            gap_seconds=15.0,
        )
        db.add(recording)
        await db.flush()

        for segment in _segments(SourceKind.RTSP, RTSP_PLAN) + _segments(SourceKind.HLS, HLS_PLAN):
            segment.recording_id = recording.id
            db.add(segment)
        db.add(
            Gap(
                recording_id=recording.id,
                source_kind=SourceKind.RTSP,
                started_at=T0 + timedelta(seconds=30),
                ended_at=T0 + timedelta(seconds=45),
                seconds=15.0,
                cause="vpn",
                detail="the VPN tunnel dropped",
                redial_attempts=2,
            )
        )
        for kind in (SourceKind.RTSP, SourceKind.HLS):
            db.add(
                StorageObject(
                    team_id=seeded["acme"],
                    recording_id=recording.id,
                    source_kind=kind,
                    s3_key=f"teams/acme/2026/08/28/{recording.id}/{kind.value}/session.mp4",
                    bytes=1024,
                )
            )
        db.add(
            StorageObject(
                team_id=seeded["acme"],
                recording_id=recording.id,
                source_kind=None,
                s3_key=f"teams/acme/2026/08/28/{recording.id}/gaps.json",
                bytes=512,
            )
        )
        await db.commit()
        return recording.id


async def test_the_comparison_carries_both_feeds_and_their_own_timelines(
    as_member, recorded, store
):
    response = await as_member.get(f"/api/recordings/{recorded}/comparison")

    assert response.status_code == 200
    body = response.json()
    assert body["window_seconds"] == 60.0
    assert [t["source_kind"] for t in body["tracks"]] == ["rtsp", "hls"]

    rtsp, hls = body["tracks"]
    assert len(hls["spans"]) == 1
    assert len(rtsp["spans"]) == 2
    assert rtsp["spans"][1] == {"media_start": 30.0, "wall_start": 45.0, "seconds": 15.0}
    assert rtsp["gaps"][0]["cause"] == "vpn"
    assert hls["gaps"] == []
    assert "signed=yes" in rtsp["url"]


async def test_the_comparison_says_how_well_it_is_aligned(as_member, recorded, store):
    body = (await as_member.get(f"/api/recordings/{recorded}/comparison")).json()

    assert body["alignment"]["method"] == "wall_clock"
    assert body["alignment"]["accuracy_seconds"] == 2.0
    assert "inference pipeline" in body["alignment"]["note"]


async def test_the_sidecar_is_not_offered_as_a_video_track(as_member, recorded, store):
    body = (await as_member.get(f"/api/recordings/{recorded}/comparison")).json()
    assert all(track["source_kind"] in ("rtsp", "hls") for track in body["tracks"])


async def test_you_cannot_compare_another_teams_recording(as_other, recorded, store):
    assert (await as_other.get(f"/api/recordings/{recorded}/comparison")).status_code == 404


async def test_a_recording_with_nothing_stored_says_so(as_member, sessions, seeded, store):
    async with sessions() as db:
        user = await db.get(User, seeded["member"])
        profile = ConnectionProfile(team_id=seeded["acme"], name="D", mode=ReachMode.DIRECT)
        db.add(profile)
        await db.flush()
        camera = Camera(team_id=seeded["acme"], profile_id=profile.id, name="Gate 2")
        db.add(camera)
        await db.flush()
        recording = Recording(
            team_id=seeded["acme"],
            camera_id=camera.id,
            requested_by=user.id,
            requested_seconds=60,
            state=RecordingState.RECORDING,
        )
        db.add(recording)
        await db.commit()
        recording_id = recording.id

    response = await as_member.get(f"/api/recordings/{recording_id}/comparison")
    assert response.status_code == 409
    assert "nothing to play yet" in response.json()["detail"]
