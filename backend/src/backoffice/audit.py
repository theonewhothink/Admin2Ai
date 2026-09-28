"""Append-only, hash-chained audit log (§52 immutable audit history, §55).

Every operation stores evidence, actor, timestamp, agent, model, parser,
extracted values, validations, action, response and corrections. Records are
never edited: a correction is a new record.

Each tenant has its own chain. A record's ``body`` is canonical JSON and is
hashed verbatim together with the previous record's hash::

    hash = SHA-256(prev_hash + "\\n" + body)        (HMAC-SHA-256 when a key is set)

The first record links to a tenant-specific genesis hash, so a chain cannot be
transplanted to another tenant. :func:`verify_chain` detects edited, reordered,
inserted and deleted records, and timestamps that go backwards. Truncation and
wholesale rewrites are detected against a checkpoint ``(seq, hash)`` or an
expected head kept outside the store. Verification never raises on damaged
records: it reports the first problem it finds.

Bodies are canonical JSON built from an exact encoding: Decimal as a string,
timezone-aware datetimes in UTC, enums by value, string keys only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
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
    "PostgresAuditStore",
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


def _plain(value: Any) -> Any:
    """``value`` as plain JSON types, using exact, deterministic encodings only.

    Raises ``TypeError`` for types without one (bytes, objects, non-string
    keys) and ``ValueError`` for values that cannot be exact (NaN, naive
    datetimes, keys that collide once encoded).
    """
    if isinstance(value, Enum):
        return _plain(value.value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number cannot be audited")
        return value
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
    if isinstance(value, BaseModel):
        return _plain(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return _plain_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        items = [_plain(v) for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
    raise TypeError(f"cannot audit value of type {type(value).__name__}")


def _plain_mapping(value: Mapping[Any, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in value.items():
        name = key.value if isinstance(key, Enum) else key
        if not isinstance(name, str):
            raise TypeError(f"audit keys must be strings, not {type(key).__name__}")
        if name in out:
            raise ValueError(f"duplicate audit key {name!r}")
        out[name] = _plain(item)
    return out


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, no NaN, Decimal as string.

    ASCII only (non-ASCII as \\u escapes), so any text, even a lone surrogate
    from a badly encoded email header, hashes and stores the same everywhere.
    """
    return json.dumps(
        _plain(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def compute_hash(prev_hash: str, body: str, key: bytes | None = None) -> str:
    """Link hash of one record (any text encodes, so damaged records still hash)."""
    message = f"{prev_hash}\n{body}".encode("utf-8", "surrogatepass")
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
        if not _is_text(self.actor) or not _is_text(self.action):
            raise ValueError("audit entries need an actor and an action")
        for name in ("agent", "model", "parser", "subject_id"):
            value = getattr(self, name)
            if value is not None and not _is_text(value):
                raise ValueError(f"{name} must be a non-blank string or None")
        if isinstance(self.evidence_ids, (str, bytes)) or any(
            not _is_text(e) for e in self.evidence_ids
        ):
            raise ValueError("evidence_ids must be a sequence of non-empty strings")
        # Snapshot the containers so later caller mutation cannot change the entry.
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "extracted_values", dict(self.extracted_values))


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


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
    the genesis hash for an empty chain). :class:`InMemoryAuditStore` and
    :class:`PostgresAuditStore` (with :data:`POSTGRES_SCHEMA`) implement it.
    """

    def head(self, tenant_id: str) -> AuditRecord | None: ...

    def append(self, record: AuditRecord) -> None: ...

    def records(
        self, tenant_id: str, *, from_seq: int = 1
    ) -> Iterator[AuditRecord]: ...


# Schema for :class:`PostgresAuditStore`. ``body`` is TEXT, not JSONB: JSONB would
# re-serialize the JSON and change the hashed bytes. The primary key and the
# (tenant_id, prev_hash) uniqueness make concurrent forks impossible; the
# triggers forbid UPDATE, DELETE and TRUNCATE. The unit tests run the store's SQL
# on SQLite with the same keys; this DDL itself (plpgsql) needs a real Postgres.
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


class PostgresAuditStore:
    """:class:`AuditStore` on PostgreSQL through a DB-API 2.0 driver (§52, §55).

    ``connect`` returns a context manager yielding a connection, e.g.
    ``lambda: contextlib.closing(psycopg.connect(dsn))`` or a psycopg_pool
    ``pool.connection``. Create the table with :data:`POSTGRES_SCHEMA`.

    ``append`` checks the head inside its transaction and relies on the
    ``(tenant_id, seq)`` primary key and ``(tenant_id, prev_hash)`` uniqueness:
    a writer that loses a race gets an integrity error, reported as
    :class:`AuditConflict` so :class:`AuditLog` retries on the new head.
    ``integrity_error`` defaults to ``psycopg.IntegrityError``; ``placeholder``
    is ``"%s"`` for psycopg (``"?"`` for qmark drivers).
    """

    _COLUMNS = "tenant_id, seq, body, prev_hash, hash"

    def __init__(
        self,
        connect: Callable[[], AbstractContextManager[Any]],
        *,
        integrity_error: type[BaseException] | None = None,
        placeholder: str = "%s",
        table: str = "audit_record",
    ) -> None:
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", table):
            raise ValueError("table must be a plain lower-case SQL identifier")
        if placeholder not in ("%s", "?"):
            raise ValueError("placeholder must be '%s' or '?'")
        self._connect = connect
        self._integrity_error = integrity_error or _psycopg_integrity_error()
        self._p = placeholder
        self._table = table

    def head(self, tenant_id: str) -> AuditRecord | None:
        with self._transaction() as cursor:
            return self._read_head(cursor, tenant_id)

    def append(self, record: AuditRecord) -> None:
        p = self._p
        sql = (
            f"INSERT INTO {self._table} (tenant_id, seq, at, body, prev_hash, hash) "
            f"VALUES ({p}, {p}, {p}, {p}, {p}, {p})"
        )
        at = _head_time(record)
        if at is None:
            raise AuditError("record body has no valid timestamp")
        try:
            with self._transaction() as cursor:
                head = self._read_head(cursor, record.tenant_id)
                expected_seq = head.seq + 1 if head else 1
                expected_prev = head.hash if head else genesis_hash(record.tenant_id)
                if record.seq != expected_seq or record.prev_hash != expected_prev:
                    raise AuditConflict("record does not extend the current head")
                cursor.execute(
                    sql,
                    (record.tenant_id, record.seq, at, record.body, record.prev_hash, record.hash),
                )
        except self._integrity_error as exc:
            raise AuditConflict("another writer extended the chain first") from exc

    def records(self, tenant_id: str, *, from_seq: int = 1) -> Iterator[AuditRecord]:
        p = self._p
        sql = (
            f"SELECT {self._COLUMNS} FROM {self._table} "
            f"WHERE tenant_id = {p} AND seq >= {p} ORDER BY seq"
        )
        with self._transaction() as cursor:
            cursor.execute(sql, (tenant_id, max(from_seq, 1)))
            rows = cursor.fetchall()
        return iter([AuditRecord(*row) for row in rows])

    def _read_head(self, cursor: Any, tenant_id: str) -> AuditRecord | None:
        cursor.execute(
            f"SELECT {self._COLUMNS} FROM {self._table} "
            f"WHERE tenant_id = {self._p} ORDER BY seq DESC LIMIT 1",
            (tenant_id,),
        )
        row = cursor.fetchone()
        return AuditRecord(*row) if row else None

    def _transaction(self) -> AbstractContextManager[Any]:
        return _Transaction(self._connect)


class _Transaction:
    """Cursor inside one committed-or-rolled-back transaction."""

    def __init__(self, connect: Callable[[], AbstractContextManager[Any]]) -> None:
        self._manager = connect()
        self._conn: Any = None

    def __enter__(self) -> Any:
        self._conn = self._manager.__enter__()
        return self._conn.cursor()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._manager.__exit__(exc_type, exc, tb)
        return None


def _psycopg_integrity_error() -> type[BaseException]:
    try:
        import psycopg  # optional dependency, only for the Postgres store
    except ImportError as exc:
        raise AuditError(
            "PostgresAuditStore needs psycopg, or pass integrity_error= for another driver"
        ) from exc
    error: type[BaseException] = psycopg.IntegrityError
    return error


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
    TIME_REVERSED = "time_reversed"  # a record is dated before its predecessor


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
    prev_at: datetime | None = None
    checked = 0
    for expected_seq, record in enumerate(records, start=1):
        problem, at = _check_record(record, tenant_id, expected_seq, prev, key)
        if problem is None and prev_at is not None and at is not None and at < prev_at:
            problem = ChainProblem.TIME_REVERSED, "record is dated before the previous one"
        if problem is None and checkpoint and record.seq == checkpoint[0]:
            if not _same_digest(record.hash, checkpoint[1]):
                problem = (
                    ChainProblem.CHECKPOINT_MISMATCH,
                    "record differs from the checkpoint",
                )
        if problem is not None:
            kind, detail = problem
            seq = record.seq if isinstance(record.seq, int) else None
            return ChainReport(False, checked, prev, kind, seq, detail)
        prev, prev_at = record.hash, at
        checked += 1
    if checkpoint and checked < checkpoint[0]:
        return ChainReport(
            False, checked, prev, ChainProblem.CHECKPOINT_MISMATCH, checkpoint[0],
            "chain is shorter than a known checkpoint",
        )  # fmt: skip
    if expected_head is not None and not _same_digest(expected_head, prev):
        return ChainReport(
            False, checked, prev, ChainProblem.HEAD_MISMATCH, None,
            "chain does not end at the expected head",
        )  # fmt: skip
    return ChainReport(True, checked, prev)


_Problem = tuple[ChainProblem, str]


def _same_digest(a: object, b: object) -> bool:
    """Constant-time equality that is simply False for non-text or odd values."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return hmac.compare_digest(a.encode("utf-8", "surrogatepass"), b.encode("utf-8", "surrogatepass"))


def _check_record(
    record: AuditRecord, tenant_id: str, expected_seq: int, prev: str, key: bytes | None
) -> tuple[_Problem | None, datetime | None]:
    """The record's first problem (if any) and its timestamp. Never raises."""
    if record.tenant_id != tenant_id:
        return (ChainProblem.WRONG_TENANT, "record belongs to another tenant"), None
    if type(record.seq) is not int or record.seq != expected_seq:
        detail = f"expected seq {expected_seq}, found {record.seq!r}"
        return (ChainProblem.BAD_SEQUENCE, detail), None
    if not _same_digest(record.prev_hash, prev):
        detail = "prev_hash does not match the previous record"
        return (ChainProblem.BROKEN_LINK, detail), None
    if not isinstance(record.body, str):
        return (ChainProblem.BODY_MISMATCH, "body is not text"), None
    if not _same_digest(compute_hash(prev, record.body, key), record.hash):
        detail = "record content does not match its hash"
        return (ChainProblem.HASH_MISMATCH, detail), None
    return _check_body(record)


def _check_body(record: AuditRecord) -> tuple[_Problem | None, datetime | None]:
    try:
        body = json.loads(record.body)
    except (ValueError, RecursionError):
        return (ChainProblem.BODY_MISMATCH, "body is not valid JSON"), None
    if not isinstance(body, dict):
        return (ChainProblem.BODY_MISMATCH, "body is not a record object"), None
    seq = body.get("seq")
    if body.get("tenant_id") != record.tenant_id or type(seq) is not int or seq != record.seq:
        detail = "body tenant or seq disagrees with the record"
        return (ChainProblem.BODY_MISMATCH, detail), None
    at = _parse_at(body.get("at"))
    if at is None:
        return (ChainProblem.BODY_MISMATCH, "body has no valid timestamp"), None
    return None, at


def _parse_at(value: object) -> datetime | None:
    """A timezone-aware timestamp from a body, or None."""
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return at if at.tzinfo is not None and at.utcoffset() is not None else None


def _head_time(head: AuditRecord | None) -> datetime | None:
    """The head's timestamp; None when there is no head or its body is damaged
    (verification reports the damage; writing must not stop because of it)."""
    if head is None:
        return None
    try:
        return _parse_at(json.loads(head.body).get("at"))
    except (ValueError, TypeError, AttributeError, RecursionError):
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
        if max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        self._store = store
        self._clock = clock
        self._key = key
        self._max_retries = max_retries
        self._checkpoints: dict[str, tuple[int, str]] = {}
        self._checkpoint_lock = threading.Lock()

    def record(self, tenant_id: str, entry: AuditEntry) -> AuditRecord:
        """Seal ``entry`` as the tenant's next record and append it.

        The timestamp is taken per attempt and never precedes the head's, so
        the chain stays in time order even if clocks drift or a race is lost.
        """
        if not _is_text(tenant_id):
            raise ValueError("tenant_id is required")
        for _ in range(self._max_retries):
            head = self._store.head(tenant_id)
            at = self._now()
            head_at = _head_time(head)
            if head_at is not None and head_at > at:
                at = head_at
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
