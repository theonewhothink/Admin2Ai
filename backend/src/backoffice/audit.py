"""Append-only, hash-chained audit log (§52 immutable audit history, §55).

Every operation stores evidence, actor, timestamp, agent, model, parser,
extracted values, validations, action, response and corrections. Records are
never edited: a correction is a new record.

Each tenant has its own chain. A record's ``body`` is canonical JSON and is
hashed verbatim together with the previous record's hash::

    hash = SHA-256(prev_hash + "\\n" + body)        (HMAC-SHA-256 when a key is set)

The first record links to a tenant-specific genesis hash, so a chain cannot be
transplanted to another tenant. :func:`verify_chain` detects edited, reordered,
inserted and deleted records. Truncation and wholesale rewrites are detected
against a checkpoint ``(seq, hash)`` or an expected head kept outside the store.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from backoffice.domain.models import utcnow

__all__ = [
    "MIN_KEY_BYTES",
    "POSTGRES_SCHEMA",
    "SCHEMA_VERSION",
    "AuditConflict",
    "AuditEntry",
    "AuditError",
    "AuditLog",
    "AuditRecord",
    "AuditStore",
    "ChainProblem",
    "ChainReport",
    "InMemoryAuditStore",
    "canonical_json",
    "compute_hash",
    "genesis_hash",
    "verify_chain",
]

SCHEMA_VERSION = 1
MIN_KEY_BYTES = 32  # HMAC-SHA-256 key: at least the digest size
_GENESIS_PREFIX = "backoffice.audit.v1:"


class AuditError(Exception):
    """Base class for audit errors (developer-facing, never shown to owners)."""


class AuditConflict(AuditError):
    """The record does not extend the current head (a concurrent append won)."""


def genesis_hash(tenant_id: str) -> str:
    """The ``prev_hash`` of a tenant's first record."""
    return hashlib.sha256(f"{_GENESIS_PREFIX}{tenant_id}".encode()).hexdigest()


def _jsonable(value: Any) -> Any:
    """``json.dumps`` fallback: exact, deterministic encodings only."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal cannot be audited")
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("audited datetimes must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    raise TypeError(f"cannot audit value of type {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, no NaN, Decimal as string."""
    return json.dumps(
        value,
        default=_jsonable,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def compute_hash(prev_hash: str, body: str, key: bytes | None = None) -> str:
    """Link hash of one record."""
    message = f"{prev_hash}\n{body}".encode()
    if key is None:
        return hashlib.sha256(message).hexdigest()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class AuditEntry:
    """What an operation reports; sealed into an :class:`AuditRecord` by the log."""

    actor: str  # who acted: "system", "owner:<id>", "accountant:<id>"
    action: str  # what was done, e.g. "extract", "match", "request_invoice"
    agent: str | None = None  # §46 agent name
    model: str | None = None  # AI model and version, if any
    parser: str | None = None  # parser / OCR engine and version, if any
    subject_id: str | None = None  # the item, document or transaction concerned
    evidence_ids: Sequence[str] = ()
    extracted_values: Mapping[str, Any] = field(default_factory=dict)
    validations: Sequence[Any] = ()
    response: Any = None
    corrections: Sequence[Any] = ()

    def __post_init__(self) -> None:
        if not self.actor.strip() or not self.action.strip():
            raise ValueError("audit entries need an actor and an action")
        if isinstance(self.evidence_ids, str) or any(
            not isinstance(e, str) or not e for e in self.evidence_ids
        ):
            raise ValueError("evidence_ids must be a sequence of non-empty strings")


@dataclass(frozen=True)
class AuditRecord:
    """One sealed record. ``body`` is the canonical JSON that was hashed."""

    tenant_id: str
    seq: int
    body: str
    prev_hash: str
    hash: str

    def data(self) -> dict[str, Any]:
        """A fresh parsed copy of the body (mutating it cannot alter the record)."""
        parsed: dict[str, Any] = json.loads(self.body)
        return parsed

    @property
    def at(self) -> datetime:
        return datetime.fromisoformat(str(self.data()["at"]))

    @property
    def actor(self) -> str:
        return str(self.data()["actor"])

    @property
    def action(self) -> str:
        return str(self.data()["action"])

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(self.data()["evidence_ids"])


@runtime_checkable
class AuditStore(Protocol):
    """Persistence for audit chains.

    Implementations must be append-only and make ``append`` atomic: it raises
    :class:`AuditConflict` unless ``record`` extends the tenant's current head
    (``seq == head.seq + 1`` and ``prev_hash == head.hash``, or ``seq == 1`` and
    the genesis hash for an empty chain). A Postgres implementation should use
    :data:`POSTGRES_SCHEMA`.
    """

    def head(self, tenant_id: str) -> AuditRecord | None: ...

    def append(self, record: AuditRecord) -> None: ...

    def records(
        self, tenant_id: str, *, from_seq: int = 1
    ) -> Iterator[AuditRecord]: ...


# Schema for a Postgres AuditStore. ``body`` is TEXT, not JSONB: JSONB would
# re-serialize the JSON and change the hashed bytes. The primary key and the
# (tenant_id, prev_hash) uniqueness make concurrent forks impossible; the
# triggers forbid UPDATE, DELETE and TRUNCATE. Not exercised by the unit tests.
POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_record (
    tenant_id  TEXT        NOT NULL,
    seq        BIGINT      NOT NULL CHECK (seq >= 1),
    at         TIMESTAMPTZ NOT NULL,
    body       TEXT        NOT NULL,
    prev_hash  CHAR(64)    NOT NULL,
    hash       CHAR(64)    NOT NULL,
    PRIMARY KEY (tenant_id, seq),
    UNIQUE (tenant_id, prev_hash),
    UNIQUE (hash)
);

CREATE OR REPLACE FUNCTION audit_record_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_record is append-only';
END;
$$;

DROP TRIGGER IF EXISTS audit_record_no_change ON audit_record;
CREATE TRIGGER audit_record_no_change
    BEFORE UPDATE OR DELETE ON audit_record
    FOR EACH ROW EXECUTE FUNCTION audit_record_append_only();

DROP TRIGGER IF EXISTS audit_record_no_truncate ON audit_record;
CREATE TRIGGER audit_record_no_truncate
    BEFORE TRUNCATE ON audit_record
    FOR EACH STATEMENT EXECUTE FUNCTION audit_record_append_only();
"""


class InMemoryAuditStore:
    """Thread-safe in-memory :class:`AuditStore` for tests and local runs."""

    def __init__(self) -> None:
        self._chains: dict[str, list[AuditRecord]] = {}
        self._lock = threading.Lock()

    def head(self, tenant_id: str) -> AuditRecord | None:
        with self._lock:
            chain = self._chains.get(tenant_id)
            return chain[-1] if chain else None

    def append(self, record: AuditRecord) -> None:
        with self._lock:
            chain = self._chains.setdefault(record.tenant_id, [])
            expected_seq = len(chain) + 1
            expected_prev = chain[-1].hash if chain else genesis_hash(record.tenant_id)
            if record.seq != expected_seq or record.prev_hash != expected_prev:
                raise AuditConflict("record does not extend the current head")
            chain.append(record)

    def records(self, tenant_id: str, *, from_seq: int = 1) -> Iterator[AuditRecord]:
        with self._lock:
            snapshot = list(self._chains.get(tenant_id, ()))
        return iter(snapshot[max(from_seq, 1) - 1 :])


class ChainProblem(str, Enum):
    WRONG_TENANT = "wrong_tenant"
    BAD_SEQUENCE = "bad_sequence"  # a record was deleted, inserted or moved
    BROKEN_LINK = "broken_link"  # prev_hash does not point at the previous record
    HASH_MISMATCH = "hash_mismatch"  # the body was edited
    BODY_MISMATCH = "body_mismatch"  # body disagrees with the record's columns
    HEAD_MISMATCH = "head_mismatch"  # the chain ends somewhere other than expected
    CHECKPOINT_MISMATCH = (
        "checkpoint_mismatch"  # a known (seq, hash) is missing or differs
    )


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    checked: int
    head_hash: str
    problem: ChainProblem | None = None
    seq: int | None = None
    detail: str = ""


def verify_chain(
    records: Iterable[AuditRecord],
    *,
    tenant_id: str,
    expected_head: str | None = None,
    checkpoint: tuple[int, str] | None = None,
    key: bytes | None = None,
) -> ChainReport:
    """Re-derive every link of a tenant's chain, stopping at the first problem.

    ``expected_head``: the chain must end at this hash.
    ``checkpoint``: the chain must contain record ``seq`` with this hash (it may
    continue past it, e.g. written by another process).
    """
    prev = genesis_hash(tenant_id)
    checked = 0
    for expected_seq, record in enumerate(records, start=1):
        problem = _check_record(record, tenant_id, expected_seq, prev, key)
        if problem is None and checkpoint and record.seq == checkpoint[0]:
            if not hmac.compare_digest(record.hash, checkpoint[1]):
                problem = (
                    ChainProblem.CHECKPOINT_MISMATCH,
                    "record differs from the checkpoint",
                )
        if problem is not None:
            kind, detail = problem
            return ChainReport(False, checked, prev, kind, record.seq, detail)
        prev = record.hash
        checked += 1
    if checkpoint and checked < checkpoint[0]:
        return ChainReport(
            False, checked, prev, ChainProblem.CHECKPOINT_MISMATCH, checkpoint[0],
            "chain is shorter than a known checkpoint",
        )  # fmt: skip
    if expected_head is not None and expected_head != prev:
        return ChainReport(
            False, checked, prev, ChainProblem.HEAD_MISMATCH, None,
            "chain does not end at the expected head",
        )  # fmt: skip
    return ChainReport(True, checked, prev)


def _check_record(
    record: AuditRecord, tenant_id: str, expected_seq: int, prev: str, key: bytes | None
) -> tuple[ChainProblem, str] | None:
    if record.tenant_id != tenant_id:
        return ChainProblem.WRONG_TENANT, "record belongs to another tenant"
    if record.seq != expected_seq:
        return (
            ChainProblem.BAD_SEQUENCE,
            f"expected seq {expected_seq}, found {record.seq}",
        )
    if record.prev_hash != prev:
        return ChainProblem.BROKEN_LINK, "prev_hash does not match the previous record"
    if not hmac.compare_digest(compute_hash(prev, record.body, key), record.hash):
        return ChainProblem.HASH_MISMATCH, "record content does not match its hash"
    try:
        body = json.loads(record.body)
    except ValueError:
        return ChainProblem.BODY_MISMATCH, "body is not valid JSON"
    if body.get("tenant_id") != record.tenant_id or body.get("seq") != record.seq:
        return (
            ChainProblem.BODY_MISMATCH,
            "body tenant or seq disagrees with the record",
        )
    return None


class AuditLog:
    """Writes and verifies tenant audit chains on top of an :class:`AuditStore`.

    The log keeps the newest ``(seq, hash)`` it wrote per tenant as a checkpoint,
    so :meth:`verify` also catches records removed from the end of the store,
    while records appended by other writers do not raise false alarms.
    Pass ``key`` (from the secrets vault / KMS, §52) to make the chain an HMAC
    chain that cannot be recomputed by someone with database access alone.
    """

    def __init__(
        self,
        store: AuditStore,
        *,
        clock: Callable[[], datetime] = utcnow,
        key: bytes | None = None,
        max_retries: int = 3,
    ) -> None:
        if key is not None and len(key) < MIN_KEY_BYTES:
            raise ValueError(f"audit HMAC key must be at least {MIN_KEY_BYTES} bytes")
        self._store = store
        self._clock = clock
        self._key = key
        self._max_retries = max_retries
        self._checkpoints: dict[str, tuple[int, str]] = {}
        self._checkpoint_lock = threading.Lock()

    def record(self, tenant_id: str, entry: AuditEntry) -> AuditRecord:
        """Seal ``entry`` as the tenant's next record and append it."""
        if not tenant_id:
            raise ValueError("tenant_id is required")
        at = self._now()
        for _ in range(self._max_retries):
            head = self._store.head(tenant_id)
            record = self._seal(tenant_id, entry, at, head)
            try:
                self._store.append(record)
            except AuditConflict:
                continue
            self._remember(record)
            return record
        raise AuditConflict(f"could not append after {self._max_retries} attempts")

    def verify(
        self, tenant_id: str, *, expected_head: str | None = None
    ) -> ChainReport:
        """Verify the tenant's chain against this log's checkpoint (and ``expected_head``)."""
        with self._checkpoint_lock:
            checkpoint = self._checkpoints.get(tenant_id)
        return verify_chain(
            self._store.records(tenant_id),
            tenant_id=tenant_id,
            expected_head=expected_head,
            checkpoint=checkpoint,
            key=self._key,
        )

    def checkpoint(self, tenant_id: str) -> tuple[int, str] | None:
        """Newest ``(seq, hash)`` this log wrote; publish it to anchor the chain."""
        with self._checkpoint_lock:
            return self._checkpoints.get(tenant_id)

    def _remember(self, record: AuditRecord) -> None:
        with self._checkpoint_lock:
            current = self._checkpoints.get(record.tenant_id)
            if current is None or record.seq > current[0]:
                self._checkpoints[record.tenant_id] = (record.seq, record.hash)

    def _now(self) -> datetime:
        at = self._clock()
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("audit clock must return timezone-aware datetimes")
        return at

    def _seal(
        self, tenant_id: str, entry: AuditEntry, at: datetime, head: AuditRecord | None
    ) -> AuditRecord:
        seq = head.seq + 1 if head else 1
        prev = head.hash if head else genesis_hash(tenant_id)
        body = canonical_json(
            {
                "v": SCHEMA_VERSION,
                "tenant_id": tenant_id,
                "seq": seq,
                "at": at,
                "actor": entry.actor,
                "agent": entry.agent,
                "model": entry.model,
                "parser": entry.parser,
                "subject_id": entry.subject_id,
                "evidence_ids": list(entry.evidence_ids),
                "extracted_values": dict(entry.extracted_values),
                "validations": list(entry.validations),
                "action": entry.action,
                "response": entry.response,
                "corrections": list(entry.corrections),
            }
        )
        return AuditRecord(
            tenant_id, seq, body, prev, compute_hash(prev, body, self._key)
        )
