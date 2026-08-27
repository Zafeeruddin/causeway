"""Where credentials live.

Application code never holds ciphertext and never holds a key -- it holds an
opaque ``ref`` string and asks the backend to resolve it. Swapping the local
sealed store for the company Vault is a new implementation of this interface
plus a migration script that reads through one and writes through the other.
See ROADMAP.md entry 3.
"""

from __future__ import annotations

import base64
from typing import Protocol, runtime_checkable

from nacl import exceptions as nacl_exc
from nacl.secret import SecretBox

from app.config import settings

#: Bumped if the sealing format changes, so old refs stay resolvable.
LOCAL_SCHEME = "local:v1:"


class SecretsError(RuntimeError):
    pass


class SecretNotFound(SecretsError):
    pass


@runtime_checkable
class SecretsBackend(Protocol):
    """Three methods. Resist adding a fourth -- every one of them has to be
    implemented again for Vault, and for whatever comes after Vault."""

    async def put(self, value: str, *, hint: str = "") -> str:
        """Store ``value``; return an opaque ref safe to write to the database."""

    async def get(self, ref: str) -> str:
        """Resolve a ref back to its plaintext."""

    async def delete(self, ref: str) -> None:
        """Forget a ref. Must be idempotent."""


class SealedColumnBackend:
    """Local default: NaCl SecretBox, key supplied as a Docker secret.

    The ref *is* the ciphertext, so there is no second store to keep in sync and
    deleting the database row deletes the secret. The cost is that key rotation
    means rewriting every ref -- acceptable while the whole system is one host.
    """

    def __init__(self, key: bytes | None = None) -> None:
        if key is None:
            key = self._key_from_settings()
        if len(key) != SecretBox.KEY_SIZE:
            raise SecretsError(f"sealing key must be {SecretBox.KEY_SIZE} bytes")
        self._box = SecretBox(key)

    @staticmethod
    def _key_from_settings() -> bytes:
        cfg = settings()
        if cfg.secrets_key:
            return base64.b64decode(cfg.secrets_key)
        if not cfg.is_dev:
            raise SecretsError(
                "SECRETS_KEY is unset. Generate one with: "
                'python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"'
            )
        # Dev only, and deliberately constant so a restart doesn't orphan local data.
        return b"cam-dashboard-development-key-32"

    async def put(self, value: str, *, hint: str = "") -> str:
        sealed = self._box.encrypt(value.encode())
        return LOCAL_SCHEME + base64.b64encode(sealed).decode()

    async def get(self, ref: str) -> str:
        if not ref.startswith(LOCAL_SCHEME):
            raise SecretNotFound(f"ref is not a local secret: {ref.split(':', 1)[0]}:...")
        try:
            sealed = base64.b64decode(ref.removeprefix(LOCAL_SCHEME))
            return self._box.decrypt(sealed).decode()
        except (nacl_exc.CryptoError, ValueError) as exc:
            raise SecretsError(
                "could not open a sealed secret -- SECRETS_KEY has probably changed"
            ) from exc

    async def delete(self, ref: str) -> None:
        # The ciphertext is the ref; dropping the row is the delete.
        return None


class MemoryBackend:
    """For tests. Keeps plaintext in a dict and hands out uuid-shaped refs."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._n = 0

    async def put(self, value: str, *, hint: str = "") -> str:
        self._n += 1
        ref = f"mem:v1:{self._n:08d}"
        self._store[ref] = value
        return ref

    async def get(self, ref: str) -> str:
        try:
            return self._store[ref]
        except KeyError as exc:
            raise SecretNotFound(ref) from exc

    async def delete(self, ref: str) -> None:
        self._store.pop(ref, None)


_backend: SecretsBackend | None = None


def secrets_backend() -> SecretsBackend:
    global _backend
    if _backend is None:
        _backend = SealedColumnBackend()
    return _backend


def set_secrets_backend(backend: SecretsBackend) -> None:
    """Swap the backend -- used by tests, and by the Vault migration when it lands."""
    global _backend
    _backend = backend
