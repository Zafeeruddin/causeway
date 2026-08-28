"""The API asking the agent to do what it cannot do itself.

The point of these is that the endpoint's answer does not change. Whether the
gate ladder ran in this process or in the agent, the dashboard gets the same
response, and the loopback bus below exercises both sides of the wire format to
prove it -- the API's deserializer against the agent's serializer, with no
mocking in between.
"""

from __future__ import annotations

import pytest

from app.agent.commands import ConnectionCommands
from app.enums import GateStatus, ProfileState, ReachMode
from app.gates.ladder import GateResult
from app.services.commands import Command, CommandBus, CommandFailed, NoAgent, Reply
from app.services.connections import ConnectOutcome
from app.services.gateway import RemoteGateway, outcome_from_payload, outcome_payload


class LoopbackBus(CommandBus):
    """The command bus with Redis taken out.

    Calls the agent's handler in-process, through the same JSON the real bus
    would carry, so a field either side forgets shows up here rather than in
    compose.
    """

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name, payload=None, *, timeout=None):
        self.calls.append((name, payload or {}))
        command = Command.from_json(Command(name=name, payload=payload or {}).to_json())
        result = await self.handler(command.name, command.payload)
        return Reply.from_json(Reply(id=command.id, payload=result).to_json()).payload

    async def close(self) -> None:
        return None


class DeadBus(CommandBus):
    """No agent behind it. What the compose stack looks like with the agent
    container stopped."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or NoAgent("no agent is listening")

    async def call(self, name, payload=None, *, timeout=None):
        raise self.error

    async def close(self) -> None:
        return None


def to_agent_mode(app, sessions, bus, *, command_bus=None):
    """Rewire the API the way compose runs it: no namespaces of its own, every
    connection made by asking."""
    connections = app.state.gateway
    handler = ConnectionCommands(
        connections=connections,
        sessions=sessions,
        status=lambda: {"netns_available": True, "recordings": 0},
    ).handle
    loopback = command_bus or LoopbackBus(handler)
    app.state.netns = None
    app.state.ports = None
    app.state.gateway = RemoteGateway(loopback)
    return loopback


@pytest.fixture
async def direct_profile(as_member, seeded):
    created = await as_member.post(
        "/api/profiles",
        json={"team_id": seeded["acme"], "name": "Direct", "mode": ReachMode.DIRECT.value},
    )
    return created.json()["id"]


# ---- the wire format ----------------------------------------------------


def test_an_outcome_survives_the_round_trip():
    outcome = ConnectOutcome(
        attempt_id="a-1",
        results=[
            GateResult(
                key="vpn_dial",
                index=1,
                title="VPN dial",
                status=GateStatus.BLOCKED,
                message="the gateway presented an unknown certificate",
                detail={"fingerprint": "ab" * 32},
                duration_ms=1200,
            )
        ],
        state=ProfileState.NEEDS_INTERACTION,
    )

    restored = outcome_from_payload(outcome_payload(outcome))

    assert restored == outcome
    # And the thing the route reads off it still works.
    assert restored.blocked_on is not None
    assert restored.blocked_on.detail == {"fingerprint": "ab" * 32}


def test_commands_and_replies_are_json():
    command = Command.from_json(Command(name="profile.connect", payload={"a": 1}).to_json())
    assert command.name == "profile.connect" and command.payload == {"a": 1}

    reply = Reply.from_json(Reply(id="1", ok=False, error="nope").to_json())
    assert not reply.ok and reply.error == "nope"


# ---- connecting through the agent ---------------------------------------


async def test_connecting_through_the_agent_answers_exactly_as_in_process(
    as_member, app, sessions, bus, direct_profile
):
    loopback = to_agent_mode(app, sessions, bus)

    response = await as_member.post(f"/api/profiles/{direct_profile}/connect")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == ProfileState.UP.value
    assert {g["status"] for g in body["gates"]} == {"skipped"}
    assert body["action_required"] is None
    assert [name for name, _ in loopback.calls] == ["profile.connect"]
    # The gates still stream to the dashboard: they are published by the
    # ConnectionService, wherever it happens to be running.
    assert any(e.type == "gate" for e in bus.events)


async def test_the_profile_row_reflects_what_the_agent_did(
    as_member, app, sessions, bus, direct_profile
):
    """The agent writes state in its own transaction, so the route has to
    re-read the row rather than answer from the copy it loaded first."""
    to_agent_mode(app, sessions, bus)

    await as_member.post(f"/api/profiles/{direct_profile}/connect")
    profile = (await as_member.get(f"/api/profiles/{direct_profile}")).json()

    assert profile["state"] == ProfileState.UP.value
    assert profile["last_connected_at"] is not None


async def test_disconnecting_goes_to_the_agent_too(as_member, app, sessions, bus, direct_profile):
    loopback = to_agent_mode(app, sessions, bus)

    await as_member.post(f"/api/profiles/{direct_profile}/connect")
    response = await as_member.post(f"/api/profiles/{direct_profile}/disconnect")

    assert response.status_code == 200
    assert response.json()["state"] == ProfileState.IDLE.value
    assert [name for name, _ in loopback.calls] == ["profile.connect", "profile.disconnect"]


async def test_the_certificate_is_pinned_by_the_agent_before_it_redials(
    as_member, app, sessions, bus, direct_profile
):
    """The pin has to be committed before the dial that reads it. Writing it in
    the API's still-open transaction would have the agent redial against the
    certificate the user was asked about."""
    loopback = to_agent_mode(app, sessions, bus)

    response = await as_member.post(
        f"/api/profiles/{direct_profile}/trust", json={"fingerprint": "AB" * 32}
    )

    assert response.status_code == 200
    assert [name for name, _ in loopback.calls] == ["profile.trust", "profile.connect"]
    profile = (await as_member.get(f"/api/profiles/{direct_profile}")).json()
    assert profile["trusted_cert"] == "ab" * 32


async def test_testing_a_camera_goes_to_the_agent(as_member, app, sessions, bus, direct_profile):
    created = await as_member.post(
        "/api/cameras",
        json={
            "team_id": (await as_member.get("/api/profiles")).json()[0]["team_id"],
            "profile_id": direct_profile,
            "name": "Gate 1",
            "sources": [{"kind": "hls", "url": "https://hls.example/s.m3u8"}],
        },
    )
    camera_id = created.json()["id"]
    loopback = to_agent_mode(app, sessions, bus)

    response = await as_member.post(f"/api/cameras/{camera_id}/test")

    assert response.status_code == 200
    assert [name for name, _ in loopback.calls] == ["camera.test_source"]


# ---- when the agent is not there ----------------------------------------


async def test_no_agent_is_a_503_that_says_so(as_member, app, sessions, bus, direct_profile):
    to_agent_mode(app, sessions, bus, command_bus=DeadBus())

    response = await as_member.post(f"/api/profiles/{direct_profile}/connect")

    assert response.status_code == 503
    assert "recorder agent is not running" in response.json()["detail"]


async def test_a_command_the_agent_refuses_comes_back_in_its_own_words(
    as_member, app, sessions, bus, direct_profile
):
    """The agent's message is what the user reads. A refusal must not arrive as
    a generic 500 with the reason in a log file somewhere else."""
    to_agent_mode(
        app,
        sessions,
        bus,
        command_bus=DeadBus(CommandFailed("the VPN client is not installed on the agent")),
    )

    response = await as_member.post(f"/api/profiles/{direct_profile}/connect")

    assert response.status_code == 503
    assert response.json()["detail"] == "the VPN client is not installed on the agent"


async def test_health_reports_the_agent_rather_than_this_process(as_member, app, sessions, bus):
    to_agent_mode(app, sessions, bus)

    body = (await as_member.get("/api/health")).json()

    assert body["connect_mode"] == "agent"
    assert body["agent"]["up"] is True
    assert body["netns_available"] is True


async def test_health_says_so_when_the_agent_is_gone(as_member, app, sessions, bus):
    to_agent_mode(app, sessions, bus, command_bus=DeadBus())

    body = (await as_member.get("/api/health")).json()

    assert body["agent"]["up"] is False
    assert body["netns_available"] is False


async def test_an_infrastructure_failure_does_not_put_an_errno_in_a_dialog():
    """Domain errors are written for people and pass through. A dead socket is
    an address and an errno: it belongs in the agent's log, and the caller gets
    something they can repeat to someone else."""
    from app.services.commands import Command, Reply, _message_for

    class Domain(Exception):
        user_message = "the VPN gateway rejected these credentials"

    assert _message_for(Domain()) == "the VPN gateway rejected these credentials"

    raw = _message_for(OSError("[Errno 111] Connect call failed ('10.0.0.9', 5432)"))
    assert "10.0.0.9" not in raw
    assert "OSError" in raw

    # And the envelope still carries it intact.
    envelope = Reply(id=Command(name="x").id, ok=False, error=raw)
    assert Reply.from_json(envelope.to_json()).error == raw
