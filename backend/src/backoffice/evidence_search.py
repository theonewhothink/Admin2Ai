"""Searching before asking (§22): a missing document is looked for in every place the business connected before
any supplier is asked for it, and every search is recorded.

The places are the connections that can be searched (``ConnectorState.searchable``: a mailbox, Google Drive or
OneDrive, the accounting software). When a payment's document is missing (§21: it needs one, nothing matched it
for a few days), :meth:`SearchAgent.requests` describes it for the production sync worker, which searches the
places live and records what each gave, before the event is recorded (server/search.py, missing/searches.py).
:meth:`SearchAgent.record` applies that record, live and on replay alike:

* the autopilot (``missing.MissingEvidenceAutopilot``) goes through the places in the spec's order (current email,
  historical email, cloud storage, the supplier's portal, the accounting software, the supplier's usual way of
  sending); each place's files are read into the normal pipeline when it is asked (ingestion, verification,
  reconciliation, with their provenance: where, which file, which path, when changed), and the first place whose
  document verifies and matches the payment ends the search: the payment closes on that evidence (§3);
* nothing found: the supplier request (§22, §25) is written as before, only now, never before the search;
* a place that could not be searched: looked again after a pause (at most three rounds, then the request goes);
* a likely or conflicting document: the usual one-tap confirmation or question, never a guess (§19, §57).

Each search is kept (what, when, where, the result) for the owner and the accountant: "I searched your email,
your Google Drive and Moloni: it is not there." A business with no searchable connection (the demo) asks the
supplier exactly as before.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import Quality, SourceKind
from backoffice.learning import day_month, display_name, format_money, join_and
from backoffice.missing import (
    AttemptOutcome,
    EvidenceCandidate,
    FoundFile,
    MissingEvidenceAutopilot,
    NextStep,
    RecordedSearch,
    SearchPattern,
    SearchRequest,
    SearchSource,
    run_recorded,
)
from backoffice.orchestrator import MESSAGE_ID_DOMAIN, TZ, ConnectorState, DocumentRecord, TxRecord, _Agent
from backoffice.policy import ActionContext, ActionKind, authorize

__all__ = ["MAX_SEARCH_ROUNDS", "SEARCH_RETRY_AFTER", "SearchAgent", "SearchRecord"]

SEARCH_RETRY_AFTER = timedelta(hours=6)  # a place that could not be searched: looked again after this
MAX_SEARCH_ROUNDS = 3  # then the supplier is asked anyway (the places that worked had nothing)
SEARCHES_PER_PASS = 10
WINDOW_BEFORE = timedelta(days=45)  # invoices usually precede their payment ...
WINDOW_AFTER = timedelta(days=15)  # ... a card receipt can follow it by a few days

_KINDS: dict[SearchSource, tuple[SourceKind, str]] = {
    SearchSource.CURRENT_EMAIL: (SourceKind.EMAIL, "email"),
    SearchSource.HISTORICAL_EMAIL: (SourceKind.EMAIL, "email"),
    SearchSource.RECURRING_SEQUENCE: (SourceKind.EMAIL, "email"),
    SearchSource.FILES: (SourceKind.CLOUD_STORAGE, "files"),
    SearchSource.SUPPLIER_PORTAL: (SourceKind.SUPPLIER_PORTAL, "portal"),
    SearchSource.ACCOUNTING_PLATFORM: (SourceKind.ACCOUNTING_SYSTEM, "accounting"),
}
_OUTCOMES = frozenset({"found", "nothing", "failed", "timed_out"})
_PROVENANCE_KEYS = ("source", "provider", "fileId", "name", "path", "modifiedAt", "webUrl", "messageId", "threadId",
                    "folder", "receivedAt", "documentId", "direction", "type", "number", "date", "portalId")
_STATUS = {NextStep.MATCH_FOUND: "found", NextStep.CONFIRM_WITH_OWNER: "likely",
           NextStep.RESOLVE_CONFLICT: "conflict", NextStep.RETRY_LATER: "retry"}
_RESULTS = {
    AttemptOutcome.VERIFIED: "Found it.",
    AttemptOutcome.LIKELY: "Found a document that is likely it.",
    AttemptOutcome.CONFLICT: "Found a document whose details disagree with the payment.",
    AttemptOutcome.NOTHING: "Nothing there.",
    AttemptOutcome.FAILED: "I couldn't search it this time.",
    AttemptOutcome.TIMED_OUT: "It did not answer in time.",
}
_SETTLED = ("Not searched.", "Not needed: found it earlier.", "Nothing there.", _RESULTS[AttemptOutcome.TIMED_OUT],
            _RESULTS[AttemptOutcome.FAILED], "Found documents, but none of them is this one.")


def _weight(result: str) -> int:
    """What a place's line says when it was searched twice: what it gave beats "nothing", which beats "not
    searched"."""
    return _SETTLED.index(result) + 1 if result in _SETTLED else (len(_SETTLED) + 1 if result else 0)


_WORD = re.compile(r"[a-zà-ÿ]{3,}")
_COMMON = frozenset({"the", "and", "for", "your", "you", "from", "with", "fwd", "para", "com", "sua", "seu", "dos",
                     "das", "pdf", "www"})


def place_of(c: ConnectorState) -> str:
    """A connection as the owner reads it in "I searched ...": "your email", "your Google Drive", "Moloni"."""
    if c.kind == "email":
        return "your email"
    if c.kind == "files":
        return f"your {c.name}"
    return c.name


@dataclass
class SearchRecord:
    """One payment's (or usual invoice's) search: when, each place and what it gave, and what followed."""

    subject_id: str
    kind: str  # "payment" | "expected_invoice"
    round: int
    at: datetime
    status: str  # "found" | "likely" | "conflict" | "nothing" | "retry"
    attempts: tuple[dict[str, Any], ...]  # source, place, startedAt, finishedAt, outcome, found, result (plain)
    found_in: str | None = None
    document_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    next_at: datetime | None = None
    next_step: str = ""
    earlier: list[dict[str, Any]] = field(default_factory=list)  # the rounds before this one

    @property
    def searched(self) -> list[str]:
        """The places that were actually searched (in order, each once)."""
        return list(dict.fromkeys(a["place"] for a in self.attempts if a.get("outcome") in ("found", "nothing")))

    @property
    def unreachable(self) -> list[str]:
        return list(dict.fromkeys(a["place"] for a in self.attempts if a.get("outcome") in ("failed", "timed_out")))


class SearchAgent(_Agent):
    """§22 searches 1-6 for each missing document, before any supplier is asked (module docstring)."""

    name = "missing_evidence"

    # ----------------------------------------------------------------- places

    def places(self, company_id: str | None = None) -> list[ConnectorState]:
        """Connections that can be searched now, for this company."""
        return sorted((c for c in self.repo.connectors.values() if c.searchable and c.healthy
                       and (company_id is None or not c.company_ids or company_id in c.company_ids)),
                      key=lambda c: (c.kind != "email", c.kind, c.id))

    def place_names(self, company_id: str | None) -> list[str]:
        return list(dict.fromkeys(place_of(c) for c in self.places(company_id)))

    def waiting(self, subject_id: str, company_id: str | None) -> bool:
        """True while the document is still to be searched for: no supplier is asked before (§22)."""
        record = self.repo.evidence_searches.get(subject_id)
        if not self.places(company_id):
            return False
        return record is None or record.status == "retry"

    def _next_round(self, subject_id: str, now: datetime) -> int | None:
        record = self.repo.evidence_searches.get(subject_id)
        if record is None:
            return 1
        if record.status == "retry" and (record.next_at is None or record.next_at <= now):
            return record.round + 1
        return None

    def _expected_round(self, subject_id: str) -> int | None:
        record = self.repo.evidence_searches.get(subject_id)
        if record is None:
            return 1
        return record.round + 1 if record.status == "retry" else None

    # ----------------------------------------------------------------- what to search for (read-only)

    def payment_missing(self, rec: TxRecord) -> bool:
        item = self.repo.items[rec.item_id]
        return (rec.missing_since is not None and not rec.document_ids and not rec.private
                and not rec.likely_document_ids and rec.decision is not None and rec.decision.requires_document
                and not rec.proof_evidence_ids and not item.is_done
                and item.stage not in (Stage.NEEDS_OWNER, Stage.CONFLICT) and rec.id not in self.repo.chases)

    def requests(self, now: datetime) -> list[dict[str, Any]]:
        """The documents to search for now, each described for the sync worker (JSON). Changes nothing."""
        repo = self.repo
        if not any(c.searchable and c.healthy for c in repo.connectors.values()):
            return []
        out: list[SearchRequest] = []
        for rec in sorted(repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id)):
            if not self.payment_missing(rec):
                continue
            number = self._next_round(rec.id, now)
            places = self.places(rec.company_id)
            if number is not None and places:
                out.append(self._payment_request(rec, number, places))
        for record in sorted(repo.expected_invoices.values(), key=lambda e: e.id):
            if record.status != "missing" or record.message is not None:
                continue
            number = self._next_round(record.id, now)
            places = self.places(record.company_id)
            if number is not None and places:
                out.append(self._expected_request(record, number, places, now))
        return [r.to_json() for r in out[:SEARCHES_PER_PASS]]

    def _supplier_terms(self, supplier: Any) -> tuple[tuple[str, ...], SearchPattern | None]:
        if supplier is None:
            return (), None
        domains = [d.lower() for d in supplier.email_domains]
        if supplier.contact_email and "@" in supplier.contact_email:
            domains.append(supplier.contact_email.split("@", 1)[1].lower())
        return tuple(dict.fromkeys(domains)), self.pattern(supplier.id)

    def _payment_request(self, rec: TxRecord, number: int, places: Sequence[ConnectorState]) -> SearchRequest:
        supplier = self.repo.resolver().resolve_transaction(rec.tx).supplier
        domains, pattern = self._supplier_terms(supplier)
        sales = rec.decision is not None and rec.decision.rule == "customer_refund"
        return SearchRequest(
            subject_id=rec.id, subject_kind="payment", round=number, company_id=rec.company_id,
            amount=abs(rec.tx.amount), currency=rec.tx.currency, paid_on=rec.tx.booked_on,
            window_start=rec.tx.booked_on - WINDOW_BEFORE, window_end=rec.tx.booked_on + WINDOW_AFTER,
            counterparty=rec.tx.counterparty, supplier_name=display_name(supplier.name) if supplier else None,
            supplier_tax_id=supplier.tax_id if supplier else None, supplier_domains=domains,
            invoice_number=self.o.statements.number_for(rec), direction="sales" if sales else "purchases",
            pattern=pattern, connections=tuple(c.id for c in places), entity_id=rec.tx.entity_id)

    def _expected_request(self, record: Any, number: int, places: Sequence[ConnectorState],
                          now: datetime) -> SearchRequest:
        supplier = self.repo.suppliers.get(record.supplier_id or "")
        domains, pattern = self._supplier_terms(supplier)
        series = record.series
        amount = series.typical_amount if series.fixed_amount else None
        return SearchRequest(
            subject_id=record.id, subject_kind="expected_invoice", round=number, company_id=record.company_id,
            amount=amount, currency=series.currency or "EUR", paid_on=record.expected_on,
            window_start=record.earliest, window_end=max(now.astimezone(TZ).date(), record.due_on),
            counterparty=record.supplier_name, supplier_name=record.supplier_name,
            supplier_tax_id=supplier.tax_id if supplier else None, supplier_domains=domains, pattern=pattern,
            connections=tuple(c.id for c in places), entity_id=record.company_id)

    def pattern(self, supplier_id: str) -> SearchPattern | None:
        """How this supplier's invoices usually arrive, from the earlier ones that came by email: the usual
        sender, the words every subject shared and the words every attachment's name shared (§22 search 6)."""
        repo = self.repo
        docs = sorted((d for d in repo.documents.values() if d.supplier_id == supplier_id and d.sender
                       and not d.sales and not d.supporting), key=lambda d: (d.received_at, d.id))
        if len(docs) < 2:
            return None
        senders = tuple(s for s, _ in Counter((d.sender or "").lower() for d in docs).most_common(3))

        def words(text: str) -> list[str]:
            return [w for w in _WORD.findall(text.lower()) if w not in _COMMON]

        subjects = [words((d.message_text or "").split("\n", 1)[0]) for d in docs]
        shared = [w for w in dict.fromkeys(subjects[0]) if all(w in s for s in subjects[1:])][:3]
        names = []
        for d in docs:
            try:
                names.append(words(repo.evidence(d.evidence_ids[0]).filename or "") if d.evidence_ids else [])
            except Exception:  # noqa: BLE001 - evidence gone: no file-name pattern
                names.append([])
        files = [w for w in dict.fromkeys(names[0]) if all(w in n for n in names[1:])][:1] if names else []
        return SearchPattern(senders, tuple(shared), tuple(files))

    # ----------------------------------------------------------------- applying a recorded search

    def record(self, subject_id: str, round_number: int, attempts: Sequence[Mapping[str, Any]],
               files: Sequence[FoundFile], at: datetime) -> dict[str, Any]:
        """Apply one recorded round of searching (module docstring). Returns what came of it."""
        repo = self.repo
        rec = repo.transactions.get(subject_id)
        expected = repo.expected_invoices.get(subject_id) if rec is None else None
        if rec is None and expected is None:
            raise KeyError(subject_id)
        if round_number != self._expected_round(subject_id):
            self.log("search_ignored", subject_id=subject_id, values={"round": round_number})
            return {"ok": True, "ignored": True}
        clean = [self._attempt(a) for a in attempts]
        clean = [a for a in clean if a is not None]
        by_source: dict[SearchSource, tuple[list[dict[str, Any]], list[FoundFile]]] = {}
        for a in clean:
            by_source.setdefault(SearchSource(a["source"]), ([], []))[0].append(a)
        for f in files:
            if f.source in by_source:
                by_source[f.source][1].append(f)
        made: dict[SearchSource, list[DocumentRecord]] = {}

        def look(source: SearchSource, found: Sequence[FoundFile]) -> list[EvidenceCandidate]:
            docs = self._ingest(found, at)
            made[source] = docs
            return [c for d in docs if (c := self._candidate(rec, expected, d)) is not None]

        searches = [RecordedSearch(source, entry[0], entry[1], look) for source, entry in by_source.items()]
        company_id = (rec.tx.entity_id or rec.holder_id) if rec is not None else expected.company_id
        company = repo.companies[company_id]
        supplier = (repo.resolver().resolve_transaction(rec.tx).supplier if rec is not None
                    else repo.suppliers.get(expected.supplier_id or ""))
        if rec is not None:
            request = self._payment_request(rec, round_number, ())
        else:
            request = self._expected_request(expected, round_number, (), at)
        pilot = MissingEvidenceAutopilot(searches, authorize=lambda r: self._may_ask(r.entity_id, subject_id),
                                         timeout_seconds=None, clock=lambda: at,
                                         message_id_domain=MESSAGE_ID_DOMAIN)
        outcome = run_recorded(pilot.run(request.evidence_query(repo.tenant_id), company=company, supplier=supplier,
                                         today=at.astimezone(TZ).date(),
                                         allow_incomplete=round_number >= MAX_SEARCH_ROUNDS))
        status = _STATUS.get(outcome.next_step, "nothing")
        asked = {a.source: a.outcome for a in outcome.attempts}
        for a in clean:
            done = asked.get(SearchSource(a["source"]))
            if done is None:
                a["result"] = "Not needed: found it earlier." if status == "found" else "Not searched."
            elif a["outcome"] in ("failed", "timed_out"):
                a["result"] = _RESULTS[AttemptOutcome.TIMED_OUT if a["outcome"] == "timed_out" else
                                       AttemptOutcome.FAILED]
            elif done is AttemptOutcome.NOTHING and made.get(SearchSource(a["source"])):
                a["result"] = "Found documents, but none of them is this one."
            else:
                a["result"] = _RESULTS[done]
        found = outcome.found
        docs = [d for ds in made.values() for d in ds]
        earlier = []
        previous = repo.evidence_searches.get(subject_id)
        if previous is not None:
            earlier = [*previous.earlier, {"round": previous.round, "at": previous.at.isoformat(),
                                           "status": previous.status, "searched": previous.searched,
                                           "unreachable": previous.unreachable}]
        record = SearchRecord(
            subject_id=subject_id, kind="payment" if rec is not None else "expected_invoice", round=round_number,
            at=at, status=status, attempts=tuple(clean),
            found_in=next((a["place"] for a in clean if found and SearchSource(a["source"]) is found.source), None),
            document_ids=tuple(dict.fromkeys(d.id for d in docs)),
            evidence_ids=tuple(dict.fromkeys(e for d in docs for e in d.evidence_ids)),
            next_at=at + SEARCH_RETRY_AFTER if status == "retry" else None, next_step=outcome.next_step.value,
            earlier=earlier)
        repo.evidence_searches[subject_id] = record
        self.log("search_evidence", subject_id=subject_id, evidence_ids=list(record.evidence_ids),
                 values={"round": round_number, "attempts": [{k: a.get(k) for k in
                                                              ("source", "place", "startedAt", "finishedAt",
                                                               "outcome", "found", "failure")} for a in clean]},
                 response={"status": status, "next_step": outcome.next_step.value,
                           "found": found.evidence_id if found else None})
        self._tell(record, rec, expected, found, at)
        self.o.run(at)  # nothing found: the supplier request (when allowed) is written now, not before
        return {"ok": True, "status": status, "documents": list(record.document_ids),
                "nextStep": outcome.next_step.value}

    @staticmethod
    def _attempt(raw: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            source = SearchSource(str(raw.get("source")))
        except ValueError:
            return None
        outcome = str(raw.get("outcome") or "")
        if outcome not in _OUTCOMES:
            return None
        found = raw.get("found")
        out = {"source": source.value, "place": " ".join(str(raw.get("place") or "").split())[:80] or source.value,
               "startedAt": str(raw.get("startedAt") or ""), "finishedAt": str(raw.get("finishedAt") or ""),
               "outcome": outcome, "found": found if isinstance(found, int) and not isinstance(found, bool) else 0}
        if raw.get("failure"):
            out["failure"] = str(raw["failure"])[:120]  # internal (the audit log), never owner copy
        return out

    def _may_ask(self, company_id: str | None, subject_id: str) -> bool:
        decision = authorize(ActionKind.SUPPLIER_INVOICE_REQUEST, self.repo.policy, ActionContext(
            tenant_id=self.repo.tenant_id, entity_id=company_id, subject_id=subject_id))
        return decision.allowed_now

    def _ingest(self, found: Sequence[FoundFile], at: datetime) -> list[DocumentRecord]:
        """Read one place's files into the pipeline (with their provenance), then reconcile: the documents they
        gave, new or already on file."""
        repo = self.repo
        before = set(repo.documents)
        evidence: set[str] = set()
        made: list[str] = []
        for f in found:
            kind, origin = _KINDS[f.source]
            context = {"found_by": "missing_document_search",
                       **{k: v for k, v in f.provenance.items() if k in _PROVENANCE_KEYS
                          and (v is None or isinstance(v, (str, int, bool)))}}
            report = self.o.ingest_file(f.data, filename=f.filename, content_type=f.content_type, source_kind=kind,
                                        at=at, origin=origin, run=False, context=context)
            evidence |= set(report.evidence_ids)
            made += report.document_ids
        self.o.run(at)
        ids = set(made) | (set(repo.documents) - before)
        if evidence:
            registry, tenant = repo.registry, repo.tenant_id
            for d in repo.documents.values():
                if d.id in ids:
                    continue
                if evidence & set(d.evidence_ids) or any(
                        s.context.get("parent_evidence_id") in evidence
                        for e in d.evidence_ids for s in registry.sightings(tenant, e)):
                    ids.add(d.id)
        return [repo.documents[i] for i in sorted(ids) if i in repo.documents]

    def _candidate(self, rec: TxRecord | None, expected: Any, doc: DocumentRecord) -> EvidenceCandidate | None:
        """What a document found for this missing one is: the verified proof (GREEN), a likely one (AMBER), one
        whose details disagree (RED), or unrelated (None: it stays on file for whatever it proves)."""
        quality: Quality | None = None
        if rec is not None:
            item = self.repo.items[rec.item_id]
            if doc.id in rec.document_ids:
                quality = Quality.GREEN if item.stage is Stage.CLOSED else Quality.AMBER
            elif doc.id in rec.likely_document_ids:
                quality = Quality.AMBER
            elif doc.document.quality is Quality.RED and doc.supplier_id and \
                    (s := self.repo.resolver().resolve_transaction(rec.tx).supplier) is not None and \
                    s.id == doc.supplier_id:
                quality = Quality.RED
        elif expected.status == "received" and expected.document_id == doc.id:
            quality = Quality.GREEN
        if quality is None or not doc.evidence_ids:
            return None
        return EvidenceCandidate(evidence_id=doc.evidence_ids[0], document_id=doc.id,
                                 invoice_number=doc.document.invoice_number, quality=quality)

    def _tell(self, record: SearchRecord, rec: TxRecord | None, expected: Any, found: Any, at: datetime) -> None:
        """One quiet Activity line (§42): found it, or looked everywhere and it is not there."""
        if rec is None:
            return  # a usual invoice that arrived says so itself ("The EDP invoice for September arrived.")
        amount = format_money(abs(rec.tx.amount), rec.tx.currency)
        who = self.o.merchant_name(rec.tx)
        if record.status == "found" and found is not None:
            self.o.activity(at, "recovered", f"Found the invoice for the {amount} payment to {who} in "
                            f"{record.found_in or 'your files'}.", rec.company_id, amount=abs(rec.tx.amount),
                            currency=rec.tx.currency, evidence_ids=[rec.evidence_id, *record.evidence_ids[:3]])
        elif record.status == "nothing" and record.searched:
            self.o.activity(at, "checked", f"Looked for the invoice for the {amount} payment to {who} in "
                            f"{join_and(record.searched)}: it is not there.", rec.company_id,
                            evidence_ids=[rec.evidence_id])

    # ----------------------------------------------------------------- plain words

    def searched_sentence(self, subject_id: str) -> str | None:
        """"I searched your email, your Google Drive and Moloni: it is not there." once a search is settled."""
        record = self.repo.evidence_searches.get(subject_id)
        if record is None or record.status == "retry" or not record.searched:
            return None
        places = join_and(record.searched)
        if record.status == "nothing":
            return f"I searched {places}: it is not there."
        return f"I searched {places}."

    def pending_sentence(self, subject_id: str, company_id: str | None, what: str) -> str | None:
        """While the search is still to happen (no supplier is asked before it)."""
        if not self.waiting(subject_id, company_id):
            return None
        places = join_and(self.place_names(company_id))
        record = self.repo.evidence_searches.get(subject_id)
        if record is not None and record.unreachable:
            return (f"I'm looking for {what} in {places}. I couldn't reach {join_and(record.unreachable)} yet, "
                    "so I will look again shortly.")
        return f"I'm looking for {what} in {places}."

    def view(self, subject_id: str) -> dict[str, Any] | None:
        """The search as the owner and the accountant see it (the payment's detail; its plan in the accountant's
        reconciliation)."""
        record = self.repo.evidence_searches.get(subject_id)
        if record is None:
            return None
        today = self.repo.today()
        when = record.at.astimezone(TZ)
        summary = {"found": f"Found it in {record.found_in}." if record.found_in else "Found it.",
                   "likely": "Found a likely document. I'm confirming it.",
                   "conflict": "Found a document whose details disagree with the payment. I asked you about it.",
                   "nothing": self.searched_sentence(subject_id) or "Nothing found.",
                   "retry": "Some places could not be searched yet. I will look again shortly."}[record.status]
        places: dict[str, dict[str, Any]] = {}  # one line per place (a mailbox is searched twice: now, earlier)
        for a in record.attempts:
            entry = places.setdefault(a["place"], {"place": a["place"], "result": "", "at": a["startedAt"]})
            if _weight(a.get("result", "")) > _weight(entry["result"]):
                entry["result"] = a.get("result", "")
        return {"subjectId": record.subject_id, "kind": record.kind, "status": record.status, "round": record.round,
                "at": record.at.isoformat(), "when": f"{day_month(when.date(), today)} at {when:%H:%M}",
                "summary": summary, "searched": record.searched, "foundIn": record.found_in,
                "documentIds": list(record.document_ids), "places": list(places.values()),
                "earlier": [{"at": e["at"], "status": e["status"], "searched": e["searched"]}
                            for e in record.earlier]}

