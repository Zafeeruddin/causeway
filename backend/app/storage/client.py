"""S3 client, pointed at the hosted Versity gateway.

Versity Gateway speaks the S3 API over a POSIX filesystem, which makes it a
drop-in for the object operations we use -- put, get, presign, delete, list --
but it is *not* AWS S3 and two absences change how the rest of the system has to
behave:

* **No lifecycle rules.** Nothing on the server side will ever expire an object
  for us. Retention is entirely :mod:`app.storage.retention`'s job, and if that
  job stops running, nothing else deletes anything.
* **No bucket quotas.** There is no server-side ceiling to catch a bug in our
  own accounting, so the admission check before each recording is the only thing
  standing between a runaway job and the org's storage.

Addressing is path-style. Versity serves ``https://s3.example.com/<bucket>/<key>``;
boto3 would otherwise try the virtual-host form ``<bucket>.s3.example.com`` and get
a DNS failure that looks nothing like a configuration problem.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import boto3
import structlog
from boto3.exceptions import Boto3Error
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings
from app.storage.keys import content_type_for

log = structlog.get_logger(__name__)

#: Recordings are 150 MB - 1 GB, so multipart matters. Versity supports it.
MULTIPART_THRESHOLD = 16 * 1024**2
MULTIPART_CHUNK = 16 * 1024**2


class StorageError(RuntimeError):
    user_message = "The recording store could not be reached."


class BucketMissing(StorageError):
    user_message = "The recordings bucket does not exist on the storage gateway yet."


@dataclass(slots=True)
class StoredObject:
    key: str
    bytes: int
    content_type: str


class ObjectStore:
    """Thin async wrapper over boto3. Every call runs on a worker thread --
    boto3 is synchronous and would otherwise stall the event loop mid-upload."""

    def __init__(
        self,
        *,
        endpoint_url: str | None = None,
        bucket: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str | None = None,
        addressing_style: str | None = None,
        connect_timeout: float = 10,
        read_timeout: float = 120,
        retries: dict[str, object] | None = None,
    ) -> None:
        # The defaults are sized for moving recordings. A caller asking a
        # question rather than uploading -- the health probe -- passes its own,
        # because a loading screen cannot wait two minutes to hear no.
        cfg = settings()
        self.bucket = bucket or cfg.s3_bucket
        self.endpoint_url = endpoint_url or cfg.s3_endpoint_url
        self._client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=access_key or cfg.s3_access_key,
            aws_secret_access_key=secret_key or cfg.s3_secret_key,
            region_name=region or cfg.s3_region,
            config=Config(
                # Versity serves path-style; virtual-host style resolves a
                # hostname that does not exist.
                s3={"addressing_style": addressing_style or cfg.s3_addressing_style},
                signature_version="s3v4",
                retries=retries or {"max_attempts": 3, "mode": "standard"},
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            ),
        )

    # ---- lifecycle -----------------------------------------------------

    async def check(self) -> bool:
        """Confirm the bucket exists and our credentials can see it."""
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self.bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchBucket"):
                raise BucketMissing(
                    f"bucket {self.bucket!r} does not exist at {self.endpoint_url}. "
                    "Create it with: versitygw admin create-bucket"
                ) from exc
            if code in ("403", "AccessDenied"):
                raise StorageError(
                    f"these credentials cannot access {self.bucket!r}. "
                    "Check the access key and its bucket ownership on the gateway."
                ) from exc
            raise StorageError(f"storage gateway returned {code or exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"could not reach {self.endpoint_url}: {exc}") from exc
        return True

    # ---- objects -------------------------------------------------------

    async def put_file(self, path: Path | str, key: str) -> StoredObject:
        path = Path(path)
        size = path.stat().st_size
        content_type = content_type_for(path.name)
        try:
            await asyncio.to_thread(
                self._client.upload_file,
                str(path),
                self.bucket,
                key,
                ExtraArgs={"ContentType": content_type},
                Config=boto3.s3.transfer.TransferConfig(
                    multipart_threshold=MULTIPART_THRESHOLD,
                    multipart_chunksize=MULTIPART_CHUNK,
                ),
            )
        except (ClientError, BotoCoreError, Boto3Error) as exc:
            # ``upload_file`` does not raise what the rest of boto3 raises: a
            # refusal from the gateway comes back as S3UploadFailedError, which
            # is a Boto3Error and neither a ClientError nor a BotoCoreError.
            # Catching only those two let a missing bucket escape put_file
            # uncaught, and an uncaught exception here strands the recording.
            raise _upload_failed(key, exc) from exc
        log.info("storage.put", key=key, bytes=size)
        return StoredObject(key=key, bytes=size, content_type=content_type)

    async def delete(self, keys: list[str]) -> int:
        """Delete up to 1000 keys. Returns how many the gateway confirmed."""
        if not keys:
            return 0
        deleted = 0
        for batch in (keys[i : i + 1000] for i in range(0, len(keys), 1000)):
            try:
                response = await asyncio.to_thread(
                    self._client.delete_objects,
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
                )
            except (ClientError, BotoCoreError) as exc:
                raise StorageError(f"delete failed: {exc}") from exc
            errors = response.get("Errors") or []
            for error in errors:
                log.warning("storage.delete.failed", key=error.get("Key"), reason=error.get("Code"))
            deleted += len(batch) - len(errors)
        log.info("storage.delete", requested=len(keys), deleted=deleted)
        return deleted

    async def presign(self, key: str, *, expires: int = 3600, filename: str | None = None) -> str:
        """A time-limited download link. The browser talks to Versity directly,
        so recordings never stream back through the API."""
        params: dict[str, object] = {"Bucket": self.bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        try:
            return await asyncio.to_thread(
                self._client.generate_presigned_url,
                "get_object",
                Params=params,
                ExpiresIn=expires,
            )
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"could not sign a link for {key}: {exc}") from exc

    async def measure_prefix(self, prefix: str) -> tuple[int, int]:
        """Bytes and object count under ``prefix``.

        Used to reconcile against our own accounting, not on the hot path: the
        ``storage_objects`` table is the source of truth for usage precisely so
        that the admission check never depends on a full bucket listing.
        """
        total = count = 0
        token: str | None = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            try:
                page = await asyncio.to_thread(self._client.list_objects_v2, **kwargs)
            except (ClientError, BotoCoreError) as exc:
                raise StorageError(f"listing {prefix} failed: {exc}") from exc
            for item in page.get("Contents", []):
                total += item.get("Size", 0)
                count += 1
            if not page.get("IsTruncated"):
                return total, count
            token = page.get("NextContinuationToken")


def _upload_failed(key: str, exc: Exception) -> StorageError:
    """Say which of the two upload failures this was.

    A bucket that is not there and a gateway that is not answering both stop an
    upload, and the person reading the failure can only act on one of them. The
    distinction is in the wrapped error's text, because S3UploadFailedError
    keeps the original response as a string and nothing else.
    """
    if "NoSuchBucket" in str(exc):
        return BucketMissing(f"upload of {key} failed: the bucket does not exist")
    return StorageError(f"upload of {key} failed: {exc}")


_store: ObjectStore | None = None


def object_store() -> ObjectStore:
    global _store
    if _store is None:
        _store = ObjectStore()
    return _store


def set_object_store(store: ObjectStore) -> None:
    global _store
    _store = store
