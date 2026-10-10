"""Artifact storage — a private, swappable layer for screenshots, recordings and
build binaries.

Two backends:
  • local  — filesystem under artifacts_dir (dev / single box)
  • s3     — any S3-compatible object store (Cloudflare R2 / AWS S3),
             required once the api and worker run as separate services.

SECURITY (zero-tolerance):
  • Buckets are PRIVATE. Nothing is publicly readable.
  • The database stores only opaque KEYS, never a usable URL.
  • A browser-usable URL is short-lived and SIGNED, and is minted ONLY by API
    handlers that have already authenticated the caller and confirmed their org
    owns the run/build (see `signed_url` callers in tamam/api/routes.py).
      - s3:    a presigned GET URL (expires after signed_url_ttl_s).
      - local: `/api/artifacts/<key>?exp=..&sig=..` — an HMAC the local artifact
               endpoint verifies. Works with <img> tags (no Authorization header
               is possible there), yet is unforgeable and time-limited.
  • Build binaries (.ipa) are NEVER given a browser URL — only fetched server-side
    by the worker via `get_bytes`.
  • Keys are validated against path traversal before any filesystem access.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import mimetypes
import re
import secrets
import shutil
import time
from functools import lru_cache
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from tamam.config import get_settings

# Keys we mint ourselves ("runs/<uuid>/step-3.png", "builds/<uuid>/app.ipa").
# Reject anything with traversal or unexpected characters before touching disk/S3.
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_.\-]*$")


def _validate_key(key: str) -> str:
    if not key or ".." in key or key.startswith("/") or not _SAFE_KEY.match(key):
        raise ValueError(f"unsafe artifact key: {key!r}")
    return key


@runtime_checkable
class ArtifactStorage(Protocol):
    async def put(self, local_path: Path, key: str) -> None: ...
    async def get_bytes(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    def signed_url(self, key: str) -> str: ...


class LocalArtifactStorage:
    """Dev backend: files under a root dir, served by the API's artifact endpoint
    guarded by a short-lived HMAC token (no public static mount)."""

    def __init__(self, root: Path, secret: bytes, ttl_s: int) -> None:
        self._root = root.resolve()
        self._secret = secret
        self._ttl = ttl_s

    def _abs(self, key: str) -> Path:
        path = (self._root / _validate_key(key)).resolve()
        # Defense in depth: the resolved path must stay under the root.
        if not path.is_relative_to(self._root):
            raise ValueError(f"artifact key escapes root: {key!r}")
        return path

    async def put(self, local_path: Path, key: str) -> None:
        dest = self._abs(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = Path(local_path).resolve()
        if src != dest:
            await asyncio.to_thread(shutil.copyfile, src, dest)

    async def get_bytes(self, key: str) -> bytes:
        return await asyncio.to_thread(self._abs(key).read_bytes)

    async def delete(self, key: str) -> None:
        # Idempotent: a missing file is already "deleted" (right-to-deletion must
        # not fail just because the object is already gone).
        await asyncio.to_thread(self._abs(key).unlink, missing_ok=True)

    def signed_url(self, key: str) -> str:
        _validate_key(key)
        exp = int(time.time()) + self._ttl
        return f"/api/artifacts/{quote(key)}?exp={exp}&sig={self._sign(key, exp)}"

    def _sign(self, key: str, exp: int) -> str:
        msg = f"{key}:{exp}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def verify(self, key: str, exp: int, sig: str) -> bool:
        """Constant-time check of a signed local artifact URL."""
        try:
            _validate_key(key)
        except ValueError:
            return False
        if exp < int(time.time()):
            return False
        return hmac.compare_digest(sig, self._sign(key, exp))

    def local_path(self, key: str) -> Path:
        return self._abs(key)


class S3ArtifactStorage:
    """Prod backend: S3-compatible object storage. Bucket is private; browser
    access is via presigned GET URLs only."""

    def __init__(
        self,
        *,
        endpoint_url: str | None,
        bucket: str,
        region: str,
        access_key_id: str | None,
        secret_access_key: str | None,
        ttl_s: int,
    ) -> None:
        self._bucket = bucket
        self._ttl = ttl_s
        self._endpoint = endpoint_url
        self._region = region
        self._akid = access_key_id
        self._secret = secret_access_key
        self.__client = None

    def _client(self):
        # Lazy: don't require boto3/creds unless S3 is actually used.
        if self.__client is None:
            import boto3
            from botocore.config import Config

            self.__client = boto3.client(
                "s3",
                endpoint_url=self._endpoint,
                region_name=self._region,
                aws_access_key_id=self._akid,
                aws_secret_access_key=self._secret,
                config=Config(signature_version="s3v4"),
            )
        return self.__client

    async def put(self, local_path: Path, key: str) -> None:
        _validate_key(key)
        # boto3's upload_file does NOT infer a content type — without this, objects
        # are stored as binary/octet-stream and browsers won't play/render them
        # (a recording stays "preparing" forever, images don't show).
        ctype, _ = mimetypes.guess_type(key)
        extra = {"ContentType": ctype} if ctype else {}
        await asyncio.to_thread(
            self._client().upload_file, str(Path(local_path)), self._bucket, key, ExtraArgs=extra
        )

    async def get_bytes(self, key: str) -> bytes:
        _validate_key(key)

        def _read() -> bytes:
            obj = self._client().get_object(Bucket=self._bucket, Key=key)
            return obj["Body"].read()

        return await asyncio.to_thread(_read)

    async def delete(self, key: str) -> None:
        _validate_key(key)
        # S3 delete_object is idempotent — deleting a missing key returns 204, so
        # right-to-deletion never fails on an already-gone object.
        await asyncio.to_thread(
            self._client().delete_object, Bucket=self._bucket, Key=key
        )

    def signed_url(self, key: str) -> str:
        _validate_key(key)
        return self._client().generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=self._ttl,
        )


@lru_cache
def get_storage() -> ArtifactStorage:
    s = get_settings()
    if s.storage_backend == "s3":
        if not s.s3_bucket:
            raise RuntimeError("storage_backend=s3 but TAMAM_S3_BUCKET is not set")
        return S3ArtifactStorage(
            endpoint_url=s.s3_endpoint_url,
            bucket=s.s3_bucket,
            region=s.s3_region,
            access_key_id=s.s3_access_key_id,
            secret_access_key=s.s3_secret_access_key,
            ttl_s=s.signed_url_ttl_s,
        )
    # Local dev: a per-process HMAC secret. URLs are short-lived, so invalidation
    # on restart is harmless; prod uses S3 presigning and never this path.
    return LocalArtifactStorage(
        root=s.artifacts_dir.resolve(),
        secret=secrets.token_bytes(32),
        ttl_s=s.signed_url_ttl_s,
    )
