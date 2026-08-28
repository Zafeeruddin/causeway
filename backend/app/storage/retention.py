"""Retention, and the admission check that has to hold in its place.

On MinIO or AWS this would lean on two server-side safety nets: a lifecycle rule
to expire old objects, and a bucket quota to stop a runaway job. Versity Gateway
has neither. So both jobs move in here, and the consequence is worth stating
plainly: **if this module stops running, nothing deletes anything, and nothing
stops a recording from filling the org's storage.**

That is why the ceiling is enforced *before* a recording starts rather than
during it. Discovering the cap mid-write leaves a truncated file and still
overshoots; refusing up front costs the user a clear message and nothing else.

Thresholds, from camera.MD:

    60 GB   warn, and start telling people which recordings are next to go
    90 GB   sweep oldest-first
    98 GB   refuse to start anything new
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import structlog

log = structlog.get_logger(__name__)

#: Assumed H.264 bitrate when a source has never been probed. 4 Mb/s is a
#: middling 1080p camera; the estimate only has to be right enough to refuse
#: a recording that clearly will not fit.
DEFAULT_BITRATE_BPS = 4_000_000

#: How long a recording is advertised as "may be deleted" before it goes.
DELETION_NOTICE_DAYS = 7


@dataclass(frozen=True, slots=True)
class StoragePolicy:
    warn_bytes: int
    gc_bytes: int
    hard_bytes: int

    def __post_init__(self) -> None:
        if not self.warn_bytes <= self.gc_bytes <= self.hard_bytes:
            raise ValueError("storage thresholds must be warn <= gc <= hard")

    @classmethod
    def from_settings(cls) -> StoragePolicy:
        from app.config import settings

        cfg = settings()
        return cls(cfg.storage_warn_bytes, cfg.storage_gc_bytes, cfg.storage_hard_bytes)


@dataclass(frozen=True, slots=True)
class Admission:
    allowed: bool
    reason: str = ""
    headroom_bytes: int = 0
    estimated_bytes: int = 0

    @property
    def headroom_gb(self) -> float:
        return round(self.headroom_bytes / 1024**3, 1)


@dataclass(frozen=True, slots=True)
class Candidate:
    """An object the sweep may remove. Ordered by age, oldest first."""

    id: str
    key: str
    bytes: int
    created_at: datetime
    recording_id: str
    eligible_for_deletion_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SweepPlan:
    delete: list[Candidate]
    freed_bytes: int
    #: Objects newly flagged as "may be deleted" so people get notice first.
    mark_eligible: list[Candidate]
    reason: str = ""


def estimate_bytes(seconds: int, *, source_count: int = 1, bitrate_bps: int | None = None) -> int:
    """Rough size of a recording. Deliberately generous rather than accurate --
    an underestimate is what lets a job overshoot the cap."""
    bitrate = bitrate_bps or DEFAULT_BITRATE_BPS
    # 12% for container overhead and the odd bitrate spike on scene changes.
    return int(seconds * source_count * bitrate / 8 * 1.12)


def admit(
    used_bytes: int,
    estimated_bytes: int,
    policy: StoragePolicy,
) -> Admission:
    """Decide whether a recording may start.

    The only thing between a bug in our accounting and the org's storage, now
    that there is no bucket-side quota to fall back on.
    """
    headroom = policy.hard_bytes - used_bytes

    if used_bytes >= policy.hard_bytes:
        return Admission(
            allowed=False,
            reason=(
                f"Storage is full ({_gb(used_bytes)} of {_gb(policy.hard_bytes)}). "
                "Delete some recordings, or wait for the next retention sweep."
            ),
            headroom_bytes=0,
            estimated_bytes=estimated_bytes,
        )

    if estimated_bytes > headroom:
        return Admission(
            allowed=False,
            reason=(
                f"This recording needs about {_gb(estimated_bytes)} and only "
                f"{_gb(headroom)} is left. Shorten it, record fewer cameras, "
                "or free some space first."
            ),
            headroom_bytes=headroom,
            estimated_bytes=estimated_bytes,
        )

    return Admission(
        allowed=True,
        reason="",
        headroom_bytes=headroom,
        estimated_bytes=estimated_bytes,
    )


#: What the dashboard renders, and the only four answers there are.
UsageState = Literal["ok", "warning", "collecting", "full"]


def usage_state(used_bytes: int, policy: StoragePolicy) -> UsageState:
    if used_bytes >= policy.hard_bytes:
        return "full"
    if used_bytes >= policy.gc_bytes:
        return "collecting"
    if used_bytes >= policy.warn_bytes:
        return "warning"
    return "ok"


def plan_sweep(
    candidates: Sequence[Candidate],
    used_bytes: int,
    policy: StoragePolicy,
    *,
    now: datetime | None = None,
    notice_days: int = DELETION_NOTICE_DAYS,
) -> SweepPlan:
    """Work out what to delete and what to warn about.

    Two behaviours, at two thresholds:

    * Past ``warn_bytes``, the oldest recordings are *flagged* with a deletion
      date so people can download anything they still want. Nothing is removed.
    * Past ``gc_bytes``, the oldest are actually deleted -- and the sweep runs
      down to ``warn_bytes``, not just back under ``gc_bytes``. Stopping at the
      threshold it just crossed would mean sweeping again on the next recording,
      and again on the one after that.
    """
    now = now or datetime.now(UTC)
    ordered = sorted(candidates, key=lambda c: c.created_at)
    state = usage_state(used_bytes, policy)

    if state == "ok":
        return SweepPlan(
            delete=[], freed_bytes=0, mark_eligible=[], reason="below the warning threshold"
        )

    if state == "warning":
        # Flag enough of the oldest to cover the gap back down to the warn line,
        # so the notice list matches what would actually go.
        to_flag: list[Candidate] = []
        running = used_bytes
        for candidate in ordered:
            if running <= policy.warn_bytes:
                break
            if candidate.eligible_for_deletion_at is None:
                to_flag.append(candidate)
            running -= candidate.bytes
        return SweepPlan(
            delete=[],
            freed_bytes=0,
            mark_eligible=to_flag,
            reason=(
                f"{_gb(used_bytes)} used - the oldest recordings are now marked for "
                f"deletion in {notice_days} days unless space frees up."
            ),
        )

    # Collecting or full: delete oldest-first down to the warn line.
    target = policy.warn_bytes
    to_delete: list[Candidate] = []
    freed = 0
    for candidate in ordered:
        if used_bytes - freed <= target:
            break
        to_delete.append(candidate)
        freed += candidate.bytes

    return SweepPlan(
        delete=to_delete,
        freed_bytes=freed,
        mark_eligible=[],
        reason=(
            f"{_gb(used_bytes)} used - removing {len(to_delete)} oldest "
            f"recordings to free {_gb(freed)}."
        ),
    )


def _gb(value: int) -> str:
    return f"{value / 1024**3:.1f} GB"
