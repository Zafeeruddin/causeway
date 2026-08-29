"""SSH forwards to cameras behind a jump host.

One multiplexed master connection per jump host **per network namespace**.
Forwards are added and removed at runtime against that master with
``-O forward`` / ``-O cancel``, so adding a camera never restarts anything and
never re-authenticates.

Every call takes the runner it must execute in, and there is deliberately no
default. A jump host behind a VPN is only routable from inside that profile's
namespace, and a forward binds ``127.0.0.1`` in whatever namespace ssh ran in --
which has to be the same one ffmpeg dials from, or the port is bound on a
loopback nobody is listening to. A manager-wide runner made both of those a
silent misconfiguration rather than a compile error, so it is gone.

The trade this makes: a dropped link takes every forward on that host down
together. At 15 streams that is the right side of it -- one reconnect brings
them all back. ROADMAP.md entry 7 has the plan for when it stops being.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from app.enums import SshAuth
from app.net.ports import Lease, PortPool
from app.net.runner import ProcResult, Runner

log = structlog.get_logger(__name__)


class SshError(RuntimeError):
    user_message = "Could not open the SSH connection to the jump host."


class SshAuthFailed(SshError):
    user_message = (
        "The jump host rejected these credentials. Check the username and password, "
        "or switch this profile to key authentication."
    )


class ForwardFailed(SshError):
    user_message = "The SSH connection is up but the port forward could not be opened."


@dataclass(slots=True)
class JumpHost:
    host: str
    port: int = 22
    username: str = ""
    auth: SshAuth = SshAuth.PASSWORD
    password: str = ""
    private_key: str = ""
    #: Accepted host key fingerprint, pinned after the first connection.
    known_host: str | None = None


@dataclass
class TunnelManager:
    """Owns the master connections and the forwards hanging off them."""

    control_dir: str = "/run/cam/ctl"
    pool: PortPool = field(default_factory=PortPool)
    _keys: dict[str, str] = field(default_factory=dict)
    _masters: set[str] = field(default_factory=set)

    # ---- master connection ---------------------------------------------

    def control_path(self, jump: JumpHost, runner: Runner) -> str:
        """One socket per jump host per namespace.

        Two profiles can name the same jump host and reach it by different
        paths -- one through a VPN, one already on the network. Sharing a master
        between them would send the second profile's forwards down the first
        profile's tunnel, and it would look like it worked.

        The name is hashed and short on purpose: a ControlPath lives in a
        ``sockaddr_un``, which caps at around 100 characters, and a long
        username plus an FQDN plus a namespace gets there. ssh's failure when it
        does is not obvious.
        """
        scope = getattr(runner, "namespace", "host")
        identity = f"{jump.username}@{jump.host}:{jump.port}|{scope}"
        digest = hashlib.sha256(identity.encode()).hexdigest()[:10]
        return str(Path(self.control_dir) / f"{_slug(jump.host)}-{_slug(scope)}-{digest}.sock")

    def _base_opts(self, jump: JumpHost, runner: Runner) -> list[str]:
        return [
            "-o",
            "BatchMode=no" if jump.auth is SshAuth.PASSWORD else "BatchMode=yes",
            # Detect a dead link in ~45s rather than waiting on TCP's own timers.
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            # Without this a failed forward leaves a connection that looks healthy
            # and silently carries nothing.
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={self.control_path(jump, runner)}",
            "-o",
            "ControlPersist=yes",
            "-p",
            str(jump.port),
        ]

    async def open_master(self, jump: JumpHost, runner: Runner, *, timeout: float = 25.0) -> None:
        """Establish the multiplexed connection, or raise with a usable message."""
        # Blocking filesystem call, once per master connection, off the hot path.
        Path(self.control_dir).mkdir(parents=True, exist_ok=True, mode=0o700)  # noqa: ASYNC240

        if await self.master_alive(jump, runner):
            return
        await self._clear_stale_socket(jump, runner)

        argv = ["ssh", *self._base_opts(jump, runner), "-fNT"]
        env = dict(os.environ)

        if jump.auth is SshAuth.KEY:
            key_path = self._write_key(jump)
            argv += ["-i", key_path, "-o", "IdentitiesOnly=yes"]
        else:
            # sshpass reads SSHPASS from the environment, so the password never
            # appears in argv and therefore never in /proc or a process listing.
            env["SSHPASS"] = jump.password
            argv = ["sshpass", "-e", *argv, "-o", "NumberOfPasswordPrompts=1"]

        argv.append(f"{jump.username}@{jump.host}")
        result = await runner.run(argv, timeout=timeout, env=env)
        if not result.ok:
            raise self._classify(result)

        self._masters.add(self.control_path(jump, runner))
        log.info(
            "ssh.master.open",
            host=jump.host,
            user=jump.username,
            via=runner.name,
            socket=self.control_path(jump, runner),
        )

    async def _clear_stale_socket(self, jump: JumpHost, runner: Runner) -> None:
        """Remove a control socket whose master is no longer behind it.

        The control directory is a volume and outlives the process that made it;
        the ssh master is a child process and does not. A restart therefore
        leaves a socket file with nothing listening, and ssh will not bind a
        ControlPath that already exists -- it says "already exists, disabling
        multiplexing" and connects anyway. So the dial *succeeds*, gate 5 passes,
        and every later ``-O forward`` against that socket is refused: the
        failure lands on the port forward and points at the wrong hop entirely.

        Only reached once ``master_alive`` has said no, so there is nothing here
        to take away from anyone.
        """
        path = Path(self.control_path(jump, runner))
        try:
            await asyncio.to_thread(path.unlink)
        except FileNotFoundError:
            return
        except OSError as exc:  # noqa: BLE001 - a socket we cannot remove is ssh's problem to report
            log.warning("ssh.master.stale_socket_kept", socket=str(path), error=str(exc))
            return
        log.info("ssh.master.stale_socket_removed", socket=str(path))

    async def master_alive(self, jump: JumpHost, runner: Runner) -> bool:
        result = await runner.run(
            [
                "ssh",
                "-o",
                f"ControlPath={self.control_path(jump, runner)}",
                "-O",
                "check",
                f"{jump.username}@{jump.host}",
            ],
            timeout=8.0,
        )
        return result.ok

    async def close_master(self, jump: JumpHost, runner: Runner) -> None:
        await runner.run(
            [
                "ssh",
                "-o",
                f"ControlPath={self.control_path(jump, runner)}",
                "-O",
                "exit",
                f"{jump.username}@{jump.host}",
            ],
            timeout=8.0,
        )
        self._masters.discard(self.control_path(jump, runner))
        log.info("ssh.master.closed", host=jump.host)

    # ---- forwards ------------------------------------------------------

    async def forward(
        self, jump: JumpHost, runner: Runner, owner: str, target_host: str, target_port: int
    ) -> Lease:
        """Lease a local port and bind ``local -> target`` on the existing master.

        ``runner`` must be the one the recorder will use for this profile: the
        port is bound on the loopback of that namespace and nowhere else.
        """
        existing = self.pool.get(owner)
        if existing is not None:
            return existing

        lease = await self.pool.lease(owner, f"{target_host}:{target_port}")
        spec = f"{lease.port}:{target_host}:{target_port}"
        result = await runner.run(
            [
                "ssh",
                "-o",
                f"ControlPath={self.control_path(jump, runner)}",
                "-O",
                "forward",
                "-L",
                spec,
                f"{jump.username}@{jump.host}",
            ],
            timeout=15.0,
        )
        if not result.ok:
            await self.pool.release(lease.port)
            raise ForwardFailed(f"could not forward {spec}: {result.output.strip()[:300]}")
        log.info("ssh.forward.open", spec=spec, owner=owner, via=runner.name)
        return lease

    async def cancel(self, jump: JumpHost, runner: Runner, owner: str) -> None:
        lease = self.pool.get(owner)
        if lease is None:
            return
        spec = f"{lease.port}:{lease.target}"
        await runner.run(
            [
                "ssh",
                "-o",
                f"ControlPath={self.control_path(jump, runner)}",
                "-O",
                "cancel",
                "-L",
                spec,
                f"{jump.username}@{jump.host}",
            ],
            timeout=10.0,
        )
        await self.pool.release(lease.port)
        log.info("ssh.forward.closed", spec=spec, owner=owner)

    # ---- helpers -------------------------------------------------------

    def _write_key(self, jump: JumpHost) -> str:
        cached = self._keys.get(jump.host)
        if cached and Path(cached).exists():
            return cached
        path = Path(self.control_dir) / f"id-{jump.username}-{jump.host}"
        path.write_text(jump.private_key.rstrip() + "\n")
        path.chmod(0o600)
        self._keys[jump.host] = str(path)
        return str(path)

    @staticmethod
    def _classify(result: ProcResult) -> SshError:
        text = result.output.lower()
        if "permission denied" in text or "authentication failed" in text:
            return SshAuthFailed(result.output.strip()[:300])
        if "could not resolve" in text or "name or service not known" in text:
            return SshError(
                f"the jump host name could not be resolved: {result.output.strip()[:200]}"
            )
        if "connection refused" in text:
            return SshError("the jump host refused the connection on this port.")
        if "connection timed out" in text or "operation timed out" in text:
            return SshError("the jump host did not answer - it may not be routable from the VPN.")
        if "host key verification failed" in text:
            return SshError("the jump host's key changed since we last connected.")
        return SshError(result.output.strip()[:300] or "ssh exited without explanation")


def _slug(value: str, limit: int = 18) -> str:
    return re.sub(r"[^A-Za-z0-9.-]", "_", value)[:limit] or "x"


async def wait_for_port(host: str, port: int, *, timeout: float = 5.0) -> bool:
    """TCP connect check -- used by the gates and after opening a forward."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (TimeoutError, OSError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True
