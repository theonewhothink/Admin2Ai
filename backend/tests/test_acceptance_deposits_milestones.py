"""Acceptance: customer deposits, milestones and staged payments, parts held back (checklist X8, I2, I4, I5).

Cases 11 (construction), 14 (architecture), 21 (law-firm retainers), 27 (recruitment milestones), 29
(photographer), 30 (event planner) and 31 (wedding venue). Each test replays the demo tenant (Hazel Tree,
Company B, Company C) and feeds it the evidence of one scenario through the real orchestrator and service:
fiscal-QR invoice text layers, bank rows and one-tap answers.

1. A deposit (a bank line that says so, or names a quote or contract; or the payment of an advance invoice)
   is recorded as a deposit for that customer, and cost center when known, never as income for the work.
   The final invoice takes it off when it says so and its amounts add up that way, or when the owner
   confirms it; the balance then closes the final invoice. Income answers never count it twice.
2. Several payments for one invoice close it when they add up exactly, each with its own evidence; a part
   payment leaves a plain "€X of €Y received". Parts are kept on proof (the reference and the customer) or
   on the owner's one tap, never on a guess.
3. Part of a staged invoice held back by the customer ("Retenção de garantia") stays owed, due on its
   release date, visible to the owner; the invoice closes for the part paid; the release closes on arrival.
4. A deposit paid to a supplier is taken off its final invoice the same way.
5. A deposit given back when a booking is cancelled is linked to it; neither is income nor a cost.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from test_acceptance_engine_fixes import CUSTOMER_NIF, hazel_sale, qr_document, upload

from backoffice.closure import Month
from backoffice.demo import build_demo
from backoffice.demo import evidence as E
from backoffice.deposits import customer_name, deposit_wording, is_advance_invoice, read_terms
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import Quality, Supplier, TransactionKind
from backoffice.language import find_jargon, find_off_tone
from backoffice.orchestrator import BankRow, Orchestrator, local_datetime
from backoffice.service import BackOfficeService
from backoffice.spending import Ledger

C_NIF = "517003210"  # Company C, the demo's events company in these scenarios
MARIA_NIF = "212345672"  # a private customer (valid check digit)
JOANA_IBAN = "PT50003500001234567890123"
MARIA_IBAN = "PT50001000001234567890154"
LIMA_NIF = "508123453"  # Marcenaria Lima, a carpenter supplying Hazel Tree
BUILDER_NIF = "507123450"  # Construtora Sul, a customer of Hazel Tree's fit-out work


# --------------------------------------------------------------------------- helpers


def at(month: int, day: int, hour: int = 10) -> datetime:
    return local_datetime(date(2026, month, day), hour)


def bank_in(bank_id: str, account: str, day: date, amount: str, who: str, description: str,
            iban: str | None = None) -> BankRow:
    kind = TransactionKind.TRANSFER_IN if Decimal(amount) > 0 else TransactionKind.TRANSFER_OUT
    return BankRow(bank_id=bank_id, account_id=account, booked_on=day, amount=Decimal(amount), counterparty=who,
                   description=description, kind=kind, counterparty_iban=iban)


def tx(o: Orchestrator, description: str):  # type: ignore[no-untyped-def]
    return next(r for r in o.repo.transactions.values() if r.tx.description == description)


def pay(o: Orchestrator, bank_id: str, account: str, day: date, amount: str, who: str, description: str,
        iban: str | None = None):  # type: ignore[no-untyped-def]
    o.ingest_bank([bank_in(bank_id, account, day, amount, who, description, iban)])
    return tx(o, description)


def stage(o: Orchestrator, record) -> Stage:  # type: ignore[no-untyped-def]
    return o.repo.items[record.item_id].stage


def document(o: Orchestrator, data: bytes, name: str):  # type: ignore[no-untyped-def]
    return o.repo.documents[upload(o, data, name).document_ids[0]]


def plain(*texts: str) -> None:
    for text in texts:
        assert not find_jargon(text), (text, find_jargon(text))
        assert not find_off_tone(text), text
        for internal in ("tx_", "doc_", "ev_", "nd_"):
            assert internal not in text, text


def owner_texts(value) -> list[str]:  # type: ignore[no-untyped-def]
    """Every string an owner may read (ids, keys and machine fields excluded)."""
    skip = {"id", "href", "companyId", "evidenceIds", "kind", "status", "tone", "due", "date", "currency", "step",
            "transactionId", "today", "stage", "at", "optionId", "until"}
    if isinstance(value, dict):
        return [t for k, v in value.items() if k not in skip for t in owner_texts(v)]
    if isinstance(value, list):
        return [t for v in value for t in owner_texts(v)]
    return [value] if isinstance(value, str) else []


def golden_rule(o: Orchestrator) -> None:
    """Every transition carries stored evidence, a closed item is GREEN, the audit chain holds, nothing reopens
    (§3, §55), and every screen still reads."""
    for item in o.repo.items.values():
        for t in item.history:
            assert t.evidence_ids and t.actor, item.id
            for ev in t.evidence_ids:
                o.repo.evidence(ev)
        if item.stage is Stage.CLOSED:
            assert item.quality is Quality.GREEN, item.id
    assert o.repo.audit.verify(o.repo.tenant_id).ok
    assert o.auditor.recheck() == []
    svc = BackOfficeService(o)
    for path in ("/api/home", "/api/needs-you", "/api/pipeline", "/api/activity", "/api/months/company-c/2026-09",
                 "/api/months/hazel-tree/2026-09", "/api/months/company-c/2026-08", "/api/months/hazel-tree/2026-08"):
        status, body = svc.dispatch("GET", path, None)
        assert status == 200, (path, body)
    for n in svc.needs_you()["items"]:  # what the owner reads in Needs you
        plain(n.get("question", ""), *n.get("why", []), *(o["label"] for o in n.get("options", [])))
    for company in ("hazel-tree", "company-c"):
        for month in ("2026-08", "2026-09"):
            view = svc.month(company, month) or {}
            plain(*(r["text"] for r in view.get("remaining", [])), *(r["text"] for r in view.get("notices", [])),
                  *(m["description"] for m in view.get("matched", [])),
                  *(r for m in view.get("matched", []) for r in m["reasons"]))
    plain(*(d["note"] for d in svc.home()["dueSoon"]), *(d["title"] for d in svc.home()["dueSoon"]),
          *(a["text"] for a in svc.activity()["items"]))
    from backoffice.internal import operations, overview

    assert overview([svc]) and operations([svc])["activity"]
    assert svc.accountant_client("hazel-tree") and svc.accountant_client("company-c")


def money_in(o: Orchestrator, company: str, start: date, end: date):  # type: ignore[no-untyped-def]
    return Ledger(BackOfficeService(o)).money(start, end, direction="in", company_ids=[company])


@pytest.fixture
def demo() -> Orchestrator:
    o = build_demo()
    o.repo.add_supplier(Supplier(id="sup-lima", tenant_id=o.repo.tenant_id, name="Marcenaria Lima",
                                 aliases=["MARCENARIA LIMA"], tax_id=LIMA_NIF, countries=["PT"],
                                 contact_email="faturas@marcenarialima.pt"))
    return o


@pytest.fixture
def svc(demo: Orchestrator) -> BackOfficeService:
    return BackOfficeService(demo)


# --------------------------------------------------------------------------- evidence


def wedding_invoice(*, number: str = "FT CC2026/41", total: str = "5000.00", net: str = "4065.04",
                    vat: str = "934.96", extra: tuple[str, ...] | None = None) -> bytes:
    """Company C's final invoice for the Silva wedding: the whole event, the deposit taken off, the rest to pay."""
    lines = extra if extra is not None else ("Casamento Silva - 19 de setembro",
                                             "Sinal recebido em 02/08/2026: -1.500,00 €",
                                             "Total a pagar: 3.500,00 €")
    return qr_document("FT", number, f"CCQ7K2MP-{number.rsplit('/', 1)[1]}", total, net, vat, "2026-09-20",
                       title="Fatura", issuer=C_NIF, issuer_name="Company C Studio, Unipessoal Lda.",
                       buyer=MARIA_NIF, buyer_line="Cliente: Maria Silva", extra=lines)


def hazel_invoice(number: str, total: str, net: str, vat: str, day: str, *, title: str = "Fatura",
                  buyer: str = CUSTOMER_NIF, buyer_line: str = "Cliente: Atelier Lume, Lda.",
                  extra: tuple[str, ...] = ()) -> bytes:
    seq = number.rsplit("/", 1)[1]
    return qr_document("FT", number, f"HTQ7K2MP-{seq}", total, net, vat, day, title=title, issuer=E.HAZEL_NIF,
                       issuer_name="Hazel Tree Interiores, Lda.", buyer=buyer, buyer_line=buyer_line, extra=extra)


def lima_invoice(number: str, total: str, net: str, vat: str, day: str, extra: tuple[str, ...] = ()) -> bytes:
    seq = number.rsplit("/", 1)[1]
    return qr_document("FT", number, f"LMQ7K2MP-{seq}", total, net, vat, day, title="Fatura", issuer=LIMA_NIF,
                       issuer_name="Marcenaria Lima, Lda.", extra=("Móveis por medida - Atelier Lume", *extra))


# =========================================================================== 1. deposits (cases 29, 30, 31)


def test_deposit_then_final_invoice_then_balance_closes_the_wedding(demo: Orchestrator,
                                                                    svc: BackOfficeService) -> None:
    repo = demo.repo
    wedding = svc.cost_center_create("company-c", {"name": "Casamento Silva", "kind": "Event",
                                                   "identifiers": {"keywords": ["CASAMENTO SILVA"]}})
    center = wedding["costCenter"]["id"]

    # August: the deposit that books the date. A deposit, for that customer and that event, not income yet.
    deposit = pay(demo, "cc-0802", "mbcp-cc", date(2026, 8, 2), "1500.00", "MARIA SILVA",
                  "TRF SINAL CASAMENTO SILVA ORC 2026/14", MARIA_IBAN)
    held = repo.deposits[deposit.id]
    assert (held.direction, held.party, held.amount, held.status, held.reference) == (
        "in", "Maria Silva", Decimal("1500.00"), "held", "ORC 2026/14")
    assert held.why == "The bank line says it is a deposit for ORC 2026/14."
    assert deposit.tx.cost_allocation is not None and deposit.tx.cost_allocation.cost_center_ids == (center,)
    assert stage(demo, deposit) is Stage.UNDERSTOOD and not deposit.document_ids  # never closed without its invoice
    assert demo.missing.plan(deposit) == (
        "Maria Silva paid a €1,500.00 deposit on 2 August for ORC 2026/14. I keep it as a deposit, not as income "
        "yet: your invoice for the work will take it off. Make it where you make your invoices and send it to me.")
    detail = svc.transaction(deposit.id)
    assert detail["deposit"] == {"status": "held", "text": "Deposit of €1,500.00 from Maria Silva on 2 August for "
                                 "Event Casamento Silva. It is kept as a deposit until your invoice takes it off."}
    august = svc.ask("How much did Company C receive in August?")["answer"]
    assert august.startswith("€1,500.00 came in for Company C in August: Maria Silva on 2 August.")
    assert "It is a deposit for work not invoiced yet." in august
    assert not [n for n in repo.open_needs() if n.subject_id == deposit.id]  # nothing to ask: it waits for its invoice

    # September: the final invoice says it takes the deposit off; the rest is what is due.
    final = document(demo, wedding_invoice(), "FT_CC2026_41.txt")
    assert final.sales and final.document.quality is Quality.GREEN and final.customer == "Maria Silva"
    assert final.part_paid == {deposit.id: Decimal("1500.00")} and held.status == "applied"
    assert held.applied_to == final.id and deposit.document_ids == [final.id]
    assert stage(demo, deposit) is Stage.CLOSED  # the deposit is proven by the invoice that takes it off
    assert repo.items[deposit.item_id].history[-1].note == "Deposit taken off invoice FT CC2026/41."
    assert "Invoice FT CC2026/41 takes off: €1,500.00" in deposit.match_why
    assert stage(demo, final) is Stage.UNDERSTOOD
    assert final.hold_reason == "€1,500.00 of €5,000.00 received for invoice FT CC2026/41. €3,500.00 is still to come."
    assert final.hold_reason in [r["text"] for r in svc.month("company-c", "2026-09")["remaining"]]

    # The balance, quoting the invoice, from the same customer: the invoice is paid in full.
    balance = pay(demo, "cc-0922", "mbcp-cc", date(2026, 9, 22), "3500.00", "MARIA SILVA", "TRF FT CC2026/41 SALDO",
                  MARIA_IBAN)
    assert balance.document_ids == [final.id] and stage(demo, balance) is Stage.CLOSED
    assert balance.match_headline == "The rest of invoice FT CC2026/41. It is paid in full."
    assert "Customer: Maria Silva, as on the invoice" in balance.match_why
    assert stage(demo, final) is Stage.CLOSED and final.hold_reason == ""
    closing = repo.items[final.item_id].history[-1]
    assert closing.note == "Paid in full: the €1,500.00 deposit of 2 August and €3,500.00 on 22 September."
    assert {deposit.evidence_id, balance.evidence_id} <= set(closing.evidence_ids)
    assert set(final.evidence_ids) <= set(closing.evidence_ids)

    # The owner sees the whole story, and the event received the whole price once.
    story = svc.document(final.id)
    assert [s["step"] for s in story["parts"]] == ["deposit", "invoice", "payment"]
    assert [s["label"] for s in story["parts"]] == [
        "Deposit of €1,500.00 from Maria Silva on 2 August", "Invoice FT CC2026/41",
        "Maria Silva paid €3,500.00 on 22 September"]
    assert story["received"] == {"received": 5000, "total": 5000, "stillToCome": 0, "text": ""}
    assert svc.transaction(balance.id)["parts"] == story["parts"]
    assert svc.cost_center(center)["received"] == 5000
    assert svc.transaction(deposit.id)["deposit"]["text"] == ("Deposit of €1,500.00 from Maria Silva on 2 August for "
                                                              "Event Casamento Silva, taken off invoice FT CC2026/41.")
    plain(*owner_texts(story), *owner_texts(svc.transaction(deposit.id)), *(a.text for a in repo.activity[-8:]))
    golden_rule(demo)


def test_a_balance_that_arrives_before_its_deposit_is_known_waits_for_it_then_closes(
        demo: Orchestrator, svc: BackOfficeService) -> None:
    """Order does not matter: the invoice and its balance first, the deposit's bank line synced late."""
    repo = demo.repo
    final = document(demo, wedding_invoice(), "FT_CC2026_41.txt")
    balance = pay(demo, "cc-0922", "mbcp-cc", date(2026, 9, 22), "3500.00", "MARIA SILVA", "TRF FT CC2026/41 SALDO",
                  MARIA_IBAN)
    assert balance.document_ids == [final.id] and stage(demo, balance) is Stage.CLOSED  # quotes it, same customer
    assert stage(demo, final) is Stage.UNDERSTOOD
    assert final.hold_reason == ("Invoice FT CC2026/41 takes off a €1,500.00 deposit that I can't find in your bank "
                                 "records yet.")
    deposit = pay(demo, "cc-0802", "mbcp-cc", date(2026, 8, 2), "1500.00", "MARIA SILVA",
                  "TRF SINAL CASAMENTO SILVA ORC 2026/14", MARIA_IBAN)
    assert deposit.document_ids == [final.id] and repo.deposits[deposit.id].status == "applied"
    assert stage(demo, deposit) is Stage.CLOSED and stage(demo, final) is Stage.CLOSED
    plain(final.hold_reason or "", *deposit.match_why)
    golden_rule(demo)


def test_wording_alone_never_turns_a_paid_invoice_or_an_invoice_payment_into_a_deposit(demo: Orchestrator) -> None:
    repo = demo.repo
    # "Já pago" describes how the invoice itself was paid: its own payment still matches it, as always.
    paid = document(demo, lima_invoice("FT LM2026/90", "246.00", "200.00", "46.00", "2026-09-10",
                                       extra=("Já pago: 246,00 €", "Total a pagar: 0,00 €")), "FT_LM2026_90.txt")
    assert paid.terms is None
    card = pay(demo, "ht-0910", "mbcp-ht", date(2026, 9, 10), "-246.00", "MARCENARIA LIMA", "TRF MARCENARIA LIMA")
    assert card.document_ids == [paid.id] and stage(demo, card) is Stage.CLOSED and stage(demo, paid) is Stage.CLOSED
    # A payment that quotes an invoice pays that invoice, whatever other words it carries.
    sale = document(demo, hazel_sale(), "FT_HT2026_31.txt")
    rec = pay(demo, "ht-0924", "mbcp-ht", date(2026, 9, 24), "1230.00", "ATELIER LUME LDA",
              "TRF RESERVA FT HT2026/31")
    assert rec.id not in repo.deposits and rec.document_ids == [sale.id] and stage(demo, rec) is Stage.CLOSED
    line = next(x for x in Ledger(BackOfficeService(demo)).lines if x.id == rec.id)
    assert line.kind == "income"
    golden_rule(demo)


def advance_and_deposit(o: Orchestrator):  # type: ignore[no-untyped-def]
    """Atelier Lume pays a deposit on a design project; Hazel Tree issues its advance invoice for it."""
    deposit = pay(o, "ht-0901", "mbcp-ht", date(2026, 9, 1), "1500.00", "ATELIER LUME LDA",
                  "TRF ADIANTAMENTO PROJETO LUME")
    advance = document(o, hazel_invoice("FT HT2026/40", "1500.00", "1219.51", "280.49", "2026-09-02",
                                        title="Fatura de adiantamento",
                                        extra=("Adiantamento - projeto de interiores Lume",)), "FT_HT2026_40.txt")
    return deposit, advance


def test_advance_invoice_and_final_invoice_take_the_deposit_off_once(demo: Orchestrator,
                                                                     svc: BackOfficeService) -> None:
    repo = demo.repo
    deposit, advance = advance_and_deposit(demo)
    held = repo.deposits[deposit.id]
    # The deposit and its advance invoice: same customer, same amount, days apart. Both close on each other.
    assert advance.advance and advance.sales and deposit.document_ids == [advance.id]
    assert held.advance_document_id == advance.id and held.status == "held"
    assert deposit.match_headline == "Deposit paid by advance invoice FT HT2026/40."
    assert "Customer: Atelier LUME, as on the invoice" in deposit.match_why
    assert stage(demo, deposit) is Stage.CLOSED and stage(demo, advance) is Stage.CLOSED
    assert svc.transaction(deposit.id)["deposit"]["text"] == (
        "Deposit of €1,500.00 from Atelier LUME on 1 September, on advance invoice FT HT2026/40. The final invoice "
        "will take it off.")

    # The final invoice for the whole project takes the advance invoice off; the balance closes it.
    final = document(demo, hazel_invoice(
        "FT HT2026/41", "5000.00", "4065.04", "934.96", "2026-09-25",
        extra=("Projeto de interiores Lume - conclusão", "Adiantamento (fatura FT HT2026/40): -1.500,00 €",
               "Total a pagar: 3.500,00 €")), "FT_HT2026_41.txt")
    assert final.netted == {advance.id: Decimal("1500.00")} and final.linked_advances == [advance.id]
    assert held.status == "applied" and held.applied_to == final.id
    assert final.hold_reason == "€1,500.00 of €5,000.00 received for invoice FT HT2026/41. €3,500.00 is still to come."
    balance = pay(demo, "ht-0928", "mbcp-ht", date(2026, 9, 28), "3500.00", "ATELIER LUME LDA", "TRF FT HT2026/41")
    assert balance.document_ids == [final.id] and stage(demo, balance) is Stage.CLOSED
    assert stage(demo, final) is Stage.CLOSED
    closing = repo.items[final.item_id].history[-1]
    assert closing.note == "Paid in full: advance invoice FT HT2026/40 (€1,500.00) and €3,500.00 on 28 September."
    assert set(advance.evidence_ids) <= set(closing.evidence_ids) and balance.evidence_id in closing.evidence_ids
    story = ["deposit", "advance_invoice", "invoice", "payment"]
    assert [s["step"] for s in svc.document(final.id)["parts"]] == story
    assert [s["step"] for s in svc.transaction(deposit.id)["parts"]] == story

    # Taken off once: the money in is the project's price, and its VAT is counted once, not on both invoices.
    september = money_in(demo, "hazel-tree", date(2026, 9, 1), date(2026, 9, 30))
    ours = [x for x in september.lines if x.id in (deposit.id, balance.id)]
    assert sum((x.amount for x in ours), Decimal(0)) == Decimal("5000.00") and {x.kind for x in ours} == {"income"}
    vat = Ledger(svc).vat(date(2026, 9, 1), date(2026, 9, 30), ["hazel-tree"])
    by_number = {s["number"]: s for s in vat.sales}
    assert by_number["FT HT2026/40"]["vat"] == Decimal("280.49")
    assert by_number["FT HT2026/41"]["vat"] == Decimal("654.47") and by_number["FT HT2026/41"]["gross"] == Decimal(
        "3500.00")
    assert vat.sales_vat == Decimal("934.96")  # the VAT of the whole project, once
    golden_rule(demo)


def test_a_final_invoice_already_net_of_its_advance_invoice_names_it_and_takes_nothing_off_twice(
        demo: Orchestrator, svc: BackOfficeService) -> None:
    """The Portuguese way: the final invoice's own total already has the advance taken off (total = amount due)."""
    repo = demo.repo
    deposit, advance = advance_and_deposit(demo)
    final = document(demo, hazel_invoice(
        "FT HT2026/41", "3500.00", "2845.53", "654.47", "2026-09-25",
        extra=("Projeto de interiores Lume - conclusão", "Regularização do adiantamento FT HT2026/40: -1.500,00 €",
               "Total a pagar: 3.500,00 €")), "FT_HT2026_41.txt")
    assert final.linked_advances == [advance.id] and final.netted == {}  # named, never taken off a second time
    assert repo.deposits[deposit.id].status == "applied" and repo.deposits[deposit.id].applied_to == final.id
    balance = pay(demo, "ht-0928", "mbcp-ht", date(2026, 9, 28), "3500.00", "ATELIER LUME LDA", "TRF FT HT2026/41")
    assert balance.document_ids == [final.id] and stage(demo, balance) is Stage.CLOSED
    assert stage(demo, final) is Stage.CLOSED
    vat = Ledger(svc).vat(date(2026, 9, 1), date(2026, 9, 30), ["hazel-tree"])
    assert vat.sales_vat == Decimal("934.96")
    golden_rule(demo)


def test_a_booking_deposit_the_invoice_does_not_mention_is_confirmed_with_one_tap(demo: Orchestrator,
                                                                                   svc: BackOfficeService) -> None:
    """Case 29, a photographer: the session invoice does not say it takes the booking deposit off."""
    repo = demo.repo
    deposit = pay(demo, "cc-0905", "mbcp-cc", date(2026, 9, 5), "200.00", "MARTA RAMOS", "SINAL SESSAO FOTOGRAFICA")
    invoice = document(demo, qr_document(
        "FT", "FT CC2026/45", "CCQ7K2MP-45", "800.00", "650.41", "149.59", "2026-09-26", title="Fatura",
        issuer=C_NIF, issuer_name="Company C Studio, Unipessoal Lda.", buyer="234567899",
        buyer_line="Cliente: Marta Ramos", extra=("Sessão fotográfica - casamento Ramos",)), "FT_CC2026_45.txt")
    assert not invoice.part_paid and stage(demo, deposit) is Stage.NEEDS_OWNER
    (item,) = [i for i in svc.needs_you()["items"] if i["id"].endswith("_deposit")]
    assert item["question"] == ("Marta Ramos paid you a €200.00 deposit on 5 September. Is it part of invoice "
                                "FT CC2026/45 (€800.00)?")
    assert "Until you answer, I keep it as a deposit." in item["why"]
    assert demo.missing.plan(deposit) == ("Marta Ramos paid a €200.00 deposit on 5 September. I asked you which "
                                          "invoice it is part of.")
    assert svc.answer(item["id"], f"part:{invoice.id}")["message"] == (
        "Done. I counted it as part of invoice FT CC2026/45. €600.00 is still to come.")
    assert stage(demo, deposit) is Stage.CLOSED and repo.deposits[deposit.id].applied_to == invoice.id
    # The rest arrives without the invoice's number: one more tap, never a guess.
    rest = pay(demo, "cc-0930", "mbcp-cc", date(2026, 9, 30), "600.00", "MARTA RAMOS", "TRF MARTA RAMOS")
    (item,) = [i for i in svc.needs_you()["items"] if i["id"].endswith("_part")]
    assert item["question"] == "Marta Ramos paid €600.00 on 30 September. Is it the rest of invoice FT CC2026/45?"
    assert svc.answer(item["id"], f"part:{invoice.id}")["message"] == "Done. Invoice FT CC2026/45 is paid in full."
    assert stage(demo, rest) is Stage.CLOSED and stage(demo, invoice) is Stage.CLOSED
    closing = repo.items[invoice.item_id].history[-1]
    assert {deposit.part_answer_ev, rest.part_answer_ev} <= set(closing.evidence_ids)  # both taps are its proof
    golden_rule(demo)


def test_a_law_firm_retainer_is_taken_off_the_fee_invoice(demo: Orchestrator, svc: BackOfficeService) -> None:
    """Case 21: a client's retainer ("provisão de fundos") is held until the fee invoice takes it off."""
    repo = demo.repo
    retainer = pay(demo, "b-0905", "cgd-b", date(2026, 9, 5), "500.00", "RUI MENDES",
                   "TRF PROVISAO DE FUNDOS PROCESSO 12")
    assert repo.deposits[retainer.id].why == "The bank line says it is a deposit."
    assert "deposit" in svc.ask("How much did Company B receive in September?")["answer"]
    fees = document(demo, qr_document(
        "FT", "FT B2026/7", "BBQ7K2MP-7", "1230.00", "1000.00", "230.00", "2026-09-25", title="Fatura",
        issuer="514987650", issuer_name="Company B, Lda.", buyer="218765436", buyer_line="Cliente: Rui Mendes",
        extra=("Honorários - processo 12", "Provisão de fundos recebida: -500,00 €", "Total a pagar: 730,00 €")),
        "FT_B2026_7.txt")
    assert fees.part_paid == {retainer.id: Decimal("500.00")} and stage(demo, retainer) is Stage.CLOSED
    rest = pay(demo, "b-0930", "cgd-b", date(2026, 9, 30), "730.00", "RUI MENDES", "TRF FT B2026/7")
    assert stage(demo, rest) is Stage.CLOSED and stage(demo, fees) is Stage.CLOSED
    assert repo.items[fees.item_id].history[-1].note == (
        "Paid in full: the €500.00 deposit of 5 September and €730.00 on 30 September.")
    golden_rule(demo)


# =========================================================================== 2. milestones, instalments, parts


def test_milestone_payments_that_add_up_exactly_close_the_invoice(demo: Orchestrator, svc: BackOfficeService) -> None:
    """Recruitment placement fee (case 27): three milestones, each quoting the invoice, add up to it exactly."""
    repo = demo.repo
    sale = document(demo, hazel_sale(), "FT_HT2026_31.txt")
    demo.ingest_bank([
        bank_in("ht-0915", "mbcp-ht", date(2026, 9, 15), "369.00", "ATELIER LUME LDA", "TRF FT HT2026/31 MARCO 1"),
        bank_in("ht-0922", "mbcp-ht", date(2026, 9, 22), "369.00", "ATELIER LUME LDA", "TRF FT HT2026/31 MARCO 2"),
        bank_in("ht-0929", "mbcp-ht", date(2026, 9, 29), "492.00", "ATELIER LUME LDA", "TRF FT HT2026/31 MARCO 3"),
    ])
    parts = [tx(demo, f"TRF FT HT2026/31 MARCO {n}") for n in (1, 2, 3)]
    assert all(p.document_ids == [sale.id] and stage(demo, p) is Stage.CLOSED for p in parts)
    assert sale.part_paid == {p.id: abs(p.tx.amount) for p in parts}
    assert parts[0].match_headline == "3 payments cover invoice FT HT2026/31."
    assert "Money received: €1,230.00 (3 payments)" in parts[0].match_why
    assert stage(demo, sale) is Stage.CLOSED
    closing = repo.items[sale.item_id].history[-1]
    assert closing.note == ("Paid in full: €369.00 on 15 September, €369.00 on 22 September and €492.00 on "
                            "29 September.")
    assert {p.evidence_id for p in parts} <= set(closing.evidence_ids)  # each payment's own evidence
    assert [s["step"] for s in svc.document(sale.id)["parts"]] == ["invoice", "payment", "payment", "payment"]
    plain(closing.note, *parts[0].match_why)
    golden_rule(demo)


def test_a_part_payment_stays_open_with_a_plain_balance(demo: Orchestrator, svc: BackOfficeService) -> None:
    repo = demo.repo
    sale = document(demo, hazel_sale(), "FT_HT2026_31.txt")
    first = pay(demo, "ht-0916", "mbcp-ht", date(2026, 9, 16), "500.00", "ATELIER LUME LDA", "TRF FT HT2026/31 1/2")
    # The payment is proven (it quotes the invoice and comes from its customer): it closes as a part.
    assert first.document_ids == [sale.id] and stage(demo, first) is Stage.CLOSED
    assert first.match_headline == "Part of invoice FT HT2026/31. €730.00 is still to come."
    assert "Received so far: €500.00 of €1,230.00" in first.match_why and "Still to come: €730.00" in first.match_why
    # The invoice stays open with a plain balance, never a missing payment and never closed early.
    assert stage(demo, sale) is Stage.UNDERSTOOD
    assert sale.hold_reason == "€500.00 of €1,230.00 received for invoice FT HT2026/31. €730.00 is still to come."
    assert sale.hold_reason in [r["text"] for r in svc.month("hazel-tree", "2026-09")["remaining"]]
    detail = svc.document(sale.id)
    assert detail["received"] == {"received": 500, "total": 1230, "stillToCome": 730, "text": sale.hold_reason}
    assert detail["waiting"] == sale.hold_reason and detail["payments"][0]["transactionId"] == first.id
    assert not demo.month_status("hazel-tree", Month(2026, 9)).closed
    assert not [n for n in repo.open_needs() if n.subject_id in (first.id, sale.id)]
    plain(sale.hold_reason, first.match_headline, *first.match_why)
    golden_rule(demo)


def test_instalments_by_reference_close_in_the_pipeline(demo: Orchestrator) -> None:
    """Architecture milestones (case 14): the instalments arrive in separate bank syncs, each quoting the invoice."""
    repo = demo.repo
    sale = document(demo, hazel_sale(), "FT_HT2026_31.txt")
    first = pay(demo, "ht-0916", "mbcp-ht", date(2026, 9, 16), "500.00", "ATELIER LUME LDA", "TRF FT HT2026/31 1/2")
    demo.run(at(10, 2, 11))
    assert stage(demo, first) is Stage.CLOSED and stage(demo, sale) is Stage.UNDERSTOOD
    assert demo.auditor.recheck() == []  # a part is never taken for a payment of the whole invoice
    second = pay(demo, "ht-0924", "mbcp-ht", date(2026, 9, 24), "730.00", "ATELIER LUME LDA", "TRF FT HT2026/31 2/2")
    assert second.document_ids == [sale.id] and stage(demo, second) is Stage.CLOSED
    assert second.match_headline == "The rest of invoice FT HT2026/31. It is paid in full."
    assert "Still to pay: €730.00" in second.match_why  # what was left when it arrived
    assert stage(demo, sale) is Stage.CLOSED and sale.hold_reason == ""
    closing = repo.items[sale.item_id].history[-1]
    assert closing.note == "Paid in full: €500.00 on 16 September and €730.00 on 24 September."
    assert {first.evidence_id, second.evidence_id} <= set(closing.evidence_ids)
    assert demo.auditor.recheck() == []
    golden_rule(demo)


def test_a_part_payment_without_the_reference_is_one_question_then_the_owners_tap(demo: Orchestrator,
                                                                                  svc: BackOfficeService) -> None:
    repo = demo.repo
    sale = document(demo, hazel_sale(), "FT_HT2026_31.txt")
    paid = pay(demo, "ht-0916", "mbcp-ht", date(2026, 9, 16), "500.00", "ATELIER LUME LDA", "TRF ATELIER LUME")
    assert not paid.document_ids and stage(demo, paid) is Stage.NEEDS_OWNER  # never linked on a guess
    (item,) = [i for i in svc.needs_you()["items"] if i["id"].endswith("_part")]
    assert item["question"] == "Atelier LUME paid €500.00 on 16 September. Is it part of invoice FT HT2026/31?"
    assert [o["label"] for o in item["options"]] == ["Yes, part of invoice FT HT2026/31", "No, it is for something else"]
    assert "Invoice FT HT2026/31 is for €1,230.00." in item["why"]
    assert demo.missing.plan(paid) == "Atelier LUME paid €500.00 on 16 September. I asked you what it is for."
    assert "Atelier LUME paid €500.00" in svc.ask("What still needs my attention?")["answer"]
    plain(item["question"], *item["why"], *(o["label"] for o in item["options"]))

    out = svc.answer(item["id"], f"part:{sale.id}")
    assert out["message"] == "Done. I counted it as part of invoice FT HT2026/31. €730.00 is still to come."
    assert paid.document_ids == [sale.id] and stage(demo, paid) is Stage.CLOSED
    assert paid.part_answer_ev in repo.items[paid.item_id].history[-1].evidence_ids  # the owner's tap is the proof
    assert sale.hold_reason == "€500.00 of €1,230.00 received for invoice FT HT2026/31. €730.00 is still to come."
    rest = pay(demo, "ht-0924", "mbcp-ht", date(2026, 9, 24), "730.00", "ATELIER LUME LDA", "TRF FT HT2026/31 2/2")
    assert stage(demo, rest) is Stage.CLOSED and stage(demo, sale) is Stage.CLOSED
    assert paid.part_answer_ev in repo.items[sale.item_id].history[-1].evidence_ids
    golden_rule(demo)


def test_a_payment_that_likely_pays_another_invoice_in_full_is_not_asked_about_as_a_part(
        demo: Orchestrator, svc: BackOfficeService) -> None:
    document(demo, hazel_sale(), "FT_HT2026_31.txt")  # €1,230.00
    small = document(demo, hazel_invoice("FT HT2026/32", "500.00", "406.50", "93.50", "2026-09-15"), "FT_32.txt")
    paid = pay(demo, "ht-0916", "mbcp-ht", date(2026, 9, 16), "500.00", "ATELIER LUME LDA", "TRF ATELIER LUME")
    assert paid.likely_document_ids == [small.id]  # the same amount as the smaller invoice: that one is confirmed
    assert not [i for i in svc.needs_you()["items"] if i["id"].endswith("_part")]


# =========================================================================== 3. held back (construction, case 11)


def stage_invoice(until: str = "15/10/2026") -> bytes:
    return hazel_invoice("FT HT2026/50", "10000.00", "8130.08", "1869.92", "2026-09-10", buyer=BUILDER_NIF,
                         buyer_line="Cliente: Construtora Sul, S.A.",
                         extra=("Auto de medição n.º 3 - remodelação da loja",
                                f"Retenção de garantia (5%): 500,00 € - a libertar em {until}",
                                "Total a pagar: 9.500,00 €"))


def test_retention_is_held_back_until_its_release_then_paid(demo: Orchestrator, svc: BackOfficeService) -> None:
    repo = demo.repo
    invoice = document(demo, stage_invoice(), "FT_HT2026_50.txt")
    held = repo.retentions[invoice.id]
    assert (held.amount, held.until, held.percent, held.status, held.party) == (
        Decimal("500.00"), date(2026, 10, 15), Decimal("5.00"), "held", "Construtora Sul")
    paid = pay(demo, "ht-0920", "mbcp-ht", date(2026, 9, 20), "9500.00", "CONSTRUTORA SUL SA", "TRF FT HT2026/50")
    # Paid for the part due now: the invoice closes, the part held back stays owed on its own.
    assert paid.document_ids == [invoice.id] and stage(demo, paid) is Stage.CLOSED
    assert paid.match_headline == ("The rest of invoice FT HT2026/50, apart from the €500.00 held back until "
                                   "15 October.")
    assert stage(demo, invoice) is Stage.CLOSED
    assert repo.items[invoice.item_id].history[-1].note == (
        "Paid: €9,500.00 on 20 September. €500.00 held back by the customer until 15 October.")
    assert held.status == "held"
    notice = "Invoice FT HT2026/50: €500.00 held back by the customer until 15 October."
    assert notice in [n["text"] for n in svc.month("hazel-tree", "2026-09").get("notices", [])]
    assert svc.document(invoice.id)["heldBack"] == {"amount": 500, "until": "2026-10-15", "status": "held",
                                                    "text": "€500.00 held back by the customer until 15 October."}
    (due,) = [d for d in svc.home()["dueSoon"] if d["id"] == f"due_held_{invoice.id}"]
    assert due["title"] == "Amount held back by Construtora Sul" and due["due"] == "2026-10-15"
    assert not [r for r in svc.month("hazel-tree", "2026-09")["remaining"] if "Construtora" in r["text"]]
    income = svc.ask("How much did Hazel Tree receive in September?")["answer"]
    assert "Still to come: €500.00 held back by the customer on invoice FT HT2026/50 until 15 October." in income
    assert demo.auditor.recheck() == []

    # The release arrives: the same amount, quoting the invoice. It closes on the invoice's evidence.
    release = pay(demo, "ht-1001", "mbcp-ht", date(2026, 10, 1), "500.00", "CONSTRUTORA SUL SA",
                  "TRF RETENCAO FT HT2026/50")
    assert release.document_ids == [invoice.id] and stage(demo, release) is Stage.CLOSED
    assert held.status == "released" and held.released_tx_ids == [release.id]
    assert release.match_headline == "The €500.00 held back on invoice FT HT2026/50, now paid."
    assert svc.document(invoice.id)["heldBack"]["text"] == "The €500.00 held back was paid."
    assert not [d for d in svc.home()["dueSoon"] if d["id"] == f"due_held_{invoice.id}"]
    assert [s["step"] for s in svc.document(invoice.id)["parts"]] == ["invoice", "payment", "held_back_paid"]
    plain(notice, due["note"], due["title"], release.match_headline, *release.match_why)
    golden_rule(demo)


def test_a_customer_who_pays_the_whole_invoice_holds_nothing_back(demo: Orchestrator, svc: BackOfficeService) -> None:
    repo = demo.repo
    invoice = document(demo, stage_invoice(), "FT_HT2026_50.txt")
    paid = pay(demo, "ht-0920", "mbcp-ht", date(2026, 9, 20), "10000.00", "CONSTRUTORA SUL SA", "TRF FT HT2026/50")
    assert paid.document_ids == [invoice.id] and stage(demo, paid) is Stage.CLOSED
    assert paid.match_headline == "The rest of invoice FT HT2026/50, the €500.00 held back included. It is paid in full."
    assert repo.retentions[invoice.id].status == "released" and stage(demo, invoice) is Stage.CLOSED
    assert repo.items[invoice.item_id].history[-1].note == "Paid in full: €10,000.00 on 20 September."
    assert not [n for n in svc.month("hazel-tree", "2026-09").get("notices", []) if "held back" in n["text"]]
    golden_rule(demo)


def test_an_amount_held_back_that_arrives_without_its_reference_is_asked_about(demo: Orchestrator,
                                                                               svc: BackOfficeService) -> None:
    repo = demo.repo
    invoice = document(demo, stage_invoice("30/06/2027"), "FT_HT2026_50.txt")
    pay(demo, "ht-0920", "mbcp-ht", date(2026, 9, 20), "9500.00", "CONSTRUTORA SUL SA", "TRF FT HT2026/50")
    assert svc.document(invoice.id)["heldBack"]["text"] == "€500.00 held back by the customer until 30 June 2027."
    release = pay(demo, "ht-1001", "mbcp-ht", date(2026, 10, 1), "500.00", "CONSTRUTORA SUL SA", "TRF CONSTRUTORA")
    assert not release.document_ids and stage(demo, release) is Stage.NEEDS_OWNER
    (item,) = [i for i in svc.needs_you()["items"] if i["id"].endswith("_part")]
    assert item["question"] == ("Construtora SUL paid €500.00 on 1 October. Is it the €500.00 held back on invoice "
                                "FT HT2026/50?")
    svc.answer(item["id"], f"held:{invoice.id}")
    assert stage(demo, release) is Stage.CLOSED and repo.retentions[invoice.id].status == "released"
    golden_rule(demo)


# =========================================================================== 4. supplier deposits (I4)


def test_a_supplier_deposit_is_taken_off_its_final_invoice(demo: Orchestrator, svc: BackOfficeService) -> None:
    repo = demo.repo
    deposit = pay(demo, "ht-0820", "mbcp-ht", date(2026, 8, 20), "-300.00", "MARCENARIA LIMA",
                  "TRF ADIANTAMENTO ORC 2026/77")
    held = repo.deposits[deposit.id]
    assert (held.direction, held.party, held.status) == ("out", "Marcenaria Lima", "held")
    assert svc.transaction(deposit.id)["deposit"]["text"] == (
        "Deposit of €300.00 paid to Marcenaria Lima on 20 August. It is kept as a deposit until its invoice takes it "
        "off.")
    final = document(demo, lima_invoice("FT LM2026/88", "900.00", "731.71", "168.29", "2026-09-25",
                                        extra=("Adiantamento pago em 20/08/2026: -300,00 €",
                                               "Total a pagar: 600,00 €")), "FT_LM2026_88.txt")
    assert final.part_paid == {deposit.id: Decimal("300.00")} and held.status == "applied"
    assert stage(demo, deposit) is Stage.CLOSED
    assert deposit.match_headline == "Deposit taken off the Marcenaria Lima invoice FT LM2026/88."
    assert final.hold_reason == ("€300.00 of €900.00 paid on the Marcenaria Lima invoice FT LM2026/88. €600.00 is "
                                 "still to pay.")
    rest = pay(demo, "ht-0928", "mbcp-ht", date(2026, 9, 28), "-600.00", "MARCENARIA LIMA", "TRF FT LM2026/88")
    assert rest.document_ids == [final.id] and stage(demo, rest) is Stage.CLOSED
    assert stage(demo, final) is Stage.CLOSED
    assert repo.items[final.item_id].history[-1].note == (
        "Paid in full: the €300.00 deposit of 20 August and €600.00 on 28 September.")
    golden_rule(demo)


def test_a_supplier_deposit_the_invoice_does_not_mention_is_taken_off_on_the_owners_tap(
        demo: Orchestrator, svc: BackOfficeService) -> None:
    repo = demo.repo
    deposit = pay(demo, "ht-0820", "mbcp-ht", date(2026, 8, 20), "-300.00", "MARCENARIA LIMA",
                  "TRF ADIANTAMENTO ORC 2026/77")
    final = document(demo, lima_invoice("FT LM2026/89", "900.00", "731.71", "168.29", "2026-09-25"),
                     "FT_LM2026_89.txt")
    assert not final.part_paid and stage(demo, deposit) is Stage.NEEDS_OWNER  # asked, not guessed
    (item,) = [i for i in svc.needs_you()["items"] if i["id"].endswith("_deposit")]
    assert item["question"] == ("You paid Marcenaria Lima a €300.00 deposit on 20 August. Is it part of the "
                                "Marcenaria Lima invoice FT LM2026/89 (€900.00)?")
    assert [o["label"] for o in item["options"]] == [
        "Yes, take it off the Marcenaria Lima invoice FT LM2026/89", "No, it is for something else"]
    plain(item["question"], *item["why"], *(o["label"] for o in item["options"]))
    out = svc.answer(item["id"], f"part:{final.id}")
    assert out["message"] == ("Done. I counted it as part of the Marcenaria Lima invoice FT LM2026/89. €600.00 is "
                              "still to pay.")
    assert stage(demo, deposit) is Stage.CLOSED and repo.deposits[deposit.id].applied_to == final.id
    assert deposit.part_answer_ev in repo.items[deposit.item_id].history[-1].evidence_ids
    rest = pay(demo, "ht-0928", "mbcp-ht", date(2026, 9, 28), "-600.00", "MARCENARIA LIMA", "TRF FT LM2026/89")
    assert stage(demo, rest) is Stage.CLOSED and stage(demo, final) is Stage.CLOSED
    golden_rule(demo)


# =========================================================================== 5. a cancelled booking (case 31)


def test_a_cancelled_booking_deposit_is_given_back_and_linked(demo: Orchestrator, svc: BackOfficeService) -> None:
    repo = demo.repo
    deposit = pay(demo, "cc-0715", "mbcp-cc", date(2026, 7, 15), "1000.00", "JOANA COSTA",
                  "TRF RESERVA QUINTA 031 CASAMENTO", JOANA_IBAN)
    assert repo.deposits[deposit.id].status == "held"
    back = pay(demo, "cc-0910", "mbcp-cc", date(2026, 9, 10), "-1000.00", "JOANA COSTA",
               "DEVOLUCAO RESERVA QUINTA 031", JOANA_IBAN)
    # Money back to the account the deposit came from, the whole deposit: linked, both close, no invoice needed.
    assert back.decision is not None and back.decision.rule == "deposit_refund"
    assert back.decision.reason == "Money back to Joana Costa, who paid you a deposit."
    assert back.deposit_refund_of == deposit.id and repo.deposits[deposit.id].status == "refunded"
    assert stage(demo, back) is Stage.CLOSED and stage(demo, deposit) is Stage.CLOSED
    assert repo.items[back.item_id].history[-1].note == "Refund of the €1,000.00 deposit Joana Costa paid on 15 July."
    assert repo.items[deposit.item_id].history[-1].note == "Deposit given back in full on 10 September."
    assert deposit.evidence_id in repo.items[back.item_id].history[-1].evidence_ids
    assert back.evidence_id in repo.items[deposit.item_id].history[-1].evidence_ids
    assert back.id not in repo.chases  # nothing to chase: nothing was sold
    detail = svc.transaction(back.id)
    assert [s["step"] for s in detail["parts"]] == ["deposit", "deposit_refund"]
    assert detail["parts"][1]["label"] == "Given back to Joana Costa: €1,000.00 on 10 September"
    assert svc.transaction(deposit.id)["deposit"]["text"] == (
        "Deposit of €1,000.00 from Joana Costa on 15 July, given back in full.")
    matched = {m["id"]: m for m in svc.month("company-c", "2026-09")["matched"]}
    assert matched[f"m_{back.id}"]["description"] == "Deposit given back"
    # Neither income nor a cost, and said so.
    july = svc.ask("How much did Company C receive in July?")["answer"]
    assert july.startswith("Nothing came in from customers for Company C in July, and no refunds.")
    assert "I left out the €1,000.00 deposit from Joana Costa: it went back to them." in july
    spent = svc.ask("How much did Company C spend in September?")["answer"]
    assert "I left out the €1,000.00 paid back to Joana Costa: it gave back their deposit." in spent
    assert back.id not in {x.id for x in Ledger(svc).money(date(2026, 9, 1), date(2026, 9, 30),
                                                           company_ids=["company-c"]).lines}
    plain(july, spent, *back.match_why, back.match_headline)
    golden_rule(demo)


def test_money_back_to_a_depositor_from_another_account_is_asked_about(demo: Orchestrator,
                                                                      svc: BackOfficeService) -> None:
    repo = demo.repo
    deposit = pay(demo, "cc-0715", "mbcp-cc", date(2026, 7, 15), "1000.00", "JOANA COSTA",
                  "TRF RESERVA QUINTA 031 CASAMENTO", JOANA_IBAN)
    back = pay(demo, "cc-0910", "mbcp-cc", date(2026, 9, 10), "-1000.00", "JOANA COSTA", "DEVOLUCAO RESERVA")
    assert back.decision is not None and back.decision.rule == "deposit_refund" and back.decision.quality is Quality.AMBER
    assert stage(demo, back) is Stage.NEEDS_OWNER and not back.deposit_refund_of
    (item,) = [i for i in svc.needs_you()["items"] if i["id"].endswith("_deposit_refund")]
    assert item["question"] == ("You paid Joana Costa €1,000.00 on 10 September. Does it give back the €1,000.00 "
                                "deposit they paid on 15 July?")
    assert "It did not go back to the bank account the deposit came from, so I won't link them on a guess." in \
        item["why"]
    assert demo.missing.plan(back) == ("The €1,000.00 paid to Joana Costa on 10 September may give back their deposit. "
                                       "I asked you about it.")
    plain(item["question"], *item["why"], *(o["label"] for o in item["options"]))
    out = svc.answer(item["id"], f"refund:{deposit.id}")
    assert out["message"] == "Done. I linked it to the €1,000.00 deposit of 15 July."
    assert stage(demo, back) is Stage.CLOSED and stage(demo, deposit) is Stage.CLOSED
    assert back.part_answer_ev in repo.items[back.item_id].history[-1].evidence_ids
    golden_rule(demo)


# =========================================================================== income answers


def test_income_answers_never_count_a_deposit_twice(demo: Orchestrator, svc: BackOfficeService) -> None:
    start, end = date(2026, 8, 1), date(2026, 9, 30)
    before = money_in(demo, "company-c", start, end)
    deposit = pay(demo, "cc-0802", "mbcp-cc", date(2026, 8, 2), "1500.00", "MARIA SILVA",
                  "TRF SINAL CASAMENTO SILVA ORC 2026/14", MARIA_IBAN)
    waiting = money_in(demo, "company-c", start, end)
    assert waiting.total - before.total == Decimal("1500.00") and waiting.by_kind["deposit"] == Decimal("1500.00")
    card = svc.chat({"message": "How much did Company C receive in August?"})["cards"][0]
    assert card["total"] == float(waiting.by_kind["deposit"]) and "It is a deposit for work not invoiced yet." in \
        card["notes"]

    document(demo, wedding_invoice(), "FT_CC2026_41.txt")
    pay(demo, "cc-0922", "mbcp-cc", date(2026, 9, 22), "3500.00", "MARIA SILVA", "TRF FT CC2026/41 SALDO", MARIA_IBAN)
    after = money_in(demo, "company-c", start, end)
    # The deposit is now part of the invoice's income: deposit + balance = the invoice, counted once.
    assert after.total - before.total == Decimal("5000.00")
    assert "deposit" not in after.by_kind
    ours = [x for x in after.lines if x.id.startswith(deposit.id) or x.merchant == "Maria Silva"]
    assert sorted(x.amount for x in ours) == [Decimal("1500.00"), Decimal("3500.00")]
    september = svc.ask("How much did Company C receive in September?")["answer"]
    assert september.startswith("€3,500.00 came in for Company C in September: Maria Silva on 22 September.")
    assert "deposit" not in september
    since = svc.ask("How much did Company C receive since August?")["answer"]
    assert since.startswith(f"€{after.total:,.2f} came in for Company C since August")
    status, body = svc.dispatch("POST", "/api/chat/tool", {"name": "spending_summary", "input": {
        "date_from": "2026-08-01", "date_to": "2026-09-30", "direction": "in", "company_id": "company-c"}})
    assert status == 200, body
    assert body["result"]["total_eur"] == float(after.total)
    assert "deposits_for_work_not_invoiced_yet" not in body["result"]["included"]
    plain(september, since)


# =========================================================================== reading the words, and the demo


def test_the_words_of_deposits_final_invoices_and_parts_held_back_are_read() -> None:
    assert deposit_wording("MARIA SILVA", "TRF SINAL CASAMENTO ORC 2026/14").reference == "ORC 2026/14"
    assert deposit_wording("ESCRITORIO", "PROVISAO DE FUNDOS PROC 12") is not None  # a law firm's retainer (case 21)
    assert deposit_wording("CLIENTE", "CONTRATO C-2026/07 MARCO 1").reference == "CONTRATO C-2026/07"
    assert deposit_wording("DEPOSITO NUMERARIO") is None  # the owner's own cash at a machine
    assert deposit_wording("HOTEL INFANTE", "RESERVA SILVA ACORES", outgoing=True) is None  # a booking paid in full
    assert deposit_wording("TRF FT HT2026/31") is None
    assert is_advance_invoice("Hazel Tree\nFatura de adiantamento n.º FT HT2026/40\nTotal: 1.500,00 €")
    assert is_advance_invoice("Studio\nAdvance invoice INV-40\nTotal: €1,500.00")
    assert not is_advance_invoice("Fatura n.º FT 1/2\nAdiantamento (fatura de adiantamento FT 1/1): -100,00 €")

    final = read_terms("Total: 5.000,00 €\nSinal recebido em 02/08/2026: -1.500,00 €\nTotal a pagar: 3.500,00 €")
    assert final.deductions[0].amount == Decimal("1500.00") and final.deductions[0].on == date(2026, 8, 2)
    assert final.mode(Decimal("5000.00")) == "includes"
    inside = read_terms("Adiantamento FT HT2026/40: -1.500,00 €\nTotal: 3.500,00 €\nTotal a pagar: 3.500,00 €")
    assert inside.deductions[0].number == "FT HT2026/40" and inside.mode(Decimal("3500.00")) == "inside"
    assert read_terms("Less deposit received: €1,500.00").mode(Decimal("5000.00")) == "unknown"
    assert read_terms("Less deposit received: €1,500.00\nAmount due: €3,000.00").mode(
        Decimal("5000.00")) == "does_not_add_up"

    held = read_terms("Retenção de garantia (5%): 500,00 € - a libertar em 30/06/2027\nTotal a pagar: 9.500,00 €\n"
                      "Retenção na fonte IRS (25%): 100,00 €")  # withholding tax is never a part held back
    assert held.held_back is not None and held.held_back.amount == Decimal("500.00")
    assert held.held_back.until == date(2027, 6, 30) and held.held_back_for(Decimal("10000.00")) == held.held_back
    english = read_terms("Retention 5%: €500.00\nreleased on 30 June 2027\nAmount due: €9,500.00")
    assert english.held_back is not None and english.held_back.until == date(2027, 6, 30)
    assert read_terms("Retention 5%: €500.00\nAmount due: €9,000.00").held_back_for(Decimal("10000.00")) is None
    assert read_terms("Extensão de garantia: 49,00 €").held_back is None  # a warranty sold is not money held back
    assert customer_name("Hazel Tree\nCliente: Atelier Lume, Lda.\nNIF: 512345678") == "Atelier Lume, Lda."

    # The owner reads "held back by the customer" and "still to come", never the accounting words.
    assert find_jargon("Retention receivable") == ["retention", "receivable"]
    assert find_jargon("€500.00 held back by the customer until 30 June 2027.") == []


def test_the_demo_is_unchanged() -> None:
    o = build_demo()
    svc = BackOfficeService(o)
    assert o.repo.deposits == {} and o.repo.retentions == {}
    assert not any(o.staged.is_staged(d) or d.advance or d.terms is not None for d in o.repo.documents.values())
    assert [n.id for n in o.repo.open_needs()] == ["nd_ikea_418", "nd_vodafone_iban"]
    assert o.month_status("company-b", Month(2026, 9)).closed and not o.month_status("hazel-tree", Month(2026, 9)).closed
    staged_actions = {"read_terms", "record_deposit", "keep_parts", "take_off_deposit", "take_off_advance",
                      "release_held_back", "give_back_deposit"}
    assert not [r for r in o.repo.audit_store.records(o.repo.tenant_id) if r.action in staged_actions]
    assert not any(t.to_stage is Stage.NEEDS_OWNER and t.actor == "system:reconciliation"
                   for i in o.repo.items.values() for t in i.history)
    for question in ("How much came in in September?", "How much did we spend in September?"):
        answer = svc.ask(question)["answer"]
        assert "deposit" not in answer and "held back" not in answer
    for company in o.repo.companies:
        view = svc.month(company, "2026-09") or {}
        assert not [n for n in view.get("notices", []) if "held back" in n["text"]]
    assert not [d for d in svc.home()["dueSoon"] if d["id"].startswith("due_held_")]
    assert o.auditor.recheck() == []
