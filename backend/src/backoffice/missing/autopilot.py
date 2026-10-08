"""Missing document autopilot (§22, §25, §21).

For a transaction that should have evidence but has none, search in a fixed
order and stop at the first *verified* hit:

1. current email  2. historical email  3. Drive / files
4. supplier portal  5. accounting platform  6. previous recurring sequence

Every attempt is recorded (source, times, outcome, failure class). A likely
(AMBER) candidate is never promoted to verified (§57); a conflicting (RED) one
is never used as a guess (§19). When nothing verified is found:

* a source failed or timed out → RETRY_LATER (unless ``allow_incomplete``):
  the invoice may be sitting in a mailbox we could not read;
* a conflicting candidate → RESOLVE_CONFLICT (owner question);
* a likely candidate → CONFIRM_WITH_OWNER (one tap);
* nothing at all → CHASE_SUPPLIER when the injected ``authorize`` callable
  permits supplier invoice requests (§25 "automatic if authorized"), the
  supplier has a contact address and the payment's company is known;
  otherwise ASK_OWNER, offering "Ask Vodafone for it" as a one-tap approval.

The request carries our company name and tax number, so the ``company`` passed
in must be the payment's own company (``query.entity_id``) in the same tenant,
and the supplier must belong to that tenant too; anything else is refused with
``ValueError`` before any search runs. A payment not yet assigned to a company
is never chased automatically: whose tax number to send would be a guess (§19).

The per-source timeout covers the search *and* the verification of its
results, so neither a hung connector nor a hung check can stall the plan.
``timeout_seconds=None`` runs without one: searches whose results were already
fetched (a recorded search, ``missing.searches.RecordedSearch``) never wait, and
the plan then runs without an event loop (``missing.searches.run_recorded``).

Callers decide beforehand that the transaction expects evidence at all
(§21 expected-evidence engine). Searches, verification and authorization are
injected, so this module has no knowledge of connectors or policy internals.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator

from backoffice.domain.models import LegalEntity, Quality, Supplier, Transaction, utcnow
from backoffice.learning.keys import counterparty_key, display_name
from backoffice.learning.plain import join_and
from backoffice.learning.questions import (
    OptionKind,
    Question,
    QuestionKind,
    QuestionOption,
    SubjectFacts,
    describe_subject,
)

from .chase import (
    ChaseFacts,
    ChaseMessage,
    activity_line,
    choose_language,
    clean_invoice_number,
    compose_request,
    thread_token,
    valid_mail_domain,
)

__all__ = [
    "MISSING_INVOICE_PROMPT",
    "SEARCH_ORDER",
    "AttemptOutcome",
    "Authorize",
    "ChaseAuthorizationRequest",
    "EvidenceCandidate",
    "EvidenceQuery",
    "EvidenceSearch",
    "FoundEvidence",
    "MissingEvidenceAutopilot",
    "MissingEvidenceOutcome",
    "NextStep",
    "SearchAttempt",
    "SearchSource",
    "Verifier",
]

MISSING_INVOICE_PROMPT = "We can't find the invoice for this payment."


class SearchSource(str, Enum):
    CURRENT_EMAIL = "current_email"
    HISTORICAL_EMAIL = "historical_email"
    FILES = "files"
    SUPPLIER_PORTAL = "supplier_portal"
    ACCOUNTING_PLATFORM = "accounting_platform"
    RECURRING_SEQUENCE = "recurring_sequence"


SEARCH_ORDER: tuple[SearchSource, ...] = tuple(SearchSource)


@dataclass(frozen=True)
class EvidenceQuery:
    """What to look for. Amount is absolute; the window bounds document dates."""

    tenant_id: str
    transaction_id: str
    amount: Decimal
    currency: str
    paid_on: date
    counterparty: str
    window_start: date
    window_end: date
    counterparty_key: str | None = None
    entity_id: str | None = None
    supplier_id: str | None = None
    supplier_tax_id: str | None = None
    invoice_number: str | None = None
    payment_reference: str | None = None
    card_last4: str | None = None
    counterparty_iban: str | None = None

    @classmethod
    def from_transaction(
        cls,
        tx: Transaction,
        *,
        supplier: Supplier | None = None,
        invoice_number: str | None = None,
        days_before: int = 45,
        days_after: int = 15,
    ) -> EvidenceQuery:
        """Invoices usually precede payment; card receipts can follow it by a few days."""
        return cls(
            tenant_id=tx.tenant_id,
            transaction_id=tx.id,
            amount=abs(tx.amount),
            currency=tx.currency,
            paid_on=tx.booked_on,
            counterparty=tx.counterparty,
            window_start=tx.booked_on - timedelta(days=days_before),
            window_end=tx.booked_on + timedelta(days=days_after),
            counterparty_key=counterparty_key(tx.counterparty),
            entity_id=tx.entity_id,
            supplier_id=supplier.id if supplier else None,
            supplier_tax_id=supplier.tax_id if supplier else None,
            invoice_number=invoice_number,
            payment_reference=tx.reference,
            card_last4=tx.card_last4,
            counterparty_iban=tx.counterparty_iban,
        )


def _evidence_id(value: str) -> str:
    """A result must point at stored evidence: nothing closes without it (§3)."""
    if not value or not value.strip():
        raise ValueError("a search result must name its evidence")
    return value


class EvidenceCandidate(BaseModel):
    """A search result. ``quality`` is the searcher's own verdict unless a verifier is injected."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    document_id: str | None = None
    invoice_number: str | None = None
    quality: Quality = Quality.AMBER

    @field_validator("evidence_id")
    @classmethod
    def _names_evidence(cls, value: str) -> str:
        return _evidence_id(value)


@runtime_checkable
class EvidenceSearch(Protocol):
    """One place to look (a Gmail search, a Drive search, a portal adapter, …)."""

    source: SearchSource

    async def search(self, query: EvidenceQuery) -> Sequence[EvidenceCandidate]: ...


Verifier = Callable[[EvidenceQuery, EvidenceCandidate], Awaitable[Quality]]


@dataclass(frozen=True)
class ChaseAuthorizationRequest:
    """Asked of the policy layer: may we email this supplier on our own? (§25)"""

    tenant_id: str
    entity_id: str | None
    supplier_id: str
    supplier_name: str
    action: str = "supplier_invoice_request"


Authorize = Callable[[ChaseAuthorizationRequest], "bool | Awaitable[bool]"]


class AttemptOutcome(str, Enum):
    VERIFIED = "verified"
    LIKELY = "likely"
    CONFLICT = "conflict"
    NOTHING = "nothing"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class SearchAttempt(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: SearchSource
    started_at: datetime
    finished_at: datetime
    outcome: AttemptOutcome
    candidates: int = 0
    evidence_id: str | None = None  # the verified hit, when there is one
    failure: str | None = None  # exception class name; internal only


class FoundEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: SearchSource
    evidence_id: str
    document_id: str | None = None
    invoice_number: str | None = None
    quality: Quality

    @field_validator("evidence_id")
    @classmethod
    def _names_evidence(cls, value: str) -> str:
        return _evidence_id(value)


class NextStep(str, Enum):
    MATCH_FOUND = "match_found"  # ingest → verify → match → close
    CONFIRM_WITH_OWNER = "confirm_with_owner"
    RESOLVE_CONFLICT = "resolve_conflict"
    RETRY_LATER = "retry_later"
    CHASE_SUPPLIER = "chase_supplier"
    ASK_OWNER = "ask_owner"


class MissingEvidenceOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    transaction_id: str
    next_step: NextStep
    attempts: tuple[SearchAttempt, ...]
    found: FoundEvidence | None = None
    candidates: tuple[FoundEvidence, ...] = ()  # unverified (likely or conflicting)
    chase: ChaseMessage | None = None
    activity: str | None = None  # quiet Activity line when the supplier was chased
    question: Question | None = None


@dataclass
class _Search:
    attempts: list[SearchAttempt]
    likely: list[FoundEvidence]
    conflicts: list[FoundEvidence]
    incomplete: bool = False
    found: FoundEvidence | None = None


class MissingEvidenceAutopilot:
    """Search plan for one transaction lacking evidence (§22)."""

    def __init__(
        self,
        searches: Sequence[EvidenceSearch],
        *,
        authorize: Authorize,
        verifier: Verifier | None = None,
        timeout_seconds: float | None = 30.0,
        clock: Callable[[], datetime] = utcnow,
        message_id_domain: str = "mail.backoffice.invalid",
    ) -> None:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        rank = {source: i for i, source in enumerate(SEARCH_ORDER)}
        indexed = list(enumerate(searches))
        self._searches = [s for _, s in sorted(indexed, key=lambda pair: (rank[pair[1].source], pair[0]))]
        self._authorize = authorize
        self._verifier = verifier
        self._timeout = timeout_seconds
        self._clock = clock
        self._domain = valid_mail_domain(message_id_domain)

    async def run(
        self,
        query: EvidenceQuery,
        *,
        company: LegalEntity,
        supplier: Supplier | None,
        today: date,
        allow_incomplete: bool = False,
    ) -> MissingEvidenceOutcome:
        """Search in order, stop at the first verified hit, then decide the next step."""
        _check_parties(query, company, supplier)
        state = await self._search_all(query)
        attempts = tuple(state.attempts)
        unverified = tuple(state.conflicts + state.likely)
        name = display_name(supplier.name if supplier else query.counterparty)

        def outcome(
            step: NextStep,
            *,
            found: FoundEvidence | None = None,
            chase: ChaseMessage | None = None,
            activity: str | None = None,
            question: Question | None = None,
        ) -> MissingEvidenceOutcome:
            return MissingEvidenceOutcome(
                transaction_id=query.transaction_id,
                next_step=step,
                attempts=attempts,
                candidates=unverified,
                found=found,
                chase=chase,
                activity=activity,
                question=question,
            )

        if state.found is not None:
            return outcome(NextStep.MATCH_FOUND, found=state.found)
        if state.incomplete and not allow_incomplete:
            return outcome(NextStep.RETRY_LATER)
        if state.conflicts:
            return outcome(NextStep.RESOLVE_CONFLICT, question=_conflict_question(query, name, today))
        if state.likely:
            return outcome(NextStep.CONFIRM_WITH_OWNER, question=_confirm_question(query, name, state.likely[0], today))
        searched = [a.source for a in attempts if a.outcome not in (AttemptOutcome.FAILED, AttemptOutcome.TIMED_OUT)]
        can_chase = supplier is not None and bool(supplier.contact_email) and query.amount > 0
        company_known = query.entity_id is not None
        if supplier is not None and can_chase and company_known and await self._is_authorized(query, supplier, name):
            facts = _facts(query, supplier, company)
            token = thread_token(query.tenant_id, query.transaction_id)
            message = compose_request(facts, token=token, today=today, message_id_domain=self._domain)
            return outcome(NextStep.CHASE_SUPPLIER, chase=message, activity=activity_line(facts))
        question = _missing_question(query, name, searched, can_chase=can_chase, today=today)
        return outcome(NextStep.ASK_OWNER, question=question)

    # ------------------------------------------------------------------ searching

    async def _search_all(self, query: EvidenceQuery) -> _Search:
        state = _Search(attempts=[], likely=[], conflicts=[])
        for search in self._searches:
            started = self._clock()
            try:
                if self._timeout is None:
                    graded = await self._look(search, query)
                else:
                    graded = await asyncio.wait_for(self._look(search, query), timeout=self._timeout)
            except TimeoutError:
                state.incomplete = True
                state.attempts.append(self._attempt(search.source, started, AttemptOutcome.TIMED_OUT))
                continue
            except Exception as exc:  # a broken source must not stop the plan; recorded, never shown
                state.incomplete = True
                state.attempts.append(
                    self._attempt(search.source, started, AttemptOutcome.FAILED, failure=type(exc).__name__)
                )
                continue
            hit = next((g for g in graded if g.quality is Quality.GREEN), None)
            red = [g for g in graded if g.quality is Quality.RED]
            amber = [g for g in graded if g.quality is Quality.AMBER]
            seen = {c.evidence_id for c in (*state.conflicts, *state.likely)}
            state.conflicts.extend(g for g in red if g.evidence_id not in seen)
            state.likely.extend(g for g in amber if g.evidence_id not in seen)
            outcome = (
                AttemptOutcome.VERIFIED if hit
                else AttemptOutcome.CONFLICT if red
                else AttemptOutcome.LIKELY if amber
                else AttemptOutcome.NOTHING
            )  # fmt: skip
            state.attempts.append(
                self._attempt(search.source, started, outcome, candidates=len(graded),
                              evidence_id=hit.evidence_id if hit else None)
            )  # fmt: skip
            if hit is not None:
                state.found = hit
                break
        return state

    async def _look(self, search: EvidenceSearch, query: EvidenceQuery) -> list[FoundEvidence]:
        """One source: search, then verify its results (both under the same timeout)."""
        results = await search.search(query)
        return await self._grade_until_verified(search.source, query, results)

    async def _grade_until_verified(
        self, source: SearchSource, query: EvidenceQuery, results: Sequence[EvidenceCandidate]
    ) -> list[FoundEvidence]:
        """Grade candidates in order; the first verified one ends the search (§22)."""
        graded: list[FoundEvidence] = []
        for candidate in results:
            graded.append(await self._grade(source, query, candidate))
            if graded[-1].quality is Quality.GREEN:
                break
        return graded

    async def _grade(self, source: SearchSource, query: EvidenceQuery, candidate: EvidenceCandidate) -> FoundEvidence:
        quality = candidate.quality if self._verifier is None else await self._verifier(query, candidate)
        return FoundEvidence(
            source=source,
            evidence_id=candidate.evidence_id,
            document_id=candidate.document_id,
            invoice_number=candidate.invoice_number,
            quality=quality,
        )

    def _attempt(
        self,
        source: SearchSource,
        started: datetime,
        outcome: AttemptOutcome,
        *,
        candidates: int = 0,
        evidence_id: str | None = None,
        failure: str | None = None,
    ) -> SearchAttempt:
        return SearchAttempt(
            source=source,
            started_at=started,
            finished_at=self._clock(),
            outcome=outcome,
            candidates=candidates,
            evidence_id=evidence_id,
            failure=failure,
        )

    # ------------------------------------------------------------------ authorization

    async def _is_authorized(self, query: EvidenceQuery, supplier: Supplier, name: str) -> bool:
        request = ChaseAuthorizationRequest(
            tenant_id=query.tenant_id, entity_id=query.entity_id, supplier_id=supplier.id, supplier_name=name
        )
        verdict = self._authorize(request)
        if inspect.isawaitable(verdict):
            verdict = await verdict
        return verdict is True


def _check_parties(query: EvidenceQuery, company: LegalEntity, supplier: Supplier | None) -> None:
    """The request names ``company`` to ``supplier``: both must be this payment's (tenant isolation)."""
    if company.tenant_id != query.tenant_id:
        raise ValueError("the company belongs to another tenant")
    if query.entity_id is not None and company.id != query.entity_id:
        raise ValueError("the company is not the one this payment belongs to")
    if supplier is not None and supplier.tenant_id != query.tenant_id:
        raise ValueError("the supplier belongs to another tenant")


def _facts(query: EvidenceQuery, supplier: Supplier, company: LegalEntity) -> ChaseFacts:
    email = supplier.contact_email or ""
    number = clean_invoice_number(query.invoice_number)
    return ChaseFacts(
        supplier_name=display_name(supplier.name),
        supplier_email=email,
        amount=query.amount,
        currency=query.currency,
        paid_on=query.paid_on,
        company_name=company.name.strip(),
        company_tax_id=company.tax_id,
        company_country=company.country,
        invoice_number=number,
        language=choose_language(supplier, email),
    )


# --------------------------------------------------------------------------- owner questions


_WHERE = {
    SearchSource.CURRENT_EMAIL: "your email",
    SearchSource.HISTORICAL_EMAIL: "your email",
    SearchSource.FILES: "your files",
    SearchSource.ACCOUNTING_PLATFORM: "your accounting software",
}


def _where(source: SearchSource, name: str) -> str:
    if source is SearchSource.SUPPLIER_PORTAL:
        return f"{name}'s website"
    if source is SearchSource.RECURRING_SEQUENCE:
        return f"earlier {name} invoices"
    return _WHERE[source]


def _subject_facts(query: EvidenceQuery, name: str) -> SubjectFacts:
    return SubjectFacts(
        subject_type="transaction",
        subject_id=query.transaction_id,
        counterparty_key=query.counterparty_key,
        counterparty_label=name,
        card_last4=query.card_last4,
        amount=query.amount,
        currency=query.currency,
        on=query.paid_on,
    )


def _action(option_id: str, label: str) -> QuestionOption:
    return QuestionOption(id=option_id, label=label, kind=OptionKind.ACTION, action=option_id)


def _missing_question(
    query: EvidenceQuery, name: str, searched: Sequence[SearchSource], *, can_chase: bool, today: date
) -> Question:
    places: list[str] = []
    for source in searched:
        place = _where(source, name)
        if place not in places:
            places.append(place)
    options = [_action("ask_supplier", f"Ask {name} for it")] if can_chase else []
    options += [_action("upload", "I'll upload it"), _action("no_invoice", "There is no invoice")]
    facts = _subject_facts(query, name)
    why = (f"I looked in {join_and(places)}.",) if places else ()
    return Question(
        tenant_id=query.tenant_id,
        kind=QuestionKind.MISSING_INVOICE,
        prompt=MISSING_INVOICE_PROMPT,
        detail=describe_subject(facts, today),
        options=tuple(options),
        why=why,
        facts=facts,
        series_key=query.counterparty_key,
    )


def _confirm_question(query: EvidenceQuery, name: str, candidate: FoundEvidence, today: date) -> Question:
    facts = _subject_facts(query, name)
    return Question(
        tenant_id=query.tenant_id,
        kind=QuestionKind.CONFIRM_MATCH,
        prompt="Is this the invoice for this payment?",
        detail=describe_subject(facts, today),
        options=(_action("confirm", "Yes"), _action("reject", "No")),
        why=(f"Found in {_where(candidate.source, name)}.", "Some details could not be checked."),
        facts=facts,
        series_key=query.counterparty_key,
    )


def _conflict_question(query: EvidenceQuery, name: str, today: date) -> Question:
    facts = _subject_facts(query, name)
    return Question(
        tenant_id=query.tenant_id,
        kind=QuestionKind.CONFIRM_MATCH,
        prompt="The invoice we found doesn't match this payment.",
        detail=describe_subject(facts, today),
        options=(
            _action("confirm", "It is the right invoice"),
            _action("reject", "It is a different invoice"),
            _action("upload", "I'll upload the right one"),
        ),
        why=("Its details disagree with the payment.",),
        facts=facts,
        series_key=query.counterparty_key,
    )
