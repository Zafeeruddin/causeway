"""Talking to MediaMTX, which republishes camera streams for the browser.

MediaMTX can pull a source itself, and its config is written as if it will --
but it cannot pull ours. A camera behind a jump host lives at ``127.0.0.1:<port>``
*inside one network namespace*, and MediaMTX is a different container with no
way into it. So the direction is inverted: the agent pushes, from inside the
namespace, and MediaMTX only ever sees a publisher arriving on a path.

Paths are created here just before a publisher starts and removed when it stops,
which is what the config comment means by "added at runtime over the API".
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

from app.config import settings

log = structlog.get_logger(__name__)


class MediaMtxError(RuntimeError):
    user_message = "The preview server could not be reached."


@dataclass(frozen=True, slots=True)
class PathState:
    name: str
    ready: bool
    readers: int
    #: What the publisher is sending, once it is sending anything.
    tracks: tuple[str, ...] = ()


class MediaMtx:
    def __init__(self, api_url: str | None = None, *, timeout: float = 5.0) -> None:
        self.api_url = (api_url or settings().mediamtx_api_url).rstrip("/")
        self._timeout = timeout

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = f"{self.api_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise MediaMtxError(f"could not reach the preview server at {url}: {exc}") from exc

    async def add_path(self, name: str) -> None:
        """Create a path that accepts a publisher.

        An empty config is the whole point: a path with no ``source`` waits for
        someone to publish to it, which is us.
        """
        response = await self._request("POST", f"/v3/config/paths/add/{name}", json={})
        # Already there is not a failure -- a retried start should be idempotent.
        if response.status_code not in (200, 201, 400):
            raise MediaMtxError(
                f"the preview server refused the path {name!r}: "
                f"{response.status_code} {response.text[:200]}"
            )
        log.info("mediamtx.path_added", path=name)

    async def remove_path(self, name: str) -> None:
        response = await self._request("DELETE", f"/v3/config/paths/delete/{name}")
        if response.status_code not in (200, 404):
            log.warning("mediamtx.path_delete_failed", path=name, status=response.status_code)
        else:
            log.info("mediamtx.path_removed", path=name)

    async def state(self, name: str) -> PathState | None:
        """What the path is doing right now, or None if it does not exist."""
        response = await self._request("GET", f"/v3/paths/get/{name}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise MediaMtxError(f"the preview server returned {response.status_code} for {name!r}")
        return _state_from(name, response.json())

    async def states(self) -> dict[str, PathState]:
        """Every path at once -- one call for the whole reaper sweep."""
        response = await self._request("GET", "/v3/paths/list")
        if response.status_code != 200:
            raise MediaMtxError(f"the preview server returned {response.status_code}")
        items = response.json().get("items") or []
        return {item["name"]: _state_from(item["name"], item) for item in items}


def _state_from(name: str, payload: dict) -> PathState:
    return PathState(
        name=name,
        ready=bool(payload.get("ready")),
        readers=len(payload.get("readers") or []),
        tracks=tuple(payload.get("tracks") or ()),
    )
