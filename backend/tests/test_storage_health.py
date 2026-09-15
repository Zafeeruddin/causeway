"""The storage check the loading screen reads before sign-in.

What is worth pinning is not that HeadBucket works -- that is boto3's job and
the gateway's -- but the promises the page depends on: the answer is short
enough to show a stranger, arrives in seconds when the store does not, and
costs the store one request however many people are refreshing.
"""

from __future__ import annotations

import asyncio

from app.storage.client import StorageError
from app.storage.health import StorageProbe, endpoint_label


class Store:
    """A store whose answer, and how often it was asked, are up to the test."""

    def __init__(self, error: Exception | None = None, delay: float = 0.0) -> None:
        self.error = error
        self.delay = delay
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def probe(check, **overrides) -> StorageProbe:
    options = {"endpoint": "storage.test", "enabled": True, "ttl": 15.0, "timeout": 5.0}
    return StorageProbe(check=check, **{**options, **overrides})


# ---- over HTTP ------------------------------------------------------------


async def test_the_loading_screen_can_ask_before_anyone_signs_in(client):
    response = await client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["storage"] == {"status": "ok", "endpoint": "storage.test"}


async def test_storage_being_down_degrades_health_without_hiding_the_build(app, client):
    """Still a 200 with the version in it: which build a machine is running is
    the first question during exactly the incident that took storage out."""
    from app.version import __version__

    app.state.storage_probe = probe(Store(StorageError("could not reach it")))

    response = await client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["version"] == __version__
    assert body["storage"] == {"status": "unavailable", "endpoint": "storage.test"}


async def test_the_public_answer_never_says_which_bucket_or_credential_failed(app, client):
    """Anyone who can load the login page can read this. Which bucket exists and
    whether a credential was refused is an operator's business, and the log has
    it for them."""
    app.state.storage_probe = probe(
        Store(StorageError("these credentials cannot access 'cam-recordings'"))
    )

    body = (await client.get("/api/health")).json()

    assert set(body["storage"]) == {"status", "endpoint"}
    assert "cam-recordings" not in str(body)
    assert "credentials" not in str(body)


async def test_a_deployment_without_the_check_reports_healthy(app, client):
    app.state.storage_probe = probe(Store(StorageError("would have failed")), enabled=False)

    body = (await client.get("/api/health")).json()

    assert body["status"] == "ok"
    assert body["storage"]["status"] == "disabled"


# ---- the probe ------------------------------------------------------------


async def test_a_store_that_does_not_answer_is_unavailable_in_seconds_not_minutes():
    """The upload client's patience is right for an upload and would leave a
    loading screen spinning."""
    subject = probe(Store(delay=30.0), timeout=0.05)

    health = await asyncio.wait_for(subject.current(), timeout=2.0)

    assert health.status == "unavailable"


async def test_one_probe_serves_every_caller_until_it_goes_stale():
    store = Store()
    clock = Clock()
    subject = probe(store, ttl=15.0, clock=clock)

    for _ in range(5):
        await subject.current()
    assert store.calls == 1

    clock.now += 16.0
    await subject.current()
    assert store.calls == 2


async def test_callers_arriving_together_share_one_request():
    """Everyone refreshing a broken page at once is when the store can least
    afford a request each."""
    store = Store(delay=0.05)
    subject = probe(store)

    results = await asyncio.gather(*(subject.current() for _ in range(20)))

    assert store.calls == 1
    assert {r.status for r in results} == {"ok"}


async def test_recovery_is_seen_once_the_last_answer_goes_stale():
    """The blocked screen polls. It has to be let through when storage returns,
    not held on a cached failure forever."""
    store = Store(StorageError("down"))
    clock = Clock()
    subject = probe(store, clock=clock)

    assert (await subject.current()).status == "unavailable"
    store.error = None
    assert (await subject.current()).status == "unavailable"
    clock.now += 16.0
    assert (await subject.current()).status == "ok"


async def test_a_deployment_that_opts_out_never_touches_storage():
    """Some deployments have no object store, or one that does not answer
    HeadBucket. Off means off, not a check whose result is ignored."""
    store = Store(StorageError("would have failed"))

    health = await probe(store, enabled=False).current()

    assert health.status == "disabled"
    assert store.calls == 0


def test_the_probe_gives_up_faster_than_an_upload_would():
    config = probe(None, timeout=4.0)._probe_store()._client.meta.config

    assert config.connect_timeout == 3.0
    assert config.read_timeout == 4.0
    assert config.retries["total_max_attempts"] == 1


def test_the_endpoint_shown_is_the_host_and_nothing_riding_along_with_it():
    assert endpoint_label("https://s3.example.com") == "s3.example.com"
    assert endpoint_label("https://s3.example.com:9000/some/path") == "s3.example.com:9000"
    assert endpoint_label("https://key:secret@s3.example.com") == "s3.example.com"
    assert endpoint_label("") == ""
