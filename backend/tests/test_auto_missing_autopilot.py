"""Missing document autopilot (§22): ordered search, stop at verified, chase vs ask."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backoffice.domain.models import LegalEntity, Quality, Supplier, Transaction
from backoffice.learning.questions import QuestionKind
from backoffice.missing.autopilot import (
    SEARCH_ORDER,
    AttemptOutcome,
    ChaseAuthorizationRequest,
    EvidenceCandidate,
    EvidenceQuery,
    EvidenceSearch,
    MissingEvidenceAutopilot,
    NextStep,
    SearchSource,
)

TODAY = date(2026, 9, 27)
HAZEL = LegalEntity(id="ent_hazel", tenant_id="t1", name="Hazel Tree Lda", country="PT", tax_id="509123456")
VODAFONE = Supplier(id="sup_voda", tenant_id="t1", name="Vodafone", contact_email="faturas@vodafone.pt",
                    countries=["PT"])  # fmt: skip
TX = Transaction(id="tx_1", tenant_id="t1", account_id="acc", booked_on=date(2026, 9, 18), amount=Decimal("-117.20"),
                 counterparty="VODAFONE PORTUGAL", entity_id="ent_hazel", card_last4="4817")  # fmt: skip


class FakeSearch:
    def __init__(self, source: SearchSource, results: Sequence[EvidenceCandidate] = (), *,
                 error: Exception | None = None, delay: float = 0.0, log: list[SearchSource] | None = None) -> None:  # fmt: skip
        self.source = source
        self._results = list(results)
        self._error = error
        self._delay = delay
        self._log = log if log is not None else []
        self.queries: list[EvidenceQuery] = []

    async def search(self, query: EvidenceQuery) -> Sequence[EvidenceCandidate]:
        self._log.append(self.source)
        self.queries.append(query)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return self._results


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def cand(evidence_id: str, quality: Quality, number: str | None = None) -> EvidenceCandidate:
    return EvidenceCandidate(evidence_id=evidence_id, quality=quality, invoice_number=number)


def run(searches, *, authorize=lambda req: False, supplier=VODAFONE, query=None, **kw):
    pilot = MissingEvidenceAutopilot(searches, authorize=authorize, clock=Clock(),
                                     message_id_domain="mail.example.invalid",
                                     **{k: v for k, v in kw.items() if k in ("verifier", "timeout_seconds")})  # fmt: skip
    q = query or EvidenceQuery.from_transaction(TX, supplier=supplier)
    extra = {k: v for k, v in kw.items() if k == "allow_incomplete"}
    return asyncio.run(pilot.run(q, company=HAZEL, supplier=supplier, today=TODAY, **extra))


def test_query_from_transaction() -> None:
    q = EvidenceQuery.from_transaction(TX, supplier=VODAFONE, invoice_number="FT 2026/183")
    assert q.amount == Decimal("117.20") and q.counterparty_key == "vodafone portugal"
    assert q.window_start == date(2026, 8, 4) and q.window_end == date(2026, 10, 3)
    assert q.supplier_id == "sup_voda" and q.card_last4 == "4817"
    assert isinstance(FakeSearch(SearchSource.FILES), EvidenceSearch)


def test_searches_run_in_spec_order_and_stop_at_first_verified() -> None:
    log: list[SearchSource] = []
    searches = [
        FakeSearch(SearchSource.RECURRING_SEQUENCE, log=log),
        FakeSearch(SearchSource.FILES, [cand("ev_files", Quality.GREEN)], log=log),
        FakeSearch(SearchSource.HISTORICAL_EMAIL, [cand("ev_old", Quality.AMBER)], log=log),
        FakeSearch(SearchSource.CURRENT_EMAIL, log=log),
        FakeSearch(SearchSource.SUPPLIER_PORTAL, [cand("ev_portal", Quality.GREEN)], log=log),
    ]
    outcome = run(searches)
    assert log == [SearchSource.CURRENT_EMAIL, SearchSource.HISTORICAL_EMAIL, SearchSource.FILES]
    assert outcome.next_step is NextStep.MATCH_FOUND
    assert outcome.found is not None and outcome.found.evidence_id == "ev_files"
    assert [a.outcome for a in outcome.attempts] == [AttemptOutcome.NOTHING, AttemptOutcome.LIKELY, AttemptOutcome.VERIFIED]
    assert outcome.attempts[-1].evidence_id == "ev_files"
    assert all(a.finished_at > a.started_at for a in outcome.attempts)
    assert [c.evidence_id for c in outcome.candidates] == ["ev_old"]
    assert outcome.chase is None and outcome.question is None
    assert list(SEARCH_ORDER) == [
        SearchSource.CURRENT_EMAIL, SearchSource.HISTORICAL_EMAIL, SearchSource.FILES,
        SearchSource.SUPPLIER_PORTAL, SearchSource.ACCOUNTING_PLATFORM, SearchSource.RECURRING_SEQUENCE,
    ]  # fmt: skip


def test_amber_is_never_promoted_even_if_found_everywhere() -> None:
    searches = [FakeSearch(s, [cand(f"ev_{s.value}", Quality.AMBER)]) for s in SEARCH_ORDER]
    outcome = run(searches)
    assert outcome.next_step is NextStep.CONFIRM_WITH_OWNER and outcome.found is None
    assert len(outcome.attempts) == 6 and len(outcome.candidates) == 6
    q = outcome.question
    assert q is not None and q.kind is QuestionKind.CONFIRM_MATCH
    assert q.prompt == "Is this the invoice for this payment?" and q.why[0] == "Found in your email."


def test_verifier_decides_quality() -> None:
    async def verifier(query: EvidenceQuery, candidate: EvidenceCandidate) -> Quality:
        return Quality.GREEN if candidate.evidence_id == "ev_real" else Quality.RED

    searches = [FakeSearch(SearchSource.CURRENT_EMAIL, [cand("ev_fake", Quality.GREEN), cand("ev_real", Quality.AMBER)])]
    outcome = run(searches, verifier=verifier)
    assert outcome.next_step is NextStep.MATCH_FOUND and outcome.found.evidence_id == "ev_real"  # type: ignore[union-attr]
    assert [c.evidence_id for c in outcome.candidates] == ["ev_fake"]


def test_grading_stops_at_first_verified_candidate() -> None:
    graded: list[str] = []

    async def verifier(query: EvidenceQuery, candidate: EvidenceCandidate) -> Quality:
        graded.append(candidate.evidence_id)
        return candidate.quality

    searches = [FakeSearch(SearchSource.CURRENT_EMAIL, [cand("ev_a", Quality.AMBER), cand("ev_b", Quality.GREEN),
                                                         cand("ev_c", Quality.GREEN)])]  # fmt: skip
    outcome = run(searches, verifier=verifier)
    assert graded == ["ev_a", "ev_b"]
    assert outcome.found is not None and outcome.found.evidence_id == "ev_b"
    assert outcome.attempts[0].candidates == 2


def test_same_evidence_from_two_sources_is_listed_once() -> None:
    searches = [FakeSearch(SearchSource.CURRENT_EMAIL, [cand("ev_same", Quality.AMBER)]),
                FakeSearch(SearchSource.HISTORICAL_EMAIL, [cand("ev_same", Quality.AMBER)])]  # fmt: skip
    outcome = run(searches)
    assert [c.evidence_id for c in outcome.candidates] == ["ev_same"]
    assert [a.outcome for a in outcome.attempts] == [AttemptOutcome.LIKELY, AttemptOutcome.LIKELY]


def test_conflict_is_never_a_guess() -> None:
    searches = [
        FakeSearch(SearchSource.CURRENT_EMAIL, [cand("ev_conflict", Quality.RED)]),
        FakeSearch(SearchSource.FILES, [cand("ev_maybe", Quality.AMBER)]),
    ]
    outcome = run(searches)
    assert outcome.next_step is NextStep.RESOLVE_CONFLICT
    assert outcome.question is not None and outcome.question.prompt == "The invoice we found doesn't match this payment."
    assert outcome.attempts[0].outcome is AttemptOutcome.CONFLICT


def test_failed_or_slow_source_means_retry_later() -> None:
    searches = [
        FakeSearch(SearchSource.CURRENT_EMAIL, error=RuntimeError("gmail token expired")),
        FakeSearch(SearchSource.FILES, delay=0.5),
        FakeSearch(SearchSource.SUPPLIER_PORTAL),
    ]
    outcome = run(searches, timeout_seconds=0.05)
    assert outcome.next_step is NextStep.RETRY_LATER
    assert [a.outcome for a in outcome.attempts] == [AttemptOutcome.FAILED, AttemptOutcome.TIMED_OUT, AttemptOutcome.NOTHING]
    assert outcome.attempts[0].failure == "RuntimeError"
    assert outcome.chase is None and outcome.question is None


def test_incomplete_search_can_proceed_when_allowed() -> None:
    searches = [FakeSearch(SearchSource.CURRENT_EMAIL, error=RuntimeError("down")), FakeSearch(SearchSource.FILES)]
    outcome = run(searches, allow_incomplete=True)
    assert outcome.next_step is NextStep.ASK_OWNER
    assert outcome.question is not None and outcome.question.why == ("I looked in your files.",)


def test_not_found_and_authorized_chases_supplier() -> None:
    seen: list[ChaseAuthorizationRequest] = []

    def authorize(request: ChaseAuthorizationRequest) -> bool:
        seen.append(request)
        return True

    query = EvidenceQuery.from_transaction(TX, supplier=VODAFONE, invoice_number="FT 2026/183")
    outcome = run([FakeSearch(s) for s in SEARCH_ORDER], authorize=authorize, query=query)
    assert outcome.next_step is NextStep.CHASE_SUPPLIER
    assert seen == [ChaseAuthorizationRequest(tenant_id="t1", entity_id="ent_hazel", supplier_id="sup_voda",
                                              supplier_name="Vodafone")]  # fmt: skip
    chase = outcome.chase
    assert chase is not None and chase.to == "faturas@vodafone.pt"
    assert chase.subject.startswith("Fatura FT 2026/183 (Ref. ")  # Portuguese supplier
    assert "117,20 € de 18 de setembro" in chase.body
    assert "tx_1" not in chase.body and "ent_hazel" not in chase.body
    assert outcome.activity == "Asked Vodafone for the invoice for the €117.20 payment."
    assert len(outcome.attempts) == 6 and outcome.question is None


def test_async_authorize_is_awaited() -> None:
    async def authorize(request: ChaseAuthorizationRequest) -> bool:
        return True

    outcome = run([FakeSearch(SearchSource.CURRENT_EMAIL)], authorize=authorize)
    assert outcome.next_step is NextStep.CHASE_SUPPLIER


def test_not_authorized_asks_owner_with_one_tap_chase() -> None:
    searches = [FakeSearch(SearchSource.CURRENT_EMAIL), FakeSearch(SearchSource.HISTORICAL_EMAIL),
                FakeSearch(SearchSource.FILES), FakeSearch(SearchSource.SUPPLIER_PORTAL),
                FakeSearch(SearchSource.RECURRING_SEQUENCE)]  # fmt: skip
    outcome = run(searches, authorize=lambda req: False)
    assert outcome.next_step is NextStep.ASK_OWNER and outcome.chase is None
    q = outcome.question
    assert q is not None and q.kind is QuestionKind.MISSING_INVOICE
    assert q.prompt == "We can't find the invoice for this payment."
    assert q.detail == "Vodafone · €117.20 · 18 September · card •••• 4817"
    assert [o.label for o in q.options] == ["Ask Vodafone for it", "I'll upload it", "There is no invoice"]
    assert q.why == ("I looked in your email, your files, Vodafone's website and earlier Vodafone invoices.",)


def test_truthy_non_bool_is_not_authorization() -> None:
    outcome = run([FakeSearch(SearchSource.CURRENT_EMAIL)], authorize=lambda req: "yes")
    assert outcome.next_step is NextStep.ASK_OWNER


def test_no_contact_or_unknown_supplier_cannot_chase() -> None:
    calls: list[ChaseAuthorizationRequest] = []
    no_email = VODAFONE.model_copy(update={"contact_email": None})
    outcome = run([FakeSearch(SearchSource.CURRENT_EMAIL)], authorize=lambda r: calls.append(r) or True, supplier=no_email)
    assert outcome.next_step is NextStep.ASK_OWNER and not calls
    assert [o.label for o in outcome.question.options] == ["I'll upload it", "There is no invoice"]  # type: ignore[union-attr]
    unknown = run([FakeSearch(SearchSource.CURRENT_EMAIL)], authorize=lambda r: True, supplier=None)
    assert unknown.next_step is NextStep.ASK_OWNER
    assert unknown.question is not None and unknown.question.detail.startswith("Vodafone Portugal · €117.20")


def test_no_sources_configured_asks_owner_without_where_line() -> None:
    outcome = run([], authorize=lambda r: False)
    assert outcome.next_step is NextStep.ASK_OWNER and outcome.attempts == ()
    assert outcome.question is not None and outcome.question.why == ()


def test_invalid_timeout() -> None:
    with pytest.raises(ValueError):
        MissingEvidenceAutopilot([], authorize=lambda r: True, timeout_seconds=0)
