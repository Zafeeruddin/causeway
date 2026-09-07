"""Sign-in limits, password reset, and the demo tier.

Three features that only exist because of one situation: an instance on the
public internet with a published login. Each is tested for the thing that would
make it useless -- a limiter that can be sidestepped with a header, a reset link
that works twice, a read-only account that can write.
"""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.auth import hash_password, verify_password
from app.config import settings
from app.models import User
from app.security.reset import issue_reset, read_reset
from app.security.throttle import Limit, Throttle, client_address


async def _login(client, email, password):
    return await client.post("/api/auth/login", json={"email": email, "password": password})


async def _reload(sessions, user_id):
    async with sessions() as db:
        return await db.get(User, user_id)


# ---- the demo tier -----------------------------------------------------


async def test_a_demo_account_sees_what_a_viewer_sees(as_demo, seeded):
    """Read access is the whole point; taking it away would leave nothing to demo."""
    assert (await as_demo.get("/api/cameras")).status_code == 200
    assert (await as_demo.get("/api/recordings")).status_code == 200

    me = (await as_demo.get("/api/auth/me")).json()
    assert me["role"] == "demo"
    assert me["may_write"] is False
    assert [t["slug"] for t in me["teams"]] == ["acme"]


async def test_a_viewer_still_reports_itself_as_writable(as_viewer):
    """The flag has to distinguish the two tiers or the dashboard cannot use it."""
    assert (await as_viewer.get("/api/auth/me")).json()["may_write"] is True


async def test_a_demo_account_cannot_record(as_demo, seeded):
    refused = await as_demo.post(
        "/api/recordings", json={"camera_ids": ["anything"], "seconds": 60}
    )
    assert refused.status_code == 403
    assert "demo" in refused.json()["detail"].lower()


async def test_a_demo_account_cannot_delete_a_recording(as_demo):
    # 403 rather than 404: the tier is refused before the row is looked for, so
    # a demo account cannot use delete as an oracle for which ids exist.
    refused = await as_demo.delete("/api/recordings/does-not-matter")
    assert refused.status_code == 403


async def test_a_demo_account_cannot_add_a_camera(as_demo, seeded):
    """The reason this tier exists.

    A published login that can add a camera can point one at any address the
    server can reach, which turns a demo into a port scanner with someone
    else's IP on it.
    """
    for path, body in (
        (
            "/api/cameras",
            {
                "team_id": seeded["acme"],
                "profile_id": "any-profile",
                "name": "Somewhere Else",
                "sources": [{"kind": "rtsp", "url": "rtsp://198.51.100.9:554/stream"}],
            },
        ),
        (
            "/api/cameras/import",
            {
                "team_id": seeded["acme"],
                "profile_id": "any-profile",
                "text": "rtsp://198.51.100.9:554/stream",
            },
        ),
    ):
        assert (await as_demo.post(path, json=body)).status_code == 403


async def test_a_demo_account_cannot_reach_the_plumbing(as_demo):
    assert (await as_demo.get("/api/profiles")).status_code == 403
    assert (await as_demo.get("/api/admin/users")).status_code == 403


async def test_a_demo_account_cannot_change_its_own_password(as_demo, sessions, seeded):
    """A shared login the first visitor can change is a login you gave away."""
    before = (await _reload(sessions, seeded["demo"])).password_hash
    refused = await as_demo.post(
        "/api/auth/password",
        json={"current_password": "demo-password", "password": "taken-over-by-me"},
    )
    assert refused.status_code == 403
    assert (await _reload(sessions, seeded["demo"])).password_hash == before


async def test_a_demo_account_cannot_be_mailed_a_reset_link(as_demo, client, sessions, seeded):
    """Same door, one step earlier: the anonymous request must not open it either."""
    before = (await _reload(sessions, seeded["demo"])).password_hash
    asked = await client.post("/api/auth/forgot-password", json={"email": "demo@example.com"})
    assert asked.status_code == 202  # same answer as any other address
    assert (await _reload(sessions, seeded["demo"])).password_hash == before


async def test_an_admin_cannot_issue_a_reset_link_for_a_demo_account(as_admin, seeded):
    refused = await as_admin.post(f"/api/admin/users/{seeded['demo']}/reset-link")
    assert refused.status_code == 409


# ---- sign-in limits ----------------------------------------------------


@pytest.fixture
def tight_limits(monkeypatch):
    monkeypatch.setenv("LOGIN_ATTEMPTS_PER_ADDRESS", "3")
    monkeypatch.setenv("LOGIN_ATTEMPTS_PER_ACCOUNT", "2")
    monkeypatch.setenv("LOGIN_ATTEMPT_WINDOW_SECONDS", "300")
    settings.cache_clear()
    yield
    settings.cache_clear()


async def test_repeated_wrong_passwords_are_eventually_refused(client, seeded, tight_limits):
    for _ in range(2):
        assert (await _login(client, "admin@example.com", "wrong")).status_code == 401

    blocked = await _login(client, "admin@example.com", "wrong")
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


async def test_the_limit_is_checked_before_the_password(client, seeded, tight_limits):
    """Otherwise the limiter is a way to make the server compute argon2 hashes.

    And, more to the point, a limit that lets the right password through is not
    a limit -- it is a slightly slower brute force.
    """
    for _ in range(2):
        await _login(client, "admin@example.com", "wrong")

    refused = await _login(client, "admin@example.com", "admin-password")
    assert refused.status_code == 429


async def test_getting_it_right_clears_the_count(client, seeded, tight_limits):
    """Two mistakes then success must not leave someone one mistake from lockout."""
    await _login(client, "admin@example.com", "wrong")
    assert (await _login(client, "admin@example.com", "admin-password")).status_code == 200

    await _login(client, "admin@example.com", "wrong")
    assert (await _login(client, "admin@example.com", "admin-password")).status_code == 200


async def test_the_account_limit_survives_a_change_of_address(app, seeded, tight_limits):
    """Credential stuffing spreads across addresses; a per-address limit misses it.

    Each request here arrives from a different socket address, so only the
    per-account window can stop it.
    """
    for index in range(2):
        transport = ASGITransport(app=app, client=(f"10.0.0.{index}", 5000))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            assert (await _login(c, "admin@example.com", "wrong")).status_code == 401

    transport = ASGITransport(app=app, client=("10.0.0.99", 5000))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await _login(c, "admin@example.com", "wrong")).status_code == 429


async def test_a_forged_forwarded_header_does_not_buy_a_fresh_limit(client, seeded, tight_limits):
    """With no proxy configured the header is ignored, so it cannot mint identities."""
    for index in range(2):
        await client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "wrong"},
            headers={"X-Forwarded-For": f"203.0.113.{index}"},
        )
    blocked = await client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "wrong"},
        headers={"X-Forwarded-For": "203.0.113.240"},
    )
    assert blocked.status_code == 429


async def test_reset_requests_are_limited(client, seeded, monkeypatch):
    monkeypatch.setenv("RESET_REQUESTS_PER_ADDRESS", "2")
    settings.cache_clear()
    try:
        for _ in range(2):
            asked = await client.post(
                "/api/auth/forgot-password", json={"email": "nobody@example.com"}
            )
            assert asked.status_code == 202
        assert (
            await client.post("/api/auth/forgot-password", json={"email": "nobody@example.com"})
        ).status_code == 429
    finally:
        settings.cache_clear()


async def test_an_unknown_address_gets_the_same_answer_as_a_known_one(client, seeded):
    """Anything else turns this endpoint into a list of your users."""
    unknown = await client.post("/api/auth/forgot-password", json={"email": "nobody@example.com"})
    known = await client.post("/api/auth/forgot-password", json={"email": "admin@example.com"})
    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()


# ---- the throttle itself -----------------------------------------------


def test_the_window_slides():
    throttle_ = Throttle()
    limit = Limit(attempts=2, window=10.0)
    now = 1000.0

    throttle_.record("k", limit, now=now)
    throttle_.record("k", limit, now=now + 1)
    # The oldest hit is at 1000 and leaves the window at 1010.
    assert throttle_.retry_after("k", limit, now=now + 2) == pytest.approx(8.0)

    # The oldest hit ages out and one attempt is available again.
    assert throttle_.retry_after("k", limit, now=now + 10.5) == 0.0


def test_keys_do_not_accumulate_for_ever():
    throttle_ = Throttle()
    limit = Limit(attempts=5, window=1.0)
    throttle_.record("k", limit, now=1000.0)
    throttle_.retry_after("k", limit, now=1002.0)
    assert "k" not in throttle_._hits


def test_a_forwarded_chain_is_read_from_the_right_end():
    class FakeRequest:
        def __init__(self, forwarded, host="10.1.1.1"):
            self.headers = {"x-forwarded-for": forwarded} if forwarded else {}
            self.client = type("C", (), {"host": host})()

    # A client that invented "1.2.3.4" and one real proxy hop: the address the
    # proxy appended is the last one, and it is the only one worth reading.
    request = FakeRequest("1.2.3.4, 198.51.100.7")
    assert client_address(request, trusted_hops=1) == "198.51.100.7"
    assert client_address(request, trusted_hops=0) == "10.1.1.1"

    assert client_address(FakeRequest(""), trusted_hops=1) == "10.1.1.1"


# ---- password reset ----------------------------------------------------


def test_a_reset_token_round_trips():
    token = issue_reset("user-1", "hash-a")
    claim = read_reset(token)
    assert claim is not None and claim[0] == "user-1"


def test_a_tampered_token_is_refused():
    token = issue_reset("user-1", "hash-a")
    body, _, signature = token.partition(".")
    assert read_reset(f"{body}.{signature[:-2]}xx") is None
    assert read_reset("nonsense") is None


def test_an_expired_token_is_refused():
    assert read_reset(issue_reset("user-1", "hash-a", ttl=-1)) is None
    # Belt and braces: the clock the token was signed against is real time.
    assert read_reset(issue_reset("user-1", "hash-a", ttl=int(time.time()) * 0 + 60)) is not None


async def test_an_admin_issues_a_link_and_it_sets_a_password(as_admin, client, sessions, seeded):
    issued = await as_admin.post(f"/api/admin/users/{seeded['viewer']}/reset-link")
    assert issued.status_code == 200
    payload = issued.json()
    assert payload["emailed"] is False  # no SMTP configured in the suite
    token = payload["url"].split("token=")[1]

    # Issuing does not change anything yet: somebody who has lost their password
    # keeps working until they redeem the link.
    assert (await _login(client, "watch@example.com", "viewer-password")).status_code == 200

    done = await client.post(
        "/api/auth/reset-password", json={"token": token, "password": "a-brand-new-password"}
    )
    assert done.status_code == 204

    user = await _reload(sessions, seeded["viewer"])
    assert verify_password(user.password_hash, "a-brand-new-password")


async def test_a_reset_link_works_once(as_admin, client, seeded):
    token = (
        (await as_admin.post(f"/api/admin/users/{seeded['viewer']}/reset-link"))
        .json()["url"]
        .split("token=")[1]
    )
    first = await client.post(
        "/api/auth/reset-password", json={"token": token, "password": "the-first-password"}
    )
    assert first.status_code == 204

    replayed = await client.post(
        "/api/auth/reset-password", json={"token": token, "password": "the-second-password"}
    )
    assert replayed.status_code == 400
    assert "already been used" in replayed.json()["detail"]


async def test_a_newer_link_voids_an_older_one(as_admin, client, seeded):
    """An administrator who issues a second link has revoked the first."""
    first = (
        (await as_admin.post(f"/api/admin/users/{seeded['viewer']}/reset-link"))
        .json()["url"]
        .split("token=")[1]
    )
    await client.post(
        "/api/auth/reset-password", json={"token": first, "password": "the-first-password"}
    )
    second = (
        (await as_admin.post(f"/api/admin/users/{seeded['viewer']}/reset-link"))
        .json()["url"]
        .split("token=")[1]
    )
    await client.post(
        "/api/auth/reset-password", json={"token": second, "password": "the-second-password"}
    )
    stale = await client.post(
        "/api/auth/reset-password", json={"token": first, "password": "a-third-password"}
    )
    assert stale.status_code == 400


async def test_a_reset_does_not_sign_anybody_in(as_admin, client, seeded):
    """The link proves you read a mailbox once. It is not a session."""
    token = (
        (await as_admin.post(f"/api/admin/users/{seeded['viewer']}/reset-link"))
        .json()["url"]
        .split("token=")[1]
    )
    async with AsyncClient(
        transport=client._transport, base_url="http://test"
    ) as fresh:  # no cookies
        await fresh.post(
            "/api/auth/reset-password", json={"token": token, "password": "a-brand-new-password"}
        )
        assert (await fresh.get("/api/auth/me")).status_code == 401


async def test_a_short_password_is_refused(as_admin, client, seeded):
    token = (
        (await as_admin.post(f"/api/admin/users/{seeded['viewer']}/reset-link"))
        .json()["url"]
        .split("token=")[1]
    )
    refused = await client.post(
        "/api/auth/reset-password", json={"token": token, "password": "abc"}
    )
    assert refused.status_code == 422


async def test_an_admin_cannot_issue_a_link_for_somebody_outside_their_teams(as_member, seeded):
    """404, not 403: "you may not touch that" confirms the account exists."""
    refused = await as_member.post(f"/api/admin/users/{seeded['other']}/reset-link")
    assert refused.status_code == 404


async def test_changing_your_own_password_needs_the_old_one(as_viewer, sessions, seeded):
    """A cookie left open on a shared machine should not be enough to take over."""
    before = (await _reload(sessions, seeded["viewer"])).password_hash
    refused = await as_viewer.post(
        "/api/auth/password",
        json={"current_password": "not-it-at-all", "password": "a-brand-new-password"},
    )
    assert refused.status_code == 400
    assert (await _reload(sessions, seeded["viewer"])).password_hash == before

    done = await as_viewer.post(
        "/api/auth/password",
        json={"current_password": "viewer-password", "password": "a-brand-new-password"},
    )
    assert done.status_code == 204
    assert verify_password(
        (await _reload(sessions, seeded["viewer"])).password_hash, "a-brand-new-password"
    )


async def test_a_password_change_voids_an_outstanding_reset_link(as_admin, client, seeded):
    """Every ``as_*`` fixture is the same client, so the sign-in is explicit here."""
    token = (
        (await as_admin.post(f"/api/admin/users/{seeded['viewer']}/reset-link"))
        .json()["url"]
        .split("token=")[1]
    )
    await _login(client, "watch@example.com", "viewer-password")
    await client.post(
        "/api/auth/password",
        json={"current_password": "viewer-password", "password": "chosen-by-the-owner"},
    )
    stale = await client.post(
        "/api/auth/reset-password", json={"token": token, "password": "chosen-by-somebody-else"}
    )
    assert stale.status_code == 400


def test_the_hash_is_what_makes_a_token_single_use():
    """The property the endpoint relies on, stated on its own."""
    original = hash_password("the-old-password")
    token = issue_reset("user-1", original)
    from app.security.reset import fingerprint

    assert read_reset(token)[1] == fingerprint(original)
    assert read_reset(token)[1] != fingerprint(hash_password("the-new-password"))
