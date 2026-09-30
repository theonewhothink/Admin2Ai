"""Immutable evidence storage (§7, §44, §52, §55).

Originals are content-addressed per tenant and written once::

    <tenant>/sha256/<first two hex>/<sha256>

A key can only ever hold the bytes whose hash it names, so an original can
never be overwritten; storing identical bytes again is an idempotent no-op.
Every read re-hashes the bytes, so silent corruption or tampering surfaces as
:class:`IntegrityError` instead of wrong evidence.

:class:`EvidenceRegistry` turns stored bytes into :class:`Evidence` records,
deduplicated by ``(tenant, sha256)``. A second arrival of the same bytes (the
same PDF by email and from the supplier portal) returns the existing record and
adds a :class:`Sighting`: provenance grows, the original never changes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from backoffice.domain.models import Evidence, EvidenceFormat, SourceKind, utcnow

__all__ = [
    "EU_AWS_REGIONS",
    "EU_DEFAULT_REGION",
    "EvidenceIndex",
    "EvidenceRegistry",
    "EvidenceStoreError",
    "InMemoryEvidenceIndex",
    "IntegrityError",
    "InvalidKey",
    "InvalidTenant",
    "LocalObjectStore",
    "ObjectNotFound",
    "ObjectStore",
    "Registration",
    "S3ObjectStore",
    "ScanVerdict",
    "Sighting",
    "StorageConfigError",
    "ContentScanner",
    "check_json_metadata",
    "evidence_id_for",
    "object_key",
    "parse_key",
    "sha256_hex",
    "validate_tenant",
]

EU_DEFAULT_REGION = "eu-west-1"  # §44/§52: EU residency; configurable per deployment

# AWS regions physically inside an EU member state (§52 EU residency).
# verified_as_of: 2026-09, source: AWS region list (author's knowledge, not
# re-checked live). An "eu-" prefix is NOT enough: eu-west-2 is London and
# eu-central-2 is Zurich, both outside the EU. The AWS European Sovereign
# Cloud region is deliberately not listed; add it once its id is confirmed.
EU_AWS_REGIONS = frozenset({
    "eu-west-1",  # Ireland
    "eu-west-3",  # Paris, France
    "eu-central-1",  # Frankfurt, Germany
    "eu-north-1",  # Stockholm, Sweden
    "eu-south-1",  # Milan, Italy
    "eu-south-2",  # Aragón, Spain
})
# GetBucketLocation quirks: us-east-1 is reported as no constraint at all,
# and "EU" is the legacy name of eu-west-1.
_LEGACY_LOCATIONS = {None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}

_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_KEY = re.compile(r"^(?P<tenant>[A-Za-z0-9][A-Za-z0-9_.-]{0,127})/sha256/(?P<p>[0-9a-f]{2})/(?P<sha>[0-9a-f]{64})$")


# --------------------------------------------------------------------------- errors


class EvidenceStoreError(Exception):
    """Base class. Messages are for engineers and logs, never for owners."""


class InvalidTenant(EvidenceStoreError, ValueError):
    pass


class InvalidKey(EvidenceStoreError, ValueError):
    pass


class ObjectNotFound(EvidenceStoreError, KeyError):
    pass


class IntegrityError(EvidenceStoreError):
    """Stored bytes no longer match the hash that names them."""


class StorageConfigError(EvidenceStoreError):
    """The storage backend is not configured the way §52 requires."""


# --------------------------------------------------------------------------- keys


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_tenant(tenant: str) -> str:
    if not isinstance(tenant, str) or not _TENANT.match(tenant) or ".." in tenant:
        raise InvalidTenant(f"invalid tenant id {tenant!r}")
    return tenant


def object_key(tenant: str, digest: str) -> str:
    """Content address of ``digest`` inside ``tenant``'s namespace."""
    validate_tenant(tenant)
    if not _SHA.match(digest):
        raise InvalidKey(f"not a lowercase sha256 hex digest: {digest!r}")
    return f"{tenant}/sha256/{digest[:2]}/{digest}"


def parse_key(key: str) -> tuple[str, str]:
    """``(tenant, sha256)`` of a key, or :class:`InvalidKey`."""
    m = _KEY.match(key) if isinstance(key, str) else None
    if not m or m["p"] != m["sha"][:2] or ".." in m["tenant"]:
        raise InvalidKey(f"not an evidence key: {key!r}")
    return m["tenant"], m["sha"]


def _verified(key: str, data: bytes) -> bytes:
    _, digest = parse_key(key)
    if sha256_hex(data) != digest:
        raise IntegrityError(f"content of {key} does not match its hash")
    return data


# --------------------------------------------------------------------------- object stores


@runtime_checkable
class ObjectStore(Protocol):
    """Write-once, content-addressed byte store."""

    def put_immutable(self, data: bytes, tenant: str, content_type: str) -> str:
        """Store ``data`` and return its key. Idempotent for identical bytes."""
        ...

    def get(self, key: str) -> bytes:
        """Bytes at ``key``, hash-verified. :class:`ObjectNotFound` if absent."""
        ...

    def exists(self, key: str) -> bool: ...


class LocalObjectStore:
    """Filesystem store for development, tests and single-node deployments.

    Writes go to a temporary file that is fsynced and then hard-linked into
    place; ``link`` never replaces an existing path, so two writers can race
    safely and nothing is ever overwritten. Stored files are made read-only.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, key: str) -> Path:
        parse_key(key)
        path = (self._root / key).resolve()
        if self._root not in path.parents:
            raise InvalidKey(f"key escapes the store: {key!r}")
        return path

    def put_immutable(self, data: bytes, tenant: str, content_type: str) -> str:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("evidence must be bytes")
        key = object_key(tenant, sha256_hex(bytes(data)))
        path = self._path(key)
        if path.exists():
            _verified(key, path.read_bytes())  # identical bytes: nothing to do
            return key
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._write_once(path, bytes(data))
        self._write_meta(path, content_type, len(data))
        return key

    def _write_once(self, path: Path, data: bytes) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".incoming-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o444)
            try:
                os.link(tmp, path)
            except FileExistsError:
                _verified(self._key_of(path), path.read_bytes())
            except OSError:  # no hard links on this filesystem: exclusive create is still write-once
                self._write_exclusive(path, data)
            _fsync_dir(path.parent)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _write_exclusive(self, path: Path, data: bytes) -> None:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        except FileExistsError:
            _verified(self._key_of(path), path.read_bytes())
            return
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())

    def _key_of(self, path: Path) -> str:
        return path.relative_to(self._root).as_posix()

    def _write_meta(self, path: Path, content_type: str, size: int) -> None:
        meta = path.with_name(path.name + ".meta.json")
        body = json.dumps({"content_type": content_type, "size": size}).encode()
        try:
            fd = os.open(meta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        except FileExistsError:
            return  # first writer's metadata stands
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise ObjectNotFound(key) from None
        return _verified(key, data)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def content_type(self, key: str) -> str | None:
        meta = self._path(key).with_name(self._path(key).name + ".meta.json")
        try:
            return json.loads(meta.read_text())["content_type"]
        except (FileNotFoundError, ValueError, KeyError):
            return None

    def purge_tenant(self, tenant: str) -> int:
        """Erase every object of ``tenant`` (account deletion, §52). Returns how many were removed.

        Only for an owner-confirmed account deletion: originals are otherwise never removed.
        """
        directory = (self._root / validate_tenant(tenant)).resolve()
        if directory.parent != self._root or not directory.is_dir():
            return 0
        removed = sum(1 for p in directory.rglob("*") if p.is_file() and not p.name.endswith(".meta.json"))
        shutil.rmtree(directory)
        return removed


def _fsync_dir(directory: Path) -> None:
    """Make the new directory entry durable (best effort; not all platforms allow it)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class S3ObjectStore:
    """S3-compatible EU store with versioning, KMS and write-once puts (§44, §52).

    ``boto3`` is imported lazily; pass ``client`` to inject a preconfigured or
    fake client. Writes use ``IfNoneMatch="*"`` (S3 conditional writes) so an
    existing object is never replaced, plus a SHA-256 checksum that S3 verifies
    on arrival. Optional Object Lock retention makes originals undeletable for
    the retention period.

    Residency: the region must be in ``eu_regions`` (default
    :data:`EU_AWS_REGIONS`); an S3-compatible EU provider passes its own region
    names there. ``allow_non_eu_region`` is an explicit, auditable override.
    """

    def __init__(
        self,
        bucket: str,
        *,
        region: str = EU_DEFAULT_REGION,
        kms_key_id: str | None = None,
        endpoint_url: str | None = None,
        prefix: str = "",
        object_lock_mode: str | None = None,
        object_lock_retention: timedelta | None = None,
        allow_non_eu_region: bool = False,
        eu_regions: frozenset[str] | set[str] = EU_AWS_REGIONS,
        client: Any = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if not bucket:
            raise StorageConfigError("bucket is required")
        self._eu_regions = frozenset(eu_regions)
        self.allow_non_eu_region = allow_non_eu_region
        if not self._in_eu(region):
            raise StorageConfigError(f"region {region!r} is outside the EU (§52)")
        if object_lock_mode not in (None, "GOVERNANCE", "COMPLIANCE"):
            raise StorageConfigError("object_lock_mode must be GOVERNANCE or COMPLIANCE")
        if (object_lock_mode is None) != (object_lock_retention is None):
            raise StorageConfigError("object lock needs both a mode and a retention")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        self.bucket = bucket
        self.region = region
        self.kms_key_id = kms_key_id
        self.endpoint_url = endpoint_url
        self.prefix = prefix
        self.object_lock_mode = object_lock_mode
        self.object_lock_retention = object_lock_retention
        self._client = client
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def client(self) -> Any:
        with self._lock:
            if self._client is None:
                try:
                    import boto3  # optional dependency
                    from botocore.config import Config
                except ImportError as exc:  # pragma: no cover - depends on env
                    raise StorageConfigError("boto3 is not installed") from exc
                self._client = boto3.client(
                    "s3",
                    region_name=self.region,
                    endpoint_url=self.endpoint_url,
                    config=Config(signature_version="s3v4", retries={"mode": "standard"}),
                )
            return self._client

    def _in_eu(self, region: str) -> bool:
        return self.allow_non_eu_region or region in self._eu_regions

    def verify_bucket(self) -> None:
        """Refuse to run on a bucket without versioning (§44) or outside the EU (§52)."""
        versioning = self.client.get_bucket_versioning(Bucket=self.bucket)
        if versioning.get("Status") != "Enabled":
            raise StorageConfigError(f"bucket {self.bucket} must have versioning enabled")
        location = self.client.get_bucket_location(Bucket=self.bucket).get("LocationConstraint")
        region = _LEGACY_LOCATIONS[location] if location in _LEGACY_LOCATIONS else str(location)
        if not self._in_eu(region):
            raise StorageConfigError(f"bucket {self.bucket} is in {region}, outside the EU")

    def _s3_key(self, key: str) -> str:
        parse_key(key)
        return f"{self.prefix}{key}"

    def put_immutable(self, data: bytes, tenant: str, content_type: str) -> str:
        data = bytes(data)
        digest = sha256_hex(data)
        key = object_key(tenant, digest)
        params: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": self._s3_key(key),
            "Body": data,
            "ContentType": content_type or "application/octet-stream",
            "ChecksumSHA256": base64.b64encode(bytes.fromhex(digest)).decode(),
            "IfNoneMatch": "*",
            "ServerSideEncryption": "aws:kms",
            "Metadata": {"sha256": digest, "tenant": tenant},
        }
        if self.kms_key_id:
            params["SSEKMSKeyId"] = self.kms_key_id
        if self.object_lock_mode and self.object_lock_retention:
            params["ObjectLockMode"] = self.object_lock_mode
            params["ObjectLockRetainUntilDate"] = self._clock() + self.object_lock_retention
        try:
            self.client.put_object(**params)
        except Exception as exc:
            if _s3_error_code(exc) not in {"PreconditionFailed", "412", "ConditionalRequestConflict", "409"}:
                raise
            self._confirm_existing(key, digest)
        return key

    def _confirm_existing(self, key: str, digest: str) -> None:
        head = self.client.head_object(Bucket=self.bucket, Key=self._s3_key(key), ChecksumMode="ENABLED")
        stored = (head.get("Metadata") or {}).get("sha256")
        checksum = head.get("ChecksumSHA256")
        expected = base64.b64encode(bytes.fromhex(digest)).decode()
        if stored != digest and checksum != expected:
            raise IntegrityError(f"existing object at {key} does not match its hash")

    def get(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._s3_key(key), ChecksumMode="ENABLED")
        except Exception as exc:
            if _s3_error_code(exc) in {"NoSuchKey", "404", "NotFound"}:
                raise ObjectNotFound(key) from None
            raise
        return _verified(key, response["Body"].read())

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._s3_key(key))
        except Exception as exc:
            if _s3_error_code(exc) in {"NoSuchKey", "404", "NotFound"}:
                return False
            raise
        return True

    def purge_tenant(self, tenant: str, *, bypass_governance: bool = False) -> int:
        """Erase every version of every object of ``tenant`` (account deletion, §52).

        With Object Lock in GOVERNANCE mode this needs ``bypass_governance`` and
        the ``s3:BypassGovernanceRetention`` permission (the evidence-deletion
        role in AWS); without them S3 refuses and :class:`StorageConfigError`
        says so, so the erasure stays pending instead of being reported done.
        """
        prefix = f"{self.prefix}{validate_tenant(tenant)}/"
        removed = 0
        paginator = self.client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            targets = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                       for v in [*(page.get("Versions") or []), *(page.get("DeleteMarkers") or [])]]
            for start in range(0, len(targets), 1000):
                batch = targets[start:start + 1000]
                params: dict[str, Any] = {"Bucket": self.bucket, "Delete": {"Objects": batch, "Quiet": True}}
                if bypass_governance:
                    params["BypassGovernanceRetention"] = True
                result = self.client.delete_objects(**params)
                errors = result.get("Errors") or []
                if errors:
                    codes = sorted({str(e.get("Code")) for e in errors})
                    raise StorageConfigError(f"S3 refused to delete {len(errors)} object versions: {', '.join(codes)}")
                removed += len(batch)
        return removed


def _s3_error_code(exc: Exception) -> str | None:
    """Error code of a botocore ``ClientError`` without importing botocore."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error") or {}
    code = error.get("Code")
    return str(code) if code is not None else None


# --------------------------------------------------------------------------- registry


@dataclass(frozen=True)
class Sighting:
    """One arrival of an evidence original: where, when and in what context (§55)."""

    evidence_id: str
    tenant_id: str
    source_kind: SourceKind
    seen_at: datetime
    original_url: str | None = None
    filename: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Registration:
    evidence: Evidence
    created: bool  # False: the same bytes were already known for this tenant
    sighting: Sighting


@dataclass(frozen=True)
class ScanVerdict:
    clean: bool
    signature: str | None = None  # engine finding, internal only


class ContentScanner(Protocol):
    """Malware scanning hook (§52). Findings quarantine, they never discard."""

    def scan(self, data: bytes, mime_type: str) -> ScanVerdict: ...


class EvidenceIndex(Protocol):
    """Evidence metadata persistence (PostgreSQL in production)."""

    def get(self, tenant_id: str, evidence_id: str) -> Evidence | None: ...

    def find_by_sha256(self, tenant_id: str, sha256: str) -> Evidence | None: ...

    def add_if_absent(self, evidence: Evidence) -> tuple[Evidence, bool]:
        """Insert unless ``(tenant, sha256)`` exists; return the stored record."""
        ...

    def add_sighting(self, sighting: Sighting) -> None: ...

    def sightings(self, tenant_id: str, evidence_id: str) -> list[Sighting]: ...


class InMemoryEvidenceIndex:
    def __init__(self) -> None:
        self._by_sha: dict[tuple[str, str], Evidence] = {}
        self._by_id: dict[tuple[str, str], Evidence] = {}
        self._sightings: dict[tuple[str, str], list[Sighting]] = {}
        self._lock = threading.Lock()

    def get(self, tenant_id: str, evidence_id: str) -> Evidence | None:
        return self._by_id.get((tenant_id, evidence_id))

    def find_by_sha256(self, tenant_id: str, sha256: str) -> Evidence | None:
        return self._by_sha.get((tenant_id, sha256))

    def add_if_absent(self, evidence: Evidence) -> tuple[Evidence, bool]:
        with self._lock:
            existing = self._by_sha.get((evidence.tenant_id, evidence.sha256))
            if existing is not None:
                return existing, False
            self._by_sha[(evidence.tenant_id, evidence.sha256)] = evidence
            self._by_id[(evidence.tenant_id, evidence.id)] = evidence
            return evidence, True

    def add_sighting(self, sighting: Sighting) -> None:
        with self._lock:
            self._sightings.setdefault((sighting.tenant_id, sighting.evidence_id), []).append(sighting)

    def sightings(self, tenant_id: str, evidence_id: str) -> list[Sighting]:
        return list(self._sightings.get((tenant_id, evidence_id), ()))


def evidence_id_for(tenant_id: str, digest: str) -> str:
    """Deterministic evidence id: the same bytes get the same id on every retry."""
    return "ev_" + hashlib.sha256(f"{tenant_id}\x00{digest}".encode()).hexdigest()[:24]


MAX_METADATA_DEPTH = 32


def check_json_metadata(value: Any, path: str = "metadata", *, _depth: int = 0) -> Any:
    """Plain-JSON copy of ``value`` or ``TypeError``.

    Floats are refused so that no amount can slip into evidence metadata as a
    binary float; send numbers as integers or decimal strings. Nesting deeper
    than :data:`MAX_METADATA_DEPTH` is refused (metadata can come from devices).
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        raise TypeError(f"{path}: floats are not allowed in evidence metadata")
    if _depth >= MAX_METADATA_DEPTH:
        raise TypeError(f"{path}: metadata is nested too deeply")
    if isinstance(value, (list, tuple)):
        return [check_json_metadata(v, f"{path}[{i}]", _depth=_depth + 1) for i, v in enumerate(value)]
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f"{path}: keys must be strings")
            out[k] = check_json_metadata(v, f"{path}.{k}", _depth=_depth + 1)
        return out
    raise TypeError(f"{path}: {type(value).__name__} is not JSON metadata")


class EvidenceRegistry:
    """Registers originals once per tenant and records every sighting (§7, §55)."""

    def __init__(
        self,
        store: ObjectStore,
        index: EvidenceIndex | None = None,
        *,
        scanner: ContentScanner | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self.index = index if index is not None else InMemoryEvidenceIndex()
        self._scanner = scanner
        self._clock = clock

    def register(
        self,
        data: bytes,
        *,
        tenant_id: str,
        source_kind: SourceKind,
        format: EvidenceFormat,
        mime_type: str,
        filename: str | None = None,
        original_url: str | None = None,
        retrieved_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> Registration:
        """Store ``data`` (if new) and return its evidence plus this sighting.

        ``metadata`` describes the bytes and is kept only on first arrival;
        ``context`` describes this arrival (parent email, device, ...).
        """
        validate_tenant(tenant_id)
        if not data:
            raise ValueError("evidence cannot be empty")
        at = retrieved_at or self._clock()
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware")
        meta = check_json_metadata(dict(metadata or {}))
        ctx = MappingProxyType(check_json_metadata(dict(context or {}), "context"))
        digest = sha256_hex(bytes(data))

        existing = self.index.find_by_sha256(tenant_id, digest)
        if existing is None:
            existing, created = self._create(bytes(data), digest, tenant_id, source_kind, format,
                                             mime_type, filename, original_url, at, meta)
        else:
            created = False
            if existing.storage_key and not self.store.exists(existing.storage_key):
                # Self-heal: the index outlived the object (restore from backup, lost volume).
                self.store.put_immutable(bytes(data), tenant_id, existing.mime_type or mime_type)
        sighting = Sighting(
            evidence_id=existing.id,
            tenant_id=tenant_id,
            source_kind=source_kind,
            seen_at=at,
            original_url=original_url,
            filename=filename,
            context=ctx,
        )
        self.index.add_sighting(sighting)
        return Registration(evidence=existing, created=created, sighting=sighting)

    def _create(
        self,
        data: bytes,
        digest: str,
        tenant_id: str,
        source_kind: SourceKind,
        fmt: EvidenceFormat,
        mime_type: str,
        filename: str | None,
        original_url: str | None,
        at: datetime,
        meta: dict[str, Any],
    ) -> tuple[Evidence, bool]:
        if self._scanner is not None:
            verdict = self._scanner.scan(data, mime_type)
            if not verdict.clean:
                meta = {**meta, "quarantined": True}
        key = self.store.put_immutable(data, tenant_id, mime_type)
        evidence = Evidence(
            id=evidence_id_for(tenant_id, digest),
            tenant_id=tenant_id,
            source_kind=source_kind,
            format=fmt,
            sha256=digest,
            storage_key=key,
            original_url=original_url,
            retrieved_at=at,
            filename=filename,
            mime_type=mime_type,
            metadata={**meta, "size": len(data)},
        )
        return self.index.add_if_absent(evidence)

    def get(self, tenant_id: str, evidence_id: str) -> Evidence:
        evidence = self.index.get(tenant_id, evidence_id)
        if evidence is None:
            raise ObjectNotFound(evidence_id)
        return evidence

    def find_by_sha256(self, tenant_id: str, digest: str) -> Evidence | None:
        return self.index.find_by_sha256(tenant_id, digest)

    def open(self, tenant_id: str, evidence_id: str) -> bytes:
        """Original bytes, verified against the evidence hash; tenant-isolated."""
        evidence = self.get(tenant_id, evidence_id)
        if evidence.storage_key is None:
            raise ObjectNotFound(evidence_id)
        key_tenant, digest = parse_key(evidence.storage_key)
        if key_tenant != tenant_id or digest != evidence.sha256:
            raise IntegrityError(f"evidence {evidence_id} points outside its tenant or hash")
        return self.store.get(evidence.storage_key)

    def sightings(self, tenant_id: str, evidence_id: str) -> list[Sighting]:
        return self.index.sightings(tenant_id, evidence_id)
