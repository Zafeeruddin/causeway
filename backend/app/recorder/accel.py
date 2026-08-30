"""How much transcoding this machine will do, and on what.

Most streams cost nothing here. A preview of an H.264 camera is a stream copy
and a recording is a stream copy; both are bookkeeping and a socket. The one
expensive thing this product does is re-encode a camera whose codec no browser
can decode -- H.265, which is what most of them ship -- and that cost lands
either on an NVIDIA card or on the CPU, differing by more than an order of
magnitude.

So there are two questions, and they are separate:

* **What do we encode with?** Detected once, overridable. A machine with a card
  uses it; a machine without one uses libx264 and is not asked to pretend.
* **How many at once?** A budget, expressed as a share of what the machine has,
  because the alternative is discovering the ceiling as stutter across every
  stream at the same time. Streams past the budget are refused *with a reason*,
  which is the whole point: "the machine is at capacity" is actionable and a
  juddering picture is not.

The budget is deliberately a count of concurrent transcodes rather than a live
utilisation reading. Utilisation tells you what happened; a count tells you
whether to admit the next one, which is the decision actually being made. Both
sides of it are measured, not guessed -- see ``deploy/production.md``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum

import structlog

from app.config import settings

log = structlog.get_logger(__name__)


class Accel(StrEnum):
    """What does the encoding."""

    #: NVIDIA: hevc_cuvid to decode, h264_nvenc to encode, frames never leaving
    #: the card in between.
    NVIDIA = "nvidia"
    #: libx264. Correct everywhere, and about half a core per 1080p stream.
    CPU = "cpu"


@dataclass(frozen=True, slots=True)
class Capacity:
    """What this machine will do, and how much of it."""

    accel: Accel
    #: Concurrent transcodes allowed. Never below one: a machine that can run a
    #: single stream should run a single stream rather than refuse everything.
    limit: int
    #: How the number was arrived at, for the health endpoint and the logs.
    detail: str


def _has_nvidia() -> bool:
    """Whether an NVIDIA card is actually usable from in here.

    `nvidia-smi` present on the host is not the question -- this runs inside a
    container, and the card is only visible if the runtime was asked to pass it
    through. Running it is the only way to know.
    """
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def detect() -> Capacity:
    """Decide once, at startup, and say so out loud."""
    cfg = settings()
    choice = cfg.transcode_accel.strip().lower()

    if choice == "cpu" or (choice == "auto" and not _has_nvidia()):
        cores = os.cpu_count() or 1
        budget = cores * (cfg.cpu_budget_percent / 100.0)
        # Measured: a 1080p15 H.265 -> H.264 transcode at ultrafast costs about
        # half a core. Dividing the budget by that is what turns "80% of this
        # machine" into a number of streams.
        limit = max(1, int(budget / cfg.cpu_cost_per_stream))
        detail = (
            f"libx264 on {cores} cores, {cfg.cpu_budget_percent:.0f}% of them "
            f"at ~{cfg.cpu_cost_per_stream} cores per stream"
        )
        if choice == "auto":
            detail += " (no NVIDIA card visible)"
        return Capacity(accel=Accel.CPU, limit=limit, detail=detail)

    if choice == "auto" or choice == "nvidia":
        limit = max(1, int(cfg.gpu_sessions * (cfg.gpu_budget_percent / 100.0)))
        return Capacity(
            accel=Accel.NVIDIA,
            limit=limit,
            detail=(
                f"NVENC/NVDEC, {cfg.gpu_budget_percent:.0f}% of "
                f"{cfg.gpu_sessions} concurrent sessions"
            ),
        )

    raise ValueError(f"TRANSCODE_ACCEL must be auto, nvidia or cpu -- got {cfg.transcode_accel!r}")


class AtCapacity(RuntimeError):
    """The machine is already doing as much encoding as it was allowed to."""

    def __init__(self, capacity: Capacity, in_use: int) -> None:
        self.user_message = (
            f"This server is already transcoding {in_use} camera"
            f"{'' if in_use == 1 else 's'}, which is all it is configured to do at "
            f"once ({capacity.detail}). Close a preview to start another."
        )
        super().__init__(self.user_message)


class TranscodeBudget:
    """Counts what is being encoded right now and refuses the rest.

    A counter rather than a sampler on purpose. Sampling GPU or CPU load answers
    "is it busy", which is the wrong question at the moment someone presses
    Preview -- the load a new stream will add has not happened yet, and by the
    time it shows up in a sample the stutter has already reached everyone
    watching. Counting what has been admitted answers the question being asked.
    """

    def __init__(self, capacity: Capacity | None = None) -> None:
        self.capacity = capacity or detect()
        self._in_use = 0
        self._lock = asyncio.Lock()

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def limit(self) -> int:
        return self.capacity.limit

    @property
    def accel(self) -> Accel:
        return self.capacity.accel

    async def reserve(self) -> None:
        """Claim one slot, or raise :class:`AtCapacity`.

        Held under a lock because two people pressing Preview at the same moment
        is the ordinary case, not the rare one, and a check-then-increment
        between them admits one more than the budget allows.
        """
        async with self._lock:
            if self._in_use >= self.capacity.limit:
                raise AtCapacity(self.capacity, self._in_use)
            self._in_use += 1

    async def release(self) -> None:
        async with self._lock:
            self._in_use = max(0, self._in_use - 1)

    def status(self) -> dict:
        return {
            "accel": str(self.capacity.accel),
            "transcodes_in_use": self._in_use,
            "transcode_limit": self.capacity.limit,
            "detail": self.capacity.detail,
        }
