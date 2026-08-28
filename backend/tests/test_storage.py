"""Retention carries more weight on Versity than it would on S3 or MinIO.

There is no lifecycle rule to expire objects and no bucket quota to catch a
runaway job, so these thresholds are the only ceiling that exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.enums import SourceKind
from app.storage.keys import content_type_for, source_key, team_prefix
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
            key=f"teams/acme/2026/08/{i:02d}/rec-{i}/raw/session.mp4",
            bytes=int(size_gb * GB),
            created_at=NOW - timedelta(days=count - i),
            recording_id=f"rec-{i}",
            eligible_for_deletion_at=NOW + timedelta(days=7) if flagged else None,
        )
        for i in range(count)
    ]


# ---- keys --------------------------------------------------------------


def test_keys_are_team_scoped_then_dated():
    key = source_key("acme", "rec-1", NOW, SourceKind.RTSP, "session.mp4")
    assert key == "teams/acme/2026/08/28/rec-1/rtsp/session.mp4"
    assert key.startswith(team_prefix("acme"))


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
