"""Prices on invoice lines (QA X20; case 3, the bakery: "raw-material prices change constantly... price anomaly
detection").

* Lines are read from e-invoices (UBL: quantity, unit, unit price, line total, VAT rate, the seller's product code)
  and from the text of Portuguese, Spanish and English invoices (a PDF's text layer, an uploaded text, an invoice
  written in an email body) where they are laid out as a table.
* Robustly: a row holds only when quantity × unit price (less a printed discount) is its total, and the rows are
  used only when they add up to the invoice's own net at each VAT rate. Otherwise nothing is read from them: a
  price is never guessed.
* A price history per supplier and product: the same product is matched by the supplier's code when printed,
  else by its normalised description; prices are compared per kilo or litre when the unit or pack size says so.
* A unit price 15% or more above the average of the last 3 purchases (both configurable) is one plain line where
  price increases already appear (Ask "what got more expensive?", the accountant's view, the audit), never a hold.
* "How much did flour cost per kg this year?" is answered from those lines.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from _server_support import bearer, harness, signup

from backoffice.countries.pt.nif import nif_check_digit
from backoffice.demo.evidence import qr_payload
from backoffice.extraction.invoicelines import read_priced_lines
from backoffice.language import find_jargon
from backoffice.line_prices import (
    COMPARE_LAST,
    UNUSUAL_CHANGE,
    check_lines,
    parse_number,
    product_key,
    rate_parts_from_text,
    read_text_lines,
    unit_money,
)
from backoffice.orchestrator import TZ
from backoffice.server.events import state_digest
from backoffice.server.runtime import TenantManager
from backoffice.service import BackOfficeService

D = Decimal
NOW = datetime(2026, 10, 2, 9, 30, tzinfo=TZ)
PADARIA = "516123459"


def nif(first_eight: str) -> str:
    return first_eight + str(nif_check_digit(first_eight))


MOAGEM = nif("50811122")  # the flour mill
LACTICINIOS = nif("50922233")  # the dairy
IBERICA = "B12345674"  # a Spanish supplier (CIF)


def pt(value: Decimal) -> str:
    """1234.5 -> '1.234,50' (as a Portuguese or Spanish invoice prints it)."""
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def bakery() -> BackOfficeService:
    svc = BackOfficeService.new_tenant("t-padaria", owner_name="Ana Silva", owner_email="ana@padaria.pt", now=NOW)
    svc.add_company("Padaria Lda", PADARIA, "Padaria Lda")
    return svc


def flour_invoice(number: int, day: date, flour_price: str, *, qr: bool = True, flour_kg: int = 500,
                  net_off: Decimal = D("0")) -> bytes:
    """Moagem do Norte's invoice to the bakery: flour and yeast at 6%, paper bags at 23%, laid out as a table.

    ``net_off``: the printed 6% net differs from its lines by this much (a table that does not add up)."""
    flour = (D(flour_kg) * D(flour_price)).quantize(D("0.01"))
    yeast, bags = D("62.00"), D("50.00")
    base6 = flour + yeast + net_off
    vat6 = (base6 * D("0.06")).quantize(D("0.01"))
    vat23 = D("11.50")
    total = base6 + vat6 + bags + vat23
    lines = [
        "Moagem do Norte, Lda.", "Zona Industrial de Trofa, 4785-000 Trofa", f"NIF: {MOAGEM}",
        f"Fatura n.º FT MN2026/{number}", f"ATCUD: MNQ7K2MX-{number}", f"Data de emissão: {day:%d/%m/%Y}",
        "Cliente: Padaria Lda", f"NIF: {PADARIA}", "",
        "Código   Descrição                  Qtd.   Un.   Preço Unit.   IVA    Total",
        f"FAR65    Farinha de trigo T65       {flour_kg}    kg    {pt(D(flour_price))}          6%     {pt(flour)}",
        "FERM1    Fermento fresco            20     kg    3,10          6%     62,00",
        "SAC01    Sacos de papel             1000   un    0,05          23%    50,00",
        "",
        f"Base tributável (6%): {pt(base6)}", f"IVA 6%: {pt(vat6)}",
        "Base tributável (23%): 50,00", "IVA 23%: 11,50", f"Total: {pt(total)} €",
    ]
    if qr:
        lines.append("Código QR: " + qr_payload(
            A=MOAGEM, B=PADARIA, C="PT", D="FT", E="N", F=f"{day:%Y%m%d}", G=f"FT MN2026/{number}",
            H=f"MNQ7K2MX-{number}", I1="PT", I3=f"{base6:.2f}", I4=f"{vat6:.2f}", I7="50.00", I8="11.50",
            N=f"{vat6 + vat23:.2f}", O=f"{total:.2f}", Q="aB3d", R="1234"))
    return ("\n".join(lines) + "\n").encode()


def upload(svc: BackOfficeService, name: str, data: bytes, content_type: str = "text/plain") -> dict[str, Any]:
    out = svc.upload_evidence(name, content_type, data)
    assert out["documents"], out
    return out


def record(svc: BackOfficeService, out: dict[str, Any]) -> Any:
    return svc.repo.documents[out["documents"][0]["id"]]


def four_flour_invoices(svc: BackOfficeService, last_price: str = "0.97") -> list[dict[str, Any]]:
    return [upload(svc, f"moagem-{i}.txt", flour_invoice(101 + i, date(2026, 9, day), price))
            for i, (day, price) in enumerate(((1, "0.80"), (8, "0.82"), (15, "0.81"), (22, last_price)))]


def plain(text: str) -> None:
    assert not find_jargon(text), (text, find_jargon(text))


# --------------------------------------------------------------------------- e-invoices


def ubl_invoice(number: str, day: date, *, flour_price: str = "20.50", bags: int = 20) -> bytes:
    """A UBL 2.1 invoice: flour in 25 kg bags (price per bag, the seller's code) and butter by the kilo."""
    flour = (D(bags) * D(flour_price)).quantize(D("0.01"))
    butter = D("12") * D("6.40")
    base6 = flour + butter
    vat6 = (base6 * D("0.06")).quantize(D("0.01"))
    total = base6 + vat6
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017</cbc:CustomizationID>
  <cbc:ID>{number}</cbc:ID>
  <cbc:IssueDate>{day.isoformat()}</cbc:IssueDate>
  <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyName><cbc:Name>Lacticínios do Vale, Lda.</cbc:Name></cac:PartyName>
    <cac:PartyTaxScheme><cbc:CompanyID>PT{LACTICINIOS}</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cac:PartyLegalEntity><cbc:RegistrationName>Padaria Lda</cbc:RegistrationName></cac:PartyLegalEntity>
    <cac:PartyTaxScheme><cbc:CompanyID>PT{PADARIA}</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
  </cac:Party></cac:AccountingCustomerParty>
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="EUR">{vat6:.2f}</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="EUR">{base6:.2f}</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="EUR">{vat6:.2f}</cbc:TaxAmount>
      <cac:TaxCategory><cbc:ID>AA</cbc:ID><cbc:Percent>6</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory>
    </cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="EUR">{base6:.2f}</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="EUR">{base6:.2f}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="EUR">{total:.2f}</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="EUR">{total:.2f}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  <cac:InvoiceLine>
    <cbc:ID>1</cbc:ID>
    <cbc:InvoicedQuantity unitCode="XBG">{bags}</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="EUR">{flour:.2f}</cbc:LineExtensionAmount>
    <cac:Item>
      <cbc:Name>Farinha T65 saco 25 kg</cbc:Name>
      <cac:SellersItemIdentification><cbc:ID>F65-25</cbc:ID></cac:SellersItemIdentification>
      <cac:ClassifiedTaxCategory><cbc:ID>AA</cbc:ID><cbc:Percent>6</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory>
    </cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="EUR">{flour_price}</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>
  <cac:InvoiceLine>
    <cbc:ID>2</cbc:ID>
    <cbc:InvoicedQuantity unitCode="KGM">12</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="EUR">{butter:.2f}</cbc:LineExtensionAmount>
    <cac:Item>
      <cbc:Name>Manteiga sem sal</cbc:Name>
      <cac:SellersItemIdentification><cbc:ID>MAN-01</cbc:ID></cac:SellersItemIdentification>
      <cac:ClassifiedTaxCategory><cbc:ID>AA</cbc:ID><cbc:Percent>6</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory>
    </cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="EUR">6.40</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>
</Invoice>
""".encode()


def test_e_invoice_lines_are_read_with_unit_prices_units_and_product_codes() -> None:
    raw = read_priced_lines(ubl_invoice("FT LV2026/7", date(2026, 9, 3)))
    assert [(x.code, x.quantity, x.unit_code, x.price, x.net, x.vat_rate) for x in raw] == [
        ("F65-25", D("20"), "XBG", D("20.50"), D("410.00"), D("6")),
        ("MAN-01", D("12"), "KGM", D("6.40"), D("76.80"), D("6"))]
    svc = bakery()
    out = upload(svc, "fatura.xml", ubl_invoice("FT LV2026/7", date(2026, 9, 3)), "application/xml")
    found = svc.orchestrator.line_prices.invoice_lines(record(svc, out))
    assert found is not None and found.source == "e-invoice"
    assert found.check == "The lines add up to the invoice's net: 6% €486.80."
    assert [(x.description, x.unit, x.unit_price, x.code) for x in found.lines] == [
        ("Farinha T65 saco 25 kg", "bag", D("20.50"), "F65-25"), ("Manteiga sem sal", "kg", D("6.40"), "MAN-01")]
    # A 25 kg bag at €20.50 is €0.82 a kilo: prices are compared per kilo whenever the invoice says how much.
    flour = next(p for p in svc.orchestrator.line_prices.purchases() if p.code == "F65-25")
    assert (flour.basis, flour.unit_price, flour.quantity, flour.per) == ("kg", D("0.82"), D("500"), "per kg")


# --------------------------------------------------------------------------- tables in text, three languages


def test_text_tables_in_portuguese_spanish_and_english_invoices_are_read_when_they_add_up() -> None:
    # Portuguese: a code column, the VAT rate before the total, two rates (from the invoice's fiscal QR code).
    svc = bakery()
    found = svc.orchestrator.line_prices.invoice_lines(record(svc, upload(
        svc, "moagem.txt", flour_invoice(101, date(2026, 9, 1), "0.82"))))
    assert found is not None and found.source == "text"
    assert [(x.code, x.description, x.quantity, x.unit, x.unit_price, x.vat_rate) for x in found.lines] == [
        ("FAR65", "Farinha de trigo T65", D("500"), "kg", D("0.82"), D("6")),
        ("FERM1", "Fermento fresco", D("20"), "kg", D("3.10"), D("6")),
        ("SAC01", "Sacos de papel", D("1000"), "unit", D("0.05"), D("23"))]
    assert found.check == "The lines add up to the invoice's net: 6% €472.00, 23% €50.00."
    # Without its QR code, the rates the invoice prints ("Base tributável (6%): ...") say the same.
    text = flour_invoice(102, date(2026, 9, 8), "0.82", qr=False).decode()
    assert [(p.rate, p.net, p.vat) for p in rate_parts_from_text(text, D("561.82"))] == [
        (D("6"), D("472.00"), D("28.32")), (D("23"), D("50.00"), D("11.50"))]
    found = svc.orchestrator.line_prices.invoice_lines(record(svc, upload(svc, "moagem-2.txt", text.encode())))
    assert found is not None and len(found.lines) == 3

    # Spanish: "Cantidad Precio Dto. IVA Importe", a discount in percent, and a summary table of the rates.
    spanish = "\n".join([
        "Suministros Iberia S.L.", f"CIF: {IBERICA}", "Calle Mayor 10, 28013 Madrid, España", "",
        "FACTURA", "Factura nº: A-2026/0415          Fecha: 10/09/2026", "", "Cliente: Padaria Lda",
        f"NIF: PT{PADARIA}", "",
        "Referencia  Descripción              Cantidad  Precio   Dto.   IVA    Importe",
        "HAR-55      Harina de trigo 55       200       0,90     5%     10%    171,00",
        "AZU-01      Azúcar blanco            100       1,20     0%     10%    120,00",
        "CAJ-20      Cajas de cartón          50        0,80     0%     21%    40,00",
        "", "Tipo   Base imponible   Cuota IVA", "10%    291,00           29,10", "21%    40,00            8,40",
        "Total factura: 368,50 €"]) + "\n"
    found = svc.orchestrator.line_prices.invoice_lines(record(svc, upload(svc, "iberia.txt", spanish.encode())))
    assert found is not None, "the Spanish table adds up to its rates"
    assert [(x.code, x.quantity, x.unit_price, x.net, x.vat_rate) for x in found.lines] == [
        ("HAR-55", D("200"), D("0.855"), D("171.00"), D("10")), ("AZU-01", D("100"), D("1.20"), D("120.00"), D("10")),
        ("CAJ-20", D("50"), D("0.80"), D("40.00"), D("21"))]

    # English: dot decimals, the unit price before the quantity, one VAT rate (Subtotal and VAT as read).
    english = "\n".join([
        "Northern Mills Ltd", "VAT number: GB123456789", "Invoice number: NM-2026-0088",
        "Invoice date: 14 September 2026", "Billed to: Padaria Lda, VAT PT516123459", "",
        "Item                         Unit price   Qty   Amount", "Rye flour (25 kg bag)        21.00        10    210.00",
        "Spelt flour (10 kg bag)      14.50        4     58.00", "",
        "Subtotal: €268.00", "VAT (0%): €0.00", "Total: €268.00"]) + "\n"
    lines = read_text_lines(english)
    assert [(x.description, x.quantity, x.unit_price) for x in lines] == [
        ("Rye flour (25 kg bag)", D("10"), D("21.00")), ("Spelt flour (10 kg bag)", D("4"), D("14.50"))]
    found = svc.orchestrator.line_prices.invoice_lines(record(svc, upload(svc, "mills.txt", english.encode())))
    assert found is not None and found.check == "The lines add up to the invoice's net: €268.00."
    rye = next(p for p in svc.orchestrator.line_prices.purchases() if p.product.startswith("Rye"))
    assert (rye.basis, rye.unit_price, rye.quantity) == ("kg", D("0.84"), D("250"))


def test_an_invoice_written_in_the_email_body_is_read_too() -> None:
    svc = bakery()
    message = EmailMessage()
    message["From"], message["To"] = "faturacao@moagem.pt", "ana@padaria.pt"
    message["Subject"], message["Message-ID"] = "Fatura FT MN2026/120", "<ft-120@moagem.pt>"
    message["Date"] = "Tue, 08 Sep 2026 10:00:00 +0100"
    message.set_content("Bom dia,\n\nSegue a fatura da entrega de hoje.\n\n"
                        + flour_invoice(120, date(2026, 9, 8), "0.82").decode() + "\nCumprimentos,\nMoagem do Norte\n")
    out = upload(svc, "fatura.eml", message.as_bytes(), "message/rfc822")
    doc = record(svc, out)
    assert doc.evidence_ids == out["evidenceIds"][:1]  # the email itself is the evidence
    found = svc.orchestrator.line_prices.invoice_lines(doc)
    assert found is not None and [x.code for x in found.lines] == ["FAR65", "FERM1", "SAC01"]


# --------------------------------------------------------------------------- never guessed


def test_lines_that_do_not_add_up_are_never_used() -> None:
    svc = bakery()
    lp = svc.orchestrator.line_prices
    # The printed 6% net is one cent more than the lines at 6%: the table was not read right, so nothing is used.
    off = upload(svc, "off.txt", flour_invoice(130, date(2026, 9, 1), "0.82", qr=False, net_off=D("0.01")))
    assert lp.invoice_lines(record(svc, off)) is None
    # A row whose own numbers do not hold (500 × 0,82 is not 420,00) is not a row; without it the rest cannot add up.
    text = flour_invoice(131, date(2026, 9, 8), "0.82").decode().replace("410,00", "420,00")
    wrong = upload(svc, "wrong.txt", text.encode())
    assert [x.code for x in read_text_lines(text)] == ["FERM1", "SAC01"]
    assert lp.invoice_lines(record(svc, wrong)) is None
    # No price was taken from either: the history is empty and nothing is claimed about flour.
    assert lp.purchases() == [] and lp.changes() == []
    answer = svc.ask("How much did flour cost per kg this year?")["answer"]
    assert "€0.82" not in answer and "€0.84" not in answer
    # Lines and totals that disagree on the rates never pass either.
    lines = read_text_lines(flour_invoice(132, date(2026, 9, 1), "0.82").decode())
    assert check_lines(lines, rate_parts_from_text(flour_invoice(132, date(2026, 9, 1), "0.82", qr=False).decode(),
                                                   D("561.82")))
    assert check_lines(lines[:2], rate_parts_from_text(flour_invoice(132, date(2026, 9, 1), "0.82", qr=False)
                                                       .decode(), D("561.82"))) is None
    # Numbers are read as the document prints them, never half-read.
    assert parse_number("1.250,50", ",") == (D("1250.50"), 2) and parse_number("1,250.50", ".") == (D("1250.50"), 2)
    assert parse_number("0,820", ",") == (D("0.820"), 3) and parse_number("12,5,0", ",") is None


# --------------------------------------------------------------------------- the history


def test_the_same_product_is_matched_by_its_code_else_by_its_normalised_description() -> None:
    assert product_key("Farinha de Trigo T65 (saco 25 kg)") == product_key("FARINHA TRIGO T65") == "farinha trigo t65"
    svc = bakery()
    four_flour_invoices(svc)
    history = svc.orchestrator.line_prices.history()
    flour = [g for key, g in history.items() if key[1] == "code:FAR65"]
    assert len(flour) == 1 and [p.unit_price for p in flour[0]] == [D("0.80"), D("0.82"), D("0.81"), D("0.97")]
    # Another supplier's invoice without codes: matched by description, and apart from the mill's.
    no_code = flour_invoice(140, date(2026, 9, 25), "0.99").decode().replace("FAR65    ", "").replace(
        "FERM1    ", "").replace("SAC01    ", "").replace("Código   ", "").replace(MOAGEM, LACTICINIOS).replace(
        "Moagem do Norte, Lda.", "Lacticínios do Vale, Lda.")
    upload(svc, "vale.txt", no_code.encode())
    upload(svc, "vale-2.txt", no_code.replace("MN2026/140", "MN2026/141").replace("25/09/2026", "29/09/2026")
           .replace("Farinha de trigo T65", "FARINHA TRIGO T65").encode())
    groups = svc.orchestrator.line_prices.history()
    vale = [g for key, g in groups.items() if key[0] == f"tax:{LACTICINIOS}" and key[1] == "name:farinha trigo t65"]
    assert len(vale) == 1 and len(vale[0]) == 2 and {p.supplier for p in vale[0]} == {"Lacticínios do Vale"}
    # The mill's code joins its lines printed without a code on a later invoice.
    later = flour_invoice(150, date(2026, 9, 29), "0.83").decode().replace("FAR65    ", "")
    later = later.replace("FERM1    ", "").replace("SAC01    ", "").replace("Código   ", "")
    upload(svc, "moagem-late.txt", later.encode())
    mill = svc.orchestrator.line_prices.history()[(f"tax:{MOAGEM}", "code:FAR65", "kg")]
    assert [p.invoice for p in mill][-1] == "FT MN2026/150"


def test_an_unusual_unit_price_rise_is_one_plain_line_where_price_increases_appear_and_never_a_hold() -> None:
    svc = bakery()
    outs = four_flour_invoices(svc)
    lp = svc.orchestrator.line_prices
    [change] = lp.changes()
    assert (UNUSUAL_CHANGE, COMPARE_LAST) == (D("0.15"), 3)
    assert (change.before, change.after, change.percent, len(change.previous)) == (D("0.81"), D("0.97"), 20, 3)
    line = "Farinha de trigo T65 (Moagem do Norte) €0.81 → €0.97 per kg, up 20%"
    assert change.line() == line
    # Configurable: a higher threshold or a shorter memory.
    assert lp.changes(threshold=D("0.25")) == []
    assert [c.before for c in lp.changes(window=1)] == [D("0.81")]
    # Ask "what got more expensive?".
    ask = svc.ask("What got more expensive?")
    assert ask["answer"] == ("One price on your invoices went up 15% or more against the last purchases: "
                             f"{line}.")
    assert ask["evidence"] == [{"label": "Moagem do Norte FT MN2026/104", "id": outs[3]["evidenceIds"][0]}]
    plain(ask["answer"])
    # The accountant's view of September.
    status, client = svc.dispatch("GET", "/api/accountant/clients/padaria-lda")
    assert status == 200
    [anomaly] = [a for a in client["anomalies"] if a["id"].startswith("an_unit_")]
    assert anomaly["title"] == "Farinha de trigo T65 price went up 20%" and anomaly["tone"] == "attention"
    assert anomaly["detail"] == ("Moagem do Norte: €0.81 → €0.97 per kg on invoice FT MN2026/104; the last 3 "
                                 "purchases averaged €0.81 per kg.")
    plain(anomaly["title"] + " " + anomaly["detail"])
    # The business audit.
    audit = svc.dispatch("GET", "/api/audit")[1]
    increased = next(f for f in audit["findings"] if f["id"] == "f_increased")
    assert (increased["value"], increased["tone"], increased["examples"]) == ("1", "attention", [line])
    assert audit["lines"][-1] == f"One price on invoices went up 15% or more: {line}"
    # Never a hold, never a question.
    assert not any(d.on_hold for d in svc.repo.documents.values())
    assert svc.needs_you()["items"] == []
    # A price back to normal is no longer "more expensive".
    upload(svc, "moagem-5.txt", flour_invoice(105, date(2026, 9, 29), "0.84"))
    assert "invoices" not in svc.ask("What got more expensive?")["answer"]


def test_how_much_flour_cost_per_kg_this_year_is_answered_from_the_lines() -> None:
    svc = bakery()
    outs = four_flour_invoices(svc)
    for question in ("How much did flour cost per kg this year?", "Quanto custou a farinha por kg este ano?"):
        answer = svc.ask(question)
        assert answer["answer"].endswith("The last was €0.97 per kg on 22 September (FT MN2026/104)."), answer
        assert "€0.85 per kg on average in 2026 so far: 2,000 kg on 4 invoices from Moagem do Norte, from €0.80 " \
               "to €0.97 per kg" in answer["answer"]
        assert [e["id"] for e in answer["evidence"]] == [o["evidenceIds"][0] for o in reversed(outs)]
        plain(answer["answer"])
    assert svc.ask("How much did flour cost per kg this year?")["answer"].startswith("Flour cost €0.85 per kg")
    # A period without flour, and a product never bought: plain, never a guess.
    assert svc.ask("How much did flour cost per kg in August?")["answer"] == \
        "I can't see flour on your invoices in August."
    assert "sugar" not in svc.ask("How much did sugar cost per kg this year?")["answer"].lower()
    # The chat answers the same, from the same lines.
    chat = svc.chat({"message": "What did yeast cost per kg in September?"})
    assert chat["reply"].startswith("Yeast cost €3.10 per kg on average in September: 80 kg on 4 invoices")
    assert unit_money(D("0.8235")) == "€0.824" and unit_money(D("20.5")) == "€20.50"


def test_the_demo_is_unchanged_by_line_prices() -> None:
    svc = BackOfficeService.demo()
    assert svc.orchestrator.line_prices.purchases() == []
    answer = svc.ask("What got more expensive?")["answer"]
    assert "invoices" not in answer and "per kg" not in answer
    increased = next(f for f in svc.dispatch("GET", "/api/audit")[1]["findings"] if f["id"] == "f_increased")
    assert all("per kg" not in e for e in increased["examples"])


def test_line_prices_are_only_read_and_a_replay_rebuilds_the_same_business(tmp_path: Path) -> None:
    h = harness(tmp_path)  # strict reads: any read that changed the business would fail here
    account = signup(h.client)
    H, tenant = bearer(account["token"]), account["tenant"]["id"]
    for i, (day, price) in enumerate(((1, "0.80"), (8, "0.82"), (15, "0.81"), (22, "0.97"))):
        res = h.client.post("/api/evidence", files={"file": (f"moagem-{i}.txt",
                                                            flour_invoice(101 + i, date(2026, 9, day), price),
                                                            "text/plain")}, headers=H)
        assert res.status_code == 200 and res.json()["documents"], res.text
    h.clock.step = h.clock.step * 0
    with h.manager.open(tenant) as rt:
        before = state_digest(rt.service)
    count = len(h.store.events(tenant))
    assert "up 20%" in h.client.get("/api/audit", headers=H).text
    assert "Farinha de trigo T65 price went up 20%" in h.client.get("/api/accountant/clients/padaria-lda",
                                                                    headers=H).text
    tool = h.client.post("/api/chat/tool", json={"name": "recurring_costs", "input": {}}, headers=H).json()
    assert tool["result"]["unit_price_increases"][0]["percent"] == 20
    with h.manager.open(tenant) as rt:
        assert state_digest(rt.service) == before and len(h.store.events(tenant)) == count
    answer = h.client.post("/api/ask", json={"question": "How much did flour cost per kg this year?"}, headers=H)
    assert answer.json()["answer"].startswith("Flour cost €0.85 per kg")
    with h.manager.open(tenant) as rt:
        live = state_digest(rt.service)
    fresh = TenantManager(h.store, h.objects, now=h.clock, strict_reads=True)
    with fresh.open(tenant) as rt:
        assert state_digest(rt.service) == live
        assert [c.percent for c in rt.service.orchestrator.line_prices.changes()] == [20]
