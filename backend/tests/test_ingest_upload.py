"""Offline mobile upload protocol, server side (§43)."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from backoffice.domain.models import EvidenceFormat, SourceKind
from backoffice.evidence.store import EvidenceRegistry, LocalObjectStore
from backoffice.evidence.upload import RejectReason, UploadRequest, UploadService, UploadStatus

T0 = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)
KEY = b"k" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"receipt-photo" * 50


def request(data: bytes = JPEG, upload_id: str = "cap-0001-aaaa", **kw) -> UploadRequest:
    params = dict(tenant_id="t1", client_upload_id=upload_id, sha256=hashlib.sha256(data).hexdigest(), data=data,
                  content_type="image/jpeg", captured_at=T0, device_id="iphone-ana", capture={"pages": 1})
    params.update(kw)
    return UploadRequest(**params)


@pytest.fixture
def service(tmp_path) -> UploadService:
    return UploadService(EvidenceRegistry(LocalObjectStore(tmp_path)), signing_key=KEY, clock=lambda: T0)


def test_verified_upload_is_stored_and_the_device_may_delete(service):
    receipt = service.receive(request())
    assert receipt.status is UploadStatus.STORED and receipt.delete_local and not receipt.retry
    ev = service.registry.get("t1", receipt.evidence_id)
    assert ev.format is EvidenceFormat.IMAGE and ev.source_kind is SourceKind.MOBILE_SCAN
    assert ev.metadata["capture"] == {"pages": 1}
    ctx = service.registry.sightings("t1", ev.id)[0].context
    assert ctx["client_upload_id"] == "cap-0001-aaaa" and ctx["device_id"] == "iphone-ana"
    assert service.registry.open("t1", ev.id) == JPEG
    assert service.verify_receipt("t1", receipt)
    assert receipt.as_dict()["status"] == "stored"


def test_retry_after_lost_response_returns_the_same_receipt(service):
    first = service.receive(request())
    again = service.receive(request())
    assert again == first
    assert service.lookup("t1", "cap-0001-aaaa") == first
    assert service.lookup("t1", "never-sent-0") is None


def test_hash_mismatch_is_not_stored_and_can_be_retried(service):
    damaged = request(sha256=hashlib.sha256(b"what the phone had").hexdigest())
    receipt = service.receive(damaged)
    assert receipt.status is UploadStatus.HASH_MISMATCH and receipt.retry and not receipt.delete_local
    assert receipt.evidence_id is None
    assert service.lookup("t1", damaged.client_upload_id) is None  # the key is not burnt by the failure


def test_retry_with_good_bytes_after_mismatch_succeeds(service):
    good = request()
    service.receive(replace(good, data=JPEG + b"corrupted"))
    assert service.receive(good).status is UploadStatus.STORED


def test_same_key_with_different_content_is_refused(service):
    service.receive(request())
    other = JPEG + b"-different"
    receipt = service.receive(request(data=other))
    assert receipt.status is UploadStatus.REJECTED and receipt.reason is RejectReason.KEY_REUSED
    assert not receipt.delete_local


def test_same_bytes_under_a_new_key_are_a_duplicate(service):
    first = service.receive(request())
    second = service.receive(request(upload_id="cap-0002-bbbb"))
    assert second.status is UploadStatus.DUPLICATE and second.delete_local
    assert second.evidence_id == first.evidence_id


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"client_upload_id": "x"}, RejectReason.BAD_REQUEST),
        ({"client_upload_id": "../../etc/pwd"}, RejectReason.BAD_REQUEST),
        ({"sha256": "abc"}, RejectReason.BAD_REQUEST),
        ({"tenant_id": "../t"}, RejectReason.BAD_REQUEST),
        ({"captured_at": datetime(2026, 9, 24, 9, 0)}, RejectReason.BAD_REQUEST),
        ({"capture": {"blur": 0.3}}, RejectReason.BAD_REQUEST),
        ({"source_kind": SourceKind.BANK}, RejectReason.BAD_REQUEST),
        ({"data": b"", "sha256": hashlib.sha256(b"").hexdigest()}, RejectReason.EMPTY),
    ],
)
def test_bad_requests_are_rejected_and_the_device_keeps_its_copy(service, changes, reason):
    receipt = service.receive(replace(request(), **changes))
    assert receipt.status is UploadStatus.REJECTED and receipt.reason is reason
    assert not receipt.delete_local and receipt.evidence_id is None


def test_too_large_and_unsupported_have_plain_copy(tmp_path):
    small = UploadService(EvidenceRegistry(LocalObjectStore(tmp_path)), max_bytes=100, clock=lambda: T0)
    big = small.receive(request())
    assert big.reason is RejectReason.TOO_LARGE and big.owner_message == "This file is too large to send."
    blob = bytes(range(80))
    odd = small.receive(request(data=blob))
    assert odd.reason is RejectReason.UNSUPPORTED and odd.owner_message == "I can't read this kind of file yet."


def test_screenshot_flag_sets_the_format(service):
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 40
    receipt = service.receive(request(data=png, upload_id="shot-0001", is_screenshot=True,
                                      source_kind=SourceKind.MOBILE_SHARE))
    assert service.registry.get("t1", receipt.evidence_id).format is EvidenceFormat.SCREENSHOT


def test_receipt_signature_detects_tampering_and_wrong_tenant(service):
    receipt = service.receive(request())
    assert not service.verify_receipt("t2", receipt)
    assert not service.verify_receipt("t1", replace(receipt, evidence_id="ev_forged"))
    unsigned = UploadService(service.registry, clock=lambda: T0)
    assert not unsigned.verify_receipt("t1", receipt)
    with pytest.raises(ValueError):
        UploadService(service.registry, signing_key=b"short")


def test_concurrent_retries_share_one_receipt(service):
    receipts = []
    threads = [threading.Thread(target=lambda: receipts.append(service.receive(request()))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({r.evidence_id for r in receipts}) == 1
    assert all(r.delete_local for r in receipts)


def test_racing_uploads_with_one_key_and_different_bytes_never_confirm_the_loser(service):
    first, second = request(), request(data=JPEG + b"-other")
    barrier = threading.Barrier(2)
    original_get = service.receipts.get

    def slow_get(tenant, key):  # both requests see "no receipt yet"
        found = original_get(tenant, key)
        barrier.wait(timeout=5)
        return found

    service.receipts.get = slow_get  # type: ignore[method-assign]
    results = {}
    threads = [threading.Thread(target=lambda r=r: results.setdefault(r.sha256, service.receive(r)))
               for r in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    confirmed = [r for r in results.values() if r.delete_local]
    assert len(confirmed) == 1
    loser = next(r for r in results.values() if not r.delete_local)
    assert loser.status is UploadStatus.REJECTED and loser.reason is RejectReason.KEY_REUSED
    winner_sha = confirmed[0].sha256
    assert winner_sha in (first.sha256, second.sha256)
