"""The demo tenant's raw evidence, built as real files (§7).

Everything here is what a real connector or the owner would hand over: AT
fiscal QR payloads (checked with the Portugal pack's own parser, NIF check
digits valid), invoice text layers, a UBL 2.1 e-invoice, RFC 5322 emails
(one with a "View invoice" button), a tax letter and bank-feed rows. The
orchestrator processes them exactly like live evidence; no number shown in
the app is typed in by hand.

All companies, people, tax numbers, IBANs and phone numbers are fictional.
Supplier brand names appear only because the owner's statements would show them.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage
from email.utils import format_datetime

from backoffice.countries.pt.qr import FIELD_ORDER
from backoffice.domain.models import TransactionKind

from ..orchestrator import BankRow

# --------------------------------------------------------------------------- identities (fictional)

HAZEL_NIF = "516123459"
COMPANY_B_NIF = "514987650"
COMPANY_C_NIF = "517003210"

HAZEL_IBAN = "PT50003300004532881710265"
COMPANY_C_IBAN = "PT50003300004532990123382"
COMPANY_B_IBAN = "PT50003504120005678123007"

VODAFONE_NIF = "503161233"
VODAFONE_IBAN = "PT50003300001004402057741"
VODAFONE_NEW_IBAN = "LT243250004018821187"  # the changed account on October's invoice
IKEA_NIF = "513880992"
UBER_NIF = "510447023"
ADOBE_NIF = "515209376"
EDP_NIF = "501000100"
LANDLORD_NIF = "234567899"
LANDLORD_IBAN = "PT50003601999910287440151"
PREDIAL_NIF = "517222337"
PREDIAL_IBAN = "PT50001000002187650019166"

OWNER_EMAIL = "laura@hazeltree.pt"
ACCOUNTANT_EMAIL = "marc@contabilidadevidal.pt"
ADOBE_INVOICE_URL = "https://accounts.adobe.com/billing/invoices/FT-AD2026-7734"


def qr_payload(**fields: str) -> str:
    """An AT fiscal QR payload with the fields in the order the specification fixes."""
    return "*".join(f"{k}:{fields[k]}" for k in FIELD_ORDER if fields.get(k) not in (None, ""))


def _invoice_text(header: list[str], body: list[str], qr: str) -> bytes:
    """The text layer of a Portuguese invoice PDF plus its decoded QR code."""
    return ("\n".join([*header, *body, f"Código QR: {qr}", ""])).encode("utf-8")


# --------------------------------------------------------------------------- invoices and receipts

LANDLORD_QR = qr_payload(
    A=LANDLORD_NIF, B=HAZEL_NIF, C="PT", D="FR", E="N", F="20260901", G="FR M2026/9", H="KX7Q2WPL-9",
    I1="PT", I2="1200.00", N="0.00", O="1200.00", Q="Tq4e", R="0000",
)
LANDLORD_RECEIPT = _invoice_text(
    ["Marta Gonçalves", "Rua da Madalena 88, 1100-321 Lisboa", f"NIF: {LANDLORD_NIF}",
     "Fatura-recibo n.º FR M2026/9", "ATCUD: KX7Q2WPL-9", "Data de emissão: 01/09/2026"],
    ["Cliente: Hazel Tree Interiores, Lda.", f"NIF: {HAZEL_NIF}",
     "Renda do estúdio da Rua da Rosa - setembro de 2026",
     "Isento de IVA (artigo 9.º, n.º 29 do CIVA)", "Base tributável: 1.200,00", "IVA: 0,00",
     "Total: 1.200,00 €"],
    LANDLORD_QR,
)

PREDIAL_QR = qr_payload(
    A=PREDIAL_NIF, B=COMPANY_B_NIF, C="PT", D="FR", E="N", F="20260901", G="FR PA2026/211", H="PQ8M3ZTA-211",
    I1="PT", I2="950.00", N="0.00", O="950.00", Q="h2Lw", R="1876",
)
PREDIAL_RECEIPT = _invoice_text(
    ["Predial Alfama, Lda.", "Largo do Chafariz de Dentro 4, 1100-139 Lisboa", f"NIF: {PREDIAL_NIF}",
     "Fatura-recibo n.º FR PA2026/211", "ATCUD: PQ8M3ZTA-211", "Data de emissão: 01/09/2026"],
    ["Cliente: Company B, Lda.", f"NIF: {COMPANY_B_NIF}", "Renda do escritório - setembro de 2026",
     "Isento de IVA", "Base tributável: 950,00", "IVA: 0,00", "Total: 950,00 €"],
    PREDIAL_QR,
)

UBER_HT_QR = qr_payload(
    A=UBER_NIF, B=HAZEL_NIF, C="PT", D="FS", E="N", F="20260915", G="FS UBR2026/48213", H="UBR7Q2KX-48213",
    I1="PT", I3="17.69", I4="1.06", N="1.06", O="18.75", Q="Zp0c", R="2210",
)
UBER_HT_RECEIPT = _invoice_text(
    ["Uber Portugal, Unipessoal Lda.", f"NIF: {UBER_NIF}", "Fatura simplificada n.º FS UBR2026/48213",
     "ATCUD: UBR7Q2KX-48213", "Data de emissão: 15/09/2026"],
    ["Cliente: Hazel Tree Interiores, Lda.", f"NIF: {HAZEL_NIF}", "Viagem Chiado - Alfragide",
     "Base tributável (6%): 17,69", "IVA 6%: 1,06", "Total: 18,75 €"],
    UBER_HT_QR,
)

UBER_B_QR = qr_payload(
    A=UBER_NIF, B=COMPANY_B_NIF, C="PT", D="FS", E="N", F="20260905", G="FS UBR2026/45077", H="UBR7Q2KX-45077",
    I1="PT", I3="22.08", I4="1.32", N="1.32", O="23.40", Q="Rr8d", R="2210",
)
UBER_B_RECEIPT = _invoice_text(
    ["Uber Portugal, Unipessoal Lda.", f"NIF: {UBER_NIF}", "Fatura simplificada n.º FS UBR2026/45077",
     "ATCUD: UBR7Q2KX-45077", "Data de emissão: 05/09/2026"],
    ["Cliente: Company B, Lda.", f"NIF: {COMPANY_B_NIF}", "Viagem Baixa - Aeroporto",
     "Base tributável (6%): 22,08", "IVA 6%: 1,32", "Total: 23,40 €"],
    UBER_B_QR,
)

IKEA_QR = qr_payload(
    A=IKEA_NIF, B="999999990", C="PT", D="FS", E="N", F="20260929", G="FS ALF2026/118834", H="IKA4ALF9-118834",
    I1="PT", I7="339.84", I8="78.16", N="78.16", O="418.00", Q="p9Xa", R="1044",
)
IKEA_RECEIPT = _invoice_text(
    ["IKEA Portugal - Móveis e Decoração, Lda.", "Loja de Alfragide", f"NIF: {IKEA_NIF}",
     "Fatura simplificada n.º FS ALF2026/118834", "ATCUD: IKA4ALF9-118834", "Data de emissão: 29/09/2026"],
    ["Consumidor final", "Entrega: Rua da Rosa 57, 1200-384 Lisboa (estúdio)",
     "2 x Estante KALLAX, 1 x Secretária MICKE", "Base tributável (23%): 339,84", "IVA 23%: 78,16",
     "Total: 418,00 €", "Pago com cartão •••• 4817"],
    IKEA_QR,
)

ADOBE_QR = qr_payload(
    A=ADOBE_NIF, B=COMPANY_C_NIF, C="PT", D="FT", E="N", F="20260922", G="FT AD2026/7734", H="ADB2K9QX-7734",
    I1="PT", I7="48.77", I8="11.22", N="11.22", O="59.99", Q="c3Vn", R="3310",
)
ADOBE_INVOICE = _invoice_text(
    ["Adobe Software Portugal, Lda.", f"NIF: {ADOBE_NIF}", "Fatura n.º FT AD2026/7734", "ATCUD: ADB2K9QX-7734",
     "Data de emissão: 22/09/2026"],
    ["Cliente: Company C Studio, Unipessoal Lda.", f"NIF: {COMPANY_C_NIF}",
     "Creative Cloud - Todas as aplicações (mensal)", "Base tributável (23%): 48,77", "IVA 23%: 11,22",
     "Total: 59,99 €", "Pago com cartão •••• 2291"],
    ADOBE_QR,
)

VODAFONE_OCT_QR = qr_payload(
    A=VODAFONE_NIF, B=HAZEL_NIF, C="PT", D="FT", E="N", F="20261001", G="FT VF2026/1290", H="JJ3K7MPX-1290",
    I1="PT", I7="75.12", I8="17.28", N="17.28", O="92.40", Q="Vd7s", R="2451",
)
VODAFONE_OCT_INVOICE = _invoice_text(
    ["Vodafone Portugal - Comunicações Pessoais, S.A.", f"NIF: {VODAFONE_NIF}", "Fatura n.º FT VF2026/1290",
     "ATCUD: JJ3K7MPX-1290", "Data de emissão: 01/10/2026", "Data de vencimento: 05/10/2026"],
    ["Cliente: Hazel Tree Interiores, Lda.", f"NIF: {HAZEL_NIF}", "Serviços móveis e internet - outubro",
     "Base tributável (23%): 75,12", "IVA 23%: 17,28", "Total: 92,40 €",
     "Pagamento por transferência para o IBAN LT24 3250 0040 1882 1187"],
    VODAFONE_OCT_QR,
)

# Not part of the replay: EDP's September invoice is the one being chased. Upload it to
# watch the chase close ("Recovered", the payment matched, Hazel Tree one step closer).
EDP_QR = qr_payload(
    A=EDP_NIF, B=HAZEL_NIF, C="PT", D="FT", E="N", F="20260918", G="FT EDP2026/558120", H="EDPQ7K2M-558120",
    I1="PT", I7="52.11", I8="11.99", N="11.99", O="64.10", Q="e1Dk", R="1422",
)
EDP_INVOICE = _invoice_text(
    ["EDP Comercial - Comercialização de Energia, S.A.", f"NIF: {EDP_NIF}", "Fatura n.º FT EDP2026/558120",
     "ATCUD: EDPQ7K2M-558120", "Data de emissão: 18/09/2026", "Data de vencimento: 19/09/2026"],
    ["Cliente: Hazel Tree Interiores, Lda.", f"NIF: {HAZEL_NIF}", "Eletricidade - estúdio, agosto/setembro",
     "Base tributável (23%): 52,11", "IVA 23%: 11,99", "Total: 64,10 €"],
    EDP_QR,
)

VODAFONE_SEPT_UBL = f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017</cbc:CustomizationID>
  <cbc:ID>FT VF2026/1183</cbc:ID>
  <cbc:IssueDate>2026-09-01</cbc:IssueDate>
  <cbc:DueDate>2026-09-02</cbc:DueDate>
  <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyName><cbc:Name>Vodafone Portugal - Comunicações Pessoais, S.A.</cbc:Name></cac:PartyName>
      <cac:PartyTaxScheme>
        <cbc:CompanyID>PT{VODAFONE_NIF}</cbc:CompanyID>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:PartyTaxScheme>
    </cac:Party>
  </cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty>
    <cac:Party>
      <cac:PartyLegalEntity><cbc:RegistrationName>Hazel Tree Interiores, Lda.</cbc:RegistrationName></cac:PartyLegalEntity>
      <cac:PartyTaxScheme>
        <cbc:CompanyID>PT{HAZEL_NIF}</cbc:CompanyID>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:PartyTaxScheme>
    </cac:Party>
  </cac:AccountingCustomerParty>
  <cac:PaymentMeans>
    <cbc:PaymentMeansCode>49</cbc:PaymentMeansCode>
    <cbc:PaymentID>FT VF2026/1183</cbc:PaymentID>
    <cac:PayeeFinancialAccount><cbc:ID>{VODAFONE_IBAN}</cbc:ID></cac:PayeeFinancialAccount>
  </cac:PaymentMeans>
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="EUR">17.28</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="EUR">75.12</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="EUR">17.28</cbc:TaxAmount>
    </cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="EUR">75.12</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="EUR">75.12</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="EUR">92.40</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="EUR">92.40</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
</Invoice>
""".encode("utf-8")

# --------------------------------------------------------------------------- letters

AT_LETTER_HAZEL = """Autoridade Tributária e Aduaneira
Lisboa, 10 de setembro de 2026
NIF: 516 123 459
Hazel Tree Interiores, Lda.
Pagamento de IVA — período 2026/07.
Referência para pagamento: 161 204 587
Total a pagar: 2.184,37 €
Data limite de pagamento: 25/09/2026. O não pagamento dentro do prazo implica coima e juros de mora.
""".encode("utf-8")

AT_LETTER_COMPANY_B = """Autoridade Tributária e Aduaneira
Lisboa, 30 de setembro de 2026
NIF: 514 987 650
Company B, Lda.
Pagamento de retenções na fonte de IRS — período 2026/09.
Referência para pagamento: 161 377 902
Total a pagar: 412,50 €
Data limite de pagamento: 20/10/2026. O não pagamento dentro do prazo implica coima e juros de mora.
""".encode("utf-8")


# --------------------------------------------------------------------------- emails


def email(*, sender: str, to: str = OWNER_EMAIL, subject: str, at: datetime, text: str, html: str | None = None,
          attachments: tuple[tuple[str, str, bytes], ...] = (), message_id: str) -> bytes:
    """An RFC 5322 message as a mail server would store it."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = format_datetime(at)
    msg["Message-ID"] = message_id
    msg.set_content(text)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    for filename, mime, data in attachments:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    # Fixed MIME boundaries: the same message gives the same bytes (and evidence id) on every build.
    seed = message_id.strip("<>").replace("@", ".")
    for i, part in enumerate(msg.walk()):
        if part.is_multipart():
            part.set_boundary(f"==demo-{i}-{seed}==")
    return msg.as_bytes()


def vodafone_september_email(at: datetime) -> bytes:
    body = (
        "Olá Hazel Tree Interiores,\n\n"
        "A sua fatura de setembro já está disponível.\n"
        "Fatura n.º FT VF2026/1183\n"
        "Cliente: Hazel Tree Interiores, Lda.\n"
        f"NIF: {HAZEL_NIF}\n"
        "Data de emissão: 01/09/2026\n"
        "Base tributável (23%): 75,12\n"
        "IVA 23%: 17,28\n"
        "Total: 92,40 €\n"
        "Será cobrada por débito direto a 02/09/2026.\n\n"
        f"Vodafone Portugal\nNIF: {VODAFONE_NIF}\n"
    )
    return email(sender="Vodafone Business <faturacao@vodafone.pt>", subject="A sua fatura Vodafone de setembro",
                 at=at, text=body, message_id="<ft-vf2026-1183@faturacao.vodafone.pt>",
                 attachments=(("FT_VF2026_1183.xml", "application/xml", VODAFONE_SEPT_UBL),))


def vodafone_october_email(at: datetime) -> bytes:
    body = (
        "Olá Hazel Tree Interiores,\n\n"
        "Enviamos em anexo a fatura de outubro.\n"
        "Informamos que os nossos dados bancários foram alterados. "
        "Por favor efetue o pagamento para o novo IBAN indicado na fatura.\n\n"
        "Vodafone Portugal\n"
    )
    return email(sender="Vodafone Business <faturacao@vodafone.pt>", subject="A sua fatura Vodafone de outubro",
                 at=at, text=body, message_id="<ft-vf2026-1290@faturacao.vodafone.pt>",
                 attachments=(("Fatura_FT_VF2026_1290.txt", "text/plain", VODAFONE_OCT_INVOICE),))


def adobe_email(at: datetime) -> bytes:
    text = ("Your Adobe invoice is ready.\n"
            f"View invoice: {ADOBE_INVOICE_URL}\n")
    html = (
        "<!DOCTYPE html><html><body>"
        "<p>Hi Jorge, your Creative Cloud invoice for September is ready.</p>"
        f'<p><a class="button" href="{ADOBE_INVOICE_URL}">View invoice</a></p>'
        '<p><a href="https://www.adobe.com/privacy.html">Privacy</a></p>'
        "</body></html>"
    )
    return email(sender="Adobe <message@adobe.com>", to="jorge@companyc.pt", subject="Your Adobe invoice is ready",
                 at=at, text=text, html=html, message_id="<inv-ft-ad2026-7734@mail.adobe.com>")


def landlord_email(at: datetime) -> bytes:
    return email(sender="Marta Gonçalves <marta.goncalves.rendas@gmail.com>", subject="Recibo de renda - setembro",
                 at=at, text="Bom dia Laura,\nSegue o recibo da renda de setembro.\nCumprimentos,\nMarta\n",
                 message_id="<fr-m2026-9@mail.gmail.com>",
                 attachments=(("Recibo_FR_M2026_9.txt", "text/plain", LANDLORD_RECEIPT),))


def uber_email(at: datetime, receipt: bytes, company: str, number: str) -> bytes:
    return email(sender="Uber Receipts <noreply@uber.com>", subject=f"A sua viagem com a Uber ({company})", at=at,
                 text="Obrigado por viajar com a Uber. A fatura segue em anexo.\n",
                 message_id=f"<{number.replace(' ', '').replace('/', '-').lower()}@uber.com>",
                 attachments=((f"{number.replace(' ', '_').replace('/', '_')}.txt", "text/plain", receipt),))


def accountant_email(at: datetime) -> bytes:
    body = (
        "Hi Laura,\n\n"
        "Two quick questions while I prepare September:\n"
        "Is the €1,200 transfer to Marta Gonçalves on 1 September the studio rent?\n"
        "Which company should the €418.00 IKEA payment of 29 September go to?\n\n"
        "Thanks,\nMarc\nContabilidade Vidal\n"
    )
    return email(sender=f"Marc Vidal <{ACCOUNTANT_EMAIL}>", subject="September - two questions", at=at, text=body,
                 message_id="<sept-questions@contabilidadevidal.pt>")


# --------------------------------------------------------------------------- bank feeds


def row(bank_id: str, account: str, day: date, amount: str, counterparty: str, description: str,
        kind: TransactionKind, *, card: str | None = None, iban: str | None = None,
        reference: str | None = None) -> BankRow:
    return BankRow(bank_id=bank_id, account_id=account, booked_on=day, amount=Decimal(amount),
                   counterparty=counterparty, description=description, kind=kind, card_last4=card,
                   counterparty_iban=iban, reference=reference)


K = TransactionKind
