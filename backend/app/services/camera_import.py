"""Adding cameras: one at a time, a pasted block, or a CSV.

All three land in the same normalizer, so a camera added by hand and a camera
added from a spreadsheet are the same object with the same validation. The
import always reports what it did rather than silently succeeding: pasting
forty lines and being told "40 added" when six were duplicates and two were
malformed is how a camera list quietly rots.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import structlog

from app.enums import SourceKind
from app.security.redaction import decoded_userinfo

log = structlog.get_logger(__name__)

DEFAULT_PORTS = {"rtsp": 554, "rtsps": 322, "http": 80, "https": 443}

#: Column names we accept, lowercased and stripped of spaces/underscores.
COLUMN_ALIASES: dict[str, str] = {
    "name": "name",
    "camera": "name",
    "cameraname": "name",
    "title": "name",
    "label": "name",
    "id": "ref",
    "cameraid": "ref",
    "camid": "ref",
    "ref": "ref",
    "reference": "ref",
    "channel": "ref",
    "assettag": "ref",
    "serial": "ref",
    "rtsp": "rtsp_url",
    "rtspurl": "rtsp_url",
    "url": "rtsp_url",
    "streamurl": "rtsp_url",
    "rtsplink": "rtsp_url",
    "link": "rtsp_url",
    "hls": "hls_url",
    "hlsurl": "hls_url",
    "m3u8": "hls_url",
    "m3u8url": "hls_url",
    "hlslink": "hls_url",
    "playlist": "hls_url",
    "location": "location",
    "site": "location",
    "area": "location",
    "place": "location",
    "username": "username",
    "user": "username",
    "login": "username",
    "password": "password",
    "pass": "password",
    "pwd": "password",
}


@dataclass(slots=True)
class ParsedSource:
    kind: SourceKind
    #: Credential-free. Credentials are split out into their own fields.
    url: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def dedupe_key(self) -> str:
        path = urlsplit(self.url).path or "/"
        return f"{self.kind.value}:{self.host}:{self.port}{path}"


@dataclass(slots=True)
class ParsedCamera:
    name: str
    #: The identifier this camera has in the customer's own world -- an NVR
    #: channel, an asset tag, a number painted on the housing. Kept apart from
    #: the name because they answer different questions: the name is what a
    #: person calls it, the ref is what they will be given over the radio.
    ref: str = ""
    location: str = ""
    sources: list[ParsedSource] = field(default_factory=list)

    @property
    def dedupe_key(self) -> str:
        primary = next((s for s in self.sources if s.kind is SourceKind.RTSP), None)
        return (primary or self.sources[0]).dedupe_key if self.sources else self.name.lower()


@dataclass(slots=True)
class ImportIssue:
    line: int
    value: str
    reason: str


@dataclass(slots=True)
class ImportReport:
    cameras: list[ParsedCamera] = field(default_factory=list)
    duplicates: list[ImportIssue] = field(default_factory=list)
    rejected: list[ImportIssue] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = [f"{len(self.cameras)} ready"]
        if self.duplicates:
            parts.append(f"{len(self.duplicates)} duplicate")
        if self.rejected:
            parts.append(f"{len(self.rejected)} rejected")
        return ", ".join(parts)


class InvalidStreamUrl(ValueError):
    pass


def parse_source_url(raw: str) -> ParsedSource:
    """Split a stream URL into address, path and credentials.

    Credentials embedded in the URL are pulled out here rather than left in the
    stored string, so the database never holds a password in a URL column. They
    are percent-decoded on the way out: what is stored is the password someone
    typed, not the escaping their NVR applied to it when it wrote the URL.
    """
    raw = raw.strip()
    if not raw:
        raise InvalidStreamUrl("empty")

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise InvalidStreamUrl(
            f"{scheme or 'no'} is not a stream URL - expected rtsp://, rtsps:// or https://"
        )
    if not parts.hostname:
        raise InvalidStreamUrl("no host in the URL")

    kind = SourceKind.RTSP if scheme.startswith("rtsp") else SourceKind.HLS
    if kind is SourceKind.HLS and ".m3u8" not in parts.path.lower():
        raise InvalidStreamUrl("an HLS source must point at an .m3u8 playlist")

    port = parts.port or DEFAULT_PORTS[scheme]
    host = parts.hostname
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    clean = f"{scheme}://{netloc}{parts.path}"
    if parts.query:
        clean += f"?{parts.query}"

    return ParsedSource(
        kind=kind,
        url=clean,
        host=host,
        port=port,
        username=decoded_userinfo(parts.username) or "",
        password=decoded_userinfo(parts.password) or "",
    )


def default_name_for(source: ParsedSource, index: int, ref: str = "") -> str:
    """A name people can recognise, derived from what we were given.

    An address alone is a poor name -- a list of them is unreadable, and
    ``10.244.116.70`` tells nobody which door it is pointing at. So the ref goes
    first when there is one, and the address stays as the part that makes it
    unique. Anything better than this comes from the person importing, which is
    why both fields exist.
    """
    path = urlsplit(source.url).path.strip("/")
    tail = path.split("/")[-1] if path else ""
    stream = tail if tail and not tail.isdigit() and "." not in tail else ""

    parts = [p for p in (ref.strip(), source.host, stream) if p]
    return " · ".join(parts) if parts else f"camera {index}"


def parse_pasted(text: str) -> ImportReport:
    """One stream per line. ``ref, name, url`` and ``name, url`` are both
    honoured, and a bare URL is fine.

    Deliberately forgiving about separators -- people paste out of Slack, out of
    a terminal, and out of a spreadsheet cell, and all three arrive differently.
    """
    report = ImportReport()
    seen: dict[str, int] = {}

    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip().strip(",;")
        if not line or line.startswith("#"):
            continue

        ref, name, url = _split_fields(line)
        try:
            source = parse_source_url(url)
        except InvalidStreamUrl as exc:
            report.rejected.append(ImportIssue(number, _shorten(line), str(exc)))
            continue

        camera = ParsedCamera(
            name=name or default_name_for(source, number, ref),
            ref=ref,
            sources=[source],
        )
        if camera.dedupe_key in seen:
            report.duplicates.append(
                ImportIssue(number, _shorten(url), f"same stream as line {seen[camera.dedupe_key]}")
            )
            continue
        seen[camera.dedupe_key] = number
        report.cameras.append(camera)

    return report


def parse_csv(content: str | bytes) -> ImportReport:
    """A CSV with a header row. Column names are matched loosely -- ``rtsp``,
    ``rtsp_url``, ``RTSP URL`` and ``Stream URL`` all mean the same thing."""
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="replace")

    report = ImportReport()
    try:
        dialect = csv.Sniffer().sniff(content[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # type: ignore[assignment]

    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    if not reader.fieldnames:
        report.rejected.append(ImportIssue(0, "", "the file has no header row"))
        return report

    mapping = {name: COLUMN_ALIASES.get(_normalise(name), "") for name in reader.fieldnames}
    if "rtsp_url" not in mapping.values() and "hls_url" not in mapping.values():
        report.rejected.append(
            ImportIssue(
                0,
                ", ".join(reader.fieldnames),
                "no stream URL column found - name one of them rtsp_url or hls_url",
            )
        )
        return report

    seen: dict[str, int] = {}
    for number, row in enumerate(reader, start=2):  # header is line 1
        fields = {
            mapping[k]: (v or "").strip() for k, v in row.items() if k in mapping and mapping[k]
        }
        camera = ParsedCamera(
            name=fields.get("name", ""),
            ref=fields.get("ref", ""),
            location=fields.get("location", ""),
        )

        for column, kind in (("rtsp_url", SourceKind.RTSP), ("hls_url", SourceKind.HLS)):
            raw = fields.get(column, "")
            if not raw:
                continue
            try:
                source = parse_source_url(raw)
            except InvalidStreamUrl as exc:
                report.rejected.append(ImportIssue(number, _shorten(raw), str(exc)))
                continue
            if source.kind is not kind:
                report.rejected.append(
                    ImportIssue(number, _shorten(raw), f"this is not a {kind.value} URL")
                )
                continue
            # Row-level credentials apply to any source that has none of its own.
            source.username = source.username or fields.get("username", "")
            source.password = source.password or fields.get("password", "")
            camera.sources.append(source)

        if not camera.sources:
            continue
        if not camera.name:
            camera.name = default_name_for(camera.sources[0], number, camera.ref)
        if camera.dedupe_key in seen:
            report.duplicates.append(
                ImportIssue(number, camera.name, f"same stream as row {seen[camera.dedupe_key]}")
            )
            continue
        seen[camera.dedupe_key] = number
        report.cameras.append(camera)

    return report


def _split_fields(line: str) -> tuple[str, str, str]:
    """Split ``[ref,] [name,] url`` into its three parts.

    One field before the URL is a name, which is what this accepted before and
    what most pasted lists look like. Two are a ref and a name, in that order,
    because that is the order they appear in every NVR export we have seen. A
    bare URL is still fine.
    """
    match = re.search(r"(?P<scheme>rtsps?|https?)://", line, re.I)
    if match is None:
        return "", "", line
    prefix = line[: match.start()].strip()
    url = line[match.start() :].strip()
    if not prefix.strip(",;\t| "):
        return "", "", url

    fields = [part.strip() for part in re.split(r"[,;\t|]", prefix)]
    # Exactly one trailing empty: the separator sitting between the last field
    # and the URL. Interior empties are kept, because they are how someone says
    # "an id and no name" -- `CAM-14,,rtsp://...` -- and dropping them all would
    # turn that into a camera named CAM-14 with no id at all.
    if fields and not fields[-1]:
        fields.pop()

    if len(fields) >= 2:
        return fields[0], " ".join(f for f in fields[1:] if f), url
    return "", fields[0], url


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _shorten(value: str, limit: int = 80) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"
