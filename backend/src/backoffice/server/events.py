"""The tenant event log: what is recorded, how it is chained, how state is fingerprinted.

Every change to a tenant is one event, stored as canonical JSON text::

    {"v": 1, "seq": 7, "at": "2026-10-02T09:31:12.000123+01:00", "kind": "request",
     "actor": "usr_...", "pre": "<state digest before>", "data": {...}}

``hash = SHA-256(prev_hash + "\\n" + body)``; the first event links to
SHA-256("backoffice.events.v1:" + tenant id). The database recomputes and
enforces the chain (migration 0007), so a missing, reordered or edited event
is caught on load.

``pre`` is the digest of the tenant's state just before the event was applied
(:func:`state_digest`). Replaying recomputes it before applying each event and
refuses to go on when they differ: a replay that diverges is never served.

File bytes never go into an event: uploads are written to the content-addressed
object store first and the event keeps ``{"$object": {"key", "sha256", "size"}}``.
Passwords (an IMAP app password) never go into an event either: they go to the
vault and the event keeps ``"$redacted"``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backoffice.orchestrator import TZ

from .store import StoredEvent

__all__ = [
    "Event",
    "EventError",
    "FILE_FIELDS",
    "REDACTED",
    "canonical",
    "chain_hash",
    "genesis_hash",
    "normalize",
    "restore_files",
    "sanitize_body",
    "state_digest",
]

VERSION = 1
GENESIS_PREFIX = "backoffice.events.v1:"
FILE_FIELDS = ("dataBase64", "data_base64")
SECRET_FIELDS = ("password",)
REDACTED = "$redacted"
OBJECT = "$object"


class EventError(Exception):
    """A stored event is malformed or does not match its hash (developer-facing)."""


def canonical(value: Any) -> str:
    """Sorted keys, no spaces, ASCII only (so no NUL or odd code point ever reaches the database)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
                      default=_default)


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if hasattr(value, "value") and isinstance(value.value, (str, int)):
        return value.value
    return str(value)


def normalize(value: Any) -> Any:
    """The JSON value as a replay will see it: key order sorted, tuples as lists."""
    return json.loads(canonical(value))


def genesis_hash(tenant_id: str) -> str:
    return hashlib.sha256((GENESIS_PREFIX + tenant_id).encode()).hexdigest()


def chain_hash(prev_hash: str, body: str) -> str:
    return hashlib.sha256(f"{prev_hash}\n{body}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Event:
    seq: int
    at: datetime
    kind: str
    actor: str
    data: dict[str, Any]
    pre: str | None
    prev_hash: str
    hash: str
    body: str

    @classmethod
    def make(cls, *, seq: int, at: datetime, kind: str, actor: str, data: Mapping[str, Any], pre: str | None,
             prev_hash: str) -> Event:
        if at.tzinfo is None:
            raise EventError("event times carry their time zone")
        payload = {"v": VERSION, "seq": seq, "at": at.isoformat(), "kind": kind, "actor": actor,
                   "pre": pre, "data": normalize(dict(data))}
        body = canonical(payload)
        return cls(seq=seq, at=at, kind=kind, actor=actor, data=payload["data"], pre=pre, prev_hash=prev_hash,
                   hash=chain_hash(prev_hash, body), body=body)

    @classmethod
    def parse(cls, row: StoredEvent) -> Event:
        if chain_hash(row.prev_hash, row.body) != row.hash:
            raise EventError(f"event {row.seq} does not match its hash")
        try:
            payload = json.loads(row.body)
            at = datetime.fromisoformat(payload["at"])
            if at.tzinfo is not None:
                at = at.astimezone(TZ)  # the engine's own zone object, as when it was recorded
            if payload["v"] != VERSION or payload["seq"] != row.seq or payload["kind"] != row.kind:
                raise ValueError
            data, pre, actor = payload["data"], payload["pre"], payload["actor"]
        except (ValueError, KeyError, TypeError) as exc:
            raise EventError(f"event {row.seq} is malformed") from exc
        if not isinstance(data, dict) or at.tzinfo is None:
            raise EventError(f"event {row.seq} is malformed")
        return cls(seq=row.seq, at=at, kind=row.kind, actor=str(actor), data=data, pre=pre,
                   prev_hash=row.prev_hash, hash=row.hash, body=row.body)

    def stored(self) -> StoredEvent:
        return StoredEvent(seq=self.seq, at=self.at, kind=self.kind, actor=self.actor, body=self.body,
                           prev_hash=self.prev_hash, hash=self.hash)


# --------------------------------------------------------------------------- request bodies


def sanitize_body(body: Mapping[str, Any], put_file: Callable[[bytes], dict[str, Any]]) -> dict[str, Any]:
    """The body as it goes into an event: file bytes become object references, passwords are dropped.

    ``put_file`` stores bytes and returns ``{"key", "sha256", "size"}``.
    Raises ``ValueError`` for base64 that does not decode (the request is refused).
    """
    clean: dict[str, Any] = {}
    for name, value in body.items():
        if name in FILE_FIELDS and isinstance(value, str) and value:
            try:
                data = base64.b64decode(value, validate=False)
            except (binascii.Error, ValueError):
                raise ValueError("file is not base64") from None
            clean[name] = {OBJECT: put_file(data)}
        elif name in SECRET_FIELDS and isinstance(value, str) and value:
            clean[name] = REDACTED
        else:
            clean[name] = value
    return normalize(clean)


def restore_files(body: Mapping[str, Any], get_file: Callable[[Mapping[str, Any]], bytes]) -> dict[str, Any]:
    """The body as the engine saw it live: object references become base64 again."""
    out = dict(body)
    for name in FILE_FIELDS:
        ref = out.get(name)
        if isinstance(ref, Mapping) and OBJECT in ref:
            out[name] = base64.b64encode(get_file(ref[OBJECT])).decode("ascii")
    return out


def object_refs(data: Any) -> list[dict[str, Any]]:
    """Every object reference inside an event's data (for exports)."""
    found: list[dict[str, Any]] = []
    if isinstance(data, Mapping):
        if OBJECT in data and isinstance(data[OBJECT], Mapping):
            found.append(dict(data[OBJECT]))
        for value in data.values():
            found += object_refs(value)
    elif isinstance(data, list):
        for value in data:
            found += object_refs(value)
    return found


# --------------------------------------------------------------------------- state digest


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def state_digest(svc: Any) -> str:
    """Fingerprint of everything a tenant's state holds, cheap enough to take before every change.

    It covers the engine (audit chain head, which records every agent step
    with its time; items, documents, payments, questions, obligations,
    connectors, companies, rules, the clock) and the service around it (sign-in
    state, report settings, API keys, the owner's tasks, drafts and reports).
    Two tenants with the same digest answer every read the same way.
    """
    repo = svc.repo
    head = repo.audit_store.head(repo.tenant_id)
    op = getattr(svc, "_assistant", None)
    summary = {
        "now": repo.clock.now().isoformat(),
        "audit": [head.seq, head.hash] if head is not None else None,
        "activity": [len(repo.activity), [(a.id, a.at.isoformat(), a.text) for a in repo.activity[-3:]]],
        "owner": [repo.owner.full_name, repo.owner.email],
        "companies": sorted([c.id, c.name, c.tax_id, list(c.own_ibans), repo.legal_names.get(c.id)]
                            for c in repo.companies.values()),
        "accounts": sorted([a.id, a.bank, a.holder_id, a.iban, a.card_last4, a.owned] for a in repo.accounts.values()),
        "suppliers": sorted([s.id, s.name, list(s.known_ibans), list(s.aliases)] for s in repo.suppliers.values()),
        "connectors": sorted([c.id, c.kind, c.account, c.healthy, list(c.company_ids), _iso(c.covered_from),
                              _iso(c.covered_until), _iso(c.last_synced_at)] for c in repo.connectors.values()),
        "relationships": [[r.id, r.kind, r.name, r.company_id] for r in repo.relationships],
        "accountant": [repo.accountant.id, repo.accountant.email, repo.accountant.person, repo.accountant.software]
        if repo.accountant else None,
        "documents": sorted([d.id, d.document.quality.value, d.on_hold, d.hold_released, sorted(d.matched_tx_ids),
                             list(d.evidence_ids)] for d in repo.documents.values()),
        "transactions": sorted([t.id, t.tx.entity_id, t.private, list(t.document_ids), list(t.proof_evidence_ids)]
                               for t in repo.transactions.values()),
        "items": sorted([i.id, i.stage.value, i.quality.value, len(i.history)] for i in repo.items.values()),
        "needs": sorted([n.id, n.status, n.answer] for n in repo.needs.values()),
        "obligations": sorted([o.obligation.id, list(o.satisfied_by)] for o in repo.obligations.values()),
        "chases": sorted(repo.chases),
        "questions": sorted([q.id, q.status] for q in repo.accountant_questions.values()),
        "closed": sorted([k[0], k[1], v.isoformat()] for k, v in repo.closed_months.items()),
        "rules": [r.id for r in repo.rulebook.rules],
        "grants": [[g.action.value, g.entity_id] for g in repo.policy.grants],
        "evidence": len(repo.store),
        "pending_links": list(repo.pending_links),
        "sign_in": {k: {n: str(v) for n, v in sorted(info.items())} for k, info in sorted(svc.sign_in.items())},
        "report": getattr(svc, "_report_cfg", None),
        "api_keys": sorted([k, v.get("hash"), v.get("name")] for k, v in (getattr(svc, "_api_keys", None) or {}).items()),
        "tasks": sorted([t.id, t.status, t.title, t.due.isoformat() if t.due else None] for t in op.tasks.values())
        if op else [],
        "outbox": sorted([m.id, m.status] for m in op.outbox.values()) if op else [],
        "reports": sorted(op.reports) if op else [],
        "seq": getattr(op, "_seq", 0),
    }
    return hashlib.sha256(canonical(summary).encode()).hexdigest()
