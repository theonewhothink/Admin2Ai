"""Offline mobile upload, server side (§43).

Device: capture → encrypt locally → queue → background upload.
Server: verify hash → store → receipt. Device: delete local copy **only**
when the receipt says ``delete_local``.

The client sends the SHA-256 it computed over the plaintext capture together
with the bytes (transport is TLS; at-rest encryption on the device is the
app's job). A mismatch means the bytes were damaged in transit: nothing is
stored and the client keeps its copy and retries. The device deletes its copy
only when ``delete_local`` is true **and** the receipt's ``sha256`` equals the
hash it computed.

``client_upload_id`` is the idempotency key. A retry after a lost response
returns the original receipt; the same key with different content is refused
(a client bug) and the client must keep its copy. The same bytes under a new
key are recognised as already stored, so the device can still clean up.

Captures are stored as they are. Email exports and archives queued by the
share extension while offline (§12) go through share-file routing, so the
message or archive is the receipt's evidence and its attachments or members
become evidence too.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Protocol

from backoffice.domain.models import EvidenceFormat, SourceKind, utcnow

from .share import ShareIntake
from .sniff import sniff
from .store import EvidenceRegistry, InvalidTenant, Registration, check_json_metadata, validate_tenant

__all__ = [
    "InMemoryReceiptStore",
    "ReceiptStore",
    "RejectReason",
    "UploadReceipt",
    "UploadRequest",
    "UploadService",
    "UploadStatus",
]

_UPLOAD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_SOURCES = frozenset({SourceKind.MOBILE_SCAN, SourceKind.MOBILE_SHARE, SourceKind.UPLOAD})
_CONTAINERS = frozenset({EvidenceFormat.EML, EvidenceFormat.ZIP})  # expanded, not just stored
_MAX_CAPTURE_JSON = 16 * 1024  # device capture notes (pages, glare, crop); untrusted input


class UploadStatus(str, Enum):
    STORED = "stored"  # new evidence
    DUPLICATE = "duplicate"  # these bytes were already stored for this tenant
    HASH_MISMATCH = "hash_mismatch"  # damaged in transit: retry
    REJECTED = "rejected"  # will not succeed on retry; keep the local copy


class RejectReason(str, Enum):
    BAD_REQUEST = "bad_request"
    EMPTY = "empty"
    TOO_LARGE = "too_large"
    UNSUPPORTED = "unsupported"
    KEY_REUSED = "key_reused"


_OWNER_COPY = {
    RejectReason.TOO_LARGE: "This file is too large to send.",
    RejectReason.UNSUPPORTED: "I can't read this kind of file yet.",
    RejectReason.EMPTY: "There was nothing to save.",
}


@dataclass(frozen=True)
class UploadRequest:
    tenant_id: str
    client_upload_id: str  # generated on the device, stable across retries
    sha256: str  # hex digest the device computed
    data: bytes = field(repr=False)
    content_type: str | None = None
    filename: str | None = None
    captured_at: datetime | None = None
    device_id: str | None = None
    source_kind: SourceKind = SourceKind.MOBILE_SCAN
    is_screenshot: bool = False
    capture: Mapping[str, Any] = field(default_factory=dict)  # pages, glare flag, ... (JSON)


@dataclass(frozen=True)
class UploadReceipt:
    client_upload_id: str
    status: UploadStatus
    sha256: str  # what the server computed (or the claimed hash when nothing was read)
    received_at: datetime
    evidence_id: str | None = None
    delete_local: bool = False  # the only signal that lets the device drop its copy
    retry: bool = False
    reason: RejectReason | None = None
    owner_message: str | None = None
    signature: str | None = None  # HMAC over the receipt, when a key is configured

    def as_dict(self) -> dict[str, Any]:
        return {
            "client_upload_id": self.client_upload_id,
            "status": self.status.value,
            "sha256": self.sha256,
            "received_at": self.received_at.isoformat(),
            "evidence_id": self.evidence_id,
            "delete_local": self.delete_local,
            "retry": self.retry,
            "reason": self.reason.value if self.reason else None,
            "owner_message": self.owner_message,
            "signature": self.signature,
        }


class ReceiptStore(Protocol):
    def get(self, tenant_id: str, client_upload_id: str) -> UploadReceipt | None: ...

    def put_if_absent(self, tenant_id: str, receipt: UploadReceipt) -> UploadReceipt:
        """Store unless a receipt for this key exists; return the stored one."""
        ...


class InMemoryReceiptStore:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], UploadReceipt] = {}
        self._lock = threading.Lock()

    def get(self, tenant_id: str, client_upload_id: str) -> UploadReceipt | None:
        return self._items.get((tenant_id, client_upload_id))

    def put_if_absent(self, tenant_id: str, receipt: UploadReceipt) -> UploadReceipt:
        with self._lock:
            return self._items.setdefault((tenant_id, receipt.client_upload_id), receipt)


class UploadService:
    """Receives queued captures from the mobile app (§43)."""

    def __init__(
        self,
        registry: EvidenceRegistry,
        *,
        receipts: ReceiptStore | None = None,
        files: ShareIntake | None = None,
        max_bytes: int = 50 * 1024 * 1024,
        signing_key: bytes | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if signing_key is not None and len(signing_key) < 32:
            raise ValueError("receipt signing key must be at least 32 bytes")
        self.registry = registry
        self.files = files if files is not None else ShareIntake(registry, clock=clock)
        self.receipts = receipts if receipts is not None else InMemoryReceiptStore()
        self.max_bytes = max_bytes
        self._key = signing_key
        self._clock = clock

    def lookup(self, tenant_id: str, client_upload_id: str) -> UploadReceipt | None:
        """Lets a device that lost the response ask before re-sending the bytes."""
        return self.receipts.get(tenant_id, client_upload_id)

    def receive(self, request: UploadRequest) -> UploadReceipt:
        now = self._clock()
        claimed = (request.sha256 or "").strip().lower()
        problem = self._validate(request, claimed)
        if problem is not None:
            return self._rejected(request, claimed, now, problem)
        previous = self.receipts.get(request.tenant_id, request.client_upload_id)
        if previous is not None:
            if previous.sha256 == claimed:
                return previous  # retry after a lost response: same answer
            return self._rejected(request, claimed, now, RejectReason.KEY_REUSED)
        return self._store(request, claimed, now)

    def verify_receipt(self, tenant_id: str, receipt: UploadReceipt) -> bool:
        """True when ``receipt`` was issued by this server for ``tenant_id``."""
        if self._key is None or receipt.signature is None:
            return False
        return hmac.compare_digest(receipt.signature, self._sign(tenant_id, receipt))

    # ----------------------------------------------------------------- internals

    def _validate(self, request: UploadRequest, claimed: str) -> RejectReason | None:
        try:
            validate_tenant(request.tenant_id)
        except InvalidTenant:
            return RejectReason.BAD_REQUEST
        if not _UPLOAD_ID.match(request.client_upload_id or "") or not _SHA.match(claimed):
            return RejectReason.BAD_REQUEST
        if request.source_kind not in _ALLOWED_SOURCES:
            return RejectReason.BAD_REQUEST
        if request.captured_at is not None and request.captured_at.tzinfo is None:
            return RejectReason.BAD_REQUEST
        try:
            capture = check_json_metadata(dict(request.capture), "capture")
        except (TypeError, ValueError):
            return RejectReason.BAD_REQUEST
        if len(json.dumps(capture)) > _MAX_CAPTURE_JSON:
            return RejectReason.BAD_REQUEST
        if not request.data:
            return RejectReason.EMPTY
        if len(request.data) > self.max_bytes:
            return RejectReason.TOO_LARGE
        return None

    def _store(self, request: UploadRequest, claimed: str, now: datetime) -> UploadReceipt:
        actual = hashlib.sha256(request.data).hexdigest()
        if not hmac.compare_digest(actual, claimed):
            # Not remembered: the retry with good bytes must be accepted.
            return UploadReceipt(request.client_upload_id, UploadStatus.HASH_MISMATCH, actual, now, retry=True)
        found = sniff(request.data, declared_type=request.content_type, filename=request.filename)
        if found.format is None:
            return self._rejected(request, claimed, now, RejectReason.UNSUPPORTED)
        registration = self._register(request, found.format, found.mime_type, found.declared_mime_type, now)
        if registration is None:
            return self._rejected(request, claimed, now, RejectReason.UNSUPPORTED)
        status = UploadStatus.STORED if registration.created else UploadStatus.DUPLICATE
        receipt = self._signed(request.tenant_id, UploadReceipt(
            request.client_upload_id, status, actual, now, registration.evidence.id, delete_local=True,
        ))
        # A concurrent retry may have stored first: everyone gets the same receipt,
        # unless the winner was different content under the same key.
        winner = self.receipts.put_if_absent(request.tenant_id, receipt)
        if winner.sha256 != actual:
            return self._rejected(request, claimed, now, RejectReason.KEY_REUSED)
        return winner

    def _register(self, request: UploadRequest, fmt: EvidenceFormat, mime_type: str, declared: str | None,
                  now: datetime) -> Registration | None:
        """Store the verified bytes; containers are expanded. ``None``: unreadable after all."""
        context = {
            "client_upload_id": request.client_upload_id,
            "device_id": request.device_id,
            "captured_at": request.captured_at.isoformat() if request.captured_at else None,
        }
        metadata = {"capture": dict(request.capture)}
        if fmt in _CONTAINERS:
            outcome = self.files.ingest_file(
                request.tenant_id, request.data, filename=request.filename, mime_type=request.content_type,
                source_kind=request.source_kind, at=now, context=context, metadata=metadata,
            )
            return outcome.registrations[0] if outcome.registrations else None
        if request.is_screenshot and fmt is EvidenceFormat.IMAGE:
            fmt = EvidenceFormat.SCREENSHOT
        return self.registry.register(
            request.data, tenant_id=request.tenant_id, source_kind=request.source_kind, format=fmt,
            mime_type=mime_type, filename=request.filename, retrieved_at=now,
            metadata={**metadata, "declared_mime_type": declared}, context=context,
        )

    def _rejected(self, request: UploadRequest, claimed: str, now: datetime, reason: RejectReason) -> UploadReceipt:
        return UploadReceipt(
            request.client_upload_id or "", UploadStatus.REJECTED, claimed, now,
            reason=reason, owner_message=_OWNER_COPY.get(reason),
        )

    def _sign(self, tenant_id: str, receipt: UploadReceipt) -> str:
        assert self._key is not None
        message = "\n".join([
            tenant_id, receipt.client_upload_id, receipt.status.value, receipt.sha256,
            receipt.evidence_id or "", receipt.received_at.isoformat(),
        ]).encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def _signed(self, tenant_id: str, receipt: UploadReceipt) -> UploadReceipt:
        if self._key is None:
            return receipt
        return replace(receipt, signature=self._sign(tenant_id, receipt))
