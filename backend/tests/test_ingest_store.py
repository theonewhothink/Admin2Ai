"""Immutable evidence storage and the evidence registry (§7, §44, §52, §55)."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
import threading
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backoffice.domain.models import EvidenceFormat, SourceKind
from backoffice.evidence.store import (
    EvidenceRegistry,
    IntegrityError,
    InvalidKey,
    InvalidTenant,
    LocalObjectStore,
    ObjectNotFound,
    ObjectStore,
    S3ObjectStore,
    ScanVerdict,
    StorageConfigError,
    evidence_id_for,
    object_key,
    parse_key,
)

T0 = datetime(2026, 9, 18, 14, 42, tzinfo=timezone.utc)
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- keys


def test_object_key_is_content_addressed_per_tenant():
    key = object_key("t1", sha(PDF))
    assert key == f"t1/sha256/{sha(PDF)[:2]}/{sha(PDF)}"
    assert parse_key(key) == ("t1", sha(PDF))


@pytest.mark.parametrize("tenant", ["", "../x", "a/b", "..", ".hidden", "a" * 200, "a..b", "t 1"])
def test_invalid_tenants_are_refused(tenant):
    with pytest.raises(InvalidTenant):
        object_key(tenant, sha(PDF))


@pytest.mark.parametrize(
    "key",
    ["t1/sha256/ab/" + "0" * 64, "t1/sha256/00/" + "0" * 63, "../sha256/00/" + "0" * 64, "t1/other/00/" + "0" * 64],
)
def test_malformed_keys_are_refused(key):
    with pytest.raises(InvalidKey):
        parse_key(key)


# --------------------------------------------------------------------------- local store


def test_local_store_writes_once_and_reads_back_verified(tmp_path):
    store = LocalObjectStore(tmp_path)
    assert isinstance(store, ObjectStore)
    key = store.put_immutable(PDF, "t1", "application/pdf")
    assert store.exists(key)
    assert store.get(key) == PDF
    assert store.content_type(key) == "application/pdf"
    mode = (tmp_path / key).stat().st_mode
    assert not mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)  # read-only original


def test_local_store_is_idempotent_for_identical_bytes(tmp_path):
    store = LocalObjectStore(tmp_path)
    first = store.put_immutable(PDF, "t1", "application/pdf")
    second = store.put_immutable(PDF, "t1", "application/octet-stream")
    assert first == second
    assert store.content_type(first) == "application/pdf"  # first writer's metadata stands


def test_local_store_detects_tampering_on_read_and_on_rewrite(tmp_path):
    store = LocalObjectStore(tmp_path)
    key = store.put_immutable(PDF, "t1", "application/pdf")
    path = tmp_path / key
    os.chmod(path, 0o644)
    path.write_bytes(b"%PDF-1.4 altered")
    with pytest.raises(IntegrityError):
        store.get(key)
    with pytest.raises(IntegrityError):
        store.put_immutable(PDF, "t1", "application/pdf")  # never silently "repairs" or overwrites


def test_local_store_missing_object(tmp_path):
    store = LocalObjectStore(tmp_path)
    key = object_key("t1", sha(b"nothing"))
    assert not store.exists(key)
    with pytest.raises(ObjectNotFound):
        store.get(key)


def test_local_store_keeps_tenants_apart(tmp_path):
    store = LocalObjectStore(tmp_path)
    a = store.put_immutable(PDF, "tenant-a", "application/pdf")
    b = store.put_immutable(PDF, "tenant-b", "application/pdf")
    assert a != b and a.startswith("tenant-a/") and b.startswith("tenant-b/")


def test_local_store_concurrent_writers_of_same_bytes(tmp_path):
    store = LocalObjectStore(tmp_path)
    keys, errors = [], []

    def put():
        try:
            keys.append(store.put_immutable(PDF, "t1", "application/pdf"))
        except Exception as exc:  # pragma: no cover - would fail the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=put) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(set(keys)) == 1
    assert [p.name for p in (tmp_path / keys[0]).parent.iterdir() if p.name.startswith(".incoming")] == []


def test_local_store_without_hard_links_still_writes_once(tmp_path, monkeypatch):
    def no_links(src, dst):
        raise OSError(95, "Operation not supported")

    monkeypatch.setattr(os, "link", no_links)
    store = LocalObjectStore(tmp_path)
    key = store.put_immutable(PDF, "t1", "application/pdf")
    assert store.get(key) == PDF
    assert store.put_immutable(PDF, "t1", "application/pdf") == key


def test_local_store_refuses_non_bytes(tmp_path):
    with pytest.raises(TypeError):
        LocalObjectStore(tmp_path).put_immutable("text", "t1", "text/plain")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- S3 adapter


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.versioning = "Enabled"
        self.location = "eu-west-1"

    def put_object(self, **kw):
        self.calls.append(("put_object", kw))
        if kw.get("IfNoneMatch") == "*" and kw["Key"] in self.objects:
            raise ClientError("PreconditionFailed")
        self.objects[kw["Key"]] = kw
        return {}

    def head_object(self, **kw):
        self.calls.append(("head_object", kw))
        if kw["Key"] not in self.objects:
            raise ClientError("404")
        obj = self.objects[kw["Key"]]
        return {"Metadata": obj["Metadata"], "ChecksumSHA256": obj["ChecksumSHA256"]}

    def get_object(self, **kw):
        self.calls.append(("get_object", kw))
        if kw["Key"] not in self.objects:
            raise ClientError("NoSuchKey")
        body = self.objects[kw["Key"]]["Body"]
        return {"Body": type("B", (), {"read": lambda self: body})()}

    def get_bucket_versioning(self, **kw):
        return {"Status": self.versioning} if self.versioning else {}

    def get_bucket_location(self, **kw):
        return {"LocationConstraint": self.location}


def test_s3_put_uses_conditional_write_kms_and_checksum():
    fake = FakeS3()
    store = S3ObjectStore("evidence-eu", kms_key_id="arn:aws:kms:eu-west-1:1:key/k", prefix="v1", client=fake,
                          object_lock_mode="COMPLIANCE", object_lock_retention=timedelta(days=3650),
                          clock=lambda: T0)
    key = store.put_immutable(PDF, "t1", "application/pdf")
    _, params = fake.calls[0]
    assert params["Key"] == f"v1/{key}"
    assert params["IfNoneMatch"] == "*"
    assert params["ServerSideEncryption"] == "aws:kms"
    assert params["SSEKMSKeyId"].endswith("key/k")
    assert params["ChecksumSHA256"] == base64.b64encode(hashlib.sha256(PDF).digest()).decode()
    assert params["ObjectLockMode"] == "COMPLIANCE"
    assert params["ObjectLockRetainUntilDate"] == T0 + timedelta(days=3650)
    assert store.get(key) == PDF
    assert store.exists(key)


def test_s3_second_put_of_same_bytes_is_a_verified_no_op():
    fake = FakeS3()
    store = S3ObjectStore("b", client=fake)
    key = store.put_immutable(PDF, "t1", "application/pdf")
    assert store.put_immutable(PDF, "t1", "application/pdf") == key
    assert [c[0] for c in fake.calls] == ["put_object", "put_object", "head_object"]


def test_s3_existing_object_with_wrong_hash_is_an_integrity_error():
    fake = FakeS3()
    store = S3ObjectStore("b", client=fake)
    key = store.put_immutable(PDF, "t1", "application/pdf")
    fake.objects[f"{key}"]["Metadata"] = {"sha256": "0" * 64}
    fake.objects[f"{key}"]["ChecksumSHA256"] = "bogus"
    with pytest.raises(IntegrityError):
        store.put_immutable(PDF, "t1", "application/pdf")


def test_s3_get_verifies_content_and_maps_missing():
    fake = FakeS3()
    store = S3ObjectStore("b", client=fake)
    key = store.put_immutable(PDF, "t1", "application/pdf")
    fake.objects[key]["Body"] = b"tampered"
    with pytest.raises(IntegrityError):
        store.get(key)
    missing = object_key("t1", sha(b"x"))
    assert not store.exists(missing)
    with pytest.raises(ObjectNotFound):
        store.get(missing)


def test_s3_other_errors_propagate():
    class Broken(FakeS3):
        def put_object(self, **kw):
            raise ClientError("AccessDenied")

    with pytest.raises(ClientError):
        S3ObjectStore("b", client=Broken()).put_immutable(PDF, "t1", "application/pdf")


def test_s3_defaults_to_eu_and_refuses_other_regions():
    assert S3ObjectStore("b", client=FakeS3()).region.startswith("eu-")
    with pytest.raises(StorageConfigError):
        S3ObjectStore("b", region="us-east-1", client=FakeS3())
    assert S3ObjectStore("b", region="us-east-1", allow_non_eu_region=True, client=FakeS3())


def test_s3_bucket_must_be_versioned_and_in_the_eu():
    fake = FakeS3()
    S3ObjectStore("b", client=fake).verify_bucket()
    fake.versioning = "Suspended"
    with pytest.raises(StorageConfigError):
        S3ObjectStore("b", client=fake).verify_bucket()
    fake.versioning, fake.location = "Enabled", "us-west-2"
    with pytest.raises(StorageConfigError):
        S3ObjectStore("b", client=fake).verify_bucket()


def test_s3_object_lock_needs_mode_and_retention():
    with pytest.raises(StorageConfigError):
        S3ObjectStore("b", object_lock_mode="COMPLIANCE", client=FakeS3())
    with pytest.raises(StorageConfigError):
        S3ObjectStore("b", object_lock_mode="FOREVER", object_lock_retention=timedelta(days=1), client=FakeS3())


# --------------------------------------------------------------------------- registry


def registry(tmp_path, **kw) -> EvidenceRegistry:
    return EvidenceRegistry(LocalObjectStore(tmp_path), clock=lambda: T0, **kw)


def register(reg: EvidenceRegistry, data: bytes = PDF, tenant: str = "t1", **kw):
    params = dict(tenant_id=tenant, source_kind=SourceKind.EMAIL, format=EvidenceFormat.PDF,
                  mime_type="application/pdf")
    params.update(kw)
    return reg.register(data, **params)


def test_register_creates_immutable_evidence(tmp_path):
    reg = registry(tmp_path)
    r = register(reg, filename="FT 183.pdf", metadata={"declared": "application/pdf"},
                 context={"parent_evidence_id": "ev_parent"})
    ev = r.evidence
    assert r.created
    assert ev.sha256 == sha(PDF) and ev.id == evidence_id_for("t1", sha(PDF))
    assert ev.storage_key == object_key("t1", sha(PDF))
    assert ev.retrieved_at == T0 and ev.filename == "FT 183.pdf"
    assert ev.metadata == {"declared": "application/pdf", "size": len(PDF)}
    assert r.sighting.context["parent_evidence_id"] == "ev_parent"
    with pytest.raises(ValidationError):
        ev.sha256 = "0" * 64  # frozen (§55)
    assert reg.open("t1", ev.id) == PDF


def test_same_bytes_dedupe_and_add_sightings(tmp_path):
    reg = registry(tmp_path)
    first = register(reg, source_kind=SourceKind.EMAIL, metadata={"n": 1})
    second = register(reg, source_kind=SourceKind.SUPPLIER_PORTAL, original_url="https://portal/x.pdf",
                      metadata={"n": 2})
    assert not second.created
    assert second.evidence == first.evidence  # the original is never changed
    assert second.evidence.metadata["n"] == 1
    kinds = [s.source_kind for s in reg.sightings("t1", first.evidence.id)]
    assert kinds == [SourceKind.EMAIL, SourceKind.SUPPLIER_PORTAL]


def test_dedupe_is_per_tenant(tmp_path):
    reg = registry(tmp_path)
    a = register(reg, tenant="tenant-a")
    b = register(reg, tenant="tenant-b")
    assert a.created and b.created and a.evidence.id != b.evidence.id
    with pytest.raises(ObjectNotFound):
        reg.open("tenant-b", a.evidence.id)  # tenant isolation


def test_register_rejects_empty_naive_time_and_float_metadata(tmp_path):
    reg = registry(tmp_path)
    with pytest.raises(ValueError):
        register(reg, data=b"")
    with pytest.raises(ValueError):
        register(reg, retrieved_at=datetime(2026, 9, 1, 12, 0))
    with pytest.raises(TypeError):
        register(reg, metadata={"amount": 92.40})
    with pytest.raises(TypeError):
        register(reg, context={"when": T0})


def test_open_detects_tampered_storage(tmp_path):
    reg = registry(tmp_path)
    ev = register(reg).evidence
    path = tmp_path / ev.storage_key
    os.chmod(path, 0o644)
    path.write_bytes(b"swapped")
    with pytest.raises(IntegrityError):
        reg.open("t1", ev.id)


def test_scanner_quarantines_but_keeps_the_original(tmp_path):
    class Scanner:
        def scan(self, data, mime_type):
            return ScanVerdict(clean=False, signature="Eicar-Test")

    reg = registry(tmp_path, scanner=Scanner())
    ev = register(reg).evidence
    assert ev.metadata["quarantined"] is True
    assert reg.open("t1", ev.id) == PDF


def test_rearrival_restores_a_lost_original(tmp_path):
    reg = registry(tmp_path)
    ev = register(reg).evidence
    path = tmp_path / ev.storage_key
    os.chmod(path, 0o644)
    path.unlink()
    with pytest.raises(ObjectNotFound):
        reg.open("t1", ev.id)
    again = register(reg, source_kind=SourceKind.SUPPLIER_PORTAL)
    assert not again.created and reg.open("t1", ev.id) == PDF


def test_concurrent_registration_yields_one_evidence(tmp_path):
    reg = registry(tmp_path)
    results = []
    threads = [threading.Thread(target=lambda: results.append(register(reg))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({r.evidence.id for r in results}) == 1
    assert sum(r.created for r in results) == 1
    assert len(reg.sightings("t1", results[0].evidence.id)) == 6
