"""Credentials are redacted where a URL is *built*, not where it is logged.

Logging-time redaction fails the moment someone adds a log line that the
redactor does not know about. A type whose ``__str__`` and ``__repr__`` are
already safe cannot leak through an f-string, a traceback, a ``repr()`` in a
debugger, or a Pydantic model dump.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

MASK = "***"

#: user:pass@ inside any URL authority.
_USERINFO = re.compile(r"//[^/@\s]*:[^/@\s]*@")


def redact(text: str) -> str:
    """Mask userinfo in any URLs found in free text. A backstop, not the defence."""
    return _USERINFO.sub(f"//{MASK}:{MASK}@", text)


@dataclass(frozen=True)
class StreamUrl:
    """A stream URL that knows its own credentials and refuses to print them.

    Build one with :meth:`build`, pass it around freely, and call
    :meth:`expose` only at the point of handing it to a subprocess.
    """

    #: Credential-free form, safe to display, store and log.
    safe: str
    _username: str | None = None
    _password: str | None = None

    @classmethod
    def build(cls, url: str, username: str | None = None, password: str | None = None) -> StreamUrl:
        parts = urlsplit(url)
        host = parts.hostname or ""
        # Credentials embedded in the URL win only if none were passed separately.
        username = username or parts.username
        password = password or parts.password
        netloc = f"[{host}]" if ":" in host else host
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        safe = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        return cls(safe=safe, _username=username, _password=password)

    def expose(self) -> str:
        """The real URL, credentials included. Call this at the subprocess boundary."""
        if not self._username:
            return self.safe
        parts = urlsplit(self.safe)
        auth = quote(self._username, safe="")
        if self._password:
            auth += ":" + quote(self._password, safe="")
        return urlunsplit(
            (parts.scheme, f"{auth}@{parts.netloc}", parts.path, parts.query, parts.fragment)
        )

    def with_host(self, host: str, port: int) -> StreamUrl:
        """Repoint at a forwarded local port, keeping the path and credentials."""
        parts = urlsplit(self.safe)
        safe = urlunsplit((parts.scheme, f"{host}:{port}", parts.path, parts.query, parts.fragment))
        return StreamUrl(safe=safe, _username=self._username, _password=self._password)

    @property
    def has_credentials(self) -> bool:
        return bool(self._username)

    def __str__(self) -> str:
        return self.safe

    def __repr__(self) -> str:
        marker = " +creds" if self.has_credentials else ""
        return f"StreamUrl({self.safe!r}{marker})"
