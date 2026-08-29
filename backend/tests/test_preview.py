"""Live preview.

The thing worth testing here is not that a stream plays -- that is MediaMTX's
job and a browser's. It is that a preview costs exactly one camera session,
gives it back when nobody is watching, and never leaves an ffmpeg behind when
it fails.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.enums import ReachMode, SourceKind
from app.models import Camera, CameraSource, ConnectionProfile, Team
from app.recorder.session import OpenPath
from app.security.redaction import StreamUrl
from app.services.preview import PreviewError, PreviewManager, whep_url
from tests.conftest import FakeMediaMtx, FakeProcess

RTSP = StreamUrl.build("rtsp://10.0.0.42:554/stream1", "admin", "hunter2")


class ScriptedPath:
    """A camera that is already reachable, with a runner that records argv."""

    def __init__(self, kind: SourceKind = SourceKind.RTSP, process: FakeProcess | None = None):
        self.kind = kind
        self.source_id = "src-1"
        self.process = process or FakeProcess()
        self.spawned: list[list[str]] = []
        self.opened = 0
        outer = self

        class Runner:
            name = "netns"
            namespace = "cam-abc12345"

            def wrap(self, argv):
                return list(argv)

            async def run(self, argv, **kwargs):  # pragma: no cover - unused
                raise NotImplementedError

            async def spawn(self, argv, **kwargs):
                outer.spawned.append(list(argv))
                return outer.process

        self.runner = Runner()

    async def open(self) -> OpenPath:
        self.opened += 1
        return OpenPath(url=RTSP, runner=self.runner)

    async def diagnose(self):
        return "camera", "not answering"

    async def close(self) -> None:
        return None


@pytest.fixture
def publishable(monkeypatch):
    """Point the publisher at an address that needs no DNS, and do not spend the
    real fifteen seconds waiting for frames that are never coming."""
    monkeypatch.setenv("MEDIAMTX_PUBLISH_URL", "rtsp://127.0.0.1:8554")
    monkeypatch.setenv("PREVIEW_READY_SECONDS", "1")
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
async def camera(sessions):
    async with sessions() as db:
        team = Team(name="MOFA", slug="mofa")
        db.add(team)
        await db.flush()
        profile = ConnectionProfile(team_id=team.id, name="Direct", mode=ReachMode.DIRECT)
        db.add(profile)
        await db.flush()
        cam = Camera(team_id=team.id, profile_id=profile.id, name="Gate 1")
        db.add(cam)
        await db.flush()
        db.add_all(
            [
                CameraSource(camera_id=cam.id, kind=SourceKind.HLS, url="https://h/s.m3u8"),
                CameraSource(camera_id=cam.id, kind=SourceKind.RTSP, url="rtsp://10.0.0.42/s1"),
            ]
        )
        await db.commit()
        await db.refresh(cam, ["sources"])
        return cam


def build(sessions, path: ScriptedPath, mtx: FakeMediaMtx | None = None) -> PreviewManager:
    return PreviewManager(
        connections=None,  # the path factory is what reaches the camera here
        sessions=sessions,
        mediamtx=mtx or FakeMediaMtx(),
        path_factory=lambda camera, source: path,
    )


# ---- addresses ----------------------------------------------------------


def test_signalling_is_same_origin_unless_told_otherwise():
    """Every request reaches the API through the dashboard's proxy, so the Host
    header says localhost however the browser got here. A URL derived from it
    would work on the machine running the stack and nowhere else -- which is the
    one place nobody watches cameras from."""
    assert whep_url("", "preview/abc") == "/rtc/preview/abc/whep"


def test_an_exposed_preview_server_is_addressed_directly():
    assert (
        whep_url("http://10.0.0.5:8889", "preview/abc") == "http://10.0.0.5:8889/preview/abc/whep"
    )


# ---- starting -----------------------------------------------------------


async def test_a_preview_publishes_from_inside_the_cameras_namespace(sessions, camera, publishable):
    path = ScriptedPath()
    previews = build(sessions, path)

    async with sessions() as db:
        info = await previews.start(db, camera, "user-1")

    assert info.path.startswith("preview/")
    assert info.source_kind is SourceKind.RTSP
    argv = path.spawned[0]
    assert argv[0] == "ffmpeg"
    # Copied, not transcoded.
    assert "-c" in argv and argv[argv.index("-c") + 1] == "copy"
    # Pinned to TCP on both legs.
    assert argv.count("-rtsp_transport") == 2
    # And the target is an address: Docker's resolver does not exist inside a
    # namespace, so a hostname here would look like the camera was down.
    assert argv[-1] == f"rtsp://127.0.0.1:8554/{info.path}"


async def test_the_path_is_not_derived_from_anything_guessable(sessions, camera, publishable):
    """Anyone who can reach MediaMTX can watch a path whose name they know, and
    camera ids travel in API responses. Until the preview server has auth of its
    own, an unguessable name is the thing standing in for it."""
    previews = build(sessions, ScriptedPath())
    async with sessions() as db:
        first = await previews.start(db, camera, "user-1")
        other = Camera(
            id="cam-2", team_id=camera.team_id, profile_id=camera.profile_id, name="Gate 2"
        )
        other.sources = list(camera.sources)
        second = await previews.start(db, other, "user-1")

    assert camera.id not in first.path
    assert first.path != second.path


async def test_the_raw_feed_is_what_preview_means(sessions, camera, publishable):
    """The camera has an HLS source listed first; preview still picks RTSP,
    because "is this camera working" is a question about the camera."""
    path = ScriptedPath()
    previews = build(sessions, path)
    async with sessions() as db:
        info = await previews.start(db, camera, "user-1")
    assert info.source_kind is SourceKind.RTSP


async def test_asking_for_a_source_the_camera_does_not_have_says_so(sessions, camera):
    previews = build(sessions, ScriptedPath())
    async with sessions() as db:
        camera.sources = [s for s in camera.sources if SourceKind(s.kind) is SourceKind.RTSP]
        with pytest.raises(PreviewError, match="no hls source"):
            await previews.start(db, camera, "user-1", SourceKind.HLS)


async def test_two_people_watching_one_camera_share_one_stream(sessions, camera, publishable):
    """The camera sees a single session however many browsers are open."""
    path = ScriptedPath()
    previews = build(sessions, path)

    async with sessions() as db:
        first = await previews.start(db, camera, "user-1")
        second = await previews.start(db, camera, "user-2")

    assert first.id == second.id
    assert len(path.spawned) == 1
    assert previews.count == 1


async def test_a_stream_that_never_arrives_leaves_nothing_behind(sessions, camera, publishable):
    """The common camera failure: the publisher connects and sends no frames.
    Returning a path that will never carry video hands the browser a spinner."""
    process = FakeProcess(stderr=["Connection timed out"])
    path = ScriptedPath(process=process)
    mtx = FakeMediaMtx(ready=False)
    previews = build(sessions, path, mtx)

    async with sessions() as db:
        with pytest.raises(PreviewError) as caught:
            await previews.start(db, camera, "user-1")

    assert "Connection timed out" in caught.value.user_message
    assert previews.count == 0
    assert mtx.paths == {}, "the path outlived the publisher"
    assert mtx.removed, "the path was never cleaned up"
    assert process.terminated or process.returncode is not None


async def test_one_viewer_leaving_does_not_blank_the_other(sessions, camera, publishable):
    """A shared stream outlives any single viewer. Tearing it down on the first
    stop is what makes the second person's picture go black for no reason they
    can see."""
    mtx = FakeMediaMtx()
    previews = build(sessions, ScriptedPath(), mtx)

    async with sessions() as db:
        info = await previews.start(db, camera, "user-1")
        await previews.start(db, camera, "user-2")

    await previews.stop(info.id, "user-1")
    assert previews.count == 1, "the other viewer's stream was torn down"

    await previews.stop(info.id, "user-2")
    assert previews.count == 0
    assert info.path in mtx.removed


async def test_a_browser_that_has_already_reconnected_keeps_its_stream(
    sessions, camera, publishable
):
    """React remounts a component twice in development, and the first mount's
    teardown lands after the second has connected. MediaMTX knows someone is
    reading even when our own bookkeeping says the last watcher left."""
    mtx = FakeMediaMtx()
    previews = build(sessions, ScriptedPath(), mtx)

    async with sessions() as db:
        info = await previews.start(db, camera, "user-1")
    mtx.paths[info.path] = 1  # the remounted player is already pulling frames

    await previews.stop(info.id, "user-1")

    assert previews.count == 1
    assert info.path not in mtx.removed


async def test_stopping_without_a_user_stops_it_outright(sessions, camera, publishable):
    """Shutdown and the reaper are not viewers leaving; they end the stream."""
    mtx = FakeMediaMtx()
    previews = build(sessions, ScriptedPath(), mtx)
    async with sessions() as db:
        info = await previews.start(db, camera, "user-1")
    mtx.paths[info.path] = 3

    await previews.stop(info.id)

    assert previews.count == 0


# ---- limits -------------------------------------------------------------


async def test_a_user_cannot_hold_more_streams_than_the_cameras_allow(
    sessions, camera, publishable, monkeypatch
):
    previews = build(sessions, ScriptedPath())
    async with sessions() as db:
        for index in range(settings().max_streams_per_user):
            other = Camera(
                id=f"cam-{index}",
                team_id=camera.team_id,
                profile_id=camera.profile_id,
                name=f"cam-{index}",
            )
            other.sources = list(camera.sources)
            await previews.start(db, other, "user-1")

        with pytest.raises(PreviewError, match="Close one to open another"):
            await previews.start(db, camera, "user-1")


async def test_the_concurrent_viewer_limit_is_the_cameras_limit_not_ours(
    sessions, camera, publishable
):
    previews = build(sessions, ScriptedPath())
    async with sessions() as db:
        for index in range(settings().max_concurrent_users):
            other = Camera(
                id=f"cam-{index}",
                team_id=camera.team_id,
                profile_id=camera.profile_id,
                name=f"cam-{index}",
            )
            other.sources = list(camera.sources)
            await previews.start(db, other, f"user-{index}")

        with pytest.raises(PreviewError, match="camera network"):
            await previews.start(db, camera, "user-99")


# ---- the reaper ---------------------------------------------------------


async def _one(sessions, camera, mtx: FakeMediaMtx) -> tuple[PreviewManager, str]:
    path = ScriptedPath()
    previews = build(sessions, path, mtx)
    async with sessions() as db:
        info = await previews.start(db, camera, "user-1")
    return previews, info.path


async def test_a_preview_nobody_is_watching_gives_the_camera_back(sessions, camera, publishable):
    mtx = FakeMediaMtx()
    previews, path = await _one(sessions, camera, mtx)

    assert await previews.reap() == 0, "it has not been idle long enough yet"

    live = next(iter(previews._live.values()))
    live.last_viewer_at = datetime.now(UTC) - timedelta(seconds=settings().preview_idle_seconds + 1)

    assert await previews.reap() == 1
    assert previews.count == 0
    assert path in mtx.removed


async def test_someone_watching_keeps_it_alive(sessions, camera, publishable):
    mtx = FakeMediaMtx()
    previews, path = await _one(sessions, camera, mtx)
    live = next(iter(previews._live.values()))
    live.last_viewer_at = datetime.now(UTC) - timedelta(hours=1)
    mtx.paths[path] = 2  # two browsers connected

    assert await previews.reap() == 0
    assert previews.count == 1
    assert live.info.viewers == 2


async def test_a_publisher_that_died_is_cleaned_up(sessions, camera, publishable):
    mtx = FakeMediaMtx()
    previews, _ = await _one(sessions, camera, mtx)
    live = next(iter(previews._live.values()))
    live.process.returncode = 1

    assert await previews.reap() == 1
    assert previews.count == 0


async def test_a_forgotten_tab_does_not_hold_a_camera_forever(sessions, camera, publishable):
    mtx = FakeMediaMtx()
    previews, path = await _one(sessions, camera, mtx)
    live = next(iter(previews._live.values()))
    live.info.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    mtx.paths[path] = 1  # still being watched, and still stopped

    assert await previews.reap() == 1
    assert previews.count == 0


async def test_the_reaper_leaves_everything_alone_when_mediamtx_is_down(
    sessions, camera, publishable
):
    """A preview server that is not answering is not evidence that nobody is
    watching, and tearing down live streams on that basis is worse than waiting."""

    class Unreachable(FakeMediaMtx):
        async def states(self):
            from app.services.mediamtx import MediaMtxError

            raise MediaMtxError("no answer")

    previews, _ = await _one(sessions, camera, Unreachable())
    assert await previews.reap() == 0
    assert previews.count == 1


async def test_stopping_everything_kills_the_publishers(sessions, camera, publishable):
    mtx = FakeMediaMtx()
    previews, path = await _one(sessions, camera, mtx)

    await previews.stop_all()

    assert previews.count == 0
    assert path in mtx.removed


# ---- through the API ----------------------------------------------------


@pytest.fixture
async def previewable(app, sessions, seeded, as_member):
    """The API with a scripted camera behind it."""
    path = ScriptedPath()
    app.state.previews = build(sessions, path)
    created = await as_member.post(
        "/api/profiles",
        json={"team_id": seeded["mofa"], "name": "Direct", "mode": ReachMode.DIRECT.value},
    )
    camera = await as_member.post(
        "/api/cameras",
        json={
            "team_id": seeded["mofa"],
            "profile_id": created.json()["id"],
            "name": "Gate 1",
            "sources": [{"kind": "rtsp", "url": "rtsp://10.0.0.42:554/s1"}],
        },
    )
    return camera.json()["id"]


async def test_starting_a_preview_tells_the_browser_where_to_negotiate(
    as_member, previewable, publishable
):
    response = await as_member.post(f"/api/cameras/{previewable}/preview")

    assert response.status_code == 200
    body = response.json()
    assert body["source_kind"] == "rtsp"
    # Relative: the browser negotiates through the origin it is already on.
    assert body["whep_url"] == f"/rtc/{body['path']}/whep"


async def test_you_cannot_stop_someone_elses_preview(as_member, previewable, publishable, app):
    """Two people watching one camera share a stream, so closing your tab must
    not blank the other person's screen. Even an admin, who can see the camera,
    does not own someone else's view of it."""
    started = (await as_member.post(f"/api/cameras/{previewable}/preview")).json()
    await as_member.post(
        "/api/auth/login", json={"email": "admin@example.com", "password": "admin-password"}
    )

    response = await as_member.delete(f"/api/cameras/{previewable}/preview/{started['id']}")

    assert response.status_code == 404
    assert app.state.previews.count == 1, "someone else's stream was torn down"


async def test_stopping_your_own_preview_works(as_member, previewable, publishable, app):
    started = (await as_member.post(f"/api/cameras/{previewable}/preview")).json()

    response = await as_member.delete(f"/api/cameras/{previewable}/preview/{started['id']}")

    assert response.status_code == 204
    assert app.state.previews.count == 0


async def test_a_camera_that_will_not_stream_is_a_409_with_the_reason(
    as_member, app, sessions, seeded, publishable
):
    app.state.previews = build(
        sessions,
        ScriptedPath(process=FakeProcess(stderr=["401 Unauthorized"])),
        FakeMediaMtx(ready=False),
    )
    profile = await as_member.post(
        "/api/profiles",
        json={"team_id": seeded["mofa"], "name": "D", "mode": ReachMode.DIRECT.value},
    )
    camera = await as_member.post(
        "/api/cameras",
        json={
            "team_id": seeded["mofa"],
            "profile_id": profile.json()["id"],
            "name": "Gate 9",
            "sources": [{"kind": "rtsp", "url": "rtsp://10.0.0.9:554/s1"}],
        },
    )

    response = await as_member.post(f"/api/cameras/{camera.json()['id']}/preview")

    assert response.status_code == 409
    assert "401 Unauthorized" in response.json()["detail"]
