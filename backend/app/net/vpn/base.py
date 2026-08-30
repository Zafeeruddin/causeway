"""The VPN driver contract.

Four operations, and the discipline to keep it at four. Every VPN we support --
and every one we might be forced onto later, including a FortiClient build that
satisfies endpoint posture, or a remote agent on a compliant host -- is a new
implementation of this class rather than a change to the gate ladder, the tunnel
manager or the recorder. ROADMAP.md entry 1 depends on that staying true.

The interesting case is not failure, it is *interruption*: a dial that stops and
waits for a human. Today that is a certificate the user has not trusted yet.
Tomorrow it is an MFA push. Both raise :class:`InteractionRequired`, so the
supervisor already knows not to burn its redial budget on them.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from app.enums import VpnKind
from app.net.runner import Runner

log = structlog.get_logger(__name__)

#: Where the kernel lists the tty line disciplines it knows about. pppd sets the
#: PPP discipline on its tty, and the discipline is registered by ``ppp_async``
#: -- a module a container cannot load for itself, because module autoloading
#: needs privileges no container has.
LDISCS = Path("/proc/tty/ldiscs")


def ppp_available() -> bool:
    """Whether pppd can actually work on this host.

    ``/dev/ppp`` existing is necessary and not sufficient: that is
    ``ppp_generic``, which is often built into the kernel, while the line
    discipline pppd needs comes from ``ppp_async``, which is usually not loaded
    until something asks for it. Checking the device alone passes on a host
    where every dial will fail.
    """
    try:
        return any(line.split()[:1] == ["ppp"] for line in LDISCS.read_text().splitlines())
    except OSError:
        # No /proc to read: assume it works rather than refusing to try.
        return True


#: Devices the kernel creates in a namespace whether or not anything uses them.
#: ``tunl0`` is the one that bites: it exists in every namespace the moment the
#: namespace does, and its name starts with "tun".
PLACEHOLDER_IFACES = frozenset(
    {"tunl0", "sit0", "gre0", "gretap0", "erspan0", "ip6tnl0", "ip6gre0", "ip_vti0", "ip6_vti0"}
)


# ---- failures ----------------------------------------------------------


class VpnError(RuntimeError):
    """Base for everything a dial can go wrong with."""

    #: Shown to the user verbatim. Say what happened and what to do about it.
    user_message = "The VPN connection failed."


class AuthFailed(VpnError):
    user_message = "The gateway refused the connection - check the username and password."


class PostureFailed(VpnError):
    """Gateway wants endpoint compliance we cannot satisfy. See ROADMAP.md entry 1."""

    user_message = (
        "The gateway requires an endpoint compliance check this client cannot pass. "
        "This profile needs the full FortiClient agent."
    )


class DialTimeout(VpnError):
    user_message = "The gateway did not respond in time."


class HostMisconfigured(VpnError):
    """The dial cannot work until something changes on the machine.

    Distinct from every other failure here because nothing about the profile,
    the credentials or the gateway is wrong -- retrying will fail identically
    until an operator acts, so the message has to name the action.
    """

    def __init__(self, message: str) -> None:
        self.user_message = message
        super().__init__(message)


class ClientExited(VpnError):
    """The client stopped before the tunnel came up, saying nothing we know how
    to classify.

    Whatever it said last is the only lead there is, so it travels on the
    message. The generic "The VPN connection failed." is true of every rung on
    this ladder and sends the reader looking in the wrong place -- a client that
    rejected its own arguments and a gateway that hung up read identically.
    """

    def __init__(self, tail: Sequence[str]) -> None:
        reason = _last_complaint(tail)
        self.user_message = (
            f"The VPN client stopped without connecting: {reason}"
            if reason
            else "The VPN client stopped without connecting."
        )
        super().__init__(f"{self.user_message}\n" + "\n".join(tail[-40:]))


def _last_complaint(tail: Sequence[str]) -> str:
    """The most recent line that reads like a complaint, without its prefix."""
    for line in reversed(tail):
        text = line.strip()
        if not text:
            continue
        if match := re.match(r"(?:ERROR|FATAL|WARN(?:ING)?):\s*(.+)", text, re.I):
            return match.group(1)[:200]
        if re.search(r"unrecognized|invalid|no such|not found|refused|failed", text, re.I):
            return text[:200]
    return ""


class InteractionRequired(VpnError):
    """Dial is paused waiting on a person. Not a failure - do not retry it."""

    user_message = "This connection needs your confirmation before it can continue."


@dataclass(eq=False)
class TrustPromptRequired(InteractionRequired):
    """The gateway presented a certificate we have not pinned yet.

    This is the dialog FortiClient shows on the desktop. We ask the same question
    and store the answer on the profile so it is asked exactly once.
    """

    host: str
    fingerprint: str
    algorithm: str = "sha256"
    reason: str = "certificate verification failed"

    def __post_init__(self) -> None:
        super().__init__(self.user_message)

    @property
    def user_message(self) -> str:  # type: ignore[override]
        return (
            f"{self.host} presented a certificate that is not trusted yet "
            f"({self.reason}). Fingerprint {self.algorithm.upper()} {self.fingerprint}."
        )


# ---- data --------------------------------------------------------------


@dataclass(slots=True)
class VpnConfig:
    """Resolved dial parameters. Built by the service layer from sealed refs, so
    plaintext lives in memory for the length of one dial and is never persisted."""

    kind: VpnKind
    gateway: str = ""
    port: int = 443
    username: str = ""
    password: str = ""
    realm: str = ""
    #: Pinned certificate digest, once the user has accepted it.
    trusted_cert: str | None = None
    #: WireGuard only: the full interface config.
    wg_config: str = ""
    extra_args: list[str] = field(default_factory=list)


@dataclass(slots=True)
class VpnStatus:
    up: bool
    detail: str = ""
    #: Address assigned inside the tunnel, when we can determine it.
    tunnel_ip: str | None = None
    interface: str | None = None


# ---- driver ------------------------------------------------------------


class VpnDriver(abc.ABC):
    """Dial, watch, hang up. That is the whole surface."""

    kind: VpnKind
    #: Interface name prefixes this driver's tunnel shows up as.
    iface_prefixes: tuple[str, ...] = ("tun", "ppp")

    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self._proc: asyncio.subprocess.Process | None = None
        self._pump: asyncio.Task[None] | None = None
        self._mux: _LineMux | None = None
        self._log_tail: list[str] = []

    @abc.abstractmethod
    async def dial(self, cfg: VpnConfig, *, timeout: float = 45.0) -> VpnStatus:
        """Bring the tunnel up, or raise. Must be safe to call again after failure."""

    async def health(self) -> VpnStatus:
        """Cheap liveness check, called on the heartbeat."""
        if self._proc is not None and self._proc.returncode is not None:
            return VpnStatus(up=False, detail=f"client exited ({self._proc.returncode})")
        iface = await self._find_interface()
        if iface is None:
            return VpnStatus(up=False, detail="no tunnel interface")
        return VpnStatus(up=True, interface=iface, tunnel_ip=await self._interface_ip(iface))

    async def hangup(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        await self._close_mux()
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 10.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()

    # ---- shared helpers ------------------------------------------------

    @property
    def log_tail(self) -> str:
        """Last lines of client output, for the failure message. Never shown raw
        to the user -- it can contain the gateway's own error text."""
        return "\n".join(self._log_tail[-40:])

    async def _find_interface(self) -> str | None:
        """The tunnel device, if one is actually carrying traffic.

        Two things make a bare prefix match wrong. The kernel puts a placeholder
        device in *every* new namespace -- ``tunl0``, which begins with "tun" --
        so matching on the name alone finds a tunnel in a namespace that has no
        VPN in it at all, and :meth:`health` then reports up. Nothing downstream
        survives that: ``SourcePath.open`` trusts health to decide whether to
        redial, so it skips the redial and the SSH forward fails against a
        master that was never opened, three layers from the cause.

        A device that is down is also not a tunnel, whatever it is called.
        """
        result = await self.runner.run(["ip", "-o", "link", "show"], timeout=5.0)
        for line in result.stdout.splitlines():
            match = re.match(r"\d+:\s+([^:@]+)[^:]*:\s+<([^>]*)>", line)
            if match is None:
                continue
            name, flags = match.group(1).strip(), match.group(2).split(",")
            if name in PLACEHOLDER_IFACES or not name.startswith(self.iface_prefixes):
                continue
            if "UP" not in flags:
                continue
            return name
        return None

    async def _interface_ip(self, iface: str) -> str | None:
        result = await self.runner.run(
            ["ip", "-4", "-o", "addr", "show", "dev", iface], timeout=5.0
        )
        match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", result.stdout)
        return match.group(1) if match else None

    async def _read_until(
        self,
        proc: asyncio.subprocess.Process,
        *,
        timeout: float,
        classify,
    ) -> VpnStatus:
        """Consume client output until ``classify`` returns a verdict.

        ``classify(line, tail)`` returns a :class:`VpnStatus` for success, raises
        a :class:`VpnError` for a known failure, or returns ``None`` to keep reading.
        """
        mux = _LineMux(proc)
        self._mux = mux

        async def pump() -> VpnStatus:
            while True:
                line = await mux.readline()
                if line is None:
                    raise ClientExited(self._log_tail)
                text = line.rstrip()
                if text:
                    self._log_tail.append(text)
                    log.debug("vpn.line", kind=str(self.kind), line=text)
                verdict = classify(text, self._log_tail)
                if verdict is not None:
                    return verdict

        try:
            return await asyncio.wait_for(pump(), timeout)
        except TimeoutError as exc:
            await self._close_mux()
            raise DialTimeout(
                f"no response from the gateway after {timeout:.0f}s.\n{self.log_tail}"
            ) from exc
        except BaseException:
            await self._close_mux()
            raise
        # On success the multiplexer stays open and is handed to the background drain.

    async def _close_mux(self) -> None:
        if self._mux is not None:
            await self._mux.aclose()
            self._mux = None

    def _start_background_pump(self, proc: asyncio.subprocess.Process) -> None:
        """Keep draining output after a successful dial, so the pipe never fills
        and blocks the client. A full stderr pipe is a classic silent hang."""
        mux = self._mux or _LineMux(proc)

        async def drain() -> None:
            while True:
                line = await mux.readline()
                if line is None:
                    return
                text = line.rstrip()
                if text:
                    self._log_tail.append(text)
                    del self._log_tail[:-200]

        self._pump = asyncio.create_task(drain())


class _LineMux:
    """Interleaves stdout and stderr into one line queue.

    One long-lived reader task per stream. Reading through a queue rather than
    racing two ``readline()`` calls matters: cancelling a readline mid-line
    discards whatever it had already buffered, which loses exactly the error
    text we need to classify a failed dial.
    """

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        streams = [s for s in (proc.stderr, proc.stdout) if s is not None]
        self._open = len(streams)
        self._tasks = [asyncio.create_task(self._feed(s)) for s in streams]

    async def _feed(self, stream: asyncio.StreamReader) -> None:
        try:
            while True:
                data = await stream.readline()
                if not data:
                    break
                await self._queue.put(data.decode(errors="replace"))
        finally:
            await self._queue.put(None)

    async def readline(self) -> str | None:
        """Next line, or ``None`` once every stream has hit EOF."""
        while self._open > 0:
            item = await self._queue.get()
            if item is None:
                self._open -= 1
                continue
            return item
        return None

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
