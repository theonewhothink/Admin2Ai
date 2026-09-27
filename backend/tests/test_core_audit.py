"""Append-only, hash-chained audit log (§55)."""

from __future__ import annotations

import dataclasses
import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import count

import pytest

from backoffice.audit import (
    AuditConflict,
    AuditEntry,
    AuditLog,
    AuditRecord,
    AuditStore,
    ChainProblem,
    InMemoryAuditStore,
    canonical_json,
    compute_hash,
    genesis_hash,
    verify_chain,
)
from backoffice.domain.models import ExtractionMethod, Quality

T0 = datetime(2026, 9, 18, 14, 42, tzinfo=timezone.utc)


def fixed_clock():
    ticks = count()
    return lambda: T0 + timedelta(seconds=next(ticks))


def entry(i: int = 0, **kw) -> AuditEntry:
    base = dict(
        actor="system",
        agent="verification",
        model="none",
        parser="pp-ocrv6-medium",
        action="verify_total",
        subject_id=f"doc_{i}",
        evidence_ids=[f"ev_{i}", "ev_qr"],
        extracted_values={"gross_amount": Decimal("1492.30"), "currency": "EUR"},
        validations=[{"check": "qr_total", "ok": True, "method": ExtractionMethod.QR}],
        response={"quality": Quality.GREEN},
    )
    base.update(kw)
    return AuditEntry(**base)


def filled(n: int = 5, *, tenant: str = "t1", key: bytes | None = None):
    store = InMemoryAuditStore()
    log = AuditLog(store, clock=fixed_clock(), key=key)
    for i in range(n):
        log.record(tenant, entry(i))
    return store, log


def chain(store: InMemoryAuditStore, tenant: str = "t1") -> list[AuditRecord]:
    return list(store.records(tenant))


def forge(record: AuditRecord, **changes) -> AuditRecord:
    return dataclasses.replace(record, **changes)


# --------------------------------------------------------------------------- writing


def test_records_are_sequenced_and_linked_from_genesis():
    store, log = filled(3)
    records = chain(store)
    assert [r.seq for r in records] == [1, 2, 3]
    assert records[0].prev_hash == genesis_hash("t1")
    assert records[1].prev_hash == records[0].hash
    assert records[2].prev_hash == records[1].hash
    assert log.verify("t1").ok


def test_record_stores_every_section_55_field():
    store, log = filled(1)
    r = chain(store)[0]
    data = r.data()
    assert set(data) == {
        "v", "tenant_id", "seq", "at", "actor", "agent", "model", "parser", "subject_id",
        "evidence_ids", "extracted_values", "validations", "action", "response", "corrections",
    }  # fmt: skip
    assert data["extracted_values"]["gross_amount"] == "1492.30"  # exact, never float
    assert data["validations"][0]["method"] == "qr"
    assert data["response"] == {"quality": "verified"}
    assert r.at == T0 and r.at.tzinfo is not None
    assert (r.actor, r.action, r.evidence_ids) == (
        "system",
        "verify_total",
        ("ev_0", "ev_qr"),
    )


def test_corrections_are_new_records_and_originals_stay():
    store, log = filled(1)
    original = chain(store)[0]
    log.record(
        "t1",
        AuditEntry(
            actor="owner:1",
            action="correct_field",
            subject_id="doc_0",
            evidence_ids=["ev_0"],
            corrections=[
                {
                    "field": "gross_amount",
                    "from": Decimal("1492.30"),
                    "to": Decimal("1429.30"),
                }
            ],
        ),
    )
    records = chain(store)
    assert records[0] == original
    assert records[1].data()["corrections"][0] == {
        "field": "gross_amount",
        "from": "1492.30",
        "to": "1429.30",
    }
    assert log.verify("t1").ok


def test_records_are_immutable_and_parsed_views_are_copies():
    store, _ = filled(1)
    r = chain(store)[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.body = "{}"  # type: ignore[misc]
    view = r.data()
    view["actor"] = "attacker"
    assert r.actor == "system"


def test_caller_mutation_after_recording_does_not_change_the_record():
    store = InMemoryAuditStore()
    log = AuditLog(store, clock=fixed_clock())
    values = {"gross_amount": Decimal("10.00")}
    log.record("t1", entry(extracted_values=values))
    values["gross_amount"] = Decimal("99.99")
    assert chain(store)[0].data()["extracted_values"] == {"gross_amount": "10.00"}
    assert log.verify("t1").ok


def test_hashing_is_deterministic():
    a, _ = filled(4)
    b, _ = filled(4)
    assert [r.hash for r in chain(a)] == [r.hash for r in chain(b)]


def test_canonical_json_ignores_key_order():
    assert canonical_json({"b": 1, "a": {"y": 2, "x": 3}}) == canonical_json(
        {"a": {"x": 3, "y": 2}, "b": 1}
    )


def test_tenants_have_independent_chains():
    store = InMemoryAuditStore()
    log = AuditLog(store, clock=fixed_clock())
    log.record("t1", entry(1))
    log.record("t2", entry(1))
    log.record("t1", entry(2))
    assert [r.seq for r in chain(store, "t1")] == [1, 2]
    assert [r.seq for r in chain(store, "t2")] == [1]
    assert genesis_hash("t1") != genesis_hash("t2")
    assert log.verify("t1").ok and log.verify("t2").ok


def test_records_can_be_read_from_a_sequence_number():
    store, _ = filled(5)
    assert [r.seq for r in store.records("t1", from_seq=4)] == [4, 5]
    assert list(store.records("nobody")) == []


# --------------------------------------------------------------------------- input validation


@pytest.mark.parametrize(
    "kw",
    [
        {"actor": " "},
        {"action": ""},
        {"evidence_ids": "ev_1"},
        {"evidence_ids": ["ev_1", ""]},
    ],
)
def test_invalid_entries_are_rejected(kw):
    with pytest.raises(ValueError):
        entry(**kw)


def test_naive_clock_is_rejected():
    log = AuditLog(InMemoryAuditStore(), clock=lambda: datetime(2026, 9, 18, 14, 42))
    with pytest.raises(ValueError, match="timezone-aware"):
        log.record("t1", entry())


@pytest.mark.parametrize(
    "value, error",
    [
        (float("nan"), ValueError),
        (Decimal("NaN"), ValueError),
        (datetime(2026, 1, 1), ValueError),
        (object(), TypeError),
        (b"raw", TypeError),
    ],
)
def test_values_without_an_exact_encoding_are_rejected(value, error):
    log = AuditLog(InMemoryAuditStore(), clock=fixed_clock())
    with pytest.raises(error):
        log.record("t1", entry(response={"value": value}))
    assert log.verify("t1").checked == 0


def test_empty_tenant_is_rejected():
    with pytest.raises(ValueError):
        AuditLog(InMemoryAuditStore()).record("", entry())


# --------------------------------------------------------------------------- tamper detection


def test_edited_body_is_detected():
    store, _ = filled(5)
    records = chain(store)
    data = records[2].data()
    data["extracted_values"]["gross_amount"] = "1429.30"
    records[2] = forge(records[2], body=canonical_json(data))
    report = verify_chain(records, tenant_id="t1")
    assert (report.ok, report.problem, report.seq, report.checked) == (
        False,
        ChainProblem.HASH_MISMATCH,
        3,
        2,
    )


def test_edit_with_recomputed_hash_breaks_the_next_link():
    store, _ = filled(5)
    records = chain(store)
    data = records[2].data()
    data["actor"] = "owner:1"
    body = canonical_json(data)
    records[2] = forge(
        records[2], body=body, hash=compute_hash(records[2].prev_hash, body)
    )
    report = verify_chain(records, tenant_id="t1")
    assert (report.problem, report.seq) == (ChainProblem.BROKEN_LINK, 4)


def test_full_rewrite_is_caught_by_the_anchor():
    store, log = filled(3)
    anchor = chain(store)[-1].hash
    rewritten: list[AuditRecord] = []
    prev = genesis_hash("t1")
    for r in chain(store):
        data = r.data()
        data["actor"] = "someone-else"
        body = canonical_json(data)
        rewritten.append(
            forge(r, body=body, prev_hash=prev, hash=compute_hash(prev, body))
        )
        prev = rewritten[-1].hash
    assert verify_chain(rewritten, tenant_id="t1").ok  # internally consistent...
    report = verify_chain(rewritten, tenant_id="t1", expected_head=anchor)
    assert report.problem is ChainProblem.HEAD_MISMATCH  # ...but not the chain we wrote


def test_deleted_middle_record_is_detected():
    store, _ = filled(5)
    records = chain(store)
    del records[1]
    report = verify_chain(records, tenant_id="t1")
    assert (report.problem, report.seq) == (ChainProblem.BAD_SEQUENCE, 3)


def test_deleted_last_record_is_detected_by_the_log_checkpoint():
    store, log = filled(5)
    store._chains["t1"].pop()  # simulate someone deleting rows behind the log's back
    report = log.verify("t1")
    assert (report.problem, report.seq, report.checked) == (
        ChainProblem.CHECKPOINT_MISMATCH,
        5,
        4,
    )


def test_rewritten_record_at_the_checkpoint_is_detected():
    store, log = filled(3)
    checkpoint = log.checkpoint("t1")
    assert checkpoint is not None and checkpoint[0] == 3
    records = chain(store)
    data = records[2].data()
    data["actor"] = "attacker"
    body = canonical_json(data)
    records[2] = forge(
        records[2], body=body, hash=compute_hash(records[2].prev_hash, body)
    )
    report = verify_chain(records, tenant_id="t1", checkpoint=checkpoint)
    assert (report.problem, report.seq) == (ChainProblem.CHECKPOINT_MISMATCH, 3)


def test_other_writers_on_the_same_store_do_not_raise_false_alarms():
    store = InMemoryAuditStore()
    mine, theirs = (
        AuditLog(store, clock=fixed_clock()),
        AuditLog(store, clock=fixed_clock()),
    )
    mine.record("t1", entry(1))
    theirs.record("t1", entry(2))
    theirs.record("t1", entry(3))
    assert mine.verify("t1").ok and theirs.verify("t1").ok
    assert mine.checkpoint("t1")[0] == 1 and theirs.checkpoint("t1")[0] == 3


def test_reordered_records_are_detected():
    store, _ = filled(5)
    records = chain(store)
    records[1], records[2] = records[2], records[1]
    report = verify_chain(records, tenant_id="t1")
    assert (report.problem, report.seq) == (ChainProblem.BAD_SEQUENCE, 3)


def test_renumbered_reorder_is_detected():
    store, _ = filled(5)
    records = chain(store)
    a, b = records[1], records[2]
    records[1], records[2] = forge(b, seq=2), forge(a, seq=3)
    report = verify_chain(records, tenant_id="t1")
    assert (report.problem, report.seq) == (ChainProblem.BROKEN_LINK, 2)


def test_record_from_another_tenant_is_detected():
    store, _ = filled(3)
    other, _ = filled(3, tenant="t2")
    records = chain(store)
    records[1] = chain(other, "t2")[1]
    report = verify_chain(records, tenant_id="t1")
    assert (report.problem, report.seq) == (ChainProblem.WRONG_TENANT, 2)


def test_transplanted_chain_fails_on_genesis():
    other, _ = filled(2, tenant="t2")
    moved = [forge(r, tenant_id="t1") for r in chain(other, "t2")]
    report = verify_chain(moved, tenant_id="t1")
    assert (report.problem, report.seq) == (ChainProblem.BROKEN_LINK, 1)


def test_body_that_disagrees_with_its_columns_is_detected():
    prev = genesis_hash("t1")
    body = canonical_json({"tenant_id": "t1", "seq": 7, "actor": "x"})
    forged = AuditRecord("t1", 1, body, prev, compute_hash(prev, body))
    report = verify_chain([forged], tenant_id="t1")
    assert report.problem is ChainProblem.BODY_MISMATCH


def test_body_that_is_not_json_is_detected():
    prev = genesis_hash("t1")
    forged = AuditRecord("t1", 1, "not json", prev, compute_hash(prev, "not json"))
    assert verify_chain([forged], tenant_id="t1").problem is ChainProblem.BODY_MISMATCH


def test_empty_chain_is_valid_and_ends_at_genesis():
    report = verify_chain([], tenant_id="t1")
    assert report.ok and report.checked == 0 and report.head_hash == genesis_hash("t1")


def test_clean_report_names_the_head():
    store, log = filled(3)
    report = log.verify("t1")
    assert (
        report.ok and report.checked == 3 and report.head_hash == chain(store)[-1].hash
    )
    assert log.verify("t1", expected_head=report.head_hash).ok


# --------------------------------------------------------------------------- keyed (HMAC) chains


def test_keyed_chain_verifies_only_with_its_key():
    store, log = filled(3, key=b"k" * 32)
    assert log.verify("t1").ok
    assert verify_chain(chain(store), tenant_id="t1", key=b"k" * 32).ok
    assert (
        verify_chain(chain(store), tenant_id="t1").problem is ChainProblem.HASH_MISMATCH
    )
    assert (
        verify_chain(chain(store), tenant_id="t1", key=b"x" * 32).problem
        is ChainProblem.HASH_MISMATCH
    )


@pytest.mark.parametrize("key", [b"", b"short"])
def test_weak_keys_are_refused(key):
    with pytest.raises(ValueError, match="at least"):
        AuditLog(InMemoryAuditStore(), key=key)


def test_keyed_chain_cannot_be_rewritten_without_the_key():
    store, _ = filled(2, key=b"k" * 32)
    records = chain(store)
    data = records[1].data()
    data["actor"] = "attacker"
    body = canonical_json(data)
    records[1] = forge(
        records[1], body=body, hash=compute_hash(records[1].prev_hash, body)
    )
    report = verify_chain(records, tenant_id="t1", key=b"k" * 32)
    assert (report.problem, report.seq) == (ChainProblem.HASH_MISMATCH, 2)


# --------------------------------------------------------------------------- store contract & concurrency


def test_in_memory_store_satisfies_the_protocol():
    assert isinstance(InMemoryAuditStore(), AuditStore)


def test_store_refuses_records_that_do_not_extend_the_head():
    store, _ = filled(2)
    head = store.head("t1")
    assert head is not None
    body = canonical_json({"tenant_id": "t1", "seq": 3})
    with pytest.raises(AuditConflict):
        store.append(AuditRecord("t1", 3, body, "0" * 64, compute_hash("0" * 64, body)))
    with pytest.raises(AuditConflict):
        store.append(
            AuditRecord("t1", 2, body, head.hash, compute_hash(head.hash, body))
        )
    with pytest.raises(AuditConflict):
        store.append(AuditRecord("t9", 2, body, genesis_hash("t9"), "x"))


class FlakyStore(InMemoryAuditStore):
    """Loses the race ``failures`` times, as if another writer appended first."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def append(self, record: AuditRecord) -> None:
        if self.failures:
            self.failures -= 1
            raise AuditConflict("lost the race")
        super().append(record)


def test_log_retries_after_losing_a_race():
    store = FlakyStore(failures=2)
    log = AuditLog(store, clock=fixed_clock())
    assert log.record("t1", entry()).seq == 1
    assert log.verify("t1").ok


def test_log_gives_up_after_max_retries():
    log = AuditLog(FlakyStore(failures=5), clock=fixed_clock(), max_retries=3)
    with pytest.raises(AuditConflict):
        log.record("t1", entry())


def test_concurrent_writers_produce_one_valid_chain():
    store = InMemoryAuditStore()
    log = AuditLog(store, max_retries=1000)

    def write(n: int) -> None:
        for i in range(25):
            log.record("t1", entry(n * 100 + i))

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    report = log.verify("t1")
    assert report.ok and report.checked == 200


def test_postgres_schema_keeps_body_as_verbatim_text_and_forbids_changes():
    from backoffice.audit import POSTGRES_SCHEMA

    assert "body       TEXT" in POSTGRES_SCHEMA and "JSONB" not in POSTGRES_SCHEMA
    assert (
        "BEFORE UPDATE OR DELETE" in POSTGRES_SCHEMA
        and "BEFORE TRUNCATE" in POSTGRES_SCHEMA
    )
    assert json.loads(canonical_json({"ok": True})) == {"ok": True}
