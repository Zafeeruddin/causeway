"""Turning a recording's segments back into a timeline you can scrub.

The compare view plays the raw camera feed and the inferred HLS feed of the same
camera at once, and the naive way to do that -- seek both players to the same
media time -- is wrong the moment either side has an outage. The session file is
a *concatenation* of what was captured, so a 12-second gap is not 12 seconds of
black in the file: it is not in the file at all. Everything after it sits 12
seconds earlier in media time than it did in the world, and the two feeds drift
apart by exactly the gaps they did not share.

So the shared transport runs on **wall-clock time**, and each track converts it
to its own media time through the spans below. A moment that one feed missed
resolves to nothing for that side, which is the honest answer and also the
useful one: "the RTSP side has no video here because the VPN dropped" is the
comparison QA is making.

Everything is returned relative to the session start, in seconds, so the browser
does arithmetic rather than date parsing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.models import Gap, Segment

#: Two segments this close together are the same continuous capture. Segment
#: boundaries inside one ffmpeg run are exact; this only has to survive rounding.
CONTIGUITY_TOLERANCE = 1.0


@dataclass(frozen=True, slots=True)
class Span:
    """A stretch of video with no discontinuity in it.

    ``media_start`` is where it sits in the joined file; ``wall_start`` is when
    it happened. One span per uninterrupted capture, so a recording that never
    dropped has exactly one.
    """

    media_start: float
    wall_start: float
    seconds: float

    @property
    def media_end(self) -> float:
        return self.media_start + self.seconds

    @property
    def wall_end(self) -> float:
        return self.wall_start + self.seconds

    def covers(self, at: float) -> bool:
        return self.wall_start <= at < self.wall_end


def build_spans(
    segments: Sequence[Segment],
    origin: datetime,
    *,
    tolerance: float = CONTIGUITY_TOLERANCE,
) -> list[Span]:
    """Coalesce sealed segments into continuous spans.

    Segments within one ffmpeg run are contiguous by construction; a new run
    starts after an outage, and that is where a span ends.
    """
    ordered = sorted(segments, key=lambda s: s.sequence)
    spans: list[Span] = []
    media = 0.0
    for segment in ordered:
        wall = (segment.started_at - origin).total_seconds()
        duration = float(segment.duration_seconds or 0.0)
        if duration <= 0:
            continue
        current = spans[-1] if spans else None
        if current is not None and abs(wall - current.wall_end) <= tolerance:
            spans[-1] = Span(
                media_start=current.media_start,
                wall_start=current.wall_start,
                seconds=round(current.seconds + duration, 3),
            )
        else:
            spans.append(
                Span(media_start=round(media, 3), wall_start=round(wall, 3), seconds=duration)
            )
        media += duration
    return spans


def media_time_at(spans: Sequence[Span], at: float) -> float | None:
    """Where in the file to seek for this moment, or None if it was not captured."""
    for span in spans:
        if span.covers(at):
            return round(span.media_start + (at - span.wall_start), 3)
    return None


def wall_time_at(spans: Sequence[Span], media: float) -> float | None:
    """The inverse: what moment a position in the file corresponds to."""
    for span in spans:
        if span.media_start <= media < span.media_end:
            return round(span.wall_start + (media - span.media_start), 3)
    return None


def origin_of(segments: Sequence[Segment]) -> datetime | None:
    """When the earliest capture in this set began."""
    starts = [s.started_at for s in segments if s.duration_seconds]
    return min(starts) if starts else None


def window_seconds(spans_by_track: Sequence[Sequence[Span]]) -> float:
    """How much wall clock the comparison covers -- the union of both tracks,
    so a feed that ran longer is not cut off at the shorter one's end."""
    return round(max((s[-1].wall_end for s in spans_by_track if s), default=0.0), 3)


@dataclass(frozen=True, slots=True)
class GapMark:
    """A gap placed on the shared timeline rather than in the file."""

    wall_start: float
    seconds: float
    cause: str
    detail: str


def gap_marks(gaps: Sequence[Gap], origin: datetime, *, window: float) -> list[GapMark]:
    marks: list[GapMark] = []
    for gap in gaps:
        start = round((gap.started_at - origin).total_seconds(), 3)
        seconds = float(gap.seconds or 0.0)
        if seconds <= 0 and gap.ended_at is None:
            # Still open when the recording ended: it ran to the end.
            seconds = round(max(window - start, 0.0), 3)
        marks.append(
            GapMark(
                wall_start=start,
                seconds=seconds,
                cause=gap.cause or "unknown",
                detail=gap.detail or "",
            )
        )
    return sorted(marks, key=lambda m: m.wall_start)
