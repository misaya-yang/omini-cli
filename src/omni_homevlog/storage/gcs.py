"""GCS access (§13.1).

Two backends, same interface:

  * `google-cloud-storage` when installed (preferred: resumable, handles retries)
  * the GCS JSON API over the ADC session otherwise

The second exists so the project has no hard dependency on the
`google-cloud-storage` package. It reuses the *same* ADC credentials as the
Vertex transport — one credential path, one place to debug.

§22 constraints honoured here:
  * buckets are assumed private; we never create one
  * we never generate a public URL
  * signed URLs, when produced, are short-lived and generated only on request
  * nothing logs object contents or credentials
"""

from __future__ import annotations

import mimetypes
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omni_homevlog.errors import ConfigError, ProviderError
from omni_homevlog.observability.logging import get_logger

logger = get_logger("gcs")

_JSON_API = "https://storage.googleapis.com/storage/v1"
_UPLOAD_API = "https://storage.googleapis.com/upload/storage/v1"


@dataclass(frozen=True, slots=True)
class GcsLocation:
    bucket: str
    name: str

    @property
    def uri(self) -> str:
        return f"gs://{self.bucket}/{self.name}"

    @staticmethod
    def parse(uri: str) -> GcsLocation:
        if not uri.startswith("gs://"):
            raise ConfigError(f"Not a gs:// URI: {uri!r}")
        rest = uri[5:]
        bucket, _, name = rest.partition("/")
        if not bucket:
            raise ConfigError(f"Malformed gs:// URI (no bucket): {uri!r}")
        return GcsLocation(bucket=bucket, name=name.lstrip("/"))


def is_gcs_uri(value: str | None) -> bool:
    return bool(value and value.startswith("gs://"))


def join_uri(prefix: str, *parts: str) -> str:
    base = prefix.rstrip("/")
    tail = "/".join(p.strip("/") for p in parts if p)
    return f"{base}/{tail}" if tail else base


class GcsClient:
    """Minimal GCS client. Prefers the official library, falls back to JSON API."""

    def __init__(self, session: Any | None = None, *, prefer_library: bool = True) -> None:
        self._session = session
        self._client: Any | None = None
        self._using_library = False

        if prefer_library:
            try:  # pragma: no cover - depends on optional dependency
                from google.cloud import storage

                self._client = storage.Client()
                self._using_library = True
            except Exception:
                self._client = None

    @property
    def backend(self) -> str:
        return "google-cloud-storage" if self._using_library else "json-api"

    @property
    def session(self) -> Any:
        if self._session is None:
            from omni_homevlog.providers.auth import get_authorized_session

            self._session = get_authorized_session()
        return self._session

    # ─ upload ─────────────────────────────────────────────────────────────

    def upload_file(self, local_path: Path, uri: str, *, content_type: str | None = None) -> str:
        local_path = Path(local_path)
        if not local_path.is_file():
            raise ProviderError(f"Cannot upload missing file: {local_path}")
        location = GcsLocation.parse(uri)
        ctype = (
            content_type or mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        )

        if self._using_library and self._client is not None:  # pragma: no cover
            bucket = self._client.bucket(location.bucket)
            blob = bucket.blob(location.name)
            blob.upload_from_filename(str(local_path), content_type=ctype)
            logger.info(
                "Uploaded to GCS",
                extra={"extra_fields": {"uri": uri, "bytes": local_path.stat().st_size}},
            )
            return location.uri

        data = local_path.read_bytes()
        url = (
            f"{_UPLOAD_API}/b/{urllib.parse.quote(location.bucket)}/o"
            f"?uploadType=media&name={urllib.parse.quote(location.name, safe='')}"
        )
        response = self.session.post(url, data=data, headers={"Content-Type": ctype}, timeout=600)
        if response.status_code >= 400:
            raise ProviderError(
                f"GCS upload failed ({response.status_code}): {response.text[:300]}",
                http_status=response.status_code,
                detail={"uri": uri},
            )
        logger.info("Uploaded to GCS", extra={"extra_fields": {"uri": uri, "bytes": len(data)}})
        return location.uri

    # ── download ──────────────────────────────────────────────────────────

    def download_to(self, uri: str, local_path: Path) -> Path:
        location = GcsLocation.parse(uri)
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        if self._using_library and self._client is not None:  # pragma: no cover
            bucket = self._client.bucket(location.bucket)
            blob = bucket.blob(location.name)
            blob.download_to_filename(str(local_path))
            return local_path

        url = (
            f"{_JSON_API}/b/{urllib.parse.quote(location.bucket)}/o/"
            f"{urllib.parse.quote(location.name, safe='')}?alt=media"
        )
        response = self.session.get(url, timeout=600)
        if response.status_code >= 400:
            raise ProviderError(
                f"GCS download failed ({response.status_code}) for {uri}: {response.text[:300]}",
                http_status=response.status_code,
                detail={"uri": uri},
            )
        # Atomic: a partially downloaded video must never look complete.
        from omni_homevlog.storage.local import atomic_write_bytes

        atomic_write_bytes(local_path, response.content)
        return local_path

    # ── introspect ─────────────────────────────────────────────────────────

    def object_exists(self, uri: str) -> bool:
        location = GcsLocation.parse(uri)
        if self._using_library and self._client is not None:  # pragma: no cover
            bucket = self._client.bucket(location.bucket)
            return bool(bucket.blob(location.name).exists())
        url = (
            f"{_JSON_API}/b/{urllib.parse.quote(location.bucket)}/o/"
            f"{urllib.parse.quote(location.name, safe='')}"
        )
        response = self.session.get(url, timeout=60)
        return int(response.status_code) == 200

    def check_writable(self, prefix_uri: str) -> tuple[bool, str]:
        """Best-effort `storage.objectCreator` check used by `omni-vlog doctor`.

        Writes a tiny probe object. That is a real write, so the caller must have
        said the bucket is approved; the object is namespaced and documented so
        it can be cleaned up.
        """
        location = GcsLocation.parse(prefix_uri)
        probe_name = f"{location.name.rstrip('/')}/_omni_vlog_write_probe.txt"
        try:
            if self._using_library and self._client is not None:  # pragma: no cover
                bucket = self._client.bucket(location.bucket)
                bucket.blob(probe_name).upload_from_string(
                    b"omni-vlog probe", content_type="text/plain"
                )
                return True, f"wrote gs://{location.bucket}/{probe_name}"

            url = (
                f"{_UPLOAD_API}/b/{urllib.parse.quote(location.bucket)}/o"
                f"?uploadType=media&name={urllib.parse.quote(probe_name, safe='')}"
            )
            response = self.session.post(
                url, data=b"omni-vlog probe", headers={"Content-Type": "text/plain"}, timeout=60
            )
            if int(response.status_code) >= 400:
                return False, f"HTTP {response.status_code}: {response.text[:200]}"
            return True, f"wrote gs://{location.bucket}/{probe_name}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def list_objects(self, prefix_uri: str) -> list[str]:
        """Every object name under a prefix.

        Handles pagination: a job with many attempts can exceed one page, and a
        partial listing would make `delete` silently leave objects behind.
        """
        location = GcsLocation.parse(prefix_uri)
        names: list[str] = []
        page_token: str | None = None

        while True:
            query: dict[str, str] = {"prefix": location.name, "maxResults": "1000"}
            if page_token:
                query["pageToken"] = page_token
            url = (
                f"{_JSON_API}/b/{urllib.parse.quote(location.bucket)}/o"
                f"?{urllib.parse.urlencode(query)}"
            )
            response = self.session.get(url, timeout=60)
            if int(response.status_code) >= 400:
                raise ProviderError(
                    f"GCS list failed ({response.status_code}) for {prefix_uri}: "
                    f"{response.text[:300]}",
                    http_status=int(response.status_code),
                    detail={"prefix": prefix_uri},
                )
            payload = response.json()
            names.extend(str(item["name"]) for item in payload.get("items", []) if item.get("name"))
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return names

    def delete_object(self, bucket: str, name: str) -> bool:
        """Delete one object. Returns False when it was already gone."""
        url = f"{_JSON_API}/b/{urllib.parse.quote(bucket)}/o/{urllib.parse.quote(name, safe='')}"
        response = self.session.delete(url, timeout=60)
        status = int(response.status_code)
        if status in (200, 204):
            return True
        if status == 404:
            return False
        raise ProviderError(
            f"GCS delete failed ({status}) for gs://{bucket}/{name}: {response.text[:300]}",
            http_status=status,
            detail={"bucket": bucket, "name": name},
        )

    def delete_prefix(self, prefix_uri: str) -> tuple[int, list[str]]:
        """Delete everything under a prefix (§22).

        Returns `(deleted_count, errors)`. A partial failure is reported rather than
        raised, because a delete that got half way is a state the operator needs
        described precisely, not an exception to swallow.
        """
        location = GcsLocation.parse(prefix_uri)
        errors: list[str] = []
        deleted = 0

        try:
            names = self.list_objects(prefix_uri)
        except ProviderError as exc:
            return 0, [exc.message]

        for name in names:
            try:
                if self.delete_object(location.bucket, name):
                    deleted += 1
            except ProviderError as exc:
                errors.append(exc.message)

        return deleted, errors


def maybe_client(enabled: bool = True) -> GcsClient | None:
    if not enabled:
        return None
    try:
        return GcsClient()
    except Exception:
        return None
