"""Asking the agent to do something, and getting the answer back.

Events already flow one way over Redis: agents publish, the dashboard reads. A
few operations need the other direction. Connecting a profile is the one that
forced it -- network namespaces belong to a container, so the API cannot create
one, cannot use a forward the agent opened, and cannot dial a VPN at all. The
request has to cross to the process that owns the network.

This is request/response over pub/sub rather than a queue, and that is the
deliberate part. ``PUBLISH`` reports how many subscribers received the message,
so an API with no agent behind it fails immediately with "the recorder agent is
not running" instead of timing out two minutes later -- or, worse than either,
queueing a connect that executes ten minutes after the person gave up.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as redis
import structlog

from app.config import settings

log = structlog.get_logger(__name__)

COMMAND_CHANNEL = "cam:commands"

#: How long each command may take, in seconds. Connecting walks the whole gate
#: ladder, and a VPN dial alone is allowed 45 seconds, so its ceiling is high on
#: purpose: the alternative is giving up on a dial that was about to succeed.
TIMEOUTS: dict[str, float] = {
    "profile.connect": 240.0,
    "profile.trust": 240.0,
    "camera.test_source": 150.0,
    "profile.disconnect": 60.0,
    "agent.ping": 3.0,
}
DEFAULT_TIMEOUT = 60.0


def reply_channel(command_id: str) -> str:
    return f"cam:reply:{command_id}"


class CommandError(RuntimeError):
    """Base for every way a command can fail to produce an answer. Carries a
    message written for the person who pressed the button."""

    user_message = "The request could not be completed."


class NoAgent(CommandError):
    user_message = "The recorder agent is not running, so nothing can dial or record right now."


class CommandTimeout(CommandError):
    user_message = "The recorder agent did not answer in time."


class CommandFailed(CommandError):
    """The agent ran the command and it raised."""

    def __init__(self, message: str) -> None:
        self.user_message = message
        super().__init__(message)


@dataclass(slots=True)
class Command:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_json(self) -> str:
        return json.dumps({"id": self.id, "name": self.name, "payload": self.payload})

    @classmethod
    def from_json(cls, raw: str | bytes) -> Command:
        data = json.loads(raw)
        return cls(name=data["name"], payload=data.get("payload") or {}, id=data["id"])


@dataclass(slots=True)
class Reply:
    id: str
    ok: bool = True
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {"id": self.id, "ok": self.ok, "payload": self.payload, "error": self.error}
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> Reply:
        data = json.loads(raw)
        return cls(
            id=data["id"],
            ok=bool(data.get("ok")),
            payload=data.get("payload") or {},
            error=data.get("error") or "",
        )


Handler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class CommandBus:
    def __init__(self, url: str | None = None) -> None:
        self._url = url or settings().redis_url
        self._client: redis.Redis | None = None

    async def client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.from_url(self._url, decode_responses=True)
        return self._client

    async def call(
        self, name: str, payload: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Send a command and wait for its answer.

        The reply channel is subscribed *before* the command goes out. Publishing
        first would leave a fast agent's answer arriving before anyone was
        listening for it, which reads as a timeout and is impossible to
        reproduce on a loaded machine.
        """
        command = Command(name=name, payload=payload or {})
        deadline = timeout or TIMEOUTS.get(name, DEFAULT_TIMEOUT)
        client = await self.client()
        pubsub = client.pubsub()
        await pubsub.subscribe(reply_channel(command.id))
        try:
            receivers = await client.publish(COMMAND_CHANNEL, command.to_json())
            if not receivers:
                raise NoAgent(f"no agent is listening on {COMMAND_CHANNEL}")
            log.debug("command.sent", command=name, id=command.id)
            reply = await self._await_reply(pubsub, deadline)
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()

        if not reply.ok:
            raise CommandFailed(reply.error or "the recorder agent could not do that")
        return reply.payload

    async def _await_reply(self, pubsub: Any, deadline: float) -> Reply:
        loop = asyncio.get_running_loop()
        until = loop.time() + deadline
        while True:
            left = until - loop.time()
            if left <= 0:
                raise CommandTimeout(f"no reply within {deadline:.0f}s")
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=left)
            if message is None:
                continue
            try:
                return Reply.from_json(message["data"])
            except (ValueError, TypeError, KeyError):
                log.warning("command.reply_unparseable", raw=str(message.get("data"))[:200])

    async def serve(self, handler: Handler) -> None:
        """Run commands until cancelled. One task per command, so a slow dial
        does not hold up a disconnect behind it."""
        client = await self.client()
        pubsub = client.pubsub()
        await pubsub.subscribe(COMMAND_CHANNEL)
        log.info("command.serving", channel=COMMAND_CHANNEL)
        running: set[asyncio.Task] = set()
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30.0)
                if message is None:
                    continue
                try:
                    command = Command.from_json(message["data"])
                except (ValueError, TypeError, KeyError):
                    log.warning("command.unparseable", raw=str(message.get("data"))[:200])
                    continue
                task = asyncio.create_task(self._dispatch(handler, command))
                running.add(task)
                task.add_done_callback(running.discard)
        finally:
            for task in running:
                task.cancel()
            await pubsub.unsubscribe()
            await pubsub.aclose()

    async def _dispatch(self, handler: Handler, command: Command) -> None:
        log.info("command.received", command=command.name, id=command.id)
        try:
            payload = await handler(command.name, command.payload)
            reply = Reply(id=command.id, ok=True, payload=payload)
        except Exception as exc:  # noqa: BLE001 - the caller is owed an answer either way
            log.exception("command.failed", command=command.name)
            reply = Reply(id=command.id, ok=False, error=_message_for(exc))

        client = await self.client()
        await client.publish(reply_channel(command.id), reply.to_json())

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _message_for(exc: BaseException) -> str:
    """What the person who pressed the button should read.

    Domain errors carry their own wording and it is better than anything we
    could substitute. Everything else is infrastructure -- a dead database, a
    socket error -- and its text is an address and an errno, which belongs in
    the agent's log and not in a dialog. The type name stays as a hint for
    whoever reads both.
    """
    written = getattr(exc, "user_message", None)
    if written:
        return str(written)
    return f"the recorder agent could not complete that request ({type(exc).__name__})"


_bus: CommandBus | None = None


def command_bus() -> CommandBus:
    global _bus
    if _bus is None:
        _bus = CommandBus()
    return _bus


def set_command_bus(bus: CommandBus) -> None:
    global _bus
    _bus = bus
