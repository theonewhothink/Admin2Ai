"""Adversarial review of the missing-document autopilot (§22, §25): defects reproduced before their fix."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backoffice.domain.models import LegalEntity, Quality, Supplier, Transaction
from backoffice.missing import (
    AttemptOutcome,
    ChaseFacts,
    ChaseMessage,
    ChaseThread,
    EvidenceCandidate,
    EvidenceQuery,
    Language,
    MissingEvidenceAutopilot,
    NextStep,
    ReminderPolicy,
    SearchSource,
    clean_invoice_number,
    compose_reminder,
    compose_request,
)

TODAY = date(2026, 9, 27)
DOMAIN = "mail.example.invalid"
HAZEL = LegalEntity(id="ent_hazel", tenant_id="t1", name="Hazel Tree Lda", country="PT", tax_id="509123456")
OAK = LegalEntity(id="ent_oak", tenant_id="t1", name="Oak Studio", country="PT", tax_id="516000000")
STRANGER = LegalEntity(id="ent_x", tenant_id="t2", name="Other Tenant Co", country="PT", tax_id="999999990")
VODAFONE = Supplier(id="sup_voda", tenant_id="t1", name="Vodafone", contact_email="faturas@vodafone.pt",
                    countries=["PT"])  # fmt: skip
TX = Transaction(id="tx_1", tenant_id="t1", account_id="acc", booked_on=date(2026, 9, 18), amount=Decimal("-117.20"),
                 counterparty="VODAFONE PORTUGAL", entity_id="ent_hazel")  # fmt: skip


def base_facts(**kw: object) -> ChaseFacts:
    data: dict[str, object] = dict(
        supplier_name="Vodafone", supplier_email="faturas@vodafone.pt", amount=Decimal("117.20"), currency="EUR",
        paid_on=date(2026, 9, 18), company_name="Hazel Tree Lda", company_tax_id="509123456",
        invoice_number="FT 2026/183", language=Language.EN,
    )  # fmt: skip
    data.update(kw)
    return ChaseFacts(**data)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- header injection


@pytest.mark.parametrize(
    "email",
    ["a@b.pt\r\nBcc: x@evil.com", "a@b.pt, x@evil.com", "a@b.pt x@evil.com", "Vodafone <a@b.pt>", "@b.pt", "a@"],
)
def test_supplier_address_must_be_one_plain_address(email: str) -> None:
    """Defect: 'a@b.pt\\r\\nBcc: x@evil.com' was accepted as the To: of a supplier email."""
    with pytest.raises(ValueError):
        base_facts(supplier_email=email)


def test_invoice_number_cannot_inject_headers() -> None:
    """Defect: an OCR'd invoice number with a line break went straight into the Subject header."""
    with pytest.raises(ValueError):
        base_facts(invoice_number="FT 1\r\nBcc: attacker@evil.com")
    assert clean_invoice_number("FT 1\r\nBcc: attacker@evil.com") == "FT 1 Bcc: attacker@evil.com"
    assert clean_invoice_number(" FT​ 2026/183\t") == "FT 2026/183"
    assert clean_invoice_number("   ") is None
    assert clean_invoice_number("X" * 61) is None  # not a plausible invoice number: left out
    built = ChaseFacts.build(TX, VODAFONE, HAZEL, invoice_number="FT 2026/183\r\nBcc: x@evil.com")
    message = compose_request(built, token="7KQ2MX", today=TODAY, message_id_domain=DOMAIN)
    assert "\r" not in message.subject and "\n" not in message.subject


def test_message_headers_are_single_line() -> None:
    with pytest.raises(ValidationError):
        ChaseMessage(to="a@b.pt", subject="x\r\nBcc: y@evil.com", body="", language=Language.EN, token="7KQ2MX",
                     message_id="<a@b>")  # fmt: skip
    with pytest.raises(ValidationError):
        ChaseMessage(to="a@b.pt", subject="x", body="", language=Language.EN, token="7KQ2MX",
                     message_id="<a@b>", references=("<a@b>\r\nX: y",))  # fmt: skip
    with pytest.raises(ValueError):
        compose_request(base_facts(), token="7KQ2MX", today=TODAY, message_id_domain="evil.com>\r\nBcc: x@y.z")
    with pytest.raises(ValueError):
        MissingEvidenceAutopilot([], authorize=lambda r: True, message_id_domain="bad domain")


def test_reminder_keeps_headers_clean() -> None:
    first = compose_request(base_facts(), token="7KQ2MX", today=TODAY, message_id_domain=DOMAIN)
    thread = ChaseThread.start(first, TODAY)
    reminder = compose_reminder(base_facts(), thread, today=date(2026, 10, 5), message_id_domain=DOMAIN)
    assert reminder.in_reply_to == first.message_id and reminder.references == (first.message_id,)


# --------------------------------------------------------------------------- reminder policy


@pytest.mark.parametrize(
    "kwargs", [{"first_after_days": 0}, {"then_every_days": -4}, {"max_reminders": -1}, {"first_after_days": 400}]
)
def test_reminder_policy_is_validated(kwargs: dict[str, int]) -> None:
    """Defect: negative waits (a reminder every day, or before the request) were accepted."""
    with pytest.raises(ValidationError):
        ReminderPolicy(**kwargs)


# --------------------------------------------------------------------------- whose name goes on the request


class Empty:
    def __init__(self, source: SearchSource) -> None:
        self.source = source

    async def search(self, query: EvidenceQuery) -> Sequence[EvidenceCandidate]:
        return ()


def _run(company: LegalEntity, tx: Transaction = TX, supplier: Supplier = VODAFONE):  # type: ignore[no-untyped-def]
    pilot = MissingEvidenceAutopilot([Empty(SearchSource.CURRENT_EMAIL)], authorize=lambda r: True,
                                     message_id_domain=DOMAIN)  # fmt: skip
    query = EvidenceQuery.from_transaction(tx, supplier=supplier)
    return asyncio.run(pilot.run(query, company=company, supplier=supplier, today=TODAY))


def test_another_tenants_company_is_never_put_on_a_request() -> None:
    """Defect: another tenant's name and tax number were emailed to this tenant's supplier."""
    with pytest.raises(ValueError):
        _run(STRANGER)


def test_request_names_the_company_the_payment_belongs_to() -> None:
    """Defect: the caller could pass any company; the supplier would invoice the wrong one."""
    with pytest.raises(ValueError):
        _run(OAK)
    outcome = _run(HAZEL)
    assert outcome.next_step is NextStep.CHASE_SUPPLIER
    assert outcome.chase is not None and "509123456" in outcome.chase.body


def test_supplier_from_another_tenant_is_refused() -> None:
    with pytest.raises(ValueError):
        _run(HAZEL, supplier=VODAFONE.model_copy(update={"tenant_id": "t2"}))


def test_unassigned_payment_is_never_chased_automatically() -> None:
    """Without a known company we would be guessing whose tax number to send (§19): ask instead."""
    outcome = _run(HAZEL, tx=TX.model_copy(update={"entity_id": None}))
    assert outcome.next_step is NextStep.ASK_OWNER and outcome.chase is None
    assert outcome.question is not None
    assert [o.label for o in outcome.question.options] == ["Ask Vodafone for it", "I'll upload it", "There is no invoice"]


# --------------------------------------------------------------------------- slow verification


def test_a_hanging_verifier_cannot_stall_the_plan() -> None:
    """Defect: the timeout covered the search but not verification, so one hung check blocked forever."""

    class One:
        source = SearchSource.CURRENT_EMAIL

        async def search(self, query: EvidenceQuery) -> Sequence[EvidenceCandidate]:
            return (EvidenceCandidate(evidence_id="ev_1", quality=Quality.AMBER),)

    async def slow_verifier(query: EvidenceQuery, candidate: EvidenceCandidate) -> Quality:
        await asyncio.sleep(5)
        return Quality.GREEN

    pilot = MissingEvidenceAutopilot([One()], authorize=lambda r: True, verifier=slow_verifier, timeout_seconds=0.05,
                                     message_id_domain=DOMAIN)  # fmt: skip
    started = time.monotonic()
    outcome = asyncio.run(pilot.run(EvidenceQuery.from_transaction(TX, supplier=VODAFONE), company=HAZEL,
                                    supplier=VODAFONE, today=TODAY))  # fmt: skip
    assert time.monotonic() - started < 2
    assert outcome.attempts[0].outcome is AttemptOutcome.TIMED_OUT
    assert outcome.next_step is NextStep.RETRY_LATER and outcome.found is None


@pytest.mark.parametrize("evidence_id", ["", "   "])
def test_a_result_without_evidence_can_never_be_a_match(evidence_id: str) -> None:
    """Defect: a GREEN candidate with a blank evidence id ended the search as MATCH_FOUND (§3)."""
    with pytest.raises(ValidationError):
        EvidenceCandidate(evidence_id=evidence_id, quality=Quality.GREEN)

    class Broken:
        source = SearchSource.CURRENT_EMAIL

        async def search(self, query: EvidenceQuery) -> Sequence[EvidenceCandidate]:
            return (EvidenceCandidate.model_construct(evidence_id=evidence_id, quality=Quality.GREEN),)

    pilot = MissingEvidenceAutopilot([Broken()], authorize=lambda r: False, message_id_domain=DOMAIN)
    outcome = asyncio.run(pilot.run(EvidenceQuery.from_transaction(TX, supplier=VODAFONE), company=HAZEL,
                                    supplier=VODAFONE, today=TODAY))  # fmt: skip
    assert outcome.found is None and outcome.next_step is NextStep.RETRY_LATER
    assert outcome.attempts[0].outcome is AttemptOutcome.FAILED


def test_zero_amount_payment_is_never_chased() -> None:
    """Defect: a €0.00 card check produced 'please send the invoice for the €0.00 payment'."""
    with pytest.raises(ValueError):
        base_facts(amount=Decimal("0"))
    outcome = _run(HAZEL, tx=TX.model_copy(update={"amount": Decimal("0")}))
    assert outcome.next_step is NextStep.ASK_OWNER and outcome.chase is None
    assert outcome.question is not None
    assert [o.label for o in outcome.question.options] == ["I'll upload it", "There is no invoice"]
