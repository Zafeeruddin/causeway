"""API behaviour, with team scoping as the thing most worth proving.

Scoping is the requirement that fails quietly: everything works in testing when
the tester is an admin, and the hole only shows up when two teams share a host.
"""

from __future__ import annotations

import io

import pytest

from app.enums import ProfileState, ReachMode, VpnKind

pytestmark = pytest.mark.asyncio


# ---- auth --------------------------------------------------------------


async def test_login_returns_the_teams_you_belong_to(client, seeded):
    response = await client.post(
        "/api/auth/login", json={"email": "qa@example.com", "password": "member-password"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "qa@example.com"
    assert [t["slug"] for t in body["teams"]] == ["acme"]


async def test_an_admin_sees_every_team(as_admin):
    body = (await as_admin.get("/api/auth/me")).json()
    assert {t["slug"] for t in body["teams"]} == {"acme", "ops"}


async def test_a_wrong_password_does_not_say_which_half_was_wrong(client, seeded):
    wrong_password = await client.post(
        "/api/auth/login", json={"email": "qa@example.com", "password": "nope"}
    )
    no_such_user = await client.post(
        "/api/auth/login", json={"email": "ghost@example.com", "password": "nope"}
    )
    assert wrong_password.status_code == no_such_user.status_code == 401
    assert wrong_password.json()["detail"] == no_such_user.json()["detail"]


async def test_unauthenticated_requests_are_refused(client):
    assert (await client.get("/api/profiles")).status_code == 401


# ---- profiles ----------------------------------------------------------


def _profile_body(team_id: str, **overrides):
    body = {
        "team_id": team_id,
        "name": "Camera VPN",
        "mode": ReachMode.VPN_JUMP.value,
        "vpn_kind": VpnKind.FORTINET.value,
        "vpn_gateway": "vpn.example.com",
        "vpn_username": "zafeer",
        "vpn_password": "hunter2",
        "jump_host": "10.20.30.71",
        "jump_username": "ops",
        "jump_password": "jump-secret",
    }
    body.update(overrides)
    return body


async def test_creating_a_profile_never_echoes_its_credentials(as_member, seeded):
    response = await as_member.post("/api/profiles", json=_profile_body(seeded["acme"]))
    assert response.status_code == 201
    raw = response.text
    assert "hunter2" not in raw
    assert "jump-secret" not in raw

    body = response.json()
    assert body["has_vpn_password"] is True
    assert body["has_jump_credentials"] is True
    assert body["state"] == ProfileState.IDLE.value


async def test_a_gateway_pasted_with_its_port_is_split_into_the_two_fields(as_member, seeded):
    """FortiClient labels this "Remote Gateway" and shows it as host:port, so
    host:port is what gets pasted. Stored whole it is not a hostname, and the
    failure surfaces two layers down as "Name or service not known"."""
    response = await as_member.post(
        "/api/profiles",
        json=_profile_body(seeded["acme"], vpn_gateway="82.197.58.159:20443"),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["vpn_gateway"] == "82.197.58.159"
    assert body["vpn_port"] == 20443


async def test_pasted_credentials_lose_the_whitespace_they_arrived_with(as_member, seeded):
    """A leading space in a VPN username is invisible in the form and comes back
    from the gateway as "check the username and password" -- which sends the
    reader to look at the password, where nothing is wrong."""
    response = await as_member.post(
        "/api/profiles",
        json=_profile_body(
            seeded["acme"],
            vpn_gateway="  82.197.58.159:20443 ",
            vpn_username=" Master-works",
            jump_host="10.20.30.71\n",
            jump_username="ubuntu ",
        ),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["vpn_username"] == "Master-works"
    assert body["vpn_gateway"] == "82.197.58.159"
    assert body["vpn_port"] == 20443, "the port survived the surrounding spaces"
    assert body["jump_host"] == "10.20.30.71"
    assert body["jump_username"] == "ubuntu"


async def test_an_edit_trims_too(as_member, seeded, direct_profile):
    response = await as_member.patch(
        f"/api/profiles/{direct_profile}", json={"vpn_username": "  someone  "}
    )
    assert response.status_code == 200
    assert response.json()["vpn_username"] == "someone"


async def test_a_gateway_without_a_port_keeps_the_one_it_was_given(as_member, seeded):
    response = await as_member.post(
        "/api/profiles",
        json=_profile_body(seeded["acme"], vpn_gateway="vpn.example.com", vpn_port=10443),
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["vpn_gateway"], body["vpn_port"]) == ("vpn.example.com", 10443)


async def test_an_ipv6_gateway_is_not_mistaken_for_a_host_and_port(as_member, seeded):
    response = await as_member.post(
        "/api/profiles",
        json=_profile_body(seeded["acme"], vpn_gateway="2001:db8::1"),
    )
    assert response.status_code == 201
    assert response.json()["vpn_gateway"] == "2001:db8::1"


async def test_a_mode_that_needs_a_vpn_refuses_to_be_created_without_one(as_member, seeded):
    response = await as_member.post(
        "/api/profiles",
        json=_profile_body(seeded["acme"], vpn_kind=VpnKind.NONE.value),
    )
    assert response.status_code == 422
    assert "needs a VPN type" in response.text


async def test_a_jump_mode_requires_a_jump_host(as_member, seeded):
    response = await as_member.post(
        "/api/profiles", json=_profile_body(seeded["acme"], jump_host="")
    )
    assert response.status_code == 422
    assert "jump host address" in response.text


async def test_you_cannot_create_a_profile_in_a_team_you_are_not_in(as_member, seeded):
    response = await as_member.post("/api/profiles", json=_profile_body(seeded["ops"]))
    assert response.status_code == 404


async def test_profiles_are_scoped_to_your_teams(as_member, as_other, seeded, client):
    """The heart of it: ACME's profile must be invisible to Ops, because the
    profile carries the credentials that reach ACME's cameras."""
    await client.post(
        "/api/auth/login", json={"email": "qa@example.com", "password": "member-password"}
    )
    created = await client.post("/api/profiles", json=_profile_body(seeded["acme"]))
    profile_id = created.json()["id"]

    await client.post(
        "/api/auth/login", json={"email": "ops@example.com", "password": "other-password"}
    )
    assert (await client.get("/api/profiles")).json() == []
    # Same answer as "does not exist" -- confirming it exists is a disclosure.
    assert (await client.get(f"/api/profiles/{profile_id}")).status_code == 404
    assert (await client.post(f"/api/profiles/{profile_id}/connect")).status_code == 404


async def test_an_admin_can_reach_every_teams_profiles(client, seeded):
    await client.post(
        "/api/auth/login", json={"email": "qa@example.com", "password": "member-password"}
    )
    await client.post("/api/profiles", json=_profile_body(seeded["acme"]))

    await client.post(
        "/api/auth/login", json={"email": "admin@example.com", "password": "admin-password"}
    )
    assert len((await client.get("/api/profiles")).json()) == 1


async def test_changing_the_gateway_drops_the_pinned_certificate(as_member, seeded):
    """It is a different server now; silently reusing the old pin defeats it."""
    created = await as_member.post("/api/profiles", json=_profile_body(seeded["acme"]))
    profile_id = created.json()["id"]

    await as_member.post(f"/api/profiles/{profile_id}/trust", json={"fingerprint": "ab" * 32})
    assert (await as_member.get(f"/api/profiles/{profile_id}")).json()["trusted_cert"]

    await as_member.patch(f"/api/profiles/{profile_id}", json={"vpn_gateway": "vpn2.example.com"})
    assert (await as_member.get(f"/api/profiles/{profile_id}")).json()["trusted_cert"] is None


async def test_a_fingerprint_is_normalised_before_it_is_pinned(as_member, seeded):
    """People paste fingerprints with colons, from a dialog that shows them."""
    created = await as_member.post("/api/profiles", json=_profile_body(seeded["acme"]))
    profile_id = created.json()["id"]
    await as_member.post(
        f"/api/profiles/{profile_id}/trust",
        json={"fingerprint": "AB:CD:" + ":".join(["ef"] * 30)},
    )
    pinned = (await as_member.get(f"/api/profiles/{profile_id}")).json()["trusted_cert"]
    assert pinned == "abcd" + "ef" * 30
    assert ":" not in pinned


async def test_a_direct_mode_profile_connects_with_every_gate_skipped(as_member, seeded, bus):
    """No VPN and no jump means nothing to check at the profile level -- and the
    skipped rungs are still reported, so it is visibly 'not needed'."""
    created = await as_member.post(
        "/api/profiles",
        json={
            "team_id": seeded["acme"],
            "name": "Already on the network",
            "mode": ReachMode.DIRECT.value,
        },
    )
    profile_id = created.json()["id"]

    response = await as_member.post(f"/api/profiles/{profile_id}/connect")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == ProfileState.UP.value
    assert {g["status"] for g in body["gates"]} == {"skipped"}
    assert body["action_required"] is None

    assert any(e.type == "gate" for e in bus.events), "gates should stream to the dashboard"
    assert any(e.type == "profile_state" for e in bus.events)


# ---- cameras -----------------------------------------------------------


@pytest.fixture
async def direct_profile(as_member, seeded):
    created = await as_member.post(
        "/api/profiles",
        json={"team_id": seeded["acme"], "name": "Direct", "mode": ReachMode.DIRECT.value},
    )
    return created.json()["id"]


async def test_adding_a_camera_strips_credentials_out_of_the_url(as_member, seeded, direct_profile):
    response = await as_member.post(
        "/api/cameras",
        json={
            "team_id": seeded["acme"],
            "profile_id": direct_profile,
            "name": "Gate",
            "sources": [
                {
                    "kind": "rtsp",
                    "url": "rtsp://admin:hunter2@10.20.30.42:554/Streaming/Channels/101",
                }
            ],
        },
    )
    assert response.status_code == 201
    assert "hunter2" not in response.text
    source = response.json()["sources"][0]
    assert source["url"] == "rtsp://10.20.30.42:554/Streaming/Channels/101"
    assert source["username"] == "admin"
    assert source["host"] == "10.20.30.42"
    assert source["port"] == 554


async def test_a_camera_can_carry_both_an_rtsp_and_an_hls_source(as_member, seeded, direct_profile):
    response = await as_member.post(
        "/api/cameras",
        json={
            "team_id": seeded["acme"],
            "profile_id": direct_profile,
            "name": "Gate",
            "sources": [
                {"kind": "rtsp", "url": "rtsp://10.20.30.42:554/s1"},
                {"kind": "hls", "url": "https://cdn.example.com/live/gate/index.m3u8"},
            ],
        },
    )
    assert response.status_code == 201
    assert {s["kind"] for s in response.json()["sources"]} == {"rtsp", "hls"}


async def test_a_url_that_is_not_the_declared_kind_is_rejected_clearly(
    as_member, seeded, direct_profile
):
    response = await as_member.post(
        "/api/cameras",
        json={
            "team_id": seeded["acme"],
            "profile_id": direct_profile,
            "name": "Gate",
            "sources": [{"kind": "hls", "url": "rtsp://10.20.30.42:554/s1"}],
        },
    )
    assert response.status_code == 422
    assert "is a rtsp stream, not hls" in response.text


async def test_pasting_a_block_reports_duplicates_and_rejects(as_member, seeded, direct_profile):
    response = await as_member.post(
        "/api/cameras/import",
        json={
            "team_id": seeded["acme"],
            "profile_id": direct_profile,
            "text": (
                "Gate, rtsp://10.20.30.41:554/s1\n"
                "rtsp://10.20.30.42:554/s1\n"
                "rtsp://10.20.30.42:554/s1\n"
                "definitely not a url\n"
            ),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 2
    assert len(body["duplicates"]) == 1
    assert len(body["rejected"]) == 1
    assert body["summary"] == "2 added, 1 duplicate, 1 rejected"


async def test_a_dry_run_writes_nothing(as_member, seeded, direct_profile):
    body = {
        "team_id": seeded["acme"],
        "profile_id": direct_profile,
        "text": "rtsp://10.20.30.41:554/s1",
        "dry_run": True,
    }
    response = await as_member.post("/api/cameras/import", json=body)
    assert response.json()["created"] == 0
    assert len(response.json()["cameras"]) == 1
    assert (await as_member.get("/api/cameras")).json() == []


async def test_csv_upload_creates_cameras_with_both_sources(as_member, seeded, direct_profile):
    csv = (
        "name,location,rtsp_url,hls_url,username,password\n"
        "Gate,North,rtsp://10.20.30.41:554/s1,https://cdn.example.com/live/gate/index.m3u8,admin,hunter2\n"
        "Lobby,South,rtsp://10.20.30.42:554/s1,,admin,hunter2\n"
    )
    response = await as_member.post(
        "/api/cameras/import/csv",
        params={"team_id": seeded["acme"], "profile_id": direct_profile},
        files={"file": ("cameras.csv", io.BytesIO(csv.encode()), "text/csv")},
    )
    assert response.status_code == 200
    assert response.json()["created"] == 2
    assert "hunter2" not in response.text

    cameras = (await as_member.get("/api/cameras")).json()
    gate = next(c for c in cameras if c["name"] == "Gate")
    assert {s["kind"] for s in gate["sources"]} == {"rtsp", "hls"}
    # The HLS feed is reachable directly even when the camera's RTSP is not.
    hls = next(s for s in gate["sources"] if s["kind"] == "hls")
    assert hls["uses_profile_path"] is False


async def test_re_importing_the_same_csv_adds_nothing_and_says_so(
    as_member, seeded, direct_profile
):
    csv = "name,rtsp_url\nGate,rtsp://10.20.30.41:554/s1\n"
    args = {
        "params": {"team_id": seeded["acme"], "profile_id": direct_profile},
    }
    await as_member.post(
        "/api/cameras/import/csv",
        files={"file": ("c.csv", io.BytesIO(csv.encode()), "text/csv")},
        **args,
    )
    second = await as_member.post(
        "/api/cameras/import/csv",
        files={"file": ("c.csv", io.BytesIO(csv.encode()), "text/csv")},
        **args,
    )
    assert second.json()["created"] == 0
    assert second.json()["duplicates"]


async def test_cameras_are_scoped_to_your_teams(client, seeded, as_member, direct_profile):
    await as_member.post(
        "/api/cameras",
        json={
            "team_id": seeded["acme"],
            "profile_id": direct_profile,
            "name": "Gate",
            "sources": [{"kind": "rtsp", "url": "rtsp://10.20.30.42:554/s1"}],
        },
    )
    await client.post(
        "/api/auth/login", json={"email": "ops@example.com", "password": "other-password"}
    )
    assert (await client.get("/api/cameras")).json() == []
    assert (await client.get("/api/cameras/page")).json()["total"] == 0


async def test_cameras_can_be_searched_and_paginated(as_member, seeded, direct_profile):
    cameras = [
        ("Zulu gate", "CAM-003", "South yard", "10.20.30.53"),
        ("Alpha lobby", "CAM-001", "Reception", "10.20.30.51"),
        ("Bravo dock", "CAM-002", "Loading bay", "10.20.30.52"),
    ]
    for name, ref, location, host in cameras:
        response = await as_member.post(
            "/api/cameras",
            json={
                "team_id": seeded["acme"],
                "profile_id": direct_profile,
                "name": name,
                "ref": ref,
                "location": location,
                "sources": [{"kind": "rtsp", "url": f"rtsp://{host}:554/s1"}],
            },
        )
        assert response.status_code == 201

    first = (await as_member.get("/api/cameras/page", params={"page_size": 2})).json()
    assert first["total"] == 3
    assert first["pages"] == 2
    assert first["page"] == 1
    assert [camera["name"] for camera in first["items"]] == ["Alpha lobby", "Bravo dock"]

    second = (await as_member.get("/api/cameras/page", params={"page": 2, "page_size": 2})).json()
    assert [camera["name"] for camera in second["items"]] == ["Zulu gate"]

    by_address = (await as_member.get("/api/cameras/page", params={"q": "10.20.30.52"})).json()
    assert by_address["total"] == 1
    assert by_address["items"][0]["ref"] == "CAM-002"

    literal_wildcard = (await as_member.get("/api/cameras/page", params={"q": "%"})).json()
    assert literal_wildcard["total"] == 0


async def test_a_camera_cannot_borrow_another_teams_profile(client, seeded, direct_profile):
    """Scoping cameras but not profiles would let Ops reach ACME's network."""
    await client.post(
        "/api/auth/login", json={"email": "ops@example.com", "password": "other-password"}
    )
    response = await client.post(
        "/api/cameras",
        json={
            "team_id": seeded["ops"],
            "profile_id": direct_profile,
            "name": "Sneaky",
            "sources": [{"kind": "rtsp", "url": "rtsp://10.20.30.42:554/s1"}],
        },
    )
    assert response.status_code == 422
    assert "different team" in response.text


# ---- storage and admission --------------------------------------------


async def test_storage_usage_starts_empty_and_reports_the_thresholds(as_member):
    body = (await as_member.get("/api/storage/usage")).json()
    assert body["used_bytes"] == 0
    assert body["state"] == "ok"
    assert body["warn_bytes"] < body["gc_bytes"] < body["hard_bytes"]


async def test_recording_more_cameras_than_allowed_is_refused(as_member, seeded):
    response = await as_member.post(
        "/api/recordings", json={"camera_ids": [f"c{i}" for i in range(6)], "seconds": 300}
    )
    assert response.status_code == 422
    assert "at most 5 cameras" in response.text


async def test_the_duration_is_bounded_by_the_configured_maximum(as_member):
    too_long = await as_member.post("/api/recordings", json={"camera_ids": ["c1"], "seconds": 1800})
    assert too_long.status_code == 422


async def test_the_estimate_endpoint_answers_before_anyone_presses_record(
    as_member, seeded, direct_profile
):
    created = await as_member.post(
        "/api/cameras",
        json={
            "team_id": seeded["acme"],
            "profile_id": direct_profile,
            "name": "Gate",
            "sources": [
                {"kind": "rtsp", "url": "rtsp://10.20.30.42:554/s1"},
                {"kind": "hls", "url": "https://cdn.example.com/live/gate/index.m3u8"},
            ],
        },
    )
    camera_id = created.json()["id"]
    body = (
        await as_member.post(
            "/api/recordings/estimate", json={"camera_ids": [camera_id], "seconds": 300}
        )
    ).json()
    assert body["allowed"] is True
    # Two sources, five minutes: about 340 MB.
    assert 300_000_000 < body["estimated_bytes"] < 400_000_000


async def test_a_profile_in_use_says_what_is_using_it(as_member, seeded, direct_profile):
    """The foreign key refuses this anyway. It refuses with an integrity error,
    which reaches the user as a 500 and names nothing they can act on."""
    await _a_camera(as_member, seeded, direct_profile, name="Held")

    response = await as_member.delete(f"/api/profiles/{direct_profile}")

    assert response.status_code == 409
    assert "1 camera still uses this profile" in response.text


async def test_a_profile_nothing_depends_on_can_go(as_member, seeded, direct_profile):
    response = await as_member.delete(f"/api/profiles/{direct_profile}")
    assert response.status_code == 204
    assert (await as_member.get(f"/api/profiles/{direct_profile}")).status_code == 404


async def test_a_viewer_cannot_remove_a_profile(as_member, seeded, direct_profile):
    await _login(as_member, "watch@example.com", "viewer-password")
    assert (await as_member.delete(f"/api/profiles/{direct_profile}")).status_code == 403


async def test_health_says_which_build_it_is(client):
    """Asked during every deployment, and the honest answer cannot come from an
    image tag -- `latest` moves, a version does not."""
    from app.version import __version__

    body = (await client.get("/api/health")).json()
    assert body["version"] == __version__


async def test_the_version_is_written_down_once():
    """Three files claiming three versions is the failure this guards. The
    Python package is the source; the others are copies that have to agree."""
    import json
    import pathlib
    import re

    from app.version import __version__

    root = pathlib.Path(__file__).resolve().parents[2]  # noqa: ASYNC240 - reading two small files
    pyproject = (root / "backend" / "pyproject.toml").read_text()
    assert re.search(rf'^version = "{re.escape(__version__)}"$', pyproject, re.M), (
        "backend/pyproject.toml disagrees with app/version.py"
    )

    package = json.loads((root / "frontend" / "package.json").read_text())
    assert package["version"] == __version__, "frontend/package.json disagrees"


# ---- who may do what ---------------------------------------------------


async def _login(client, email: str, password: str) -> None:
    """Swap identity on the shared client mid-test.

    The permission tests all have the same shape -- an administrator sets
    something up, and then someone else tries to touch it -- and both halves
    need the same app and database.
    """
    response = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200


async def _a_camera(client, seeded, profile_id: str, name: str = "Gate") -> str:
    created = await client.post(
        "/api/cameras",
        json={
            "team_id": seeded["acme"],
            "profile_id": profile_id,
            "name": name,
            "sources": [{"kind": "rtsp", "url": "rtsp://10.20.30.42:554/s1"}],
        },
    )
    assert created.status_code == 201
    return created.json()["id"]


async def test_a_viewer_sees_their_teams_cameras(as_member, seeded, direct_profile):
    """The whole point of the role: everything they need to watch a camera, and
    nothing about how the camera is reached."""
    camera_id = await _a_camera(as_member, seeded, direct_profile)
    await _login(as_member, "watch@example.com", "viewer-password")

    listed = await as_member.get("/api/cameras")
    assert listed.status_code == 200
    row = next(c for c in listed.json() if c["id"] == camera_id)
    # The profile's name travels on the camera, so the label survives without
    # the viewer being allowed anywhere near the profiles API.
    assert row["profile_name"] == "Direct"


async def test_a_viewer_may_not_change_the_cameras_they_watch(as_member, seeded, direct_profile):
    camera_id = await _a_camera(as_member, seeded, direct_profile)
    await _login(as_member, "watch@example.com", "viewer-password")

    assert (await as_member.delete(f"/api/cameras/{camera_id}")).status_code == 403
    assert (await as_member.post(f"/api/cameras/{camera_id}/test")).status_code == 403
    created = await as_member.post(
        "/api/cameras",
        json={
            "team_id": seeded["acme"],
            "profile_id": direct_profile,
            "name": "Sneaky",
            "sources": [{"kind": "rtsp", "url": "rtsp://10.20.30.43:554/s1"}],
        },
    )
    assert created.status_code == 403
    imported = await as_member.post(
        "/api/cameras/import",
        json={
            "team_id": seeded["acme"],
            "profile_id": direct_profile,
            "text": "rtsp://10.20.30.44:554/s1",
        },
    )
    assert imported.status_code == 403


async def test_a_viewer_cannot_reach_connection_profiles_at_all(as_member, seeded, direct_profile):
    """Not read-only -- absent. A profile is gateways, jump hosts and a dial
    that holds a VPN open for the whole team."""
    await _login(as_member, "watch@example.com", "viewer-password")

    assert (await as_member.get("/api/profiles")).status_code == 403
    assert (await as_member.get(f"/api/profiles/{direct_profile}")).status_code == 403
    assert (await as_member.post(f"/api/profiles/{direct_profile}/connect")).status_code == 403
    assert (await as_member.delete(f"/api/profiles/{direct_profile}")).status_code == 403


async def test_a_viewer_may_still_record_and_take_the_file(as_member, seeded, direct_profile):
    """Watching, recording and downloading are the job. Withholding them would
    leave the role with nothing to do."""
    camera_id = await _a_camera(as_member, seeded, direct_profile)
    await _login(as_member, "watch@example.com", "viewer-password")

    estimate = await as_member.post(
        "/api/recordings/estimate", json={"camera_ids": [camera_id], "seconds": 60}
    )
    assert estimate.status_code == 200
    started = await as_member.post(
        "/api/recordings", json={"camera_ids": [camera_id], "seconds": 60}
    )
    assert started.status_code == 202


async def test_an_admin_administers_their_own_teams_and_no_others(as_member, seeded):
    """`may_administer` says they administer something; it never says which."""
    other_team = seeded["ops"]

    made = await as_member.post(
        "/api/profiles",
        json={"team_id": other_team, "name": "Theirs", "mode": ReachMode.DIRECT.value},
    )
    assert made.status_code == 404, "an admin reached into a team they are not in"

    added = await as_member.post(
        f"/api/admin/teams/{other_team}/members", json={"user_id": seeded["viewer"]}
    )
    assert added.status_code == 404


async def test_an_admin_sees_only_their_own_teams_and_people(as_member, as_admin, seeded):
    everything = await as_admin.get("/api/admin/teams")
    assert {t["slug"] for t in everything.json()} == {"acme", "ops"}

    await _login(as_admin, "qa@example.com", "member-password")
    mine = await as_admin.get("/api/admin/teams")
    assert {t["slug"] for t in mine.json()} == {"acme"}

    people = await as_admin.get("/api/admin/users")
    assert {u["email"] for u in people.json()} == {
        "qa@example.com",
        "watch@example.com",
        "demo@example.com",
    }


async def test_an_admin_cannot_create_an_account_above_their_own_reach(as_member, seeded):
    """Otherwise the shortest path to superadmin is to make one."""
    promoted = await as_member.post(
        "/api/admin/users",
        json={
            "email": "climber@example.com",
            "password": "a-long-enough-password",
            "role": "superadmin",
            "team_ids": [seeded["acme"]],
        },
    )
    assert promoted.status_code == 403

    homeless = await as_member.post(
        "/api/admin/users",
        json={
            "email": "nowhere@example.com",
            "password": "a-long-enough-password",
            "role": "viewer",
        },
    )
    assert homeless.status_code == 422, "an account its creator could not then see"

    elsewhere = await as_member.post(
        "/api/admin/users",
        json={
            "email": "theirs@example.com",
            "password": "a-long-enough-password",
            "role": "viewer",
            "team_ids": [seeded["ops"]],
        },
    )
    assert elsewhere.status_code == 404

    fine = await as_member.post(
        "/api/admin/users",
        json={
            "email": "mine@example.com",
            "password": "a-long-enough-password",
            "role": "viewer",
            "team_ids": [seeded["acme"]],
        },
    )
    assert fine.status_code == 201
    assert fine.json()["role"] == "viewer"


# ---- admin -------------------------------------------------------------


async def test_a_viewer_reaches_no_admin_route(as_viewer):
    assert (await as_viewer.get("/api/admin/users")).status_code == 403
    assert (await as_viewer.get("/api/admin/teams")).status_code == 403


async def test_an_admin_can_create_a_team_and_add_a_member(as_admin, seeded):
    team = await as_admin.post(
        "/api/admin/teams", json={"name": "Field", "slug": "field", "description": ""}
    )
    assert team.status_code == 201
    team_id = team.json()["id"]

    added = await as_admin.post(
        f"/api/admin/teams/{team_id}/members", json={"user_id": seeded["member"]}
    )
    assert added.status_code == 204
    # Idempotent: adding twice is not an error.
    assert (
        await as_admin.post(
            f"/api/admin/teams/{team_id}/members", json={"user_id": seeded["member"]}
        )
    ).status_code == 204

    teams = (await as_admin.get("/api/admin/teams")).json()
    assert next(t for t in teams if t["slug"] == "field")["member_count"] == 1


async def test_a_duplicate_team_slug_is_a_conflict_not_a_crash(as_admin):
    body = {"name": "Field", "slug": "field", "description": ""}
    assert (await as_admin.post("/api/admin/teams", json=body)).status_code == 201
    second = await as_admin.post("/api/admin/teams", json={**body, "name": "Field Two"})
    assert second.status_code == 409


async def test_creating_a_user_never_returns_a_password_hash(as_admin):
    response = await as_admin.post(
        "/api/admin/users",
        json={"email": "new@example.com", "password": "a-long-enough-password", "role": "viewer"},
    )
    assert response.status_code == 201
    assert "password" not in response.text.lower()
