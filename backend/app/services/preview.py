"""Live preview: watching a camera without recording it.

The requirement this answers is the first one in ``camera.MD`` -- *test the
camera connectivity, show it live* -- and it is the same boundary problem as
everything else here, one layer further out. The camera exists at
``127.0.0.1:<port>`` inside one network namespace. The browser is on a laptop.
Between them: an ffmpeg the agent starts *inside* the namespace, which copies
the stream out to MediaMTX over the namespace's control route, and MediaMTX,
which turns it into WebRTC the browser can play.

Nothing is transcoded and nothing is written to disk. A preview costs one copy
of a stream that is already arriving, and it stops as soon as the last viewer
closes the tab -- which matters, because cameras cap concurrent sessions hard
and a preview left running is a session a recording cannot have.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import socket
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.enums import SourceKind
from app.models import Camera, CameraSource
from app.recorder.ffmpeg import publish_argv
from app.recorder.session import SourcePath
from app.services.connections import ConnectionService
from app.services.mediamtx import MediaMtx, MediaMtxError
from app.services.paths import PathUnavailable, ProfileSourcePath

log = structlog.get_logger(__name__)

#: Lines of ffmpeg stderr kept, so a preview that never starts can say why.
STDERR_LINES = 15


class PreviewError(RuntimeError):
    def __init__(self, message: str) -> None:
        self.user_message = message
        super().__init__(message)


@dataclass(slots=True)
class PreviewInfo:
    """What the browser needs to play it, and nothing else."""

    id: str
    path: str
    camera_id: str
    source_kind: SourceKind
    started_at: datetime
    expires_at: datetime
    viewers: int = 0
    #: What the camera is actually sending, as MediaMTX names it ("H264",
    #: "H265"). The browser needs this before it negotiates: a codec it cannot
    #: decode fails inside WebRTC, where the only symptom is a black frame.
    codec: str = ""


@dataclass
class _Live:
    info: PreviewInfo
    team_id: str
    #: Everyone who asked for this stream. One camera session serves all of
    #: them, so it outlives any single viewer leaving.
    watchers: set[str]
    process: asyncio.subprocess.Process
    stderr: deque[str] = field(default_factory=lambda: deque(maxlen=STDERR_LINES))
    #: Last time anyone was watching. Starts now, so a preview gets its grace
    #: period before the first viewer has finished negotiating WebRTC.
    last_viewer_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class PreviewGateway(Protocol):
    async def start(
        self, db: AsyncSession, camera: Camera, user_id: str, kind: SourceKind | None
    ) -> PreviewInfo: ...

    async def stop(self, preview_id: str, user_id: str | None = None) -> None: ...

    async def list(self, user_id: str | None = None) -> list[PreviewInfo]: ...


class PreviewManager:
    """Owns every live preview in this process.

    Runs wherever the namespaces are -- the agent in compose, the API in a
    single-process dev run -- because starting one means spawning a process
    inside a namespace, which is not something that can be asked for remotely
    without the whole gateway underneath it.
    """

    def __init__(
        self,
        *,
        connections: ConnectionService,
        sessions: async_sessionmaker[AsyncSession],
        mediamtx: MediaMtx | None = None,
        path_factory: Callable[[Camera, CameraSource], SourcePath] | None = None,
    ) -> None:
        self.connections = connections
        self._sessions = sessions
        self.mediamtx = mediamtx or MediaMtx()
        # How a source is reached. The one seam preview has of its own: the
        # tests supply a scripted path here, and a future reachability model
        # that is not "profile with a tunnel" plugs in without touching this.
        self._path_factory = path_factory or self._profile_path
        self._live: dict[str, _Live] = {}
        #: Starts that have not finished yet, so a second request for the same
        #: camera joins one rather than racing it.
        self._starting: dict[tuple[str, SourceKind], asyncio.Future[PreviewInfo]] = {}

    def _profile_path(self, camera: Camera, source: CameraSource) -> SourcePath:
        return ProfileSourcePath(
            source_id=source.id,
            kind=SourceKind(source.kind),
            profile_id=camera.profile_id,
            connections=self.connections,
            sessions=self._sessions,
        )

    # ---- starting ------------------------------------------------------

    async def start(
        self, db: AsyncSession, camera: Camera, user_id: str, kind: SourceKind | None = None
    ) -> PreviewInfo:
        source = self._pick_source(camera, kind)
        self._check_caps(user_id, camera.id, source)
        key = (camera.id, SourceKind(source.kind))

        existing = self._existing(camera.id, SourceKind(source.kind))
        if existing is not None:
            # Two people watching one camera share one stream. The camera only
            # ever sees a single session, which is the whole reason MediaMTX is
            # in the middle.
            existing.watchers.add(user_id)
            return existing.info

        # A stream is only in _live once it is ready, which takes seconds, so
        # two starts arriving inside that window both find nothing and both
        # build one. They then contend for the same tunnel: the first to be
        # stopped releases the port lease, and the second loses its input and
        # times out as "the camera did not start sending video". React mounting
        # a component twice is enough to produce it.
        inflight = self._starting.get(key)
        if inflight is not None:
            # Shielded: this caller giving up must not cancel the start the
            # other one is still waiting on.
            info = await asyncio.shield(inflight)
            live = self._live.get(info.id)
            if live is not None:
                live.watchers.add(user_id)
            return info

        task = asyncio.ensure_future(self._start_one(camera, source, user_id))
        self._starting[key] = task
        try:
            return await task
        finally:
            self._starting.pop(key, None)

    async def _start_one(self, camera: Camera, source: CameraSource, user_id: str) -> PreviewInfo:
        cfg = settings()
        path = self._path_factory(camera, source)
        try:
            opened = await path.open()
        except PathUnavailable as exc:
            raise PreviewError(exc.user_message) from exc

        name = f"preview/{secrets.token_urlsafe(12)}"
        target = await self._publish_target(name)
        try:
            await self.mediamtx.add_path(name)
        except MediaMtxError as exc:
            raise PreviewError(exc.user_message) from exc

        process = await opened.runner.spawn(
            publish_argv(opened.url, SourceKind(source.kind), target)
        )
        started = datetime.now(UTC)
        live = _Live(
            info=PreviewInfo(
                id=secrets.token_urlsafe(8),
                path=name,
                camera_id=camera.id,
                source_kind=SourceKind(source.kind),
                started_at=started,
                expires_at=started + timedelta(seconds=cfg.preview_max_seconds),
            ),
            team_id=camera.team_id,
            watchers={user_id},
            process=process,
        )
        asyncio.create_task(_drain(process, live.stderr))

        try:
            await self._await_ready(name, live)
        except PreviewError:
            await self._tear_down(live)
            raise

        self._live[live.info.id] = live
        log.info(
            "preview.started",
            camera=camera.id,
            path=name,
            kind=source.kind,
            via=opened.runner.name,
        )
        return live.info

    async def _await_ready(self, name: str, live: _Live) -> None:
        """Wait for frames to actually arrive at MediaMTX.

        A publisher that connects and sends nothing is the common camera failure,
        and returning a path that will never carry video hands the browser a
        spinner with no explanation in it.
        """
        deadline = asyncio.get_running_loop().time() + settings().preview_ready_seconds
        while asyncio.get_running_loop().time() < deadline:
            if live.process.returncode is not None:
                raise PreviewError(_why(live) or "the preview stream stopped immediately")
            with contextlib.suppress(MediaMtxError):
                state = await self.mediamtx.state(name)
                if state is not None and state.ready:
                    return
            await asyncio.sleep(0.4)
        raise PreviewError(
            _why(live) or "the camera did not start sending video within the time allowed"
        )

    # ---- stopping ------------------------------------------------------

    async def stop(self, preview_id: str, user_id: str | None = None) -> None:
        """Stop watching. The stream goes only when nobody is left on it.

        Two things share a stream and neither is visible from here: a second
        person watching the same camera, and the same person's browser
        reconnecting -- React remounts a component twice in development, and the
        first mount's teardown arrives after the second has already connected.
        Tearing down on the first stop blanks both.

        So the last watcher leaving is a question for MediaMTX, which knows who
        is actually reading. If it says someone still is, this leaves the stream
        alone and the reaper collects it once that stops being true.
        """
        live = self._live.get(preview_id)
        if live is None:
            return
        if user_id is not None:
            live.watchers.discard(user_id)
            if live.watchers:
                return
            if await self._still_being_read(live):
                # Someone is on it who never asked us for it, or the same
                # browser has already reconnected. Let the reaper decide.
                live.last_viewer_at = datetime.now(UTC)
                return
        self._live.pop(preview_id, None)
        await self._tear_down(live)
        log.info("preview.stopped", path=live.info.path, camera=live.info.camera_id)

    async def _still_being_read(self, live: _Live) -> bool:
        try:
            state = await self.mediamtx.state(live.info.path)
        except MediaMtxError:
            # No answer is not evidence that nobody is watching.
            return True
        return bool(state and state.readers)

    async def stop_all(self) -> None:
        for preview_id in list(self._live):
            await self.stop(preview_id)

    async def _tear_down(self, live: _Live) -> None:
        if live.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                live.process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(live.process.wait(), timeout=5.0)
        if live.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                live.process.kill()
        with contextlib.suppress(MediaMtxError):
            await self.mediamtx.remove_path(live.info.path)

    # ---- the reaper ----------------------------------------------------

    async def reap(self) -> int:
        """Stop previews nobody is watching. Returns how many went.

        Three reasons to stop: the publisher died, the last viewer left a while
        ago, or it has simply been running too long. The middle one is the
        important one -- a tab left open on a wall display is not a reason to
        hold a camera session forever, but neither is a five second gap while
        someone reloads.
        """
        if not self._live:
            return 0
        cfg = settings()
        now = datetime.now(UTC)
        try:
            states = await self.mediamtx.states()
        except MediaMtxError:
            log.warning("preview.reap_skipped", reason="the preview server did not answer")
            return 0

        stopped = 0
        for preview_id, live in list(self._live.items()):
            state = states.get(live.info.path)
            live.info.viewers = state.readers if state else 0
            if live.info.viewers:
                live.last_viewer_at = now

            idle = (now - live.last_viewer_at).total_seconds()
            reason = None
            if live.process.returncode is not None:
                reason = "the stream stopped"
            elif now >= live.info.expires_at:
                reason = "it reached the maximum preview length"
            elif idle >= cfg.preview_idle_seconds:
                reason = "nobody is watching"

            if reason:
                log.info("preview.reaped", path=live.info.path, reason=reason)
                await self.stop(preview_id)
                stopped += 1
        return stopped

    # ---- queries -------------------------------------------------------

    @property
    def count(self) -> int:
        return len(self._live)

    async def list(self, user_id: str | None = None) -> list[PreviewInfo]:
        return [
            live.info for live in self._live.values() if user_id is None or user_id in live.watchers
        ]

    def _existing(self, camera_id: str, kind: SourceKind) -> _Live | None:
        return next(
            (
                live
                for live in self._live.values()
                if live.info.camera_id == camera_id
                and live.info.source_kind is kind
                and live.process.returncode is None
            ),
            None,
        )

    # ---- rules ---------------------------------------------------------

    @staticmethod
    def _pick_source(camera: Camera, kind: SourceKind | None) -> CameraSource:
        sources = list(camera.sources)
        if not sources:
            raise PreviewError("This camera has no sources to preview.")
        if kind is not None:
            chosen = next((s for s in sources if SourceKind(s.kind) is kind), None)
            if chosen is None:
                raise PreviewError(f"This camera has no {kind.value} source.")
            return chosen
        # The raw feed is what "is this camera working" means; the inferred one
        # is a comparison, and it is produced somewhere we do not control.
        return next((s for s in sources if SourceKind(s.kind) is SourceKind.RTSP), sources[0])

    def _check_caps(self, user_id: str, camera_id: str, source: CameraSource) -> None:
        cfg = settings()
        mine = [live for live in self._live.values() if user_id in live.watchers]
        if any(
            live.info.camera_id == camera_id and live.info.source_kind is SourceKind(source.kind)
            for live in mine
        ):
            return
        if len(mine) >= cfg.max_streams_per_user:
            raise PreviewError(
                f"You are already previewing {cfg.max_streams_per_user} cameras. "
                "Close one to open another."
            )
        everyone = {watcher for live in self._live.values() for watcher in live.watchers}
        if len(everyone | {user_id}) > cfg.max_concurrent_users:
            raise PreviewError(
                f"{cfg.max_concurrent_users} people are already watching live streams. "
                "This is a limit of the camera network, not of the dashboard."
            )

    async def _publish_target(self, name: str) -> str:
        """``rtsp://<ip>:<port>/<path>``, resolved here rather than in ffmpeg.

        The publisher runs inside a network namespace, where Docker's resolver
        on 127.0.0.11 does not exist. A hostname would fail to resolve and the
        error would read as though the camera were unreachable.
        """
        base = urlsplit(settings().mediamtx_publish_url)
        host, port = base.hostname or "mediamtx", base.port or 8554
        try:
            info = await asyncio.to_thread(
                socket.getaddrinfo, host, port, socket.AF_INET, socket.SOCK_STREAM
            )
        except OSError as exc:
            raise PreviewError(
                f"the preview server's address ({host}) could not be resolved: {exc}"
            ) from exc
        address = info[0][4][0]
        return f"rtsp://{address}:{port}/{name}"


def whep_url(configured: str, path: str) -> str:
    """Where the browser negotiates WebRTC for this path.

    Same-origin by default, through the dashboard's own proxy, for the reason
    the API is proxied too: the browser reaches this app on one address and the
    server has no way to know what that address was. Every request arrives here
    through the frontend's rewrite, so the Host header says ``localhost`` --
    deriving a public URL from it produces a link that works only on the machine
    running the stack, and fails on the laptops this is for.

    Only the signalling goes through the proxy. The media itself flows straight
    from MediaMTX to the browser over UDP, which is why the preview server still
    has to advertise an address the browser can reach.

    Set PREVIEW_PUBLIC_BASE when MediaMTX is exposed directly instead.
    """
    if configured:
        return f"{configured.rstrip('/')}/{path}/whep"
    return f"/rtc/{path}/whep"


async def _drain(process: asyncio.subprocess.Process, tail: deque[str]) -> None:
    if process.stderr is None:
        return
    while True:
        line = await process.stderr.readline()
        if not line:
            return
        tail.append(line.decode(errors="replace").rstrip())


def _why(live: _Live) -> str:
    """The last thing ffmpeg said, which is usually the answer."""
    for line in reversed(live.stderr):
        if line.strip():
            return line.strip()[:200]
    return ""
