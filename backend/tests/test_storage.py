"""Retention carries more weight on Versity than it would on S3 or MinIO.

There is no lifecycle rule to expire objects and no bucket quota to catch a
runaway job, so these thresholds are the only ceiling that exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import EndpointConnectionError

from app.config import settings
from app.enums import SourceKind
from app.recorder import accel as accel_module
from app.storage.client import BucketMissing, ObjectStore, StorageError
from app.storage.keys import content_type_for, download_name, source_key, team_prefix
from app.storage.retention import (
    Candidate,
    StoragePolicy,
    admit,
    estimate_bytes,
    plan_sweep,
    usage_state,
)

GB = 1024**3
POLICY = StoragePolicy(warn_bytes=60 * GB, gc_bytes=90 * GB, hard_bytes=98 * GB)
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _objects(count: int, size_gb: float, *, flagged: bool = False) -> list[Candidate]:
    return [
        Candidate(
            id=f"o{i}",
            key=f"teams/mofa/2026/08/{i:02d}/rec-{i}/raw/session.mp4",
            bytes=int(size_gb * GB),
            created_at=NOW - timedelta(days=count - i),
            recording_id=f"rec-{i}",
            eligible_for_deletion_at=NOW + timedelta(days=7) if flagged else None,
        )
        for i in range(count)
    ]


# ---- keys --------------------------------------------------------------


def test_keys_are_team_scoped_then_dated():
    key = source_key("mofa", "rec-1", NOW, SourceKind.RTSP, "session.mp4")
    assert key == "teams/mofa/2026/08/28/rec-1/rtsp/session.mp4"
    assert key.startswith(team_prefix("mofa"))


def test_a_downloaded_file_says_which_camera_and_when():
    """In the bucket every session file is session.mp4, which is right there and
    useless in a downloads folder: the second one is "session (1).mp4" and
    nothing on it says which camera or which day."""
    name = download_name(
        "EPM-CAM", NOW, SourceKind.RTSP, "teams/mofa/2026/08/28/rec-1/rtsp/session.mp4"
    )
    assert name == "EPM-CAM-2026-08-28-120000Z-rtsp.mp4"


def test_the_two_feeds_of_one_recording_do_not_collide():
    rtsp = download_name("EPM-CAM", NOW, SourceKind.RTSP, "x/rtsp/session.mp4")
    hls = download_name("EPM-CAM", NOW, SourceKind.HLS, "x/hls/session.mp4")
    assert rtsp != hls


def test_the_sidecar_keeps_its_own_name_and_extension():
    """gaps.json belongs to no source, so the source slot carries what it is."""
    name = download_name("EPM-CAM", NOW, None, "teams/mofa/2026/08/28/rec-1/gaps.json")
    assert name == "EPM-CAM-2026-08-28-120000Z-gaps.json"


def test_a_camera_name_is_made_safe_without_being_mangled():
    """Spaces and punctuation collapse to single dashes rather than vanishing --
    "Gate1North" is not a name anyone will recognise."""
    assert download_name("Gate 1 - North", NOW, SourceKind.RTSP, "x/session.mp4").startswith(
        "Gate-1-North-"
    )
    assert download_name("../../etc/passwd", NOW, SourceKind.RTSP, "x/session.mp4") == (
        "etc-passwd-2026-08-28-120000Z-rtsp.mp4"
    )
    assert download_name("   ", NOW, SourceKind.RTSP, "x/session.mp4").startswith("camera-")


def test_hls_playlists_get_the_right_content_type():
    assert content_type_for("index.m3u8") == "application/vnd.apple.mpegurl"
    assert content_type_for("session.mp4") == "video/mp4"
    assert content_type_for("gaps.json") == "application/json"


# ---- admission ---------------------------------------------------------


def test_a_recording_that_fits_is_admitted():
    decision = admit(10 * GB, estimate_bytes(300, source_count=2), POLICY)
    assert decision.allowed
    assert decision.headroom_bytes == 88 * GB


def test_a_recording_that_would_overshoot_is_refused_before_it_starts():
    """Refusing up front costs a message. Discovering it mid-write costs a
    truncated file and overshoots anyway.

    The case that matters: one user recording all five cameras, both sources,
    for the full fifteen minutes -- about 5 GB against 1 GB of headroom.
    """
    five_cameras_both_sources = estimate_bytes(900, source_count=10)
    decision = admit(97 * GB, five_cameras_both_sources, POLICY)
    assert not decision.allowed
    assert "only 1.0 GB is left" in decision.reason
    assert "Shorten it" in decision.reason


def test_a_recording_that_just_fits_is_still_allowed():
    """The check refuses what will not fit, not everything near the line."""
    decision = admit(97 * GB, estimate_bytes(900, source_count=2), POLICY)
    assert decision.allowed, "0.94 GB into 1 GB of headroom should pass"


def test_a_full_store_says_so_plainly():
    decision = admit(98 * GB, 1, POLICY)
    assert not decision.allowed
    assert "Storage is full" in decision.reason
    assert decision.headroom_bytes == 0


def test_the_estimate_is_generous_rather_than_accurate():
    """An underestimate is what lets a job overshoot the cap."""
    exact = 300 * 4_000_000 / 8
    assert estimate_bytes(300) > exact


def test_the_estimate_scales_with_both_sources():
    assert estimate_bytes(300, source_count=2) == 2 * estimate_bytes(300, source_count=1)


def test_a_probed_bitrate_beats_the_default():
    assert estimate_bytes(300, bitrate_bps=8_000_000) > estimate_bytes(300)


# ---- sweep -------------------------------------------------------------


def test_below_the_warning_line_nothing_happens():
    plan = plan_sweep(_objects(10, 1), 30 * GB, POLICY, now=NOW)
    assert plan.delete == []
    assert plan.mark_eligible == []


def test_at_the_warning_line_people_are_told_before_anything_is_deleted():
    """camera.MD: give a window of 'this might get deleted' at 60% usage."""
    plan = plan_sweep(_objects(70, 1), 65 * GB, POLICY, now=NOW)
    assert plan.delete == [], "nothing is removed at the warning threshold"
    assert plan.mark_eligible, "the oldest should be flagged"
    assert "7 days" in plan.reason
    # Exactly enough to cover the overshoot, oldest first.
    assert plan.mark_eligible[0].recording_id == "rec-0"
    assert len(plan.mark_eligible) == 5


def test_already_flagged_objects_are_not_flagged_again():
    plan = plan_sweep(_objects(70, 1, flagged=True), 65 * GB, POLICY, now=NOW)
    assert plan.mark_eligible == []


def test_the_sweep_deletes_oldest_first():
    plan = plan_sweep(_objects(95, 1), 92 * GB, POLICY, now=NOW)
    assert plan.delete
    assert [c.recording_id for c in plan.delete[:3]] == ["rec-0", "rec-1", "rec-2"]


def test_the_sweep_runs_down_to_the_warning_line_not_just_under_the_trigger():
    """Stopping at the threshold it just crossed means sweeping again on the
    next recording, and the one after that."""
    plan = plan_sweep(_objects(95, 1), 92 * GB, POLICY, now=NOW)
    remaining = 92 * GB - plan.freed_bytes
    assert remaining <= POLICY.warn_bytes
    assert remaining > POLICY.warn_bytes - 2 * GB, "should not over-delete either"


def test_a_full_store_still_only_sweeps_down_to_the_warning_line():
    plan = plan_sweep(_objects(100, 1), 99 * GB, POLICY, now=NOW)
    assert 99 * GB - plan.freed_bytes <= POLICY.warn_bytes


def test_usage_state_names_the_four_bands():
    assert usage_state(10 * GB, POLICY) == "ok"
    assert usage_state(65 * GB, POLICY) == "warning"
    assert usage_state(92 * GB, POLICY) == "collecting"
    assert usage_state(99 * GB, POLICY) == "full"


def test_thresholds_must_be_ordered():
    with pytest.raises(ValueError, match="warn <= gc <= hard"):
        StoragePolicy(warn_bytes=90 * GB, gc_bytes=60 * GB, hard_bytes=98 * GB)


# ---- how much this machine will encode ----------------------------------


@pytest.fixture
def fresh_settings():
    """Settings are cached for the life of the process, so a test that changes
    the environment has to drop the cache on the way in *and* on the way out --
    otherwise the next test to read settings gets this one's."""
    settings.cache_clear()
    yield
    settings.cache_clear()


def test_the_cpu_budget_is_a_share_of_the_cores_actually_present(monkeypatch, fresh_settings):
    """Eighty percent of a machine, divided by what one stream costs. Naming a
    fixed number of streams instead would be wrong on both a laptop and a
    32-core server, and wrong in the direction that matters on the laptop."""
    monkeypatch.setenv("TRANSCODE_ACCEL", "cpu")
    monkeypatch.setenv("CPU_BUDGET_PERCENT", "80")
    monkeypatch.setenv("CPU_COST_PER_STREAM", "0.6")
    monkeypatch.setattr(accel_module.os, "cpu_count", lambda: 32)

    capacity = accel_module.detect()
    assert capacity.accel is accel_module.Accel.CPU
    # 32 * 0.8 = 25.6 cores of budget, at 0.6 cores each.
    assert capacity.limit == 42
    assert "32 cores" in capacity.detail


def test_a_small_machine_still_gets_one_stream(monkeypatch, fresh_settings):
    """Zero would be arithmetically right and useless: a machine that can carry
    one stream should carry one rather than refuse everything."""
    monkeypatch.setenv("TRANSCODE_ACCEL", "cpu")
    monkeypatch.setenv("CPU_BUDGET_PERCENT", "10")
    monkeypatch.setattr(accel_module.os, "cpu_count", lambda: 1)

    assert accel_module.detect().limit == 1


def test_the_gpu_budget_is_a_share_of_its_sessions(monkeypatch, fresh_settings):
    monkeypatch.setenv("TRANSCODE_ACCEL", "nvidia")
    monkeypatch.setenv("GPU_BUDGET_PERCENT", "80")
    monkeypatch.setenv("GPU_SESSIONS", "16")

    capacity = accel_module.detect()
    assert capacity.accel is accel_module.Accel.NVIDIA
    assert capacity.limit == 12


def test_auto_falls_back_to_the_cpu_when_no_card_answers(monkeypatch, fresh_settings):
    """`nvidia-smi` on the host is not the question -- this runs in a container,
    and the card is only there if the runtime was asked to pass it through."""
    monkeypatch.setenv("TRANSCODE_ACCEL", "auto")
    monkeypatch.setattr(accel_module, "_has_nvidia", lambda: False)

    capacity = accel_module.detect()
    assert capacity.accel is accel_module.Accel.CPU
    assert "no NVIDIA card visible" in capacity.detail


async def test_the_budget_refuses_rather_than_overcommitting():
    budget = accel_module.TranscodeBudget(
        accel_module.Capacity(accel=accel_module.Accel.CPU, limit=2, detail="test")
    )
    await budget.reserve()
    await budget.reserve()
    with pytest.raises(accel_module.AtCapacity) as caught:
        await budget.reserve()

    assert "already transcoding 2 cameras" in caught.value.user_message
    await budget.release()
    await budget.reserve()  # the freed slot is usable again
    assert budget.in_use == 2


# ---- what an upload failure looks like ----------------------------------


class _RefusingClient:
    """Stands in for the boto3 client. ``upload_file`` is the only call the
    shipper makes, and it is the one that does not raise what the rest of
    boto3 raises."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def upload_file(self, *args, **kwargs):
        raise self.error


def _store(error: Exception) -> ObjectStore:
    store = ObjectStore(
        endpoint_url="https://storage.invalid",
        bucket="cam-recordings",
        access_key="k",
        secret_key="s",
        region="us-east-1",
        addressing_style="path",
    )
    store._client = _RefusingClient(error)  # type: ignore[assignment]
    return store


async def test_a_gateway_refusal_during_upload_is_a_storage_error(tmp_path):
    """S3UploadFailedError is a Boto3Error and neither a ClientError nor a
    BotoCoreError. Letting it escape put_file strands the recording in
    FINALIZING with nothing written down anywhere."""
    path = tmp_path / "session.mp4"
    path.write_bytes(b"x")
    store = _store(
        S3UploadFailedError(
            "Failed to upload session.mp4 to cam-recordings/key: An error occurred "
            "(AccessDenied) when calling the CreateMultipartUpload operation"
        )
    )

    with pytest.raises(StorageError) as caught:
        await store.put_file(path, "teams/mofa/session.mp4")

    assert "AccessDenied" in str(caught.value)


async def test_a_missing_bucket_says_so_rather_than_quoting_boto3(tmp_path):
    path = tmp_path / "session.mp4"
    path.write_bytes(b"x")
    store = _store(
        S3UploadFailedError(
            "Failed to upload session.mp4 to cam-recordings/key: An error occurred "
            "(NoSuchBucket) when calling the CreateMultipartUpload operation: "
            "The specified bucket does not exist"
        )
    )

    with pytest.raises(BucketMissing) as caught:
        await store.put_file(path, "teams/mofa/session.mp4")

    assert caught.value.user_message == BucketMissing.user_message


async def test_an_unreachable_gateway_is_still_a_storage_error(tmp_path):
    path = tmp_path / "session.mp4"
    path.write_bytes(b"x")
    store = _store(EndpointConnectionError(endpoint_url="https://storage.invalid"))

    with pytest.raises(StorageError):
        await store.put_file(path, "teams/mofa/session.mp4")
