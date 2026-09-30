"""Documents from abroad next to a Portuguese company (checklist P7 case 49, X31, Q6).

The company is Portuguese; its suppliers are not all Portuguese. A Meta ads
receipt from Ireland, a Booking.com commission invoice from the Netherlands,
an AWS invoice in US dollars, a UK studio's invoice in pounds and a Spanish
hotel bill follow their own country's conventions. They are read with their
own labels, checked by rules that hold anywhere (the bank confirms the total,
net + VAT = total, a VAT rate valid in the issuer's country, the issuer's own
VAT number) and close GREEN when those hold. Portuguese invoices keep the
Portuguese rules. VAT mechanics (reverse charge, foreign VAT) go to the
accountant only; an unusual currency is a check, not a hold.

All companies, tax numbers and IBANs are fictional; supplier brand names
appear only because the owner's statements would show them.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from backoffice.countries.foreign import (
    check_tax_number,
    detect_issuer,
    document_language,
    mentions_reverse_charge,
    read_foreign_text,
    vat_rates,
)
from backoffice.countries.pt import extract_text_fields
from backoffice.demo import build_demo
from backoffice.demo import evidence as E
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import (
    CriticalField,
    Document,
    DocumentType,
    ExtractionMethod,
    LegalEntity,
    Quality,
    Supplier,
    TransactionKind,
)
from backoffice.fraud.engine import FraudCase, Severity, SignalKind, assess
from backoffice.language import find_jargon
from backoffice.orchestrator import (
    Account,
    AccountantProfile,
    BankRow,
    ConnectorState,
    DocumentRecord,
    Orchestrator,
    OwnerProfile,
    Repository,
    TxRecord,
    _Part,
    local_datetime,
)
from backoffice.service import FOREIGN_VAT_FLAG, REVERSE_CHARGE_FLAG, BackOfficeService

F = CriticalField
D = Decimal
K = TransactionKind

TENANT = "t-abroad"
HAZEL_NIF = E.HAZEL_NIF
OWN = ("PT" + HAZEL_NIF,)
META_VAT = "IE4817263W"
BOOKING_VAT = "NL855661021B01"
PRINT_VAT = "DE274163854"
STUDIO_VAT = "GB284719326"
HOTEL_CIF = "B76524131"
AWS_EIN = "84-1739265"
TODAY = date(2026, 10, 2)

# --------------------------------------------------------------------------- the documents

META_REVERSE_CHARGE = f"""Meta Platforms Ireland Limited
4 Grand Canal Square, Grand Canal Harbour, Dublin 2, Ireland
VAT Reg. No.: {META_VAT}

Bill to:
Hazel Tree Interiores, Lda.
Rua da Rosa 57, 1200-384 Lisboa, Portugal
VAT: PT{HAZEL_NIF}

Invoice number: FBADS-2026-0914
Invoice date: 14 September 2026
Advertising services, 1-14 September 2026
Subtotal: €310.00
VAT (0%): €0.00
Total: €310.00
Reverse charge: VAT to be accounted for by the recipient (Article 196, Directive 2006/112/EC).
Paid with the card ending 5530.
"""

META_IRISH_VAT = f"""Meta Platforms Ireland Limited
4 Grand Canal Square, Dublin 2, Ireland
VAT Reg. No.: {META_VAT}

Bill to:
Hazel Tree Interiores, Lda.
Lisboa, Portugal

Invoice number: FBADS-2026-0928
Invoice date: 28 September 2026
Subtotal: €200.00
VAT (23%): €46.00
Total: €246.00
"""

BOOKING_COMMISSION = f"""Booking.com B.V.
Herengracht 597, 1017 CE Amsterdam, The Netherlands
VAT number: {BOOKING_VAT}

Commission invoice
Invoice number: 1946-2026-09
Invoice date: 01/09/2026
Bill to:
Hazel Tree Interiores, Lda.
VAT number: PT{HAZEL_NIF}

Commission on reservations with check-out in August 2026
Subtotal: 312,40 EUR
VAT 0%: 0,00 EUR
Total amount due: 312,40 EUR
VAT reverse charged: the recipient accounts for the VAT (article 196 VAT Directive).
We will collect this amount by direct debit on 15/09/2026.
"""

AWS_INVOICE = f"""Amazon Web Services, Inc.
410 Terry Avenue North, Seattle, WA 98109-5210, United States
Federal Tax ID: {AWS_EIN}

Bill to:
Hazel Tree Interiores, Lda.
Rua da Rosa 57, 1200-384 Lisboa, Portugal
VAT: PT{HAZEL_NIF}

Invoice Number: 1432987654
Invoice Date: September 2, 2026
Billing period: August 1 - August 31, 2026
Subtotal: USD 125.00
Tax: USD 0.00
Total: USD 125.00
Amount due: USD 0.00
Charged to the card ending 5530 on September 3, 2026.
"""

UK_STUDIO = """Northlight Studio Ltd
12 Shoreditch High Street, London E1 6JE, United Kingdom
VAT Reg No: GB 284 7193 26

Invoice No: NLS-0917
Date of issue: 17/09/2026
Customer: Hazel Tree Interiores, Lda. (VAT PT516123459)
Photography workshop, London, 16 September 2026
Subtotal: £200.00
VAT @ 20%: £40.00
Total: £240.00
"""

SPANISH_HOTEL = f"""Hotel Mirador de la Alhambra S.L.
Calle Real 12, 18009 Granada, España
CIF: B-{HOTEL_CIF[1:]}

Factura nº: F-2026/0412
Fecha de emisión: 22/09/2026
Cliente: Hazel Tree Interiores, Lda.
NIF cliente: PT{HAZEL_NIF}
Concepto: Alojamiento, 2 noches (20-22/09/2026)
Base imponible: 180,00 €
IVA (10%): 18,00 €
Importe total: 198,00 €
Forma de pago: tarjeta
"""

GERMAN_WRONG_RATE = f"""Druckhaus Berlin GmbH
Friedrichstraße 100, 10117 Berlin, Germany
USt-IdNr.: {PRINT_VAT}

Invoice number: DB-2026-311
Invoice date: 10/09/2026
Printing of 500 catalogues
Subtotal: €100.00
VAT (23%): €23.00
Total: €123.00
"""


def card(bank_id: str, day: date, amount: str, who: str, description: str = "COMPRA CARTAO") -> BankRow:
    return BankRow(bank_id=bank_id, account_id="card-5530", booked_on=day, amount=D(amount), counterparty=who,
                   description=description, kind=K.CARD, card_last4="5530")


def tenant() -> Orchestrator:
    """Hazel Tree (Lisbon) and the suppliers it has abroad, learned from its history (§6)."""
    owner = OwnerProfile(first_name="Laura", full_name="Laura Medina", email=E.OWNER_EMAIL)
    repo = Repository(tenant_id=TENANT, owner=owner, now=local_datetime(date(2026, 9, 1), 7, 0))
    repo.add_company(id="hazel-tree", name="Hazel Tree", legal_name="Hazel Tree Interiores, Lda.", tax_id=HAZEL_NIF,
                     ibans=[E.HAZEL_IBAN])
    repo.add_account(Account(id="mbcp-ht", bank="Millennium BCP", holder_id="hazel-tree", iban=E.HAZEL_IBAN))
    repo.add_account(Account(id="card-5530", bank="Millennium BCP", holder_id="hazel-tree", card_last4="5530"))
    for supplier in (
        Supplier(id="sup-meta", tenant_id=TENANT, name="Meta", aliases=["FACEBK *ADS", "FACEBK ADS"],
                 tax_id=META_VAT, countries=["IE"]),
        Supplier(id="sup-booking", tenant_id=TENANT, name="Booking.com", aliases=["BOOKING.COM BV", "BOOKING.COM"],
                 tax_id=BOOKING_VAT, countries=["NL"]),
        Supplier(id="sup-aws", tenant_id=TENANT, name="AWS", aliases=["AWS EMEA", "AMAZON WEB SERVICES"],
                 tax_id=AWS_EIN, countries=["US"]),
        Supplier(id="sup-studio", tenant_id=TENANT, name="Northlight Studio", aliases=["NORTHLIGHT STUDIO"],
                 tax_id=STUDIO_VAT, countries=["GB"]),
        Supplier(id="sup-hotel", tenant_id=TENANT, name="Hotel Mirador", aliases=["HOTEL MIRADOR GRANADA"],
                 tax_id="ES" + HOTEL_CIF, countries=["ES"]),
        Supplier(id="sup-print", tenant_id=TENANT, name="Druckhaus Berlin", aliases=["DRUCKHAUS BERLIN"],
                 tax_id=PRINT_VAT, countries=["DE"]),
    ):
        repo.add_supplier(supplier)
    covered, synced = local_datetime(date(2026, 6, 1), 0, 0), local_datetime(TODAY, 8, 0)
    for id_, kind, name in (("gmail", "email", "Gmail"), ("millennium", "bank", "Millennium BCP")):
        repo.add_connector(ConnectorState(id=id_, name=name, kind=kind, account=name, company_ids=("hazel-tree",),
                                          healthy=True, covered_from=covered, covered_until=synced,
                                          last_synced_at=synced))
    repo.accountant = AccountantProfile(id="acct-vidal", firm="Contabilidade Vidal", person="Marc Vidal",
                                        email=E.ACCOUNTANT_EMAIL)
    return Orchestrator(repo)


def upload(o: Orchestrator, text: str, day: date, name: str = "invoice.txt") -> DocumentRecord:
    report = o.ingest_file(text.encode("utf-8"), filename=name, content_type="text/plain",
                           at=local_datetime(day, 10, 0))
    [doc_id] = report.document_ids
    return o.repo.documents[doc_id]


def pay(o: Orchestrator, row: BankRow) -> TxRecord:
    report = o.ingest_bank([row], at=local_datetime(row.booked_on, 18, 0))
    [tx_id] = report.transaction_ids
    return o.repo.transactions[tx_id]


INVOICE_FIELDS = (F.SUPPLIER_TAX_ID, F.INVOICE_NUMBER, F.ISSUE_DATE, F.CURRENCY, F.GROSS_AMOUNT, F.NET_AMOUNT,
                  F.VAT_AMOUNT)
# Keys whose values are ids, codes or dates, not words an owner reads (as in test_api_service).
_NOT_WORDS = {"id", "href", "companyId", "evidenceIds", "confirmOptionId", "optionId", "currentMonth", "months",
              "month", "key", "at", "date", "due", "lastSyncedAt", "pendingItemIds", "evidence", "documents",
              "transactions", "tone", "kind", "status", "currency", "head", "taxId"}


def owner_texts(body: object) -> list[str]:
    if isinstance(body, dict):
        return [t for k, v in body.items() if k not in _NOT_WORDS for t in owner_texts(v)]
    if isinstance(body, list):
        return [t for v in body for t in owner_texts(v)]
    return [body] if isinstance(body, str) else []


def value(doc: DocumentRecord, name: CriticalField) -> object:
    return doc.checks[name.value].value


def quality(doc: DocumentRecord, name: CriticalField) -> Quality:
    return doc.checks[name.value].quality


def closed(o: Orchestrator, *items: DocumentRecord | TxRecord) -> bool:
    return all(o.repo.items[i.item_id].stage is Stage.CLOSED for i in items)


# --------------------------------------------------------------------------- the issuer's country


@pytest.mark.parametrize(
    ("raw", "country", "valid"),
    [
        (META_VAT, "IE", True), ("IE 4817263A", "IE", False),
        (BOOKING_VAT, "NL", True), ("NL855661022B01", "NL", False),
        (PRINT_VAT, "DE", True), ("DE274163855", "DE", False),
        ("GB 284 7193 26", "GB", True), ("GB284719327", "GB", False),
        ("ES" + HOTEL_CIF, "ES", True), ("ESB76524132", "ES", False),
        ("IT00743110157", "IT", True), ("FR40303265045", "FR", True), ("BE0403019261", "BE", True),
        ("PT" + HAZEL_NIF, "PT", True), ("EL094259216", "GR", True),
        (AWS_EIN, "US", True), ("07-1234567", "US", False),
    ],
)
def test_vat_numbers_are_checked_by_their_own_countrys_rules(raw: str, country: str, valid: bool) -> None:
    number = check_tax_number(raw)
    assert number is not None and number.country == country and number.valid is valid
    if raw.startswith(("IE", "NL", "DE", "GB", "ES", "PT")):
        assert number.checksum is valid  # these countries' numbers carry check digits
    if raw == AWS_EIN:
        assert number.kind == "ein" and number.checksum is None  # an EIN has none: its format is all there is


@pytest.mark.parametrize(
    ("text", "country"),
    [
        (META_REVERSE_CHARGE, "IE"),  # its VAT number; the customer's PT number is ours, never the issuer's
        (BOOKING_COMMISSION, "NL"),
        (AWS_INVOICE, "US"),  # a US EIN
        (UK_STUDIO, "GB"),
        (SPANISH_HOTEL, "ES"),  # a Spanish CIF without its ES prefix, labelled
        ("Studio Nord\nKeizersgracht 1, Amsterdam, The Netherlands\nInvoice number: 7\nTotal: €10.00", "NL"),
        ("Invoice number: 7\nTotal: USD 12.00\n", "US"),  # nothing but the currency: the weakest signal
        (E.EDP_INVOICE.decode().split("Código QR")[0], "PT"),  # a labelled NIF, even without its QR code
        ("Invoice number: 7\nTotal: €10.00\n", None),  # nothing says where it comes from
        (f"VAT: {META_VAT}\nUSt-IdNr.: {PRINT_VAT}\nTotal: €10.00", None),  # two countries: never guess
    ],
)
def test_the_issuers_country_comes_from_its_own_details(text: str, country: str | None) -> None:
    issuer = detect_issuer(text, own_tax_ids=OWN)
    assert issuer.country == country
    assert issuer.is_foreign is (country not in (None, "PT"))
    assert detect_issuer(E.EDP_INVOICE.decode(), own_tax_ids=OWN, fiscal_qr=True).country == "PT"
    assert detect_issuer("", structured_tax_ids=[BOOKING_VAT]).country == "NL"  # an e-invoice's own copy


def test_parts_of_an_iban_are_never_read_as_a_vat_number() -> None:
    text = "Pay to IBAN BE71 0961 2345 6769 or PT50 0033 0000 4532 8817 1026 5\nATCUD: KX7Q2WPL-9"
    assert detect_issuer(text, own_tax_ids=OWN).tax_number is None


def test_vat_rates_for_eu_countries_and_the_uk() -> None:
    on = date(2026, 9, 15)
    assert vat_rates("NL", on) == (D("0.09"), D("0.21"))
    assert vat_rates("IE", on) == (D("0.048"), D("0.09"), D("0.135"), D("0.23"))
    assert vat_rates("GB", on) == (D("0.05"), D("0.20"))
    assert vat_rates("EL", on) == vat_rates("GR", on)
    assert D("0.23") not in vat_rates("DE", on)
    assert D("0.21") in vat_rates("RO", on) and D("0.19") in vat_rates("RO", date(2025, 3, 1))
    assert D("0.19") not in vat_rates("RO", on)  # Romania moved to 21% on 1 August 2025
    assert vat_rates("US", on) == () and vat_rates("PT", on) == ()  # Portugal has its own pack


@pytest.mark.parametrize(
    "text",
    [
        "Reverse charge: VAT to be accounted for by the recipient",
        "VAT reverse-charged (article 196)",
        "IVA - autoliquidação",
        "Inversión del sujeto pasivo",
        "Steuerschuldnerschaft des Leistungsempfängers",
        "BTW verlegd",
    ],
)
def test_reverse_charge_wording_in_several_languages(text: str) -> None:
    assert mentions_reverse_charge(text)
    assert not mentions_reverse_charge("VAT (23%): €46.00")


# --------------------------------------------------------------------------- reading English and Spanish text


def test_english_labels_keep_the_key_fields_on_one_line_or_many() -> None:
    text = ("Invoice No.: INV-2026-0042     Date: 09/03/2026\n"
            "Subtotal   100.00\nSales tax (0%): 0.00\nAmount paid: $100.00\nAmount due: $0.00\nNet 30 days\n")
    us = detect_issuer(text + "Seattle, WA 98101", own_tax_ids=OWN)
    fields = read_foreign_text(text, "ev", method=ExtractionMethod.EMBEDDED_TEXT, issuer=us)
    got = {f: [o.value for o in obs] for f, obs in fields.observations.items()}
    assert got[F.INVOICE_NUMBER] == ["INV-2026-0042"]
    assert got[F.ISSUE_DATE] == [date(2026, 9, 3)]  # a US date: month first
    assert got[F.NET_AMOUNT] == [D("100.00")] and got[F.VAT_AMOUNT] == [D("0.00")]
    assert got[F.GROSS_AMOUNT] == [D("100.00")]  # "Amount due: $0.00" on a paid invoice is not its total
    assert got[F.CURRENCY] == ["USD"]  # "$" on a US invoice
    assert fields.stated_rates == (D("0"),)
    assert all(o.location.startswith("text:line ") for obs in fields.observations.values() for o in obs)
    # The same text from nowhere in particular: "09/03/2026" could be two dates, so none is read.
    unknown = read_foreign_text(text, "ev", method=ExtractionMethod.EMBEDDED_TEXT, issuer=detect_issuer(text))
    assert F.ISSUE_DATE not in unknown.observations and F.CURRENCY not in unknown.observations


def test_spanish_invoice_text_keeps_its_key_fields() -> None:
    issuer = detect_issuer(SPANISH_HOTEL, own_tax_ids=OWN)
    assert issuer.country == "ES" and issuer.language == "es" and document_language(SPANISH_HOTEL) == "es"
    fields = read_foreign_text(SPANISH_HOTEL, "ev", method=ExtractionMethod.EMBEDDED_TEXT, issuer=issuer,
                               own_tax_ids=OWN)
    got = {f: [o.value for o in obs] for f, obs in fields.observations.items()}
    assert got[F.INVOICE_NUMBER] == ["F-2026/0412"]
    assert got[F.ISSUE_DATE] == [date(2026, 9, 22)]
    assert got[F.NET_AMOUNT] == [D("180.00")] and got[F.VAT_AMOUNT] == [D("18.00")]
    assert got[F.GROSS_AMOUNT] == [D("198.00")] and got[F.CURRENCY] == ["EUR"]
    assert got[F.SUPPLIER_TAX_ID] == ["ES" + HOTEL_CIF] and got[F.CUSTOMER_TAX_ID] == ["PT" + HAZEL_NIF]
    assert fields.stated_rates == (D("0.10"),)
    # The Portuguese reader finds none of it: that is why the issuer's country is decided first.
    portuguese = {o.field for o in extract_text_fields(SPANISH_HOTEL, "ev").observations}
    assert not {F.INVOICE_NUMBER, F.GROSS_AMOUNT, F.NET_AMOUNT} <= portuguese


def test_spanish_hotel_bill_closes_green_against_the_card_charge() -> None:
    o = tenant()
    doc = upload(o, SPANISH_HOTEL, date(2026, 9, 22), "Factura_F-2026-0412.txt")
    assert doc.is_foreign and doc.issuer.country == "ES" and doc.document.doc_type is DocumentType.INVOICE
    assert doc.supplier_id == "sup-hotel"
    assert doc.document.supplier_tax_id == "ES" + HOTEL_CIF and doc.document.entity_id == "hazel-tree"
    tx = pay(o, card("c-0922", date(2026, 9, 22), "-198.00", "HOTEL MIRADOR GRANADA"))
    assert tx.document_ids == [doc.id] and doc.document.quality is Quality.GREEN and closed(o, doc, tx)
    assert "10%" in doc.checks["vat_amount"].reasons[0] and "Spain" in doc.checks["vat_amount"].reasons[0]


# --------------------------------------------------------------------------- closing against the bank


def test_meta_ads_invoice_from_ireland_closes_green_against_the_card_charge() -> None:
    o = tenant()
    doc = upload(o, META_REVERSE_CHARGE, date(2026, 9, 14))
    assert doc.issuer.country == "IE" and doc.issuer.reverse_charge and doc.supplier_id == "sup-meta"
    # On its own the document has one source: nothing is confirmed yet, and nothing is guessed.
    assert doc.document.quality is Quality.AMBER and quality(doc, F.GROSS_AMOUNT) is Quality.AMBER
    assert value(doc, F.INVOICE_NUMBER) == "FBADS-2026-0914" and value(doc, F.ISSUE_DATE) == date(2026, 9, 14)
    tx = pay(o, card("c-0914", date(2026, 9, 14), "-310.00", "FACEBK *ADS"))
    assert tx.document_ids == [doc.id] and doc.matched_tx_ids == [tx.id]
    assert doc.document.quality is Quality.GREEN and closed(o, doc, tx)
    assert all(quality(doc, f) is Quality.GREEN for f in INVOICE_FIELDS)
    assert "bank" in doc.checks["gross_amount"].reasons[0]
    assert doc.checks["vat_amount"].reasons[0].startswith("It adds up to the total the bank charged, with no VAT")
    assert "check digits" in doc.checks["supplier_tax_id"].reasons[0]
    # The auditor re-checks it against its evidence and leaves it closed.
    assert o.run(local_datetime(TODAY, 9, 30)).reopened == [] and closed(o, doc, tx)


def test_irish_vat_charged_on_meta_ads_is_a_valid_irish_rate() -> None:
    o = tenant()
    doc = upload(o, META_IRISH_VAT, date(2026, 9, 28))
    tx = pay(o, card("c-0928", date(2026, 9, 28), "-246.00", "FACEBK *ADS"))
    assert doc.document.quality is Quality.GREEN and closed(o, doc, tx)
    assert doc.document.vat_amount == D("46.00")
    assert "23%" in doc.checks["vat_amount"].reasons[0] and "Ireland" in doc.checks["vat_amount"].reasons[0]


def test_booking_com_commission_from_the_netherlands_closes_against_the_direct_debit() -> None:
    o = tenant()
    doc = upload(o, BOOKING_COMMISSION, date(2026, 9, 1))
    assert doc.issuer.country == "NL" and doc.issuer.has_valid_vat_number and doc.issuer.reverse_charge
    assert value(doc, F.GROSS_AMOUNT) == D("312.40") and value(doc, F.ISSUE_DATE) == date(2026, 9, 1)
    tx = pay(o, BankRow(bank_id="dd-0915", account_id="mbcp-ht", booked_on=date(2026, 9, 15), amount=D("-312.40"),
                        counterparty="BOOKING.COM BV", description="SEPA DD BOOKING.COM COMISSAO",
                        kind=K.DIRECT_DEBIT))
    assert tx.document_ids == [doc.id] and doc.document.quality is Quality.GREEN and closed(o, doc, tx)
    assert "15 September 2026 fits this date" in doc.checks["issue_date"].reasons[0]


def test_aws_invoice_in_dollars_closes_against_the_euro_card_charge_and_its_fx_line() -> None:
    o = tenant()
    doc = upload(o, AWS_INVOICE, date(2026, 9, 2))
    assert doc.issuer.country == "US" and doc.document.currency == "USD" and doc.document.gross_amount == D("125.00")
    assert value(doc, F.ISSUE_DATE) == date(2026, 9, 2) and doc.supplier_id == "sup-aws"
    # The card was charged in euros; the bank's line says it was USD 125.00 at 0.9215.
    tx = pay(o, card("c-0903", date(2026, 9, 3), "-115.19", "AWS EMEA", "COMPRA AWS EMEA USD 125,00 TAXA 0,9215"))
    assert tx.tx.currency == "EUR" and tx.document_ids == [doc.id]
    assert doc.document.quality is Quality.GREEN and closed(o, doc, tx)
    bank = [o_ for o_ in doc.checks["gross_amount"].observations if o_.method is ExtractionMethod.BANK]
    assert [b.value for b in bank] == [D("125.00")]  # the dollars the bank declared, never the euros
    assert o.run(local_datetime(TODAY, 9, 30)).reopened == []  # the auditor compares like with like


def test_a_dollar_invoice_without_the_banks_fx_line_is_not_confirmed_by_a_euro_amount() -> None:
    o = tenant()
    doc = upload(o, AWS_INVOICE, date(2026, 9, 2))
    tx = pay(o, card("c-0903", date(2026, 9, 3), "-115.19", "AWS EMEA"))
    assert doc.document.quality is Quality.AMBER and not closed(o, tx) and not closed(o, doc)
    assert tx.document_ids == []


def test_uk_invoice_in_pounds_with_uk_vat_closes_against_the_euro_card_charge() -> None:
    o = tenant()
    doc = upload(o, UK_STUDIO, date(2026, 9, 17))
    assert doc.issuer.country == "GB" and doc.issuer.stated_rates == (D("0.20"),)
    assert doc.document.currency == "GBP" and doc.document.supplier_tax_id == STUDIO_VAT
    assert value(doc, F.INVOICE_NUMBER) == "NLS-0917" and value(doc, F.ISSUE_DATE) == date(2026, 9, 17)
    assert (value(doc, F.NET_AMOUNT), value(doc, F.VAT_AMOUNT)) == (D("200.00"), D("40.00"))
    tx = pay(o, card("c-0917", date(2026, 9, 17), "-281.21", "NORTHLIGHT STUDIO",
                     "COMPRA NORTHLIGHT STUDIO GBP 240,00 TAXA 1,1717"))
    assert doc.document.quality is Quality.GREEN and closed(o, doc, tx)
    assert "the United Kingdom" in doc.checks["vat_amount"].reasons[0]


def test_a_foreign_invoice_with_an_impossible_vat_rate_stays_amber() -> None:
    o = tenant()
    doc = upload(o, GERMAN_WRONG_RATE, date(2026, 9, 10))
    assert doc.issuer.country == "DE" and doc.issuer.has_valid_vat_number
    tx = pay(o, card("c-0910", date(2026, 9, 10), "-123.00", "DRUCKHAUS BERLIN"))
    # 23% is no German rate: the bank agrees on the total, yet the document is not proved.
    assert doc.document.quality is Quality.AMBER and quality(doc, F.VAT_AMOUNT) is Quality.AMBER
    assert any("rate" in r for r in doc.checks["vat_amount"].reasons)
    assert not closed(o, tx) and not closed(o, doc) and tx.document_ids == []
    assert tx.likely_document_ids == [doc.id]  # likely, never closure (§57)
    o.run(local_datetime(TODAY, 9, 30))
    [flag] = BackOfficeService(o).accountant_client("hazel-tree")["taxFlags"]
    assert flag["title"] == FOREIGN_VAT_FLAG
    assert flag["detail"] == ("Druckhaus Berlin (Germany) charged €23.00 of VAT on invoice DB-2026-311 (€123.00). "
                              "23% is not a VAT rate used in Germany.")


def test_an_eu_invoice_without_its_vat_number_stays_amber() -> None:
    o = tenant()
    text = BOOKING_COMMISSION.replace(f"VAT number: {BOOKING_VAT}", "Chamber of Commerce: 31047344")
    doc = upload(o, text, date(2026, 9, 1))
    assert doc.issuer.country == "NL" and doc.issuer.tax_number is None  # the address says the Netherlands
    tx = pay(o, BankRow(bank_id="dd-0915", account_id="mbcp-ht", booked_on=date(2026, 9, 15), amount=D("-312.40"),
                        counterparty="BOOKING.COM BV", kind=K.DIRECT_DEBIT))
    assert doc.document.quality is Quality.AMBER and not closed(o, tx)


# --------------------------------------------------------------------------- Portuguese invoices are unchanged


def test_portuguese_invoices_are_read_and_checked_by_the_portuguese_rules() -> None:
    demo = build_demo()
    repo = demo.repo
    assert repo.documents and all(d.issuer is not None and d.issuer.country == "PT" for d in repo.documents.values())
    assert not any(d.is_foreign for d in repo.documents.values())
    parsers = [json.loads(r.body).get("parser") or "" for r in repo.audit_store.records(repo.tenant_id)
               if json.loads(r.body)["action"] == "extract"]
    assert parsers and not any("intl_text_fields" in p for p in parsers)
    # A Portuguese invoice without its QR code is still Portuguese (its NIF), read by the Portuguese reader.
    text = E.EDP_INVOICE.decode().split("Código QR")[0]
    extracted = demo.documents.read([_Part("ev_edp", "text", text=text)])
    assert extracted is not None and extracted.issuer.country == "PT"
    assert extracted.parsers == ["pt_text_fields"]
    portuguese = extract_text_fields(text, "ev_edp", method=ExtractionMethod.EMBEDDED_TEXT,
                                     known_customer_tax_ids=repo.own_tax_ids()).observations
    assert sum(len(v) for k, v in extracted.observations.items() if k != "currency") == len(portuguese)
    # The demo's accountant view shows only what it showed before: the private landlord's rent.
    svc = BackOfficeService(demo)
    flags = [f["title"] for c in repo.companies for f in svc.accountant_client(c)["taxFlags"]]
    assert flags and all(f.startswith("Rent paid to a private landlord") for f in flags)


# --------------------------------------------------------------------------- VAT flags: the accountant only


def month_of_abroad() -> BackOfficeService:
    o = tenant()
    upload(o, META_REVERSE_CHARGE, date(2026, 9, 14), "meta1.txt")
    upload(o, META_IRISH_VAT, date(2026, 9, 28), "meta2.txt")
    upload(o, BOOKING_COMMISSION, date(2026, 9, 1), "booking.txt")
    upload(o, AWS_INVOICE, date(2026, 9, 2), "aws.txt")
    upload(o, UK_STUDIO, date(2026, 9, 17), "studio.txt")
    upload(o, SPANISH_HOTEL, date(2026, 9, 22), "hotel.txt")
    o.ingest_bank([
        card("c-0903", date(2026, 9, 3), "-115.19", "AWS EMEA", "COMPRA AWS EMEA USD 125,00 TAXA 0,9215"),
        card("c-0914", date(2026, 9, 14), "-310.00", "FACEBK *ADS"),
        BankRow(bank_id="dd-0915", account_id="mbcp-ht", booked_on=date(2026, 9, 15), amount=D("-312.40"),
                counterparty="BOOKING.COM BV", description="SEPA DD BOOKING.COM", kind=K.DIRECT_DEBIT),
        card("c-0917", date(2026, 9, 17), "-281.21", "NORTHLIGHT STUDIO", "NORTHLIGHT STUDIO GBP 240,00 TAXA 1,1717"),
        card("c-0922", date(2026, 9, 22), "-198.00", "HOTEL MIRADOR GRANADA"),
        card("c-0928", date(2026, 9, 28), "-246.00", "FACEBK *ADS"),
    ], at=local_datetime(date(2026, 9, 30), 23, 0))
    o.run(local_datetime(TODAY, 9, 30))
    return BackOfficeService(o)


def test_reverse_charge_and_foreign_vat_flags_reach_the_accountant_only() -> None:
    svc = month_of_abroad()
    repo = svc.repo
    assert all(closed(svc.orchestrator, d) for d in repo.documents.values())  # the whole month closed GREEN
    flags = svc.accountant_client("hazel-tree")["taxFlags"]
    by_title: dict[str, list[str]] = {}
    for flag in flags:
        by_title.setdefault(flag["title"], []).append(flag["detail"])
    reverse, foreign = by_title[REVERSE_CHARGE_FLAG], by_title[FOREIGN_VAT_FLAG]
    assert REVERSE_CHARGE_FLAG == "Possible reverse charge: VAT to be declared by you"
    assert FOREIGN_VAT_FLAG == "Foreign VAT charged — may be reclaimable abroad, not deductible in Portugal"
    assert len(reverse) == 3 and len(foreign) == 3
    assert any(d.startswith("Meta (Ireland), invoice FBADS-2026-0914") and "reverse-charged" in d for d in reverse)
    assert any(d.startswith("Booking.com (the Netherlands)") for d in reverse)
    assert any(d.startswith("AWS (the United States)") and "outside the EU" in d for d in reverse)
    assert any(d.startswith("Meta (Ireland) charged €46.00 of VAT on invoice FBADS-2026-0928") for d in foreign)
    assert any(d.startswith("Northlight Studio (the United Kingdom) charged £40.00") for d in foreign)
    assert any(d.startswith("Hotel Mirador (Spain) charged €18.00") for d in foreign)
    # Never for the owner: no question, no notice, no VAT mechanics in anything the owner reads.
    assert svc.dispatch("GET", "/api/needs-you", None)[1]["items"] == []
    for path in ("/api/home", "/api/needs-you", "/api/activity", "/api/companies/hazel-tree",
                 "/api/months/hazel-tree/2026-09", "/api/documents"):
        status, body = svc.dispatch("GET", path, None)
        assert status == 200, path
        for text in owner_texts(body):
            assert "reverse" not in text.lower() and "VAT to be declared" not in text, (path, text)
            assert "reclaimable" not in text and not find_jargon(text), (path, text, find_jargon(text))
    assert find_jargon(REVERSE_CHARGE_FLAG) == ["reverse charge"]  # the owner's language check catches it


# --------------------------------------------------------------------------- unusual currency: a check


HAZEL = LegalEntity(id="hazel-tree", tenant_id=TENANT, name="Hazel Tree", country="PT", tax_id=HAZEL_NIF)
META = Supplier(id="sup-meta", tenant_id=TENANT, name="Meta", tax_id=META_VAT, countries=["IE"],
                known_ibans=["IE29 AIBK 9311 5212 3456 78"])


def meta_invoice(n: int, currency: str = "EUR", amount: str = "250.00", iban: str | None = None) -> Document:
    return Document(id=f"doc_{n}", tenant_id=TENANT, evidence_ids=[f"ev_{n}"], doc_type=DocumentType.INVOICE,
                    supplier_name="Meta", supplier_tax_id=META_VAT, invoice_number=f"FBADS-{n}",
                    issue_date=date(2026, 1 + n % 9, 14), currency=currency, gross_amount=D(amount), iban=iban)


HISTORY = [meta_invoice(i) for i in range(1, 6)]


def test_an_invoice_in_a_currency_the_supplier_never_used_is_a_check_not_a_hard_stop() -> None:
    result = assess(FraudCase(entities=[HAZEL], supplier=META, document=meta_invoice(9, "USD"), history=HISTORY))
    [signal] = result.of_kind(SignalKind.UNUSUAL_CURRENCY)
    assert signal.severity is Severity.WARNING and not result.hard_stop and result.owner_message is None
    assert signal.owner_line == "Meta usually bills you in euros. This invoice is in US dollars."
    assert not find_jargon(signal.owner_line)
    # Not enough history to know what is usual: nothing is said.
    few = assess(FraudCase(entities=[HAZEL], supplier=META, document=meta_invoice(9, "USD"), history=HISTORY[:4]))
    assert not few.of_kind(SignalKind.UNUSUAL_CURRENCY)
    # A currency used before is not unusual.
    usual = assess(FraudCase(entities=[HAZEL], supplier=META, document=meta_invoice(9), history=HISTORY))
    assert not usual.of_kind(SignalKind.UNUSUAL_CURRENCY)


def test_an_unusual_currency_next_to_changed_bank_details_is_a_hard_stop() -> None:
    new_account = "GB82 WEST 1234 5698 7654 32"
    result = assess(FraudCase(entities=[HAZEL], supplier=META, history=HISTORY,
                              document=meta_invoice(9, "USD", iban=new_account)))
    assert result.of_kind(SignalKind.CHANGED_IBAN) and result.hard_stop
    signals = result.of_kind(SignalKind.UNUSUAL_CURRENCY)
    assert signals and all(s.severity is Severity.HIGH for s in signals)
    assert "The bank account is in the United Kingdom, which uses pounds, but the invoice is in US dollars." in [
        s.owner_line for s in signals]


def test_bank_details_whose_country_does_not_fit_the_currency_are_a_check() -> None:
    usual_account = META.known_ibans[0]
    result = assess(FraudCase(entities=[HAZEL], supplier=META, history=HISTORY[:2],
                              document=meta_invoice(9, "GBP", iban=usual_account)))
    [signal] = result.of_kind(SignalKind.UNUSUAL_CURRENCY)
    assert signal.severity is Severity.WARNING and not result.hard_stop
    assert signal.owner_line == "The bank account is in Ireland, which uses euros, but the invoice is in pounds."
    # The same account in its own currency, or a currency it took before, is nothing new.
    assert not assess(FraudCase(entities=[HAZEL], supplier=META, history=HISTORY[:2],
                                document=meta_invoice(9, iban=usual_account))).of_kind(SignalKind.UNUSUAL_CURRENCY)
    before = [*HISTORY[:2], meta_invoice(7, "GBP", iban=usual_account)]
    assert not assess(FraudCase(entities=[HAZEL], supplier=META, history=before,
                                document=meta_invoice(9, "GBP", iban=usual_account))).of_kind(
        SignalKind.UNUSUAL_CURRENCY)


def test_the_owner_sees_an_unusual_currency_as_a_check_and_the_accountant_as_an_anomaly() -> None:
    o = tenant()
    for i, day in enumerate((date(2026, 4, 14), date(2026, 5, 14), date(2026, 6, 14), date(2026, 7, 14),
                             date(2026, 8, 14)), start=1):
        text = META_REVERSE_CHARGE.replace("FBADS-2026-0914", f"FBADS-2026-0{i}14").replace(
            "14 September 2026", f"{day.day} {day.strftime('%B')} 2026")
        upload(o, text, day, f"meta{i}.txt")
    dollars = META_REVERSE_CHARGE.replace("€", "USD ").replace("FBADS-2026-0914", "FBADS-2026-0915")
    doc = upload(o, dollars, date(2026, 9, 15), "meta-usd.txt")
    assert doc.document.currency == "USD" and not doc.on_hold and not doc.fraud.hard_stop
    [signal] = doc.fraud.of_kind(SignalKind.UNUSUAL_CURRENCY)
    line = "Meta usually bills you in euros. This invoice is in US dollars."
    assert signal.owner_line == line
    assert f"Checked the Meta invoice. {line}" in [a.text for a in o.repo.activity]
    assert not any(n.subject_id == doc.id for n in o.repo.needs.values())  # a check, not a question
    o.run(local_datetime(TODAY, 9, 30))
    svc = BackOfficeService(o)
    anomalies = svc.accountant_client("hazel-tree")["anomalies"]
    assert {"id": f"an_currency_{doc.id}", "title": "Meta invoice in an unusual currency", "detail": line,
            "tone": "attention"} in anomalies
