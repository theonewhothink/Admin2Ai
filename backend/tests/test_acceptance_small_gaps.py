"""Acceptance: smaller gaps closed in the engine (checklist A10, A11, U1, U2, H2, H3, H5, L4, J7, J3, X23, F8).

1. The first run after connecting asks at most the capped number of questions (A10); the rest are answered
   by learning or deferred, and never flood Needs You.
2. Time to first value, activation and the owner's onboarding minutes are measured from recorded events
   (A11, U1, U2); the demo, set up by hand, says "Not measured yet".
3. The billing name, billing address and receiving mailbox are votes for the company (H2, H3, H5); they never
   override a tax number, and a disagreement is the one question.
4. The usual card or account of a supplier is learned (L4): another one is noted, never held, and the usual
   one is searched first.
5. "This never has an invoice" / "always needs one", from the owner or the accountant, is learned (J7) and used
   for every later payment of that counterparty.
6. Payslips prove salaries (J3); a salary without its payslip stays open with a plain request.
7. A personal purchase on a company card is one plain question with "always" learning (X23).
8. A stated due date is verified and used: an unpaid invoice past it is overdue in plain words (F8).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from backoffice.closure import InteractionKind, Month
from backoffice.demo import build_demo
from backoffice.demo import evidence as E
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import (
    Document,
    DocumentType,
    ExtractionMethod,
    FieldObservation,
    LegalEntity,
    Quality,
    SourceKind,
    Supplier,
    Transaction,
    TransactionKind,
)
from backoffice.internal import overview
from backoffice.language import find_jargon, find_off_tone
from backoffice.learning import (
    DEFAULT_QUESTION_LIMIT,
    MAX_QUESTION_LIMIT,
    Addressee,
    CompanyDirectory,
    OwnershipBook,
    assign_entity,
    find_payment,
    learn_from_transactions,
    payment_method_note,
    personal_signal,
    same_address,
)
from backoffice.orchestrator import BankRow, Orchestrator, local_datetime
from backoffice.payroll import read_payslip
from backoffice.reconciliation import EvidenceExpectation
from backoffice.service import BackOfficeService
from backoffice.verification import assess_document
from backoffice.verification.document import DUE_BEFORE_ISSUE

NORTE_NIF = "509123457"  # Papelaria Norte, a supplier (fictional, valid check digit)
ANA_IBAN = "PT21003300004532111122223"  # fictional, passes mod-97
OTHER_IBAN = "PT63003500001234567890123"


# --------------------------------------------------------------------------- helpers


def plain(*texts: str) -> None:
    for text in texts:
        assert not find_jargon(text), (text, find_jargon(text))
        assert not find_off_tone(text), text


def _pt(value: str) -> str:
    whole, cents = value.split(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    return ".".join([whole, *groups]) + "," + cents


def qr_invoice(number: str, total: str, net: str, vat: str, day: str, *, buyer: str = E.HAZEL_NIF,
               buyer_name: str = "Hazel Tree Interiores, Lda.", due: str | None = None,
               atcud: str = "NRT7Q2KX-1") -> bytes:
    """A Portuguese invoice's text layer and its decoded fiscal QR code (like the demo's own files)."""
    payload = E.qr_payload(A=NORTE_NIF, B=buyer, C="PT", D="FT", E="N", F=day.replace("-", ""), G=number, H=atcud,
                           I1="PT", I7=net, I8=vat, N=vat, O=total, Q="ab12", R="1234")
    issued = f"{day[8:10]}/{day[5:7]}/{day[0:4]}"
    header = ["Papelaria Norte, Lda.", f"NIF: {NORTE_NIF}", f"Fatura n.º {number}", f"ATCUD: {atcud}",
              f"Data de emissão: {issued}"]
    if due:
        header.append(f"Data de vencimento: {due}")
    return E._invoice_text(header, [f"Cliente: {buyer_name}", f"NIF: {buyer}", f"Base tributável (23%): {_pt(net)}",
                                    f"IVA 23%: {_pt(vat)}", f"Total: {_pt(total)} €"], payload)


def row(bank_id: str, account: str, day: date, amount: str, who: str, description: str = "",
        kind: TransactionKind = TransactionKind.TRANSFER_OUT, *, card: str | None = None,
        iban: str | None = None) -> BankRow:
    return BankRow(bank_id=bank_id, account_id=account, booked_on=day, amount=Decimal(amount), counterparty=who,
                   description=description, kind=kind, card_last4=card, counterparty_iban=iban)


def at(day: int, hour: int = 10, minute: int = 0, month: int = 10) -> datetime:
    return local_datetime(date(2026, month, day), hour, minute)


def upload(o: Orchestrator, data: bytes, name: str = "document.txt", when: datetime | None = None):  # type: ignore[no-untyped-def]
    return o.ingest_file(data, filename=name, content_type="text/plain", at=when)


def mail(o: Orchestrator, raw: bytes, when: datetime | None = None):  # type: ignore[no-untyped-def]
    return o.ingest_file(raw, filename="message.eml", content_type="message/rfc822", source_kind=SourceKind.EMAIL,
                         at=when, origin="email")


def tx_by(o: Orchestrator, description: str):  # type: ignore[no-untyped-def]
    return next(r for r in o.repo.transactions.values() if r.tx.description == description)


def stage(o: Orchestrator, record: Any) -> Stage:
    return o.repo.items[record.item_id].stage


def open_needs(o: Orchestrator) -> list[Any]:
    return o.repo.open_needs()


def sync_state(now: datetime) -> dict[str, str]:
    return {"last_successful_sync": now.isoformat(), "coverage_start": (now - timedelta(days=90)).isoformat(),
            "coverage_end": now.isoformat()}


@pytest.fixture
def demo() -> Orchestrator:
    o = build_demo()
    o.repo.add_supplier(Supplier(id="sup-norte", tenant_id=o.repo.tenant_id, name="Papelaria Norte",
                                 aliases=["PAPELARIA NORTE"], tax_id=NORTE_NIF, countries=["PT"],
                                 contact_email="faturas@papelarianorte.pt"))
    return o


# =========================================================================== 1. the first run's questions (A10)


def _new_business(now: datetime, *, companies: int = 2) -> BackOfficeService:
    svc = BackOfficeService.new_tenant("t-first-run", owner_name="Rita Sousa", owner_email="rita@lumen.pt", now=now)
    svc.add_company("Hazel Tree", E.HAZEL_NIF, "Hazel Tree Interiores, Lda.")
    if companies > 1:
        svc.add_company("Company B", E.COMPANY_B_NIF, "Company B, Lda.")
    return svc


def test_the_first_run_asks_at_most_the_capped_questions_and_the_rest_are_learned_or_deferred() -> None:
    start = at(5, 9, 0)
    svc = _new_business(start)
    o = svc.orchestrator
    repo = svc.repo
    assert repo.onboarding.learning and repo.onboarding.question_limit == DEFAULT_QUESTION_LIMIT < 10
    # A card both companies use, and a bank account that reads its first 90 days.
    svc.add_source({"kind": "card", "last4": "4817", "bank": "Millennium BCP", "companyId": "hazel-tree",
                    "personal": True})
    linked = svc.link_bank("Millennium BCP", "hazel-tree", [E.HAZEL_IBAN], date(2027, 3, 1))
    connector = linked["connectionId"]
    assert connector in repo.onboarding.waiting_for
    # Ninety days of card purchases: one merchant three times, eleven others once.
    rows = [row(f"r-ikea-{m}", "card-4817", date(2026, m, 12), "-300.00", "IKEA ALFRAGIDE", "COMPRA CARTAO",
                TransactionKind.CARD, card="4817") for m in (7, 8, 9)]
    shops = ["LOJA DO GATO", "FERRAGENS SILVA", "TINTAS LUX", "PAPEL E CIA", "OFICINA RUI", "CAFE CENTRAL",
             "VIVEIRO VERDE", "LAVANDARIA SOL", "GRAFICA NOVA", "CANDEEIROS LDA", "MOLDURAS ARTE"]
    rows += [row(f"r-{i}", "card-4817", date(2026, 9, 1 + i), f"-{40 + 10 * i}.00", name, "COMPRA CARTAO",
                 TransactionKind.CARD, card="4817") for i, name in enumerate(shops)]
    # And six payments from Hazel Tree's own account: its account says which company, nothing to ask.
    account = next(a.id for a in repo.accounts.values() if a.iban == E.HAZEL_IBAN)
    rows += [row(f"o-{i}", account, date(2026, 8, 3 + i), "-50.00", f"FORNECEDOR {chr(65 + i)}", "TRF")
             for i in range(6)]
    repo.clock.advance_to(start + timedelta(minutes=3))
    svc.sync_bank(connector, rows[:7], None)
    # Still reading its first 90 days: nothing is asked yet, in whatever order payments arrived.
    assert not open_needs(o)
    svc.sync_bank(connector, rows[7:], sync_state(start + timedelta(minutes=4)))
    assert not repo.onboarding.learning
    asked = open_needs(o)
    assert len(asked) == DEFAULT_QUESTION_LIMIT <= MAX_QUESTION_LIMIT < 10
    # One question per series, the most valuable first (amount x times x how unsure): IKEA's three €300
    # purchases, then the three largest one-offs. The fourteen payments are never fourteen questions.
    subjects = {repo.transactions[n.subject_id].tx.counterparty for n in asked}
    assert subjects == {"IKEA ALFRAGIDE", "MOLDURAS ARTE", "CANDEEIROS LDA", "GRAFICA NOVA"}
    deferred = repo.onboarding.deferred
    assert len(deferred) == 14 - len(asked)
    # The deferred ones stay open, never closed on a guess, with a plain line for the owner.
    waiting = repo.transactions[sorted(deferred)[0]]
    assert waiting.tx.entity_id is None and not repo.items[waiting.item_id].is_done
    line = o.missing.plan(waiting)
    assert line.startswith("I will ask you later which company") and "Your first answers may settle it." in line
    plain(line)
    status = svc.onboarding_status()
    assert status["confirm"] == "We need you to confirm 4 things."
    # 6 of 20 payments explained, €300 of €2,190: the lower share, rounded down, is shown (§5, §57).
    assert status["headline"] == "We understand 13% of your business." and status["deferred"] == 10
    learned = [a for a in repo.activity if a.kind == "learned" and a.text.startswith("Learned how your business works")]
    assert learned and "We need you to confirm" in learned[-1].text
    plain(status["headline"], status["confirm"], learned[-1].text)

    # Answered with "Always": the whole IKEA series is learned, not asked again.
    ikea = next(n for n in asked if repo.transactions[n.subject_id].tx.counterparty == "IKEA ALFRAGIDE")
    svc.answer(ikea.id, "entity:hazel-tree", remember=True)
    ikea_rows = [r for r in repo.transactions.values() if r.tx.counterparty == "IKEA ALFRAGIDE"]
    assert all(r.tx.entity_id == "hazel-tree" for r in ikea_rows)
    assert not any(r.id in repo.onboarding.deferred for r in ikea_rows)
    # While first-run questions are open, deferred ones are never added.
    assert len(open_needs(o)) == len(asked) - 1
    for n in open_needs(o):
        svc.answer(n.id, "entity:company-b", remember=True)
    # Then one deferred question at a time, at most one a day: Needs You is never flooded.
    assert len(open_needs(o)) == 1
    o.run()
    assert len(open_needs(o)) == 1
    svc.answer(open_needs(o)[0].id, "entity:hazel-tree")
    assert not open_needs(o)  # the same day: nothing more
    o.run(start + timedelta(days=1))
    assert len(open_needs(o)) == 1


def test_a_first_run_without_bank_connections_decides_once_its_history_is_in() -> None:
    """A business that uploads a bank export instead of linking its bank: the export is the history."""
    svc = _new_business(at(5, 9, 0))
    repo = svc.repo
    svc.add_source({"kind": "card", "last4": "4817", "bank": "Millennium BCP", "companyId": "hazel-tree",
                    "personal": True})
    rows = [row(f"x-{i}", "card-4817", date(2026, 9, 1 + i), f"-{25 + i}.00", f"SHOP NUMBER {chr(65 + i)}",
                "COMPRA CARTAO", TransactionKind.CARD, card="4817") for i in range(12)]
    svc.orchestrator.ingest_bank(rows, at=at(5, 9, 5))
    assert not repo.onboarding.learning
    assert len(open_needs(svc.orchestrator)) == DEFAULT_QUESTION_LIMIT
    assert len(repo.onboarding.deferred) == 12 - DEFAULT_QUESTION_LIMIT


def test_a_connection_added_later_is_learned_the_same_way_within_the_same_cap() -> None:
    svc = _new_business(at(5, 9, 0))
    o, repo = svc.orchestrator, svc.repo
    svc.add_source({"kind": "card", "last4": "4817", "bank": "Millennium BCP", "companyId": "hazel-tree",
                    "personal": True})
    o.ingest_bank([row(f"a-{i}", "card-4817", date(2026, 9, 1 + i), f"-{30 + i}.00", f"SHOP NUMBER {chr(65 + i)}",
                       "COMPRA CARTAO", TransactionKind.CARD, card="4817") for i in range(6)], at=at(5, 9, 5))
    assert len(open_needs(o)) == DEFAULT_QUESTION_LIMIT
    # Company B's bank is linked a day later and reads its own 90 days: nothing more is asked while four are open.
    repo.clock.advance_to(at(6, 9, 0))
    connector = svc.link_bank("Caixa Geral de Depósitos", "company-b", [E.COMPANY_B_IBAN], date(2027, 3, 1))[
        "connectionId"]
    assert repo.onboarding.learning
    svc.sync_bank(connector, [row(f"b-{i}", "card-4817", date(2026, 8, 1 + i), f"-{60 + i}.00",
                                  f"LOJA NUMERO {chr(65 + i)}", "COMPRA CARTAO", TransactionKind.CARD, card="4817")
                              for i in range(8)], sync_state(at(6, 9, 5)))
    assert not repo.onboarding.learning
    assert len(open_needs(o)) == DEFAULT_QUESTION_LIMIT and len(repo.onboarding.deferred) == 2 + 8


def test_the_demo_has_no_first_run_and_asks_as_before() -> None:
    o = build_demo()
    assert not o.repo.onboarding.started and not o.repo.onboarding.deferred
    assert [n.id for n in open_needs(o)] == ["nd_ikea_418", "nd_vodafone_iban"]


# =========================================================================== 2. onboarding measured (A11, U1, U2)


def _email_with_invoice(when: datetime, company_nif: str) -> bytes:
    invoice = qr_invoice("FT N2026/501", "123.00", "100.00", "23.00", "2026-10-03", buyer=company_nif)
    return E.email(sender="Papelaria Norte <faturas@papelarianorte.pt>", to="rita@lumen.pt",
                   subject="Fatura FT N2026/501", at=when, text="Segue a fatura.\n",
                   attachments=(("FT_N2026_501.txt", "text/plain", invoice),),
                   message_id="<ft-n2026-501@papelarianorte.pt>")


def test_onboarding_milestones_time_to_first_value_and_active_minutes_come_from_recorded_events() -> None:
    t0 = at(5, 10, 0)
    svc = BackOfficeService.new_tenant("t-onboarding", owner_name="Rita Sousa", owner_email="rita@lumen.pt", now=t0)
    o, repo = svc.orchestrator, svc.repo
    assert o.activation() is not None and not o.activation().activated

    def step(minutes: float) -> datetime:
        return repo.clock.advance_to(t0 + timedelta(seconds=int(minutes * 60)))

    step(1)
    svc.add_company("Hazel Tree", E.HAZEL_NIF, "Hazel Tree Interiores, Lda.")
    step(2)
    mailbox = svc.add_source({"kind": "email", "address": "rita@lumen.pt", "provider": "google"})["id"]
    step(2.5)
    svc.add_source({"kind": "supplier", "name": "Papelaria Norte", "taxId": NORTE_NIF,
                    "email": "faturas@papelarianorte.pt"})
    step(3)
    connector = svc.link_bank("Millennium BCP", "hazel-tree", [E.HAZEL_IBAN], date(2027, 3, 1))["connectionId"]
    account = next(a.id for a in repo.accounts.values() if a.iban == E.HAZEL_IBAN)
    step(4)
    svc.sync_bank(connector, [row("n-1", account, date(2026, 10, 3), "-123.00", "PAPELARIA NORTE", "TRF FT N2026/501")],
                  sync_state(t0 + timedelta(minutes=4)))
    step(4.5)
    svc.sync_mail(mailbox, [_email_with_invoice(t0, E.HAZEL_NIF)], sync_state(t0 + timedelta(minutes=4.5)))
    paid = next(iter(repo.transactions.values()))
    assert stage(o, paid) is Stage.CLOSED  # matched by itself

    milestones = repo.onboarding.milestones
    assert milestones["account_created"] == t0
    assert milestones["company_added"] == t0 + timedelta(minutes=1)
    assert milestones["email_connected"] == t0 + timedelta(minutes=2)
    assert milestones["bank_connected"] == t0 + timedelta(minutes=3)
    assert milestones["historical_scan_complete"] == t0 + timedelta(minutes=4)
    assert milestones["first_document_found"] == t0 + timedelta(minutes=4.5)
    assert milestones["first_auto_match"] == t0 + timedelta(minutes=4.5)
    # Every milestone is an audit event with its time.
    recorded = [r.data() for r in repo.audit_store.records(repo.tenant_id)]
    logged = {d["subject_id"]: d["extracted_values"]["at"] for d in recorded if d.get("action") == "milestone"}
    assert logged["first_auto_match"] == (t0 + timedelta(minutes=4.5)).isoformat()

    report = o.activation()
    assert not report.activated and [c.value for c in report.missing] == ["time_saved_seen"]
    assert report.time_to_first_value == timedelta(minutes=4.5) and report.first_value_on_target is True
    # The owner's onboarding time: one span per set-up step while onboarding was open.
    spans = [i for i in repo.interactions if i.kind is InteractionKind.ONBOARDING]
    assert len(spans) == 5 and sum(i.active_seconds for i in spans) == 200  # account, company, email, supplier, bank
    assert repo.onboarding.finished_at == t0 + timedelta(minutes=4)  # history in, nothing to ask

    targets = {t["id"]: t for t in overview([svc])["targets"]}
    assert targets["time_to_first_value"]["value"] == 4.5 and targets["time_to_first_value"]["onTarget"] is True
    assert targets["time_to_first_value"]["display"] == "4.5 min"
    assert targets["activated"]["display"] == "No" and "the owner saw the time saved" in \
        targets["activated"]["evidence"]
    assert targets["onboarding_minutes"]["value"] == 4 and targets["onboarding_minutes"]["estimate"] is True
    assert targets["onboarding_minutes"]["onTarget"] is True

    step(5)
    status, body = svc.dispatch("POST", "/api/onboarding/seen", {"what": "time_saved"})
    assert status == 200 and body["recorded"] is True
    report = o.activation()
    assert report.activated and report.activated_at == t0 + timedelta(minutes=5)
    targets = {t["id"]: t for t in overview([svc])["targets"]}
    assert targets["activated"]["display"] == "Yes" and targets["activated"]["onTarget"] is True
    status, body = svc.dispatch("GET", "/api/onboarding")
    assert status == 200 and body["activated"] is True and body["timeToFirstValueSeconds"] == 270


def test_answers_to_first_run_questions_count_as_onboarding_time_and_later_answers_do_not() -> None:
    svc = _new_business(at(5, 9, 0))
    repo = svc.repo
    svc.add_source({"kind": "card", "last4": "4817", "bank": "Millennium BCP", "companyId": "hazel-tree",
                    "personal": True})
    svc.orchestrator.ingest_bank([row("q-1", "card-4817", date(2026, 9, 3), "-80.00", "LOJA DO GATO", "COMPRA",
                                      TransactionKind.CARD, card="4817")], at=at(5, 9, 5))
    first = open_needs(svc.orchestrator)[0]
    svc.answer(first.id, "entity:hazel-tree")
    kinds = [i.kind for i in repo.interactions]
    assert kinds[-1] is InteractionKind.ONBOARDING
    assert repo.onboarding.finished_at is not None
    svc.orchestrator.ingest_bank([row("q-2", "card-4817", date(2026, 10, 4), "-90.00", "FERRAGENS SILVA", "COMPRA",
                                      TransactionKind.CARD, card="4817")], at=at(6, 9, 0))
    later = next(n for n in open_needs(svc.orchestrator) if n.subject_id != first.subject_id)
    svc.answer(later.id, "entity:company-b")
    assert repo.interactions[-1].kind is InteractionKind.ANSWER


def test_the_demo_shows_not_measured_yet_for_onboarding_figures() -> None:
    svc = BackOfficeService.demo()
    targets = {t["id"]: t for t in overview([svc])["targets"]}
    for key in ("onboarding_minutes", "time_to_first_value", "activated"):
        assert targets[key]["value"] is None and targets[key]["onTarget"] is None
        assert targets[key]["display"] == "Not measured yet"
        assert targets[key]["evidence"].startswith("Not measured yet")
    assert svc.orchestrator.activation() is None
    status, body = svc.dispatch("GET", "/api/onboarding")
    assert status == 200 and body["started"] is False and body["message"].startswith("Not measured yet")
    status, _ = svc.dispatch("POST", "/api/onboarding/seen", {"what": "time_saved"})
    assert status == 409


# =========================================================================== 3. which company: name, address, mailbox (H2, H3, H5)


def _companies() -> list[LegalEntity]:
    return [LegalEntity(id="hazel-tree", tenant_id="t", name="Hazel Tree", country="PT", tax_id=E.HAZEL_NIF),
            LegalEntity(id="company-b", tenant_id="t", name="Company B", country="PT", tax_id=E.COMPANY_B_NIF)]


DIRECTORY = CompanyDirectory(
    names={"hazel-tree": ("Hazel Tree Interiores, Lda.",), "company-b": ("Company B, Lda.",)},
    addresses={"hazel-tree": ("Rua da Rosa 57, 1200-384 Lisboa",), "company-b": ("Rua das Flores 12, 1200-195 Lisboa",)},
    mailboxes={"faturas@companyb.pt": "company-b"},
)


def _doc(customer: str | None = None) -> Document:
    return Document(id="doc_x", tenant_id="t", evidence_ids=["ev_x"], doc_type=DocumentType.SIMPLIFIED_INVOICE,
                    supplier_name="Papelaria Norte", supplier_tax_id=NORTE_NIF, customer_tax_id=customer,
                    gross_amount=Decimal("45.10"))


def test_billing_name_address_and_mailbox_are_votes_with_plain_reasons() -> None:
    both = assign_entity(entities=_companies(), document=_doc(), directory=DIRECTORY,
                         addressee=Addressee(name="Company B, Lda.", address="Rua das Flores, n.º 12 - 1200-195 Lisboa"))
    assert (both.entity_id, both.quality, both.question) == ("company-b", Quality.GREEN, None)
    assert both.why == ("Made out to Company B, Lda.",
                        "Billed to Company B's address (Rua das Flores, n.º 12 - 1200-195 Lisboa)")
    mailbox = assign_entity(entities=_companies(), document=_doc(), directory=DIRECTORY,
                            addressee=Addressee(name="COMPANY B", mailboxes=("faturas@companyb.pt",)))
    assert mailbox.entity_id == "company-b" and mailbox.quality is Quality.GREEN
    assert "Sent to faturas@companyb.pt, Company B's mailbox" in mailbox.why
    # One hint alone is likely, and asked as the one question.
    alone = assign_entity(entities=_companies(), document=_doc(), directory=DIRECTORY,
                          addressee=Addressee(name="Hazel Tree Interiores Lda"))
    assert alone.quality is Quality.AMBER and alone.question is not None and alone.entity_id == "hazel-tree"
    assert "Made out to Hazel Tree Interiores Lda" in alone.question.why
    plain(*both.why, *mailbox.why)
    # A house number or postal code that differs is another address; nothing fuzzy.
    assert not same_address("Rua da Rosa 57, 1200-384 Lisboa", "Rua da Rosa 59, 1200-384 Lisboa")
    assert not same_address("Rua da Rosa 57, 1200-384 Lisboa", "Rua da Rosa 57, 4000-001 Porto")


def test_a_hint_never_overrides_the_tax_number_and_a_disagreement_is_the_one_question() -> None:
    agree = assign_entity(entities=_companies(), document=_doc(E.COMPANY_B_NIF), directory=DIRECTORY,
                          addressee=Addressee(name="Company B, Lda."))
    assert agree.entity_id == "company-b" and agree.quality is Quality.GREEN
    for hint in (Addressee(name="Hazel Tree Interiores, Lda."), Addressee(address="Rua da Rosa 57, 1200-384 Lisboa"),
                 Addressee(mailboxes=("faturas@companyb.pt",))):
        tax = E.HAZEL_NIF if hint.mailboxes else E.COMPANY_B_NIF
        result = assign_entity(entities=_companies(), document=_doc(tax), directory=DIRECTORY, addressee=hint)
        assert result.entity_id is None and result.quality is Quality.RED
        assert result.question is not None and result.question.prompt == "We aren't sure which company this belongs to."
    # Against the paying company's own account, too.
    tx = Transaction(id="tx_1", tenant_id="t", account_id="acct-b", booked_on=date(2026, 9, 20),
                     amount=Decimal("-45.10"), counterparty="PAPELARIA NORTE")
    paid = assign_entity(entities=_companies(), transaction=tx, document=_doc(), directory=DIRECTORY,
                         ownership=OwnershipBook(accounts={"acct-b": "company-b"}),
                         addressee=Addressee(name="Hazel Tree Interiores, Lda."))
    assert paid.entity_id is None and paid.question is not None


def test_the_pipeline_uses_the_billing_name_and_address_on_an_invoice_without_a_tax_number(demo: Orchestrator) -> None:
    demo.repo.company_addresses["company-b"] = ["Rua das Flores 12, 1200-195 Lisboa"]
    text = ("Papelaria Norte, Lda.\nNIF: 509123457\nFatura simplificada n.º FS N2026/77\n"
            "Data de emissão: 20/09/2026\nCliente: Company B, Lda.\nRua das Flores 12\n1200-195 Lisboa\n"
            "Resmas de papel A4\nTotal: 45,10 €\n").encode()
    upload(demo, text, "FS_N2026_77.txt", at(2, 11))
    record = next(d for d in demo.repo.documents.values() if d.document.invoice_number == "FS N2026/77")
    assert record.document.customer_tax_id is None
    assert (record.billing_name, record.billing_address) == ("Company B, Lda.", "Rua das Flores 12, 1200-195 Lisboa")
    assert record.document.entity_id == "company-b"


def test_a_mailbox_or_alias_is_learned_from_invoices_and_can_be_set_by_the_owner(demo: Orchestrator) -> None:
    repo = demo.repo
    # The connected mailbox reads for all three companies: it is never one company's.
    assert "laura@hazeltree.pt" in repo.shared_mailboxes() and "laura@hazeltree.pt" not in repo.mailbox_map()
    for i, day in enumerate(("2026-07-20", "2026-08-20", "2026-09-20"), start=1):
        invoice = qr_invoice(f"FT N2026/{i}", "12.30", "10.00", "2.30", day, buyer=E.COMPANY_B_NIF,
                             buyer_name="Company B, Lda.", atcud=f"NRT7Q2KX-{i}")
        mail(demo, E.email(sender="Papelaria Norte <faturas@papelarianorte.pt>", to="compras@companyb.pt",
                           subject=f"Fatura {i}", at=at(2, 10), text="Segue a fatura.\n",
                           attachments=((f"FT_{i}.txt", "text/plain", invoice),),
                           message_id=f"<ft-{i}@papelarianorte.pt>"), at(2, 10, i))
    assert repo.mailbox_map()["compras@companyb.pt"] == "company-b"
    # An invoice without the buyer's tax number, sent to that alias and made out to Company B: Company B's.
    text = ("Papelaria Norte, Lda.\nNIF: 509123457\nFatura simplificada n.º FS N2026/90\n"
            "Data de emissão: 25/09/2026\nCliente: Company B\nTotal: 18,45 €\n").encode()
    mail(demo, E.email(sender="Papelaria Norte <faturas@papelarianorte.pt>", to="compras@companyb.pt",
                       subject="Fatura FS N2026/90", at=at(2, 11), text="Segue.\n",
                       attachments=(("FS_90.txt", "text/plain", text),), message_id="<fs-90@papelarianorte.pt>"),
         at(2, 11, 5))
    record = next(d for d in repo.documents.values() if d.document.invoice_number == "FS N2026/90")
    assert record.document.entity_id == "company-b"
    # The owner can point an alias at a company themselves.
    svc = BackOfficeService(demo)
    out = svc.mailboxes({"address": "Obras@HazelTree.pt", "companyId": "hazel-tree"})
    assert out["ok"] and repo.mailbox_map()["obras@hazeltree.pt"] == "hazel-tree"
    plain(out["message"])


# =========================================================================== 4. the usual card or account (L4)


def _adobe(i: int, day: date, card: str) -> Transaction:
    return Transaction(id=f"tx_a{i}", tenant_id="t", account_id=f"card-{card}", booked_on=day, amount=Decimal("-59.99"),
                       counterparty="ADOBE *CREATIVE CLOUD", card_last4=card)


def test_a_payment_series_remembers_the_usual_card_and_searches_it_first() -> None:
    history = [_adobe(i, date(2026, m, 22), "2291") for i, m in enumerate((5, 6, 7, 8, 9))]
    series = learn_from_transactions(history)[0]
    assert series.usual_method == "card:2291"
    other = _adobe(9, date(2026, 10, 22), "4817")
    note = payment_method_note(series, other, {"card:2291": "card •••• 2291", "card:4817": "card •••• 4817"})
    assert note == "Paid with a different card than usual: card •••• 4817 this time, usually card •••• 2291."
    assert payment_method_note(series, _adobe(8, date(2026, 10, 22), "2291")) is None
    plain(note)
    # Many other payments on other cards: the usual card is looked at first, so fewer are examined.
    noise = [Transaction(id=f"tx_n{i}", tenant_id="t", account_id="card-5530", booked_on=date(2026, 10, 1 + i % 28),
                         amount=Decimal(f"-{10 + i}.00"), counterparty=f"SHOP {i}", card_last4="5530") for i in range(40)]
    paid = _adobe(10, date(2026, 10, 22), "2291")
    found, looked = find_payment(series, [*noise, paid], around=date(2026, 10, 22))
    assert found == paid and looked == 1
    elsewhere = _adobe(11, date(2026, 10, 23), "4817")
    found, looked = find_payment(series, [*noise, elsewhere], around=date(2026, 10, 22))
    assert found == elsewhere and looked == len(noise) + 1


def test_a_payment_from_another_card_is_noted_not_held(demo: Orchestrator) -> None:
    before = len(open_needs(demo))
    demo.ingest_bank([row("ad-10", "card-5530", date(2026, 10, 2), "-59.99", "ADOBE *CREATIVE CLOUD", "COMPRA CARTAO",
                          TransactionKind.CARD, card="5530")], at=at(2, 12))
    rec = next(r for r in demo.repo.transactions.values()
               if r.tx.booked_on == date(2026, 10, 2) and "ADOBE" in r.tx.counterparty)
    assert rec.notes == ["Paid with a different card than usual: card •••• 5530 this time, usually card •••• 2291."]
    assert stage(demo, rec) is not Stage.NEEDS_OWNER and len(open_needs(demo)) == before
    line = next(a.text for a in demo.repo.activity if a.text.startswith("Adobe: Paid with a different card"))
    plain(line)
    detail = BackOfficeService(demo).transaction(rec.id)
    assert detail is not None and detail["notes"] == rec.notes
    # The demo's own payments were all paid the usual way: nothing noted there.
    assert all(not r.notes for r in build_demo().repo.transactions.values())


# =========================================================================== 5. learned expected evidence (J7)


def test_the_owners_answer_this_never_has_an_invoice_is_learned_for_later_payments(demo: Orchestrator) -> None:
    svc = BackOfficeService(demo)
    demo.ingest_bank([row("cl-1", "mbcp-ht", date(2026, 10, 1), "-25.00", "CLUBE LEITURA LISBOA", "TRF QUOTA")],
                     at=at(2, 12))
    first = tx_by(demo, "TRF QUOTA")
    assert first.decision.requires_document and demo.reconciliation.engine().overrides is not None
    assert [c["need"] for c in svc.transaction(first.id)["evidenceChoices"]] == ["none", "invoice"]
    status, body = svc.dispatch("POST", f"/api/transactions/{first.id}/evidence", {"need": "none"})
    assert status == 200 and body["ok"]
    plain(body["message"])
    assert first.decision.rule == "learned" and not first.decision.requires_document
    assert stage(demo, first) is Stage.NOT_REQUIRED
    answer = demo.repo.items[first.item_id].history[-1].evidence_ids
    assert len(answer) == 2 and answer[0] == first.evidence_id  # the owner's answer is the evidence (§54)
    # The next payment to the same club needs nothing, without asking.
    demo.ingest_bank([row("cl-2", "mbcp-ht", date(2026, 11, 2), "-25.00", "CLUBE LEITURA LISBOA", "TRF QUOTA NOV")],
                     at=local_datetime(date(2026, 11, 2), 12))
    later = tx_by(demo, "TRF QUOTA NOV")
    assert later.decision.expectation is EvidenceExpectation.BANK_EVIDENCE_SUFFICES
    assert later.decision.reason == "You told me Clube Leitura Lisboa never sends an invoice. The bank record is enough."
    assert stage(demo, later) is Stage.NOT_REQUIRED and later.id not in demo.repo.chases
    status, _ = svc.dispatch("POST", f"/api/transactions/{later.id}/evidence", {"need": "invoice"})
    assert status == 409  # already settled
    # "It always needs one" is learned the same way: a shop where a receipt would do.
    demo.ingest_bank([row("lc-1", "card-5530", date(2026, 11, 3), "-32.00", "LIVRARIA DO CHIADO", "COMPRA LIVRO 1",
                          TransactionKind.CARD, card="5530")], at=local_datetime(date(2026, 11, 3), 12))
    book = tx_by(demo, "COMPRA LIVRO 1")
    assert book.decision.expectation is EvidenceExpectation.RECEIPT and book.decision.quality is Quality.AMBER
    status, body = svc.dispatch("POST", f"/api/transactions/{book.id}/evidence", {"need": "invoice"})
    assert status == 200 and book.decision.expectation is EvidenceExpectation.INVOICE
    assert book.decision.quality is Quality.GREEN and book.decision.rule == "learned"
    demo.ingest_bank([row("lc-2", "card-5530", date(2026, 11, 20), "-18.00", "LIVRARIA DO CHIADO", "COMPRA LIVRO 2",
                          TransactionKind.CARD, card="5530")], at=local_datetime(date(2026, 11, 20), 12))
    assert tx_by(demo, "COMPRA LIVRO 2").decision.expectation is EvidenceExpectation.INVOICE
    status, _ = svc.dispatch("POST", f"/api/transactions/{book.id}/evidence", {"need": "maybe"})
    assert status == 400


def test_an_accountant_rule_about_invoices_is_learned_and_limited_to_its_company(demo: Orchestrator) -> None:
    svc = BackOfficeService(demo)
    demo.ingest_bank([
        row("lb-1", "mbcp-ht", date(2026, 10, 1), "-18.50", "LIVRARIA BERTRAND", "COMPRA LIVROS HT",
            TransactionKind.CARD, card="5530"),
        row("lb-2", "cgd-b", date(2026, 10, 1), "-21.00", "LIVRARIA BERTRAND", "COMPRA LIVROS B",
            TransactionKind.CARD, card="7702"),
        row("cl-3", "mbcp-ht", date(2026, 10, 1), "-25.00", "CLUBE LEITURA LISBOA", "TRF QUOTA HT"),
    ], at=at(2, 12))
    out = svc.accountant_rule("Livraria Bertrand always needs an invoice", "client", "hazel-tree")
    assert out["ok"] and out["rule"]["label"] == "Livraria Bertrand always needs an invoice"
    ht, b = tx_by(demo, "COMPRA LIVROS HT"), tx_by(demo, "COMPRA LIVROS B")
    assert ht.decision.expectation is EvidenceExpectation.INVOICE and ht.decision.rule == "learned"
    assert ht.decision.reason == "Your accountant said Livraria Bertrand always needs an invoice."
    assert b.decision.rule != "learned"  # Company B's payment: that rule is not used there
    out = svc.accountant_rule("Clube Leitura Lisboa never has an invoice.", "client", "hazel-tree")
    club = tx_by(demo, "TRF QUOTA HT")
    assert club.decision.expectation is EvidenceExpectation.BANK_EVIDENCE_SUFFICES
    assert stage(demo, club) is Stage.NOT_REQUIRED
    assert any(r.label == "Clube Leitura Lisboa needs no invoice"
               for r in demo.accountant_rules("hazel-tree"))
    plain(out["message"], club.decision.reason)


# =========================================================================== 6. payslips prove salaries (J3)


def payslip(name: str = "Ana Maria Costa", net: str = "1.012,50", *, iban: str | None = ANA_IBAN,
            gross: str | None = "1.250,00", period: str = "setembro de 2026", employer_nif: str = E.HAZEL_NIF) -> bytes:
    lines = ["Recibo de Vencimento", "Entidade patronal: Hazel Tree Interiores, Lda.", f"NIF: {employer_nif}",
             f"Trabalhador: {name} - NIF 234567890"]
    if iban:
        lines.append(f"IBAN: {iban[:4]} {iban[4:8]} {iban[8:12]} {iban[12:16]} {iban[16:20]} {iban[20:]}")
    lines += [f"Período: {period}", "Data: 30/09/2026", "Vencimento base: 1.250,00"]
    if gross:
        lines += [f"Total ilíquido: {gross} €", "Segurança Social (11%): 137,50 €", "Retenção IRS: 100,00 €",
                  "Total de descontos: 237,50 €"]
    lines.append(f"Líquido a receber: {net} €")
    return ("\n".join(lines) + "\n").encode()


def test_a_payslip_is_read_as_payroll_evidence_and_never_as_a_letter_or_an_invoice(demo: Orchestrator) -> None:
    read = read_payslip(payslip().decode())
    assert read is not None and (read.employee, read.net, read.gross, str(read.period)) == (
        "Ana Maria Costa", Decimal("1012.50"), Decimal("1250.00"), "2026-09")
    assert read.employee_iban == ANA_IBAN and read.employer_tax_id == E.HAZEL_NIF and read.quality is Quality.GREEN
    obligations = len(demo.repo.obligations)
    report = upload(demo, payslip(), "recibo_ana_2026-09.txt", at(2, 11))
    record = demo.repo.documents[report.document_ids[0]]
    assert record.document.doc_type is DocumentType.PAYROLL and record.payslip is not None
    assert record.document.entity_id == "hazel-tree" and record.label.startswith("Payslip")
    assert len(demo.repo.obligations) == obligations  # Social Security and IRS lines are not a tax letter
    assert demo.repo.employees == {ANA_IBAN: "Ana Maria Costa"}
    plain(report.message)


def test_a_salary_to_the_employees_account_closes_only_on_that_months_payslip(demo: Orchestrator) -> None:
    # Paid before the payslip arrived: not yet known as a salary.
    demo.ingest_bank([row("sal-1", "mbcp-ht", date(2026, 9, 30), "-1012.50", "ANA COSTA", "TRF",
                          iban=ANA_IBAN)], at=at(2, 11))
    salary = tx_by(demo, "TRF")
    assert salary.decision.expectation is not EvidenceExpectation.PAYROLL
    assert stage(demo, salary) is not Stage.CLOSED
    upload(demo, payslip(), "recibo_ana.txt", at(2, 12))
    assert salary.decision.expectation is EvidenceExpectation.PAYROLL and salary.decision.quality is Quality.GREEN
    assert stage(demo, salary) is Stage.CLOSED
    assert salary.match_headline == "Ana Maria Costa's salary for September"
    assert "Paid to Ana Maria Costa's account ending in 2223, as on the payslip" in salary.match_why
    plain(salary.match_headline, *salary.match_why)
    # October's salary to the same account, the payslip not in yet: a salary by the account, still open.
    demo.ingest_bank([row("sal-2", "mbcp-ht", date(2026, 10, 30), "-1012.50", "ANA COSTA", "TRF OUT",
                          iban=ANA_IBAN)], at=local_datetime(date(2026, 10, 30), 18))
    october = tx_by(demo, "TRF OUT")
    assert october.decision.rule == "employee_iban" and stage(demo, october) is not Stage.CLOSED
    assert not october.document_ids  # September's payslip never proves October's salary


def test_a_salary_by_wording_stays_open_with_a_plain_request_until_its_payslip_arrives(demo: Orchestrator) -> None:
    demo.ingest_bank([row("sal-3", "mbcp-ht", date(2026, 9, 30), "-980.00", "RUI PEDRO SANTOS",
                          "SALARIO SETEMBRO")], at=at(2, 11))
    salary = tx_by(demo, "SALARIO SETEMBRO")
    assert salary.decision.expectation is EvidenceExpectation.PAYROLL and salary.decision.quality is Quality.AMBER
    assert stage(demo, salary) is not Stage.CLOSED  # never closed by wording alone
    # The accountant runs payroll: one request to them, through the send path.
    demo.run(at(4, 9))
    requests = [m for m in demo.repo.outbox.values() if m.kind == "payslip_request"]
    assert len(requests) == 1 and requests[0].to == E.ACCOUNTANT_EMAIL and requests[0].sent
    assert "Rui Pedro Santos: €980.00 paid on 30 September" in requests[0].body
    plan = demo.missing.plan(salary)
    assert plan == ("The €980.00 salary to Rui Pedro Santos on 30 September needs Rui Pedro Santos's payslip for "
                    "September. I asked Contabilidade Vidal for it.")
    asked = [a.text for a in demo.repo.activity if "payslip" in a.text]
    assert asked == ["Asked your accountant for one payslip."]  # not counted as a supplier email
    plain(plan, *asked)
    demo.run(at(5, 9))
    assert len([m for m in demo.repo.outbox.values() if m.kind == "payslip_request"]) == 1  # asked once
    # A payslip with another net pay does not prove it.
    upload(demo, payslip("Rui Pedro Santos", "1.000,00", iban=None, gross=None), "recibo_rui_errado.txt", at(5, 10))
    assert stage(demo, salary) is not Stage.CLOSED
    upload(demo, payslip("Rui Pedro Santos", "980,00", iban=None, gross=None), "recibo_rui.txt", at(5, 11))
    assert stage(demo, salary) is Stage.CLOSED and "The bank line names Rui Pedro Santos" in salary.match_why


def test_the_owner_who_runs_payroll_gets_the_request_and_tax_payments_keep_their_rules(demo: Orchestrator) -> None:
    svc = BackOfficeService(demo)
    svc.company_profile("hazel-tree", {"payroll": "owner"})
    demo.ingest_bank([row("sal-4", "mbcp-ht", date(2026, 9, 30), "-870.00", "JOANA MELO", "VENCIMENTO SETEMBRO"),
                      row("ss-1", "mbcp-ht", date(2026, 10, 1), "-412.30", "SEG SOCIAL", "PAG SEG SOCIAL 2026/09")],
                     at=at(2, 11))
    demo.run(at(5, 9))
    salary = tx_by(demo, "VENCIMENTO SETEMBRO")
    assert not [m for m in demo.repo.outbox.values() if m.kind == "payslip_request"]
    assert demo.missing.plan(salary).endswith("Please send it to me, and I will match it.")
    social = tx_by(demo, "PAG SEG SOCIAL 2026/09")
    assert social.decision.expectation is EvidenceExpectation.TAX_NOTICE_OR_PROOF
    upload(demo, payslip("Joana Melo", "870,00", iban=None, gross=None), "recibo_joana.txt", at(5, 10))
    assert stage(demo, salary) is Stage.CLOSED
    assert social.decision.expectation is EvidenceExpectation.TAX_NOTICE_OR_PROOF and not social.document_ids


# =========================================================================== 7. personal purchase on a company card (X23)


def test_clearly_personal_shops_are_flagged_only_as_strong_signals() -> None:
    assert personal_signal("NETFLIX.COM", "COMPRA CARTAO") == "a streaming service"
    assert personal_signal("ZARA PORTUGAL") == "a clothes shop"
    assert personal_signal("ZARA PORTUGAL", sector="fashion boutique") is None
    assert personal_signal("CONTINENTE MATOSINHOS") is None  # without the sector, a supermarket is not strong
    assert personal_signal("CONTINENTE MATOSINHOS", sector="interior design") == "a supermarket"
    assert personal_signal("CONTINENTE MATOSINHOS", sector="Café e pastelaria") is None
    assert personal_signal("NETFLIX.COM", known_supplier=True) is None
    assert personal_signal("PAPELARIA NORTE") is None and personal_signal("BARBEARIA DO ZE", sector="barber") is None


def test_a_personal_purchase_on_a_company_card_is_one_plain_question_with_always_learning(demo: Orchestrator) -> None:
    svc = BackOfficeService(demo)
    demo.ingest_bank([row("nf-1", "card-5530", date(2026, 10, 1), "-13.99", "NETFLIX.COM", "COMPRA NETFLIX 1",
                          TransactionKind.CARD, card="5530")], at=at(2, 12))
    netflix = tx_by(demo, "COMPRA NETFLIX 1")
    assert netflix.tx.entity_id is None and stage(demo, netflix) is Stage.NEEDS_OWNER
    needs = next(n for n in open_needs(demo) if n.subject_id == netflix.id)
    assert needs.question.prompt == "Was this Netflix payment for Hazel Tree or personal?"
    assert [o.label for o in needs.question.options][:2] == ["Hazel Tree", "Personal"]
    assert "Netflix is a streaming service, usually a personal cost" in needs.why
    plain(needs.question.prompt, *needs.why)
    shown = next(i for i in svc.needs_you()["items"] if i["id"] == needs.id)
    assert shown["remember"]["overrides"]["personal"] == "Always treat Netflix paid with card •••• 5530 as personal"
    svc.answer(needs.id, "personal", remember=True)
    assert netflix.private
    demo.ingest_bank([row("nf-2", "card-5530", date(2026, 11, 1), "-13.99", "NETFLIX.COM", "COMPRA NETFLIX 2",
                          TransactionKind.CARD, card="5530")], at=local_datetime(date(2026, 11, 1), 12))
    later = tx_by(demo, "COMPRA NETFLIX 2")
    assert later.private and not any(n.subject_id == later.id for n in demo.repo.needs.values())


def test_an_answer_for_the_company_is_remembered_and_ordinary_suppliers_are_never_asked(demo: Orchestrator) -> None:
    svc = BackOfficeService(demo)
    demo.repo.company_sectors["hazel-tree"] = "interior design"
    demo.ingest_bank([
        row("sp-1", "card-5530", date(2026, 10, 1), "-9.99", "SPOTIFY P1234", "COMPRA SPOTIFY 1", TransactionKind.CARD,
            card="5530"),
        row("pn-1", "card-5530", date(2026, 10, 1), "-35.00", "PAPELARIA NORTE", "COMPRA PAPEL", TransactionKind.CARD,
            card="5530"),
        row("lx-1", "card-5530", date(2026, 10, 1), "-64.20", "LOJA DAS TINTAS", "COMPRA TINTAS", TransactionKind.CARD,
            card="5530"),
    ], at=at(2, 12))
    assert tx_by(demo, "COMPRA PAPEL").tx.entity_id == "hazel-tree"
    assert tx_by(demo, "COMPRA TINTAS").tx.entity_id == "hazel-tree"
    spotify = tx_by(demo, "COMPRA SPOTIFY 1")
    needs = next(n for n in open_needs(demo) if n.subject_id == spotify.id)
    svc.answer(needs.id, "entity:hazel-tree")  # music for the showroom: no "always"
    demo.ingest_bank([row("sp-2", "card-5530", date(2026, 11, 1), "-9.99", "SPOTIFY P1234", "COMPRA SPOTIFY 2",
                          TransactionKind.CARD, card="5530")], at=local_datetime(date(2026, 11, 1), 12))
    assert tx_by(demo, "COMPRA SPOTIFY 2").tx.entity_id == "hazel-tree"  # remembered: not asked again
    # A supermarket is flagged for this interior design business, never for a café.
    demo.ingest_bank([row("ct-1", "card-5530", date(2026, 11, 2), "-48.30", "CONTINENTE", "COMPRA SUPER",
                          TransactionKind.CARD, card="5530")], at=local_datetime(date(2026, 11, 2), 12))
    assert tx_by(demo, "COMPRA SUPER").tx.entity_id is None
    demo.repo.company_sectors["company-b"] = "café"
    demo.ingest_bank([row("ct-2", "card-7702", date(2026, 11, 2), "-52.10", "CONTINENTE", "COMPRA SUPER B",
                          TransactionKind.CARD, card="7702")], at=local_datetime(date(2026, 11, 2), 13))
    assert tx_by(demo, "COMPRA SUPER B").tx.entity_id == "company-b"


# =========================================================================== 8. due dates (F8)


def _obs(value: Any, method: ExtractionMethod, source: str = "ev_1") -> FieldObservation:
    return FieldObservation(value=value, source=source, method=method, confidence=0.95)


def test_a_due_date_is_checked_against_the_issue_date_and_across_sources() -> None:
    base = {"issue_date": [_obs(date(2026, 9, 20), ExtractionMethod.EMBEDDED_TEXT)],
            "gross_amount": [_obs(Decimal("12.30"), ExtractionMethod.EMBEDDED_TEXT)]}
    before = assess_document({**base, "due_date": [_obs(date(2026, 9, 1), ExtractionMethod.EMBEDDED_TEXT)]})
    due = before.fields["due_date"]
    assert due.value is None and due.quality is Quality.AMBER and DUE_BEFORE_ISSUE in due.reasons
    assert before.quality is not Quality.RED  # kept, not used; never a question on its own
    ok = assess_document({**base, "due_date": [_obs(date(2026, 9, 30), ExtractionMethod.EMBEDDED_TEXT)]})
    assert ok.fields["due_date"].value == date(2026, 9, 30)
    agree = assess_document({**base, "due_date": [_obs(date(2026, 9, 30), ExtractionMethod.STRUCTURED_XML, "ev_x"),
                                                  _obs(date(2026, 9, 30), ExtractionMethod.EMBEDDED_TEXT)]})
    assert agree.fields["due_date"].quality is Quality.GREEN
    clash = assess_document({**base, "due_date": [_obs(date(2026, 9, 30), ExtractionMethod.STRUCTURED_XML, "ev_x"),
                                                  _obs(date(2026, 10, 30), ExtractionMethod.EMBEDDED_TEXT)]})
    assert clash.fields["due_date"].quality is Quality.RED and clash.quality is Quality.RED
    # A date that does not exist is never used.
    bad = assess_document({**base, "due_date": [_obs("31/02/2026", ExtractionMethod.EMBEDDED_TEXT)]})
    assert bad.fields["due_date"].value is None


def test_an_unpaid_invoice_past_its_due_date_is_overdue_in_plain_words(demo: Orchestrator) -> None:
    svc = BackOfficeService(demo)
    upload(demo, qr_invoice("FT N2026/601", "123.00", "100.00", "23.00", "2026-09-20", due="30/09/2026",
                            atcud="NRT7Q2KX-601"), "FT_601.txt", at(2, 11))
    upload(demo, qr_invoice("FT N2026/602", "61.50", "50.00", "11.50", "2026-09-25", due="10/10/2026",
                            atcud="NRT7Q2KX-602"), "FT_602.txt", at(2, 11, 5))
    upload(demo, qr_invoice("FT N2026/603", "24.60", "20.00", "4.60", "2026-09-26", due="20/09/2026",
                            atcud="NRT7Q2KX-603"), "FT_603.txt", at(2, 11, 10))
    docs = {d.document.invoice_number: d for d in demo.repo.documents.values()}
    overdue = demo.invoice_due(docs["FT N2026/601"])
    assert overdue is not None and overdue["overdue"] and overdue["due"] == date(2026, 9, 30)
    assert overdue["line"] == "The Papelaria Norte invoice of €123.00 was due on 30 September. I haven't seen its payment yet."
    soon = demo.invoice_due(docs["FT N2026/602"])
    assert soon is not None and not soon["overdue"] and soon["line"] == \
        "The Papelaria Norte invoice of €61.50 is due on 10 October."
    assert demo.invoice_due(docs["FT N2026/603"]) is None  # due before it was issued: not used
    home = {i["id"]: i for i in svc.home()["dueSoon"]}
    shown = home[f"due_{docs['FT N2026/601'].id}"]
    assert shown["tone"] == "risk" and shown["note"] == overdue["line"] and shown["title"] == "Papelaria Norte invoice"
    assert home[f"due_{docs['FT N2026/602'].id}"]["tone"] == "neutral"
    month = svc.month("hazel-tree", "2026-09")
    assert {"id": f"r_{docs['FT N2026/601'].id}", "tone": "attention", "text": overdue["line"]} in month["remaining"]
    detail = svc.document(docs["FT N2026/601"].id)
    assert detail["due"] == "2026-09-30" and detail["dueLine"] == overdue["line"]
    plain(overdue["line"], soon["line"])
    # Paid: no longer due.
    demo.ingest_bank([row("pn-601", "mbcp-ht", date(2026, 10, 2), "-123.00", "PAPELARIA NORTE", "TRF FT N2026/601")],
                     at=at(2, 14))
    assert docs["FT N2026/601"].matched_tx_ids and demo.invoice_due(docs["FT N2026/601"]) is None
    assert f"due_{docs['FT N2026/601'].id}" not in {i["id"] for i in svc.home()["dueSoon"]}


def test_a_likely_payment_on_the_usual_account_is_named_instead_of_calling_it_unpaid(demo: Orchestrator) -> None:
    # Papelaria Norte is paid every month from Hazel Tree's account.
    demo.repo.history_transactions += [
        Transaction(id=f"hist_pn{m}", tenant_id=demo.repo.tenant_id, account_id="mbcp-ht", booked_on=date(2026, m, 28),
                    amount=Decimal("-61.50"), counterparty="PAPELARIA NORTE", entity_id="hazel-tree")
        for m in (6, 7, 8)]
    # An invoice with no fiscal code (its total is not confirmed), due on 30 September, and a payment of that amount.
    text = ("Papelaria Norte, Lda.\nNIF: 509123457\nFatura n.º FT N2026/700\nData de emissão: 20/09/2026\n"
            "Data de vencimento: 30/09/2026\nCliente: Hazel Tree Interiores, Lda.\nNIF: 516123459\n"
            "Total: 61,50 €\n").encode()
    upload(demo, text, "FT_700.txt", at(2, 11))
    demo.ingest_bank([row("pn-700", "mbcp-ht", date(2026, 9, 29), "-61.50", "PAPELARIA NORTE", "TRF PAPEL")],
                     at=at(2, 12))
    record = next(d for d in demo.repo.documents.values() if d.document.invoice_number == "FT N2026/700")
    assert record.document.quality is not Quality.GREEN and not record.matched_tx_ids  # never matched on a guess
    found = demo.invoice_due(record)
    assert found is not None and found["overdue"]
    assert found["line"] == ("The Papelaria Norte invoice of €61.50 was due on 30 September. A payment of €61.50 on "
                             "29 September from Millennium BCP •••• 0265 may be it: I'm checking.")
    plain(found["line"])


# =========================================================================== the demo


def test_the_demo_outcomes_are_unchanged() -> None:
    svc = BackOfficeService.demo()
    home = svc.home()
    assert (home["headline"], home["needsYouCount"], home["currentMonth"]["percentClosed"]) == ("Action required.", 2, 85)
    assert [(c["id"], c["statusLabel"]) for c in home["companies"]] == [
        ("hazel-tree", "On track"), ("company-b", "Closed"), ("company-c", "Needs one answer")]
    assert [i["id"] for i in home["dueSoon"]] == ["due_nd_vodafone_iban", "due_obl_f2220c211f4ee738"]
    assert [i["id"] for i in svc.needs_you()["items"]] == ["nd_ikea_418", "nd_vodafone_iban"]
    assert len(svc.activity()["items"]) == 28
    months = {(c, m): svc.month(c, m)["percentClosed"] for c, m in (("company-b", "2026-09"), ("company-c", "2026-09"),
                                                                    ("hazel-tree", "2026-09"), ("hazel-tree", "2026-10"))}
    assert months == {("company-b", "2026-09"): 100, ("company-c", "2026-09"): 66, ("hazel-tree", "2026-09"): 90,
                      ("hazel-tree", "2026-10"): 0}
    repo = svc.repo
    assert not repo.onboarding.started and not repo.employees and not repo.payslip_requests
    assert all(not r.notes for r in repo.transactions.values())
    assert all(d.payslip is None for d in repo.documents.values())
    assert Month(2026, 9) == svc._current_month()
