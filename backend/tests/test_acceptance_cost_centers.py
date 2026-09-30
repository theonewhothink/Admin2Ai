"""Acceptance: cost centers (jobs, properties, vehicles, outlets, events, courses, clients).

Businesses that work by job, property, vehicle or client need every cost on
the right one: construction and plumbing jobs, architecture projects,
buildings and apartments, agency clients, reseller customers, events, vans,
cleaning sites, courses, outlets, winery activities. The engine decides with a
reason for "Why?" (a rule the owner taught, an identifier on the evidence, the
card it was paid with, a consistent supplier history), splits a shared invoice
exactly to the cent, and otherwise asks one plain question with one-tap
learning. A company without cost centers is never asked anything.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from backoffice.countries.pt.nif import validate_nif
from backoffice.demo.evidence import qr_payload
from backoffice.domain.cost_centers import SplitError, split_by_amounts, split_by_percent, split_by_weights
from backoffice.domain.models import (
    AllocationMethod,
    AllocationShare,
    CostAllocation,
    Quality,
    TransactionKind,
    VatPart,
)
from backoffice.language import find_jargon, find_off_tone
from backoffice.orchestrator import TZ, BankRow
from backoffice.service import BackOfficeService

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=TZ)
COMPANY_A = "516123459"
COMPANY_B = "501234560"


def nif(prefix8: str) -> str:
    """A Portuguese tax number with a valid check digit."""
    total = sum(int(d) * w for d, w in zip(prefix8, range(9, 1, -1), strict=True))
    check = 11 - total % 11
    number = prefix8 + str(0 if check >= 10 else check)
    assert validate_nif(number).valid, number
    return number


LEROY = nif("50328047")
GALP = nif("50494212")
EDP = nif("50300000")
DROGARIA = nif("51234567")
MICROSOFT = nif("98000012")


# --------------------------------------------------------------------------- a business, payments, invoices


def ok(result: tuple[int, dict[str, Any]], status: int = 200) -> dict[str, Any]:
    code, body = result
    assert code == status, body
    return body


def get(svc: BackOfficeService, path: str) -> dict[str, Any]:
    return ok(svc.dispatch("GET", path, None))


def post(svc: BackOfficeService, path: str, body: dict[str, Any], status: int = 200) -> dict[str, Any]:
    return ok(svc.dispatch("POST", path, body), status)


class Business:
    """One owner, one or two companies, a bank account and cards per company."""

    def __init__(self, *, second_company: bool = False) -> None:
        self.svc = BackOfficeService.new_tenant("t-cost", owner_name="Rui Silva", owner_email="rui@obras.pt", now=NOW)
        self.svc.add_company("Obras Silva", COMPANY_A)
        self.a = "obras-silva"
        self.bank = {self.a: post(self.svc, "/api/sources", {"kind": "bank", "bank": "Millennium BCP",
                                                             "companyId": self.a,
                                                             "iban": "PT50000201231234567890154"})["id"]}
        self.cards: dict[str, str] = {}
        if second_company:
            self.svc.add_company("Casa Norte", COMPANY_B)
            self.b = "casa-norte"
            self.bank[self.b] = post(self.svc, "/api/sources", {"kind": "bank", "bank": "Caixa", "companyId": self.b,
                                                                "iban": "PT50003504120005678123007"})["id"]
        for name, tax_id in (("Leroy Merlin", LEROY), ("Galp", GALP), ("EDP Comercial", EDP),
                             ("Drogaria Central", DROGARIA), ("Microsoft", MICROSOFT)):
            post(self.svc, "/api/sources", {"kind": "supplier", "name": name, "taxId": tax_id})

    @property
    def repo(self) -> Any:
        return self.svc.repo

    def card(self, last4: str, company: str | None = None) -> str:
        body = {"kind": "card", "bank": "Millennium BCP", "companyId": company or self.a, "last4": last4}
        self.cards[last4] = post(self.svc, "/api/sources", body)["id"]
        return self.cards[last4]

    def center(self, name: str, kind: str = "Job", company: str | None = None, **identifiers: list[str]) -> str:
        body: dict[str, Any] = {"name": name, "kind": kind}
        if identifiers:
            body["identifiers"] = identifiers
        return post(self.svc, f"/api/companies/{company or self.a}/cost-centers", body)["costCenter"]["id"]

    def pay(self, day: date, amount: str, counterparty: str, *, card: str | None = None, company: str | None = None,
            description: str = "", kind: TransactionKind | None = None) -> str:
        account = self.cards[card] if card else self.bank[company or self.a]
        row = BankRow(bank_id=f"b-{counterparty}-{day}-{amount}", account_id=account, booked_on=day,
                      amount=Decimal(amount), counterparty=counterparty, description=description,
                      kind=kind or (TransactionKind.CARD if card else TransactionKind.TRANSFER_OUT), card_last4=card)
        report = self.svc.orchestrator.ingest_bank([row])
        return report.transaction_ids[0]

    def upload(self, name: str, data: bytes, content_type: str = "text/plain") -> dict[str, Any]:
        return self.svc.upload_evidence(name, content_type, data)

    def tx(self, tx_id: str) -> Any:
        return self.repo.transactions[tx_id]

    def allocation(self, tx_id: str) -> CostAllocation | None:
        return self.repo.transactions[tx_id].tx.cost_allocation

    def needs(self) -> list[dict[str, Any]]:
        return get(self.svc, "/api/needs-you")["items"]

    def question_for(self, tx_id: str) -> dict[str, Any]:
        found = [n for n in self.repo.open_needs() if n.kind == "cost_center" and n.subject_id == tx_id]
        assert len(found) == 1, [n.id for n in self.repo.open_needs()]
        return next(i for i in self.needs() if i["id"] == found[0].id)


def invoice(supplier: str, supplier_nif: str, number: str, day: date, net: str, vat: str, *body: str,
            customer: str = COMPANY_A) -> bytes:
    """A Portuguese invoice's text layer with its fiscal QR code (23% VAT)."""
    gross = Decimal(net) + Decimal(vat)
    seq = number.rsplit("/", 1)[-1]
    qr = qr_payload(A=supplier_nif, B=customer, C="PT", D="FT", E="N", F=day.strftime("%Y%m%d"), G=number,
                    H=f"CSDF7T5H-{seq}", I1="PT", I7=net, I8=vat, N=vat, O=f"{gross:.2f}", Q="e1Dk", R="1422")
    lines = [supplier, f"NIF: {supplier_nif}", f"Fatura n.º {number}", f"ATCUD: CSDF7T5H-{seq}",
             f"Data de emissão: {day:%d/%m/%Y}", f"Data de vencimento: {day:%d/%m/%Y}", "Cliente: Obras Silva, Lda.",
             f"NIF: {customer}", *body, f"Base tributável (23%): {net.replace('.', ',')}",
             f"IVA 23%: {vat.replace('.', ',')}", f"Total: {str(gross).replace('.', ',')} €", f"Código QR: {qr}", ""]
    return "\n".join(lines).encode()


def ubl_invoice(number: str, day: date, lines: list[tuple[str, str]], *, supplier: str = "Microsoft Ireland",
                supplier_nif: str = MICROSOFT, rate: str = "23") -> tuple[bytes, Decimal]:
    """A UBL e-invoice with one line per (note, net amount); returns the XML and its total."""
    net = sum((Decimal(a) for _, a in lines), Decimal(0))
    vat = (net * Decimal(rate) / 100).quantize(Decimal("0.01"))
    gross = net + vat
    body = "".join(
        f"""<cac:InvoiceLine><cbc:ID>{i}</cbc:ID><cbc:Note>{note}</cbc:Note>
        <cbc:InvoicedQuantity unitCode="C62">1</cbc:InvoicedQuantity>
        <cbc:LineExtensionAmount currencyID="EUR">{amount}</cbc:LineExtensionAmount>
        <cac:Item><cbc:Name>Microsoft 365 Business Standard</cbc:Name>
        <cac:ClassifiedTaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>{rate}</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory></cac:Item>
        <cac:Price><cbc:PriceAmount currencyID="EUR">{amount}</cbc:PriceAmount></cac:Price></cac:InvoiceLine>"""
        for i, (note, amount) in enumerate(lines, start=1))
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017</cbc:CustomizationID>
  <cbc:ID>{number}</cbc:ID>
  <cbc:IssueDate>{day.isoformat()}</cbc:IssueDate>
  <cbc:DueDate>{day.isoformat()}</cbc:DueDate>
  <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty><cac:Party><cac:PartyName><cbc:Name>{supplier}</cbc:Name></cac:PartyName>
    <cac:PartyTaxScheme><cbc:CompanyID>PT{supplier_nif}</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
    </cac:PartyTaxScheme></cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cac:PartyLegalEntity><cbc:RegistrationName>Obras Silva, Lda.</cbc:RegistrationName></cac:PartyLegalEntity>
    <cac:PartyTaxScheme><cbc:CompanyID>PT{COMPANY_A}</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
    </cac:PartyTaxScheme></cac:Party></cac:AccountingCustomerParty>
  <cac:TaxTotal><cbc:TaxAmount currencyID="EUR">{vat}</cbc:TaxAmount>
    <cac:TaxSubtotal><cbc:TaxableAmount currencyID="EUR">{net}</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="EUR">{vat}</cbc:TaxAmount>
      <cac:TaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>{rate}</cbc:Percent>
      <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory></cac:TaxSubtotal></cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="EUR">{net}</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="EUR">{net}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="EUR">{gross}</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="EUR">{gross}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  {body}
</Invoice>
"""
    return xml.encode(), gross


def plain(*texts: str) -> None:
    for text in texts:
        assert find_jargon(text) == [], text
        assert find_off_tone(text) == [], text


# --------------------------------------------------------------------------- 1. a rule the owner taught


def test_a_rule_the_owner_taught_puts_future_payments_on_the_job() -> None:
    biz = Business()
    biz.card("5530")
    biz.card("7788")
    flores = biz.center("Rua das Flores")
    biz.center("Rua do Sol")
    first = biz.pay(date(2026, 9, 3), "-45.90", "LEROY MERLIN ALFRAGIDE", card="5530")
    card = biz.question_for(first)
    assert card["remember"]["template"] == "Always put Leroy Merlin paid with card •••• 5530 on {choice}"
    out = post(biz.svc, f"/api/needs-you/{card['id']}/answer", {"option_id": f"cc:{flores}", "remember": True})
    assert out["learned"] == "Always put Leroy Merlin paid with card •••• 5530 on Job Rua das Flores"

    later = biz.pay(date(2026, 9, 21), "-88.10", "LEROY MERLIN AMADORA", card="5530")
    allocation = biz.allocation(later)
    assert allocation is not None and allocation.method is AllocationMethod.RULE
    assert allocation.quality is Quality.GREEN and allocation.cost_center_ids == (flores,)
    assert allocation.why == ("You told us: Always put Leroy Merlin paid with card •••• 5530 on Job Rua das Flores.",)
    assert allocation.rule_id is not None
    assert not [n for n in biz.repo.open_needs() if n.subject_id == later]

    # The rule is about that card: the same shop on another card is asked about, not guessed.
    other = biz.pay(date(2026, 9, 22), "-12.00", "LEROY MERLIN AMADORA", card="7788")
    assert biz.allocation(other) is None
    assert biz.question_for(other)["question"] == "Which job is this for?"


# --------------------------------------------------------------------------- 2. a card that belongs to a cost center


def test_a_technicians_card_and_a_vehicles_fuel_card_put_costs_on_their_cost_center() -> None:
    biz = Business()
    biz.card("4417")
    biz.card("8820")
    joao = biz.center("João", kind="Technician", cards=["4417"])
    van = biz.center("12-AB-34", kind="Vehicle", cards=["8820"])
    parts = biz.pay(date(2026, 9, 10), "-63.40", "LEROY MERLIN LOURES", card="4417")
    fuel = biz.pay(date(2026, 9, 11), "-71.25", "GALP AREEIRO", card="8820")
    for tx_id, center, why in (
        (parts, joao, "Paid with card •••• 4417, which you use for Technician João."),
        (fuel, van, "Paid with card •••• 8820, which you use for Vehicle 12-AB-34."),
    ):
        allocation = biz.allocation(tx_id)
        assert allocation is not None and allocation.method is AllocationMethod.CARD
        assert allocation.cost_center_ids == (center,) and allocation.why == (why,)
        assert allocation.total == abs(biz.tx(tx_id).tx.amount)
        plain(*allocation.why)
    assert not [n for n in biz.repo.open_needs() if n.kind == "cost_center"]


# --------------------------------------------------------------------------- 3. identifiers on the invoice


def test_an_address_a_plate_and_a_project_code_on_the_invoice_name_the_cost_center() -> None:
    biz = Business()
    flores = biz.center("Rua das Flores", addresses=["Rua das Flores 12"])
    sol = biz.center("Rua do Sol", references=["OBR-2026-014"])
    van = biz.center("Van 12-AB-34", kind="Vehicle", plates=["12-AB-34"])
    cases = [
        (flores, biz.pay(date(2026, 9, 18), "-117.20", "LEROY MERLIN", description="COMPRA"),
         invoice("Leroy Merlin Portugal, S.A.", LEROY, "FT LM2026/183", date(2026, 9, 18), "95.28", "21.92",
                 "Obra: Rua das Flores, n.º 12, Lisboa"),
         "The invoice shows the address Rua das Flores 12, which is Job Rua das Flores."),
        (sol, biz.pay(date(2026, 9, 19), "-246.00", "LEROY MERLIN"),
         invoice("Leroy Merlin Portugal, S.A.", LEROY, "FT LM2026/190", date(2026, 9, 19), "200.00", "46.00",
                 "Vossa referência: OBR2026014"),
         "The invoice names OBR-2026-014, which is Job Rua do Sol."),
        (van, biz.pay(date(2026, 9, 20), "-61.50", "GALP ENERGIA"),
         invoice("Galp Energia, S.A.", GALP, "FT GP2026/77", date(2026, 9, 20), "50.00", "11.50",
                 "Matrícula: 12 AB 34 · Gasóleo 32,1 L"),
         "The invoice shows the plate 12-AB-34, which is Vehicle Van 12-AB-34."),
    ]
    for center, tx_id, data, why in cases:
        biz.upload(f"{tx_id}.txt", data)
        rec = biz.tx(tx_id)
        assert rec.document_ids, "the invoice matched its payment"
        allocation = biz.allocation(tx_id)
        assert allocation is not None and allocation.method is AllocationMethod.IDENTIFIER
        assert allocation.cost_center_ids == (center,) and allocation.why == (why,)
        # The same answer is on the invoice, and the VAT is kept by rate (from the fiscal QR).
        doc = biz.repo.documents[rec.document_ids[0]].document
        assert doc.cost_allocation is not None and doc.cost_allocation.cost_center_ids == (center,)
        share = allocation.shares[0]
        assert share.amount == abs(rec.tx.amount) and share.parts and share.parts[0].rate == Decimal(23)
        assert sum((p.gross for p in share.parts), Decimal(0)) == share.amount
        plain(why)
    assert not [n for n in biz.repo.open_needs() if n.kind == "cost_center"]


def test_a_bank_line_naming_the_site_is_enough_for_money_received() -> None:
    biz = Business()
    flores = biz.center("Rua das Flores", addresses=["Rua das Flores 12"])
    biz.center("Rua do Sol")
    paid_in = biz.pay(date(2026, 9, 25), "2500.00", "JOANA MARQUES", kind=TransactionKind.TRANSFER_IN,
                      description="TRF 2A PRESTACAO OBRA RUA DAS FLORES 12")
    allocation = biz.allocation(paid_in)
    assert allocation is not None and allocation.cost_center_ids == (flores,)
    assert allocation.why == ("The bank line shows the address Rua das Flores 12, which is Job Rua das Flores.",)
    assert get(biz.svc, f"/api/cost-centers/{flores}")["received"] == 2500


# --------------------------------------------------------------------------- 4. supplier history


def test_a_supplier_always_on_the_same_job_is_put_there_as_likely() -> None:
    biz = Business()
    flores = biz.center("Rua das Flores")
    sol = biz.center("Rua do Sol")
    earlier = [biz.pay(date(2026, 9, d), f"-{d}.00", "DROGARIA CENTRAL") for d in (2, 9, 16)]
    for tx_id in earlier:  # the owner put each one on Rua das Flores (no "always")
        post(biz.svc, "/api/cost-centers/allocate", {"subjectId": tx_id, "costCenterId": flores})
    assert all(biz.allocation(t).method is AllocationMethod.OWNER for t in earlier)  # type: ignore[union-attr]

    next_one = biz.pay(date(2026, 9, 23), "-23.00", "DROGARIA CENTRAL")
    allocation = biz.allocation(next_one)
    assert allocation is not None and allocation.method is AllocationMethod.HISTORY
    assert allocation.quality is Quality.AMBER  # likely, never shown as proven
    assert allocation.cost_center_ids == (flores,)
    assert allocation.why == ("The last 3 Drogaria Central payments were all for Job Rua das Flores.",)
    row = next(p for p in get(biz.svc, f"/api/cost-centers/{flores}")["payments"] if p["id"] == next_one)
    assert row["likely"] is True and row["why"][-1] == "Likely, not proven. Tell me if it is wrong."

    # A supplier that went to different jobs is not enough to be sure: one question.
    post(biz.svc, "/api/cost-centers/allocate", {"subjectId": earlier[0], "costCenterId": sol})
    mixed = biz.pay(date(2026, 9, 24), "-24.00", "DROGARIA CENTRAL")
    assert biz.allocation(mixed) is None
    why = biz.question_for(mixed)["why"]
    assert why == ["Earlier Drogaria Central payments went to Job Rua das Flores and Job Rua do Sol. "
                   "That is not enough to be sure."]


# --------------------------------------------------------------------------- 5. unknown: one question, one-tap learning


def test_an_unknown_cost_is_one_question_and_always_resolves_it_and_future_ones() -> None:
    biz = Business()
    biz.card("5530")
    flores = biz.center("Rua das Flores", addresses=["Rua das Flores 12"])
    sol = biz.center("Rua do Sol")
    first = biz.pay(date(2026, 9, 14), "-117.20", "LEROY MERLIN ALFRAGIDE", card="5530")
    second = biz.pay(date(2026, 9, 17), "-33.15", "LEROY MERLIN ALFRAGIDE", card="5530")
    card = biz.question_for(first)
    assert card["kind"] == "choice" and card["eyebrow"] == "We need one answer"
    assert card["question"] == "Which job is this for?"
    assert [o["label"] for o in card["options"]] == ["Job Rua das Flores", "Job Rua do Sol",
                                                      "General costs, not for one job"]
    assert card["why"] == ["Nothing on it shows which job it is for."]
    assert card["paidWith"] == "card •••• 5530" and card["merchant"] == "Leroy Merlin" and card["amount"] == 117.2
    assert card["remember"] == {"template": "Always put Leroy Merlin paid with card •••• 5530 on {choice}",
                                "defaultChecked": True,
                                "overrides": {"general": "Always treat Leroy Merlin paid with card •••• 5530 as "
                                                         "general costs"}}
    assert card["split"]["optionId"] == "split" and {c["id"] for c in card["split"]["costCenters"]} == {flores, sol}
    second_card = biz.question_for(second)
    plain(card["question"], *card["why"], *(o["label"] for o in card["options"]), card["remember"]["template"])
    # "What needs my attention?" names it in plain words.
    attention = ok(biz.svc.dispatch("POST", "/api/ask", {"question": "What needs my attention?"}))["answer"]
    assert "Leroy Merlin payment of €117.20: which job is this for?" in attention

    out = post(biz.svc, f"/api/needs-you/{card['id']}/answer", {"option_id": f"cc:{sol}", "remember": True})
    assert out["message"] == "Done. The Leroy Merlin payment is now on Job Rua do Sol."
    assert out["learned"] == "Always put Leroy Merlin paid with card •••• 5530 on Job Rua do Sol"
    assert out["alsoResolved"] == [second_card["id"]]
    plain(out["message"], out["learned"])
    mine = biz.allocation(first)
    assert mine is not None and mine.method is AllocationMethod.OWNER
    assert mine.why == ("You said this is for Job Rua do Sol.",)
    owner_evidence = [e for e in mine.evidence_ids if biz.repo.evidence(e).metadata.get("kind") == "owner_answer"]
    assert len(owner_evidence) == 1  # the answer itself is kept as evidence
    also = biz.allocation(second)
    assert also is not None and also.method is AllocationMethod.RULE and also.cost_center_ids == (sol,)
    assert not [n for n in biz.needs() if n["id"] in (card["id"], second_card["id"])]
    # Answering again is refused; the question is gone.
    assert biz.svc.dispatch("POST", f"/api/needs-you/{card['id']}/answer", {"option_id": f"cc:{sol}"})[0] == 409

    # Future payments: no question.
    third = biz.pay(date(2026, 9, 28), "-9.99", "LEROY MERLIN ALFRAGIDE", card="5530")
    assert biz.allocation(third).cost_center_ids == (sol,)  # type: ignore[union-attr]
    assert not [n for n in biz.repo.open_needs() if n.kind == "cost_center"]

    # Evidence on the invoice still wins over nothing, but a rule is never silently overridden by it:
    # an invoice naming another job for a payment the rule covers is asked about.
    conflict = biz.pay(date(2026, 9, 29), "-117.20", "LEROY MERLIN ALFRAGIDE", card="5530")
    biz.upload("lm.txt", invoice("Leroy Merlin Portugal, S.A.", LEROY, "FT LM2026/201", date(2026, 9, 29), "95.28",
                                 "21.92", "Obra: Rua das Flores 12"))
    assert biz.tx(conflict).document_ids and biz.allocation(conflict) is None
    why = biz.question_for(conflict)["why"]
    assert why[0] == "You told us: Always put Leroy Merlin paid with card •••• 5530 on Job Rua do Sol."
    assert "The invoice shows the address Rua das Flores 12, which is Job Rua das Flores." in why
    assert why[-1] == "They don't agree, so I'm asking you."


# --------------------------------------------------------------------------- 6. no cost centers: never asked


def test_a_company_without_cost_centers_is_never_asked() -> None:
    biz = Business(second_company=True)
    biz.center("Rua das Flores")
    theirs = biz.pay(date(2026, 9, 12), "-54.00", "DROGARIA CENTRAL", company=biz.b)
    ours = biz.pay(date(2026, 9, 12), "-54.00", "DROGARIA CENTRAL")
    assert biz.allocation(theirs) is None
    assert not [n for n in biz.repo.needs.values() if n.subject_id == theirs]
    assert [n.kind for n in biz.repo.open_needs() if n.subject_id == ours] == ["cost_center"]
    audited = [r.data() for r in biz.repo.audit_store.records(biz.repo.tenant_id)]
    assert not [r for r in audited if r.get("subject_id") == theirs and r["agent"] == "cost_center"]
    view = get(biz.svc, f"/api/companies/{biz.b}/cost-centers")
    assert view["usesCostCenters"] is False and view["costCenters"] == [] and view["notDecided"]["payments"] == 0
    assert view["headline"] == "Add a job and I will put each cost on the right one."

    # The demo businesses keep no cost centers: nothing changes there.
    demo = BackOfficeService.demo()
    assert not demo.repo.cost_centers
    assert not [n for n in demo.repo.needs.values() if n.kind == "cost_center"]
    assert all(r.tx.cost_allocation is None for r in demo.repo.transactions.values())
    assert all(d.document.cost_allocation is None for d in demo.repo.documents.values())
    assert get(demo, "/api/companies/hazel-tree/cost-centers")["usesCostCenters"] is False


# --------------------------------------------------------------------------- 7. splits exact to the cent


def test_one_utility_bill_split_across_three_apartments_exact_to_the_cent() -> None:
    biz = Business()
    flats = [biz.center(f"Apartment {n}", kind="Apartment") for n in ("1A", "1B", "2A")]
    bill = biz.pay(date(2026, 9, 8), "-100.00", "EDP COMERCIAL", kind=TransactionKind.DIRECT_DEBIT)
    biz.upload("edp-set.txt", invoice("EDP Comercial, S.A.", EDP, "FT EDP2026/81", date(2026, 9, 8), "81.30", "18.70",
                                      "Eletricidade - prédio Rua Nova 3"))
    card = biz.question_for(bill)
    assert card["question"] == "Which apartment is this for?"
    split = [{"costCenterId": flats[0], "percent": "50"}, {"costCenterId": flats[1], "percent": "30"},
             {"costCenterId": flats[2], "percent": "20"}]
    out = post(biz.svc, f"/api/needs-you/{card['id']}/answer", {"option_id": "split", "split": split, "remember": True})
    assert out["message"] == ("Done. The EDP Comercial payment is now split: Apartment 1A €50.00, "
                              "Apartment 1B €30.00 and Apartment 2A €20.00.")
    assert out["learned"] == ("Always split EDP Comercial paid from Millennium BCP •••• 0154: Apartment 1A 50%, "
                              "Apartment 1B 30% and Apartment 2A 20%")
    plain(out["message"], out["learned"])

    # October's bill is split the same way on its own, each VAT part rounded down and the leftover
    # cents given to the largest share: €117.23 = net €95.31 + VAT €21.92.
    october = biz.pay(date(2026, 9, 29), "-117.23", "EDP COMERCIAL", kind=TransactionKind.DIRECT_DEBIT)
    biz.upload("edp-out.txt", invoice("EDP Comercial, S.A.", EDP, "FT EDP2026/92", date(2026, 9, 29), "95.31",
                                      "21.92", "Eletricidade - prédio Rua Nova 3"))
    allocation = biz.allocation(october)
    assert allocation is not None and allocation.method is AllocationMethod.LEARNED_SPLIT
    assert [(s.cost_center_id, s.amount) for s in allocation.shares] == [
        (flats[0], Decimal("58.63")), (flats[1], Decimal("35.16")), (flats[2], Decimal("23.44"))]
    assert [(p.net, p.vat) for s in allocation.shares for p in s.parts] == [
        (Decimal("47.66"), Decimal("10.97")), (Decimal("28.59"), Decimal("6.57")), (Decimal("19.06"), Decimal("4.38"))]
    assert sum((s.amount for s in allocation.shares), Decimal(0)) == Decimal("117.23")
    assert sum((p.net for s in allocation.shares for p in s.parts), Decimal(0)) == Decimal("95.31")
    assert sum((p.vat for s in allocation.shares for p in s.parts), Decimal(0)) == Decimal("21.92")
    doc = biz.repo.documents[biz.tx(october).document_ids[0]].document
    assert doc.cost_allocation is not None and doc.cost_allocation.shares == allocation.shares

    # Equal thirds of €100.00 by amount: the owner's amounts are kept to the cent, VAT follows them.
    given = [(flats[0], Decimal("33.33"), None), (flats[1], Decimal("33.33"), None), (flats[2], Decimal("33.34"), None)]
    thirds = split_by_amounts(Decimal("100.00"), given,
                              [VatPart(rate=Decimal(23), net=Decimal("81.30"), vat=Decimal("18.70"))])
    assert [s.amount for s in thirds] == [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")]
    assert sum((s.parts[0].vat for s in thirds), Decimal(0)) == Decimal("18.70")
    by_weight = split_by_weights(Decimal("100.00"), [("a", Decimal(1)), ("b", Decimal(1)), ("c", Decimal(1))])
    assert [s.amount for s in by_weight] == [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")]


def test_a_reseller_licence_invoice_is_split_across_its_customers_by_its_lines() -> None:
    biz = Business()
    customers = [nif(f"50912{n:03d}") for n in range(12)]
    centers = [biz.center(f"Acme {i + 1:02d}", kind="Client", tax_ids=[t]) for i, t in enumerate(customers)]
    amounts = ["62.50", "125.00", "12.50", "37.50", "250.00", "12.50", "75.00", "25.00", "12.50", "50.00", "87.50",
               "12.49"]
    lines = [(f"Cliente NIF {t} · 5 utilizadores", a) for t, a in zip(customers, amounts, strict=True)]
    xml, gross = ubl_invoice("FT MS2026/901", date(2026, 9, 5), lines)
    payment = biz.pay(date(2026, 9, 6), f"-{gross}", "MICROSOFT*365", kind=TransactionKind.DIRECT_DEBIT)
    biz.upload("microsoft.xml", xml, "application/xml")
    # The lines alone already put the invoice on its customers, before it is matched.
    early = biz.repo.documents[next(iter(biz.repo.documents))].document.cost_allocation
    assert early is not None and early.method is AllocationMethod.LINES and len(early.shares) == 12
    # Its printed copy with the fiscal QR confirms the totals (one document, two renditions); then it matches.
    net_total = sum((Decimal(a) for a in amounts), Decimal(0))
    biz.upload("microsoft.txt", invoice("Microsoft Ireland Operations Ltd", MICROSOFT, "FT MS2026/901",
                                        date(2026, 9, 5), f"{net_total:.2f}", f"{gross - net_total:.2f}"))
    assert len(biz.repo.documents) == 1
    rec = biz.tx(payment)
    assert rec.document_ids
    doc = biz.repo.documents[rec.document_ids[0]].document
    assert len(doc.lines) == 12 and doc.lines[0].vat_rate == Decimal(23)
    allocation = biz.allocation(payment)
    assert allocation is not None and allocation.method is AllocationMethod.LINES
    assert [s.cost_center_id for s in allocation.shares] == centers
    assert sum((s.amount for s in allocation.shares), Decimal(0)) == gross
    net = sum((Decimal(a) for a in amounts), Decimal(0))
    assert [p.net for s in allocation.shares for p in s.parts] == [Decimal(a) for a in amounts]
    assert sum((p.vat for s in allocation.shares for p in s.parts), Decimal(0)) == gross - net
    assert allocation.why[0].startswith("Split by the 12 invoice lines, one for each: Client Acme 01, Client Acme 02, ")
    assert doc.cost_allocation is not None and doc.cost_allocation.shares == allocation.shares
    view = get(biz.svc, f"/api/cost-centers/{centers[4]}")
    assert view["spent"] == float(allocation.amount_for(centers[4]))
    assert view["payments"][0]["split"] is True and view["payments"][0]["total"] == float(gross)


def test_a_split_that_does_not_add_up_is_refused_with_a_plain_message() -> None:
    biz = Business()
    flats = [biz.center(f"Apartment {n}", kind="Apartment") for n in ("1A", "1B", "2A")]
    bill = biz.pay(date(2026, 9, 8), "-100.00", "EDP COMERCIAL", kind=TransactionKind.DIRECT_DEBIT)
    card = biz.question_for(bill)
    before = (len(biz.repo.interactions), len(biz.repo.store), len(biz.repo.activity))
    path = f"/api/needs-you/{card['id']}/answer"
    short = [{"costCenterId": flats[0], "amount": "50.00"}, {"costCenterId": flats[1], "amount": "30.00"},
             {"costCenterId": flats[2], "amount": "19.99"}]
    refused = post(biz.svc, path, {"option_id": "split", "split": short}, status=400)
    assert refused["message"] == ("These amounts add up to €99.99, but the total is €100.00. "
                                  "They must match to the cent.")
    percents = [{"costCenterId": flats[0], "percent": 50}, {"costCenterId": flats[1], "percent": 40}]
    assert post(biz.svc, path, {"option_id": "split", "split": percents}, status=400)["message"] == \
        "The shares add up to 90%, not 100%."
    mixed = [{"costCenterId": flats[0], "percent": 50}, {"costCenterId": flats[1], "amount": "50.00"}]
    assert post(biz.svc, path, {"option_id": "split", "split": mixed}, status=400)["message"] == \
        "Give either amounts or percentages for every share, not both."
    cents = [{"costCenterId": flats[0], "amount": "50.005"}, {"costCenterId": flats[1], "amount": "49.995"}]
    assert post(biz.svc, path, {"option_id": "split", "split": cents}, status=400)["message"] == \
        "Use amounts in whole cents."
    stranger = [{"costCenterId": flats[0], "percent": 50}, {"costCenterId": "cc-elsewhere", "percent": 50}]
    assert post(biz.svc, path, {"option_id": "split", "split": stranger}, status=400)["message"] == \
        "One of those isn't one of this company's apartments."
    assert post(biz.svc, path, {"option_id": "split"}, status=400)["message"] == \
        "Tell me how to split it: an amount or a percentage for each one."
    # Nothing was recorded or changed by a refused split: the question is still open.
    assert (len(biz.repo.interactions), len(biz.repo.store), len(biz.repo.activity)) == before
    assert biz.allocation(bill) is None and biz.question_for(bill)["id"] == card["id"]
    # The same checks guard the direct route and the model itself.
    assert post(biz.svc, "/api/cost-centers/allocate", {"subjectId": bill, "split": short}, status=400)["message"] \
        .startswith("These amounts add up to €99.99")
    with pytest.raises(ValueError, match="add up exactly"):
        CostAllocation(total=Decimal("100.00"), method=AllocationMethod.OWNER,
                       shares=(AllocationShare(cost_center_id="a", amount=Decimal("60.00")),
                               AllocationShare(cost_center_id="b", amount=Decimal("39.99"))))
    with pytest.raises(SplitError, match="not 100%"):
        split_by_percent(Decimal("10.00"), [("a", Decimal("33.33")), ("b", Decimal("33.33")), ("c", Decimal("33.33"))])
    two_rates = [VatPart(rate=Decimal(23), net=Decimal("50.00"), vat=Decimal("11.50")),
                 VatPart(rate=Decimal(6), net=Decimal("20.00"), vat=Decimal("1.20"))]
    with pytest.raises(SplitError, match="more than one VAT rate"):
        split_by_amounts(Decimal("82.70"), [("a", Decimal("41.35"), None), ("b", Decimal("41.35"), None)], two_rates)
    at_each_rate = [("a", Decimal("61.50"), Decimal(23)), ("b", Decimal("21.20"), Decimal(6))]
    per_rate = split_by_amounts(Decimal("82.70"), at_each_rate, two_rates)
    assert [(s.cost_center_id, s.amount) for s in per_rate] == [("a", Decimal("61.50")), ("b", Decimal("21.20"))]
    for message in (refused["message"], "The shares add up to 90%, not 100%."):
        plain(message)


# --------------------------------------------------------------------------- 8. per-cost-center summary


def test_each_cost_center_shows_what_it_cost_what_came_in_and_the_proof() -> None:
    biz = Business()
    biz.card("5530")
    flores = biz.center("Rua das Flores", addresses=["Rua das Flores 12"], cards=["5530"])
    sol = biz.center("Rua do Sol", references=["OBR-2026-014"])
    tiles = biz.pay(date(2026, 9, 18), "-117.20", "LEROY MERLIN", description="COMPRA")
    biz.upload("tiles.txt", invoice("Leroy Merlin Portugal, S.A.", LEROY, "FT LM2026/183", date(2026, 9, 18), "95.28",
                                    "21.92", "Obra: Rua das Flores 12"))
    screws = biz.pay(date(2026, 9, 22), "-19.90", "LEROY MERLIN ALFRAGIDE", card="5530")  # card: no invoice yet
    august = biz.pay(date(2026, 8, 28), "-40.00", "LEROY MERLIN ALFRAGIDE", card="5530")
    paid_in = biz.pay(date(2026, 9, 25), "2500.00", "JOANA MARQUES", kind=TransactionKind.TRANSFER_IN,
                      description="OBRA RUA DAS FLORES 12")
    shared = biz.pay(date(2026, 9, 26), "-300.00", "DROGARIA CENTRAL")
    post(biz.svc, "/api/cost-centers/allocate", {"subjectId": shared, "split": [
        {"costCenterId": flores, "amount": "200.00"}, {"costCenterId": sol, "amount": "100.00"}]})
    general = biz.pay(date(2026, 9, 27), "-80.00", "DROGARIA CENTRAL")
    post(biz.svc, "/api/cost-centers/allocate", {"subjectId": general, "general": True})

    view = get(biz.svc, f"/api/cost-centers/{flores}?month=2026-09")
    assert view["label"] == "Job Rua das Flores" and view["period"] == {"from": "2026-09-01", "to": "2026-09-30",
                                                                      "label": "September"}
    assert view["spent"] == 117.2 + 19.9 + 200 and view["received"] == 2500
    assert [p["id"] for p in view["payments"]] == [tiles, screws, paid_in, shared]
    assert [p["amount"] for p in view["payments"]] == [117.2, 19.9, 2500, 200]
    assert [p["split"] for p in view["payments"]] == [False, False, False, True]
    assert [d["label"] for d in view["documents"]] == ["Invoice FT LM2026/183 · €117.20"]
    tiles_row = view["payments"][0]
    doc_id = biz.tx(tiles).document_ids[0]
    assert tiles_row["status"] == "closed" and [e["id"] for e in tiles_row["evidence"]] == [
        biz.tx(tiles).evidence_id, biz.repo.documents[doc_id].evidence_ids[0]]
    assert {e["id"] for e in view["evidence"]} >= {biz.tx(t).evidence_id for t in (tiles, screws, paid_in, shared)}
    # Still open: the screws' receipt, the invoice for the money received, and the shared bill's invoice.
    assert [o["id"] for o in view["openItems"]] == [screws, paid_in, shared]
    assert view["summary"] == ("Job Rua das Flores in September: €337.10 spent, €2,500.00 received, 4 payments. "
                               "3 still open.")
    plain(view["summary"], *(o["text"] for o in view["openItems"]), *(w for p in view["payments"] for w in p["why"]))
    # All time includes August.
    assert get(biz.svc, f"/api/cost-centers/{flores}")["spent"] == 117.2 + 19.9 + 200 + 40

    company = get(biz.svc, f"/api/companies/{biz.a}/cost-centers?month=2026-09")
    rows = {r["id"]: r for r in company["costCenters"]}
    assert rows[flores]["spent"] == 337.1 and rows[flores]["received"] == 2500 and rows[flores]["payments"] == 4
    assert rows[sol]["spent"] == 100 and rows[sol]["payments"] == 1
    assert company["general"] == {"spent": 80, "received": 0, "payments": 1}
    assert company["kind"] == "Job" and company["kindPlural"] == "Jobs" and company["usesCostCenters"] is True
    assert company["headline"] == "Every cost is on its job."
    assert august not in [p["id"] for p in view["payments"]]
    assert biz.svc.dispatch("GET", f"/api/cost-centers/{flores}?month=2026-13", None)[0] == 400
    assert biz.svc.dispatch("GET", "/api/cost-centers/cc-nope", None)[0] == 404
    assert biz.svc.dispatch("GET", "/api/companies/nope/cost-centers", None)[0] == 404


def test_cost_centers_are_added_renamed_and_checked() -> None:
    biz = Business()
    created = post(biz.svc, f"/api/companies/{biz.a}/cost-centers", {"name": "Casamento Silva", "kind": "event",
                                                                     "identifiers": {"emails": ["Silva@Eventos.pt"]}})
    center = created["costCenter"]
    assert created["message"] == "Done. Event Casamento Silva is set up. I will put its costs on it."
    assert center["kind"] == "Event" and center["identifiers"]["emails"] == ["silva@eventos.pt"]
    # The company's own word is kept for the next one.
    assert post(biz.svc, f"/api/companies/{biz.a}/cost-centers", {"name": "Batizado Costa"})["costCenter"]["kind"] == \
        "Event"
    again = post(biz.svc, f"/api/companies/{biz.a}/cost-centers", {"name": "casamento silva"}, status=409)
    assert again["message"] == "Event Casamento Silva is already here."
    assert post(biz.svc, f"/api/companies/{biz.a}/cost-centers", {"name": ""}, status=400)["message"] == \
        "What is it called? For example the site's street, the plate or the client."
    assert post(biz.svc, f"/api/companies/{biz.a}/cost-centers",
                {"name": "X1", "identifiers": {"cards": ["12"]}}, status=400)["message"] == \
        "A card is the last 4 digits."
    assert post(biz.svc, f"/api/companies/{biz.a}/cost-centers",
                {"name": "X2", "identifiers": {"colour": ["red"]}}, status=400)["message"] == (
        "I can only recognise it by addresses, plates, cards, accounts, references, tax numbers, email addresses "
        "or keywords.")
    tagged = post(biz.svc, f"/api/companies/{biz.a}/cost-centers",
                  {"name": "Feira Norte", "identifiers": {"taxIds": ["PT 509 123 450"], "plates": 7}}, status=400)
    assert tagged["message"] == "Give the plates as a list."
    client = post(biz.svc, f"/api/companies/{biz.a}/cost-centers", {"name": "Feira Norte",
                                                                    "identifiers": {"taxIds": ["PT 509 123 450"]}})
    assert client["costCenter"]["identifiers"]["taxIds"] == ["PT509123450"]
    plain(tagged["message"])
    renamed = post(biz.svc, f"/api/cost-centers/{center['id']}", {"name": "Casamento Silva & Costa",
                                                                  "identifiers": {"references": ["EV-0927"]}})
    assert renamed["message"] == "Done. It is now called Event Casamento Silva & Costa."
    assert renamed["costCenter"]["identifiers"]["emails"] == ["silva@eventos.pt"]  # kept
    assert renamed["costCenter"]["identifiers"]["references"] == ["EV-0927"]
    waiting = biz.pay(date(2026, 9, 12), "-54.00", "DROGARIA CENTRAL")
    assert [o["label"] for o in biz.question_for(waiting)["options"]] == [
        "Event Batizado Costa", "Event Casamento Silva & Costa", "Event Feira Norte",
        "General costs, not for one event"]
    archived = post(biz.svc, f"/api/cost-centers/{center['id']}", {"active": False})
    assert archived["message"] == "Done. Event Casamento Silva & Costa is archived. Its past costs stay on it."
    # The open question now offers only what is still active; with none left, nobody is asked.
    assert [o["label"] for o in biz.question_for(waiting)["options"]] == [
        "Event Batizado Costa", "Event Feira Norte", "General costs, not for one event"]
    for cc in list(biz.repo.cost_centers):
        if biz.repo.cost_centers[cc].active:
            post(biz.svc, f"/api/cost-centers/{cc}", {"active": False})
    assert not [n for n in biz.repo.open_needs() if n.kind == "cost_center"]
    assert get(biz.svc, f"/api/companies/{biz.a}/cost-centers")["usesCostCenters"] is False
    assert post(biz.svc, "/api/cost-centers/cc-nope", {"name": "x"}, status=404)["message"] == "I can't find that one."
    for message in (created["message"], renamed["message"], archived["message"]):
        plain(message)


# --------------------------------------------------------------------------- 9. evidence never leaks across companies


def test_evidence_never_leaks_across_companies() -> None:
    biz = Business(second_company=True)
    ours = biz.center("Rua das Flores", addresses=["Rua das Flores 12"])
    theirs = biz.center("Rua das Flores", company=biz.b, addresses=["Rua das Flores 12"])
    north = biz.center("Loja Norte", company=biz.b, kind="Outlet")
    tx_id = biz.pay(date(2026, 9, 18), "-117.20", "LEROY MERLIN")
    biz.upload("tiles.txt", invoice("Leroy Merlin Portugal, S.A.", LEROY, "FT LM2026/183", date(2026, 9, 18), "95.28",
                                    "21.92", "Obra: Rua das Flores 12"))
    allocation = biz.allocation(tx_id)
    assert allocation is not None and allocation.cost_center_ids == (ours,)
    mine = get(biz.svc, f"/api/cost-centers/{ours}")
    other = get(biz.svc, f"/api/cost-centers/{theirs}")
    assert mine["spent"] == 117.2 and other["spent"] == 0
    assert other["payments"] == [] and other["documents"] == [] and other["evidence"] == []
    ids = set(allocation.evidence_ids)
    for cid in (theirs, north):
        view = get(biz.svc, f"/api/cost-centers/{cid}")
        assert not ids & {e["id"] for e in view["evidence"]}
    # A payment of one company cannot be put on another company's cost center.
    refused = post(biz.svc, "/api/cost-centers/allocate", {"subjectId": tx_id, "costCenterId": north}, status=400)
    assert refused["message"] == "That isn't one of this company's jobs."
    assert biz.allocation(tx_id).cost_center_ids == (ours,)  # type: ignore[union-attr]
    # Questions only offer the payment's own company's cost centers.
    unknown = biz.pay(date(2026, 9, 20), "-20.00", "DROGARIA CENTRAL")
    offered = {o["id"] for o in biz.question_for(unknown)["options"]}
    assert offered == {f"cc:{ours}", "general"}
    assert post(biz.svc, f"/api/needs-you/{biz.question_for(unknown)['id']}/answer",
                {"option_id": f"cc:{north}"}, status=400)["message"] == "Please pick one of the options."


# --------------------------------------------------------------------------- 10. the chat


def test_the_chat_answers_what_a_job_cost_in_a_month_with_its_evidence() -> None:
    biz = Business()
    biz.card("5530")
    flores = biz.center("Rua das Flores", addresses=["Rua das Flores 12"], cards=["5530"])
    sol = biz.center("Rua do Sol")
    a = biz.pay(date(2026, 9, 18), "-117.20", "LEROY MERLIN ALFRAGIDE", card="5530")
    b = biz.pay(date(2026, 9, 22), "-19.90", "LEROY MERLIN ALFRAGIDE", card="5530")
    biz.pay(date(2026, 8, 5), "-40.00", "LEROY MERLIN ALFRAGIDE", card="5530")
    reply = ok(biz.svc.dispatch("POST", "/api/ask", {"question": "How much did we spend on Job Rua das Flores in "
                                                                 "September?"}))
    assert reply["answer"] == ("You spent €137.10 on Job Rua das Flores in September: 2 payments. "
                               "The biggest was Leroy Merlin, €117.20.")
    assert [e["id"] for e in reply["evidence"]] == [biz.tx(a).evidence_id, biz.tx(b).evidence_id]
    none = ok(biz.svc.dispatch("POST", "/api/ask", {"question": "what did we spend on rua do sol in august"}))
    assert none["answer"] == "Nothing went out for Job Rua do Sol in August."
    chat = ok(biz.svc.dispatch("POST", "/api/chat", {"message": "and in August?", "history": [
        {"role": "user", "content": "How much did we spend on Job Rua das Flores in September?"},
        {"role": "assistant", "content": reply["answer"]}]}))
    assert chat["reply"].startswith("You spent €40.00 on Job Rua das Flores in August: one payment.")
    plain(reply["answer"], none["answer"])
    assert flores in biz.repo.cost_centers

    # The owner can also answer "which job" in the chat, with "always" for the future.
    unknown = biz.pay(date(2026, 9, 24), "-64.00", "DROGARIA CENTRAL")
    assert biz.question_for(unknown)["question"] == "Which job is this for?"
    done = ok(biz.svc.dispatch("POST", "/api/chat", {"message": "Put the Drogaria Central payment on Rua do Sol, "
                                                                "always"}))
    assert done["reply"] == ("Done. The Drogaria Central payment is now on Job Rua do Sol. Always put Drogaria "
                             "Central paid from Millennium BCP •••• 0154 on Job Rua do Sol.")
    assert biz.allocation(unknown).cost_center_ids == (sol,)  # type: ignore[union-attr]
    assert biz.allocation(biz.pay(date(2026, 9, 27), "-8.00", "DROGARIA CENTRAL")).method is AllocationMethod.RULE  # type: ignore[union-attr]


# --------------------------------------------------------------------------- 11. every sentence is plain


def test_every_cost_center_sentence_is_plain_and_calm() -> None:
    from backoffice.domain.cost_centers import CostCenter, CostCenterIdentifiers
    from backoffice.learning.cost_centers import CostCenterFacts, decide_cost_center

    flores = CostCenter(id="cc-flores", tenant_id="t", company_id="c", name="Rua das Flores", identifiers=
                        CostCenterIdentifiers(addresses=["Rua das Flores 12"], plates=["12-AB-34"],
                                              references=["OBR-2026-014"], tax_ids=["509123450"],
                                              emails=["obra.flores@obras.pt"], keywords=["telhado"], cards=["5530"],
                                              accounts=["acct-1"]))
    casa = CostCenter(id="cc-casa", tenant_id="t", company_id="c", name="Casa Azul", kind="Property")
    texts = ["Obra: Rua das Flores, n.º 12", "Matrícula 12-AB-34", "Ref. OBR2026014", "Cliente NIF 509 123 450",
             "Enviado para obra.flores@obras.pt", "Reparação do telhado", "Reserva Casa Azul"]
    sentences: list[str] = []
    for i, text in enumerate(texts):
        facts = CostCenterFacts(tenant_id="t", company_id="c", subject_type="document", subject_id=f"d{i}",
                                total=Decimal("10.00"), counterparty_label="Leroy Merlin",
                                texts=(("the invoice", text),))
        decision = decide_cost_center(centers=[flores, casa], facts=facts)
        assert decision.allocation is not None, text
        sentences += decision.allocation.why
    for card, account in (("5530", None), (None, "acct-1"), (None, None)):
        facts = CostCenterFacts(tenant_id="t", company_id="c", subject_type="transaction", subject_id="t1",
                                total=Decimal("10.00"), counterparty_key="leroy merlin",
                                counterparty_label="Leroy Merlin", card_last4=card, account_id=account,
                                account_label="Millennium BCP •••• 0154",
                                texts=(("the bank line", "LEROY MERLIN"),))
        decision = decide_cost_center(centers=[flores, casa], facts=facts, history={"leroy merlin": {"cc-casa": 1}})
        sentences += list(decision.why)
        if card is None and account is None:
            assert decision.why == ("Earlier Leroy Merlin payments went to Property Casa Azul. "
                                    "That is not enough to be sure.",)
        if decision.question is not None:
            sentences += [decision.question.prompt, decision.question.detail,
                          *(o.label for o in decision.question.options)]
    both = CostCenterFacts(tenant_id="t", company_id="c", subject_type="document", subject_id="d9",
                           total=Decimal("10.00"), texts=(("the invoice", "Rua das Flores 12 e Casa Azul"),))
    asked = decide_cost_center(centers=[flores, casa], facts=both)
    assert asked.question is not None and asked.question.prompt == "Which job or property is this for?"
    sentences += [*asked.why, *(o.label for o in asked.question.options)]
    for build in (lambda: split_by_percent(Decimal("1.00"), [("a", Decimal(60)), ("b", Decimal(60))]),
                  lambda: split_by_amounts(Decimal("1.00"), [("a", Decimal("0.50"), None)]),
                  lambda: split_by_weights(Decimal("0.01"), [("a", Decimal(1)), ("b", Decimal(1))])):
        with pytest.raises(SplitError) as refused:
            build()
        sentences.append(refused.value.message)
    assert len(sentences) > 15
    plain(*sentences)
