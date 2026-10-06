"""Password-reset tokens that carry their own state.

A reset link is normally a row in a table: issue it, store the hash, mark it
used. That table would need a migration, and this schema has none yet
(ROADMAP entry 10), so a reset would be a feature you cannot deploy to the
instance already running.

So the token carries what a row would have held. It is signed with the app
secret and contains three things: who it is for, when it stops working, and a
fingerprint of the password hash it was issued against. Redemption checks the
fingerprint against the hash in the database *now*, which buys two properties
for free:

* **Single use.** Redeeming it replaces the hash, so the fingerprint no longer
  matches and the same link cannot be replayed.
* **Superseded by any other change.** An administrator issuing a second link,
  or the person remembering their password and changing it, invalidates every
  link outstanding for that account.

What it does not buy is revocation without a password change, and there is no
"cancel this link" -- the answer to a link sent in error is to issue another
one, which voids the first. The window is short for the same reason.
"""

from __future__ import annotations

import hmac
import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from hashlib import sha256

from app.config import settings

#: Long enough to walk to a desk, short enough that a link in a mailbox someone
#: else can read is not a standing key to the account.
RESET_TTL_SECONDS = 60 * 60


def fingerprint(password_hash: str) -> str:
    """A short, keyed digest of a password hash.

    Keyed rather than bare so the token never carries anything derived from the
    stored hash that an attacker could work backwards from offline.
    """
    mac = hmac.new(_key(), password_hash.encode(), sha256)
    return urlsafe_b64encode(mac.digest()).decode().rstrip("=")[:22]


def issue_reset(user_id: str, password_hash: str, *, ttl: int = RESET_TTL_SECONDS) -> str:
    payload = json.dumps(
        {"sub": user_id, "exp": int(time.time()) + ttl, "fp": fingerprint(password_hash)},
        separators=(",", ":"),
    ).encode()
    body = urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{_sign(body)}"


def read_reset(token: str) -> tuple[str, str] | None:
    """``(user_id, fingerprint)`` if the token is well-formed and unexpired.

    Says nothing about whether it has been used -- that is the fingerprint's
    job, and only the caller has the current hash to compare it against.
    """
    body, _, signature = token.partition(".")
    if not signature or not hmac.compare_digest(signature, _sign(body)):
        return None
    try:
        payload = json.loads(urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    user_id, fp = payload.get("sub"), payload.get("fp")
    if not isinstance(user_id, str) or not isinstance(fp, str):
        return None
    return user_id, fp


def _sign(body: str) -> str:
    # Domain-separated from the session signature. The two are different
    # capabilities with different lifetimes, and one secret signing both means a
    # session cookie and a reset link are interchangeable to anyone who can make
    # the server parse the wrong one.
    mac = hmac.new(_key(), b"reset:" + body.encode(), sha256)
    return urlsafe_b64encode(mac.digest()).decode().rstrip("=")


def _key() -> bytes:
    return settings().app_secret_key.encode()
