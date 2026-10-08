"""SAF-T (PT) sales invoices reader and a subset ledger export (§7, §13, §28, §50).

Reading: SAF-T (PT) 1.04_01 ``SourceDocuments/SalesInvoices`` into typed
invoices and core Document-shaped dicts. Parsing is namespace-agnostic and
uses stdlib expat with defusedxml-style protections: any DOCTYPE, entity
declaration or external entity is refused before it can be expanded.

Writing: :func:`export_ledger_csv` produces a plain CSV ledger for the
accountant package. It is a convenience subset, NOT a SAF-T file and not
output from AT-certified software; the notice travels with every export.
"""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from xml.parsers import expat

from backoffice.countries.base import CountryPackError, NamedObservation
from backoffice.domain.models import (
    CriticalField,
    Document,
    DocumentType,
    ExtractionMethod,
    Quality,
)

from .atcud import ATCUD, ATCUD_NOT_APPLICABLE, ATCUDError, parse_atcud, parse_document_number
from .documents import CANCELLED_STATUS, get_document_type, preferred_code
from .nif import normalize_nif

# ElementTree holds the whole file in memory (several times its size), so the
# default stays well above any SME's monthly SAF-T but far from exhausting a
# worker. Callers with a known larger file can pass ``max_bytes``.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_TOLERANCE = Decimal("0.01")
_ZERO = Decimal("0.00")
_DECIMAL = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")
_ISO_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
# SAF-T rule: file-level TotalDebit/TotalCredit exclude cancelled ("A") and
# already-invoiced ("F") documents.
_EXCLUDED_FROM_TOTALS = frozenset({CANCELLED_STATUS, "F"})
_STRUCTURED_CONFIDENCE = 0.97
# Totals that contradict the file's own lines are still reported, at the
# lowest confidence verification treats as a deliberate reading, next to the
# recomputed value: the disagreement becomes a CONFLICT, never a pick (§19).
_INCONSISTENT_CONFIDENCE = 0.40
_ARITHMETIC_CONFIDENCE = 0.95
_CENT = Decimal("0.01")
_HALF_CENT = Decimal("0.005")


class SaftError(CountryPackError, ValueError):
    """The file is not a readable SAF-T (PT) export."""

    owner_message = "I couldn't read this accounting file."


class SaftSecurityError(SaftError):
    """The XML uses DTDs or entities, which are refused."""

    owner_message = "I couldn't read this accounting file safely, so I left it untouched."


# --------------------------------------------------------------------------- #
# Safe XML loading
# --------------------------------------------------------------------------- #


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _refuse(kind: str) -> Callable[..., None]:
    def handler(*_args: object) -> None:
        raise SaftSecurityError(f"{kind} is not allowed in SAF-T files")

    return handler


def _refuse_external_entity(
    _context: str, _base: str | None, _system_id: str | None, _public_id: str | None
) -> int:
    raise SaftSecurityError("external entity is not allowed in SAF-T files")


def parse_xml_safely(data: bytes | str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> ET.Element:
    """Parse XML into an ElementTree with namespace prefixes stripped.

    Refuses DOCTYPE declarations (and therefore entity expansion and external
    entities), and inputs larger than ``max_bytes``.
    """
    if isinstance(data, str):
        raw, encoding = data.encode("utf-8"), "utf-8"
    else:
        raw, encoding = bytes(data), None
    if len(raw) > max_bytes:
        raise SaftError(f"file is larger than {max_bytes} bytes")
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate(encoding, "}")
    parser.StartElementHandler = lambda name, attrs: builder.start(
        _local(name), {_local(k): v for k, v in attrs.items()}
    )
    parser.EndElementHandler = lambda name: builder.end(_local(name))
    parser.CharacterDataHandler = builder.data
    parser.StartDoctypeDeclHandler = _refuse("DOCTYPE")
    parser.EntityDeclHandler = _refuse("entity declaration")
    parser.UnparsedEntityDeclHandler = _refuse("entity declaration")
    parser.ExternalEntityRefHandler = _refuse_external_entity
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.buffer_text = True
    try:
        parser.Parse(raw, True)
    except expat.ExpatError as exc:
        raise SaftError(f"not well-formed XML ({exc})") from None
    return builder.close()


# --------------------------------------------------------------------------- #
# Typed result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SaftHeader:
    audit_file_version: str | None
    company_tax_id: str | None  # issuer NIF (9 digits when valid-shaped)
    company_name: str | None
    fiscal_year: int | None
    start_date: date | None
    end_date: date | None
    currency: str
    software_certificate: str | None


@dataclass(frozen=True)
class SaftTaxBase:
    """Line amounts grouped by tax; ``base`` is in the document's own sense (positive)."""

    tax_type: str  # IVA, IS, NS
    region: str  # PT, PT-AC, PT-MA, ...
    code: str  # RED, INT, NOR, ISE, OUT, ...
    percentage: Decimal | None
    base: Decimal


@dataclass(frozen=True)
class SaftInvoice:
    invoice_no: str
    atcud_raw: str | None
    invoice_type: str
    status: str
    invoice_date: date
    customer_id: str | None
    customer_tax_id: str | None
    customer_name: str | None
    customer_country: str | None
    net_total: Decimal
    tax_payable: Decimal  # all taxes (VAT + stamp duty)
    gross_total: Decimal
    stamp_duty: Decimal
    withholding_total: Decimal | None
    foreign_currency: str | None
    foreign_amount: Decimal | None
    tax_bases: tuple[SaftTaxBase, ...]
    line_count: int
    lines_net: Decimal  # sum of the lines, in the document's own sense
    lines_vat: Decimal  # VAT implied by the lines' rates, rounded to the cent
    problems: tuple[str, ...]  # technical descriptions, not owner-facing

    @property
    def totals_consistent(self) -> bool:
        """GrossTotal equals NetTotal + TaxPayable (to the cent)."""
        return abs(self.net_total + self.tax_payable - self.gross_total) <= _TOLERANCE

    @property
    def lines_consistent(self) -> bool:
        """NetTotal equals the sum of the lines (to the cent)."""
        return abs(self.lines_net - self.net_total) <= _TOLERANCE

    @property
    def taxes_consistent(self) -> bool:
        """TaxPayable - stamp duty equals the VAT implied by the lines' rates.

        Software may round VAT per line, so up to half a cent per line of
        drift (plus one cent) is accepted.
        """
        allowed = _TOLERANCE + _HALF_CENT * self.line_count
        return abs(self.vat_total - self.lines_vat) <= allowed

    @property
    def consistent(self) -> bool:
        return self.totals_consistent and self.lines_consistent and self.taxes_consistent

    @property
    def doc_type(self) -> DocumentType:
        entry = get_document_type(self.invoice_type)
        return entry.doc_type if entry else DocumentType.OTHER

    @property
    def vat_total(self) -> Decimal:
        return self.tax_payable - self.stamp_duty

    @property
    def is_cancelled(self) -> bool:
        return self.status == CANCELLED_STATUS

    @property
    def atcud(self) -> ATCUD | None:
        try:
            return parse_atcud(self.atcud_raw) if self.atcud_raw else None
        except ATCUDError:
            return None

    def to_document_dict(self, header: SaftHeader) -> dict[str, Any]:
        """Keyword arguments for ``domain.models.Document`` (plus tenant/evidence ids)."""
        return {
            "doc_type": self.doc_type,
            "supplier_name": header.company_name,
            "supplier_tax_id": header.company_tax_id,
            "customer_tax_id": self.customer_tax_id,
            "invoice_number": self.invoice_no,
            "issue_date": self.invoice_date,
            "currency": header.currency,
            "net_amount": self.net_total,
            "vat_amount": self.vat_total,
            "gross_amount": self.gross_total,
        }


@dataclass(frozen=True)
class SaftSalesInvoices:
    header: SaftHeader
    invoices: tuple[SaftInvoice, ...]
    problems: tuple[str, ...]  # file-level control totals that do not match

    def documents(self, *, include_cancelled: bool = False) -> list[dict[str, Any]]:
        """Document-shaped dicts, cancelled documents left out by default."""
        return [
            inv.to_document_dict(self.header)
            for inv in self.invoices
            if include_cancelled or not inv.is_cancelled
        ]


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def parse_saft_sales_invoices(
    data: bytes | str, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> SaftSalesInvoices:
    """Read SalesInvoices from a SAF-T (PT) file.

    Structural gaps (missing InvoiceNo, dates, totals...) raise SaftError:
    such a file is not a valid export and none of it is guessed. Arithmetic
    mismatches are reported in ``problems`` on the invoice or the file.
    """
    root = parse_xml_safely(data, max_bytes=max_bytes)
    if root.tag != "AuditFile":
        raise SaftError("root element is not AuditFile")
    header = _header(root.find("Header"))
    customers = _customers(root.find("MasterFiles"))
    section = root.find("SourceDocuments/SalesInvoices")
    if section is None:
        return SaftSalesInvoices(header, (), ())
    invoices = tuple(
        _invoice(el, customers, position)
        for position, el in enumerate(section.findall("Invoice"), start=1)
    )
    return SaftSalesInvoices(header, invoices, _file_problems(section, invoices))


def _header(el: ET.Element | None) -> SaftHeader:
    if el is None:
        raise SaftError("Header is missing")
    tax_id = _text(el, "TaxRegistrationNumber")
    year = _text(el, "FiscalYear")
    return SaftHeader(
        audit_file_version=_text(el, "AuditFileVersion"),
        company_tax_id=(normalize_nif(tax_id) or tax_id) if tax_id else None,
        company_name=_text(el, "CompanyName"),
        fiscal_year=int(year) if year and year.isdigit() else None,
        start_date=_optional_date(el, "StartDate"),
        end_date=_optional_date(el, "EndDate"),
        currency=_text(el, "CurrencyCode") or "EUR",
        software_certificate=_text(el, "SoftwareCertificateNumber"),
    )


def _customers(master: ET.Element | None) -> dict[str, dict[str, str | None]]:
    if master is None:
        return {}
    return {
        cid: {
            "tax_id": _text(c, "CustomerTaxID"),
            "name": _text(c, "CompanyName"),
            "country": _text(c, "BillingAddress/Country"),
        }
        for c in master.findall("Customer")
        if (cid := _text(c, "CustomerID"))
    }


def _invoice(el: ET.Element, customers: Mapping[str, Mapping[str, str | None]], position: int) -> SaftInvoice:
    where = f"Invoice #{position}"
    invoice_no = _required(el, "InvoiceNo", where)
    try:
        parse_document_number(invoice_no)
    except ATCUDError:
        raise SaftError(f"{where}: InvoiceNo does not follow the SAF-T pattern") from None
    invoice_type = _required(el, "InvoiceType", where)
    totals = el.find("DocumentTotals")
    if totals is None:
        raise SaftError(f"{where}: DocumentTotals is missing")
    sign = Decimal(-1) if _doc_sign_is_debit(invoice_type) else Decimal(1)
    bases, stamp, lines_net, line_count = _lines(el, sign, where)
    lines_vat = _lines_vat(bases)
    customer = customers.get(_text(el, "CustomerID") or "", {})
    net_total = _required_decimal(totals, "NetTotal", where)
    tax_payable = _required_decimal(totals, "TaxPayable", where)
    gross_total = _required_decimal(totals, "GrossTotal", where)
    withholding = [
        _decimal(w.findtext("WithholdingTaxAmount"), where) or _ZERO for w in el.findall("WithholdingTax")
    ]
    invoice = SaftInvoice(
        invoice_no=invoice_no,
        atcud_raw=_text(el, "ATCUD"),
        invoice_type=invoice_type,
        status=_required(el, "DocumentStatus/InvoiceStatus", where),
        invoice_date=_required_date(el, "InvoiceDate", where),
        customer_id=_text(el, "CustomerID"),
        customer_tax_id=customer.get("tax_id"),
        customer_name=customer.get("name"),
        customer_country=customer.get("country"),
        net_total=net_total,
        tax_payable=tax_payable,
        gross_total=gross_total,
        stamp_duty=stamp,
        withholding_total=sum(withholding, _ZERO) if withholding else None,
        foreign_currency=_text(totals, "Currency/CurrencyCode"),
        foreign_amount=_decimal(totals.findtext("Currency/CurrencyAmount"), where),
        tax_bases=bases,
        line_count=line_count,
        lines_net=lines_net,
        lines_vat=lines_vat,
        problems=(
            _invoice_problems(net_total, tax_payable, gross_total, lines_net)
            + _identity_problems(invoice_no, invoice_type, _text(el, "ATCUD"))
        ),
    )
    if not invoice.taxes_consistent:
        problems = (*invoice.problems,
                    f"TaxPayable {tax_payable} minus stamp duty {stamp} differs from the VAT "
                    f"on the lines {lines_vat}")
        invoice = replace(invoice, problems=problems)
    return invoice


def _doc_sign_is_debit(invoice_type: str) -> bool:
    """Credit notes carry their lines as DebitAmount."""
    entry = get_document_type(invoice_type)
    return entry is not None and entry.doc_type == DocumentType.CREDIT_NOTE


def _lines(
    invoice: ET.Element, sign: Decimal, where: str
) -> tuple[tuple[SaftTaxBase, ...], Decimal, Decimal, int]:
    groups: dict[tuple[str, str, str, Decimal | None], Decimal] = {}
    stamp_fixed = _ZERO
    stamp_rated: dict[Decimal, Decimal] = {}  # IS percentage -> line amounts
    total = _ZERO
    lines = invoice.findall("Line")
    for line in lines:
        credit = _decimal(line.findtext("CreditAmount"), where) or _ZERO
        debit = _decimal(line.findtext("DebitAmount"), where) or _ZERO
        amount = (credit - debit) * sign
        total += amount
        tax = line.find("Tax")
        if tax is None:
            continue
        key = (
            _text(tax, "TaxType") or "",
            _text(tax, "TaxCountryRegion") or "",
            _text(tax, "TaxCode") or "",
            _decimal(tax.findtext("TaxPercentage"), where),
        )
        groups[key] = groups.get(key, _ZERO) + amount
        if key[0] != "IS":
            continue
        # Stamp duty is either a fixed TaxAmount or a percentage of the line.
        fixed = _decimal(tax.findtext("TaxAmount"), where)
        if fixed is not None:
            stamp_fixed += fixed
        elif key[3]:
            stamp_rated[key[3]] = stamp_rated.get(key[3], _ZERO) + amount
    bases = tuple(SaftTaxBase(t, r, c, p, amount) for (t, r, c, p), amount in groups.items())
    return bases, stamp_fixed + _percentage_stamp_duty(stamp_rated), total, len(lines)


def _lines_vat(bases: Iterable[SaftTaxBase]) -> Decimal:
    """VAT the lines' rates imply (IVA only), rounded half-up to the cent."""
    total = sum(
        (b.base * b.percentage / 100 for b in bases if b.tax_type == "IVA" and b.percentage),
        _ZERO,
    )
    return total.quantize(_CENT, rounding=ROUND_HALF_UP)


def _percentage_stamp_duty(rated: Mapping[Decimal, Decimal]) -> Decimal:
    """Stamp duty charged as a percentage of the line amounts (e.g. 4% on credit).

    Rounded per rate, half-up to the cent; any rounding drift then shows up
    in the GrossTotal check instead of being absorbed.
    """
    return sum(
        ((base * pct / 100).quantize(_CENT, rounding=ROUND_HALF_UP) for pct, base in rated.items()),
        _ZERO,
    )


def _invoice_problems(net: Decimal, tax: Decimal, gross: Decimal, lines_net: Decimal) -> tuple[str, ...]:
    problems = []
    if abs(net + tax - gross) > _TOLERANCE:
        problems.append(f"GrossTotal {gross} differs from NetTotal + TaxPayable {net + tax}")
    if abs(lines_net - net) > _TOLERANCE:
        problems.append(f"NetTotal {net} differs from the sum of lines {lines_net}")
    return tuple(problems)


def _identity_problems(invoice_no: str, invoice_type: str, atcud_raw: str | None) -> tuple[str, ...]:
    """Unknown document type, or an ATCUD that does not belong to this InvoiceNo."""
    problems = []
    if get_document_type(invoice_type) is None:
        problems.append(f"InvoiceType {invoice_type} is not a known SAF-T (PT) type")
    if atcud_raw is None or atcud_raw == ATCUD_NOT_APPLICABLE:
        return tuple(problems)
    try:
        atcud = parse_atcud(atcud_raw)
    except ATCUDError:
        problems.append(f"ATCUD {atcud_raw!r} is malformed")
        return tuple(problems)
    number = parse_document_number(invoice_no)
    if atcud.sequence != number.number:
        problems.append(
            f"ATCUD sequence {atcud.sequence_text} does not match InvoiceNo {invoice_no}"
        )
    return tuple(problems)


def _file_problems(section: ET.Element, invoices: tuple[SaftInvoice, ...]) -> tuple[str, ...]:
    problems = []
    counts = Counter(inv.invoice_no for inv in invoices)
    problems.extend(
        f"InvoiceNo {number} appears more than once" for number, n in counts.items() if n > 1
    )
    entries = _text(section, "NumberOfEntries")
    if entries is not None and entries.isdigit() and int(entries) != len(invoices):
        problems.append(f"NumberOfEntries {entries} but {len(invoices)} invoices found")
    debit, credit = _control_sums(section)
    for name, actual in (("TotalDebit", debit), ("TotalCredit", credit)):
        declared = _decimal(section.findtext(name), "SalesInvoices")
        if declared is not None and abs(declared - actual) > _TOLERANCE:
            problems.append(f"{name} {declared} but lines add up to {actual}")
    return tuple(problems)


def _control_sums(section: ET.Element) -> tuple[Decimal, Decimal]:
    debit = credit = _ZERO
    for invoice in section.findall("Invoice"):
        if _text(invoice, "DocumentStatus/InvoiceStatus") in _EXCLUDED_FROM_TOTALS:
            continue
        for line in invoice.findall("Line"):
            debit += _decimal(line.findtext("DebitAmount"), "SalesInvoices") or _ZERO
            credit += _decimal(line.findtext("CreditAmount"), "SalesInvoices") or _ZERO
    return debit, credit


def _text(el: ET.Element, path: str) -> str | None:
    value = el.findtext(path)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _required(el: ET.Element, path: str, where: str) -> str:
    value = _text(el, path)
    if value is None:
        raise SaftError(f"{where}: {path} is missing")
    return value


def _decimal(value: str | None, where: str) -> Decimal | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    if not _DECIMAL.match(text):
        raise SaftError(f"{where}: '{text}' is not a decimal amount")
    return Decimal(text)


def _required_decimal(el: ET.Element, path: str, where: str) -> Decimal:
    value = _decimal(el.findtext(path), where)
    if value is None:
        raise SaftError(f"{where}: {path} is missing")
    return value


def _parse_iso_date(text: str, where: str) -> date:
    if not _ISO_DATE.match(text):
        raise SaftError(f"{where}: '{text}' is not a YYYY-MM-DD date")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise SaftError(f"{where}: '{text}' is not a real date") from None


def _required_date(el: ET.Element, path: str, where: str) -> date:
    return _parse_iso_date(_required(el, path, where), where)


def _optional_date(el: ET.Element, path: str) -> date | None:
    value = _text(el, path)
    return _parse_iso_date(value, "Header") if value else None


# --------------------------------------------------------------------------- #
# Observations (§13: structured evidence outranks OCR)
# --------------------------------------------------------------------------- #


def saft_invoice_observations(
    invoice: SaftInvoice,
    header: SaftHeader,
    evidence_id: str,
    *,
    include_cancelled: bool = False,
) -> list[NamedObservation]:
    """Field observations (method=STRUCTURED_XML) for one SAF-T invoice.

    A cancelled invoice evidences nothing, so it yields no observations unless
    ``include_cancelled``. When the file's own totals disagree (GrossTotal vs
    NetTotal + TaxPayable, NetTotal vs the lines, or the VAT vs the lines'
    rates) the amounts drop to low
    confidence and the recomputed value is added (method=ARITHMETIC, located
    on the structured_xml channel so it never counts as independent), so the
    disagreement surfaces as a CONFLICT instead of being hidden (§19).
    """
    if invoice.is_cancelled and not include_cancelled:
        return []
    where = f"saft:{invoice.invoice_no}"
    ids = _STRUCTURED_CONFIDENCE
    amounts = _STRUCTURED_CONFIDENCE if invoice.consistent else _INCONSISTENT_CONFIDENCE
    values: list[tuple[CriticalField, object, str, float]] = [
        (CriticalField.INVOICE_NUMBER, invoice.invoice_no, "InvoiceNo", ids),
        (CriticalField.ISSUE_DATE, invoice.invoice_date, "InvoiceDate", ids),
        (CriticalField.NET_AMOUNT, invoice.net_total, "DocumentTotals/NetTotal", amounts),
        (CriticalField.VAT_AMOUNT, invoice.vat_total, "DocumentTotals/TaxPayable-IS", amounts),
        (CriticalField.GROSS_AMOUNT, invoice.gross_total, "DocumentTotals/GrossTotal", amounts),
        (CriticalField.CURRENCY, header.currency, "Header/CurrencyCode", ids),
    ]
    if header.company_tax_id:
        values.append((CriticalField.SUPPLIER_TAX_ID, header.company_tax_id,
                       "Header/TaxRegistrationNumber", ids))
    if invoice.customer_tax_id:
        values.append((CriticalField.CUSTOMER_TAX_ID, invoice.customer_tax_id,
                       "Customer/CustomerTaxID", ids))
    observations = [
        NamedObservation(field=f, value=v, source=evidence_id, method=ExtractionMethod.STRUCTURED_XML,
                         confidence=c, location=f"{where}/{path}")
        for f, v, path, c in values
    ]
    return observations + _arithmetic_observations(invoice, evidence_id)


def _arithmetic_observations(invoice: SaftInvoice, evidence_id: str) -> list[NamedObservation]:
    """Recomputed totals, only where the file contradicts itself."""
    derived: list[tuple[CriticalField, Decimal, str]] = []
    if not invoice.totals_consistent:
        derived.append((CriticalField.GROSS_AMOUNT, invoice.net_total + invoice.tax_payable,
                        "NetTotal+TaxPayable"))
    if not invoice.lines_consistent:
        derived.append((CriticalField.NET_AMOUNT, invoice.lines_net, "sum(Line)"))
    if not invoice.taxes_consistent:
        derived.append((CriticalField.VAT_AMOUNT, invoice.lines_vat, "sum(Line*TaxPercentage)"))
    return [
        NamedObservation(field=f, value=v, source=evidence_id, method=ExtractionMethod.ARITHMETIC,
                         confidence=_ARITHMETIC_CONFIDENCE,
                         location=f"structured_xml:arithmetic({formula})@saft:{invoice.invoice_no}")
        for f, v, formula in derived
    ]


# --------------------------------------------------------------------------- #
# Subset ledger export for the accountant package (§27 Day 0, §28)
# --------------------------------------------------------------------------- #

LEDGER_NOTICE = (
    "Subset ledger export. This is not a SAF-T (PT) file and was not produced by "
    "AT-certified invoicing software. It lists documents collected and checked by "
    "Back Office, with their verification status, to help the accountant; the "
    "official records remain the original documents and the issuers' SAF-T files."
)

_HEADERS = {
    "pt": ("Data", "Tipo", "Número", "ATCUD", "NIF", "Entidade", "Base tributável", "IVA",
           "Total", "Retenção", "Moeda", "Estado", "Evidência"),
    "iso": ("date", "doc_type", "number", "atcud", "tax_id", "counterparty", "net", "vat",
            "gross", "withholding", "currency", "status", "evidence"),
}
_STATUS_WORDS = {
    "pt": {Quality.GREEN: "verificado", Quality.AMBER: "por confirmar", Quality.RED: "em conflito"},
    "iso": {Quality.GREEN: "verified", Quality.AMBER: "unconfirmed", Quality.RED: "conflict"},
}
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
# The period ends up in a file name: keep it to a plain label (no paths).
_PERIOD = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,31}$")


@dataclass(frozen=True)
class LedgerRow:
    """One ledger line. Amounts are signed: credit notes are negative."""

    issued_on: date
    doc_code: str
    number: str
    atcud: str
    counterparty_tax_id: str
    counterparty_name: str
    net: Decimal | None
    vat: Decimal | None
    gross: Decimal
    withholding: Decimal | None
    currency: str
    status: Quality
    evidence: str


@dataclass(frozen=True)
class LedgerExport:
    csv: str
    notice: str
    row_count: int
    filename: str


def ledger_row_from_document(
    document: Document,
    *,
    atcud: str = "",
    withholding: Decimal | None = None,
) -> LedgerRow:
    """Ledger row for a purchase document (counterparty = supplier).

    A credit note is always negative, whether its amounts arrive as
    magnitudes (the domain convention) or already signed.
    """
    if document.issue_date is None or document.gross_amount is None:
        raise ValueError("a ledger row needs an issue date and a gross amount")
    credit = document.doc_type == DocumentType.CREDIT_NOTE
    return LedgerRow(
        issued_on=document.issue_date,
        doc_code=_code_for(document),
        number=document.invoice_number or "",
        atcud=atcud,
        counterparty_tax_id=document.supplier_tax_id or "",
        counterparty_name=document.supplier_name or "",
        net=_signed(document.net_amount, credit),
        vat=_signed(document.vat_amount, credit),
        gross=_signed(document.gross_amount, credit),
        withholding=withholding,
        currency=document.currency,
        status=document.quality,
        evidence=" ".join(document.evidence_ids),
    )


def _signed(value: Decimal | None, credit_note: bool) -> Decimal | None:
    if value is None:
        return None
    return -abs(value) if credit_note else value


def _code_for(document: Document) -> str:
    if document.invoice_number:
        prefix = document.invoice_number.split(" ", 1)[0]
        if get_document_type(prefix) is not None:
            return prefix
    return preferred_code(document.doc_type) or ""


def export_ledger_csv(
    rows: Iterable[LedgerRow],
    *,
    dialect: Literal["pt", "iso"] = "pt",
    period: str | None = None,
) -> LedgerExport:
    """CSV ledger. ``pt``: ';' separator and decimal comma (opens in PT Excel);
    ``iso``: ',' separator and decimal point. Rows are sorted by date and
    number (then counterparty and evidence, so the output is deterministic).
    Text cells that could be read as spreadsheet formulas are neutralised.
    ``period`` (e.g. "2026-09") goes into the file name, so it must be a plain
    label of letters, digits, "-" or "_".
    """
    if dialect not in _HEADERS:
        raise ValueError(f"unknown dialect {dialect!r}")
    if period is not None and not _PERIOD.match(period):
        raise ValueError("period must be a short label of letters, digits, '-' or '_'")
    ordered = sorted(rows, key=_row_order)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";" if dialect == "pt" else ",", lineterminator="\r\n")
    writer.writerow(_HEADERS[dialect])
    for row in ordered:
        writer.writerow(_cells(row, dialect))
    suffix = f"-{period}" if period else ""
    return LedgerExport(
        csv=buffer.getvalue(),
        notice=LEDGER_NOTICE,
        row_count=len(ordered),
        filename=f"ledger{suffix}-subset-not-saft.csv",
    )


def _row_order(row: LedgerRow) -> tuple[object, ...]:
    return (row.issued_on, row.number, row.counterparty_tax_id, row.doc_code, row.evidence,
            row.gross)


def _cells(row: LedgerRow, dialect: str) -> list[str]:
    return [
        row.issued_on.isoformat(),
        _safe_text(row.doc_code),
        _safe_text(row.number),
        _safe_text(row.atcud),
        _safe_text(row.counterparty_tax_id),
        _safe_text(row.counterparty_name),
        _money(row.net, dialect),
        _money(row.vat, dialect),
        _money(row.gross, dialect),
        _money(row.withholding, dialect),
        _safe_text(row.currency),
        _STATUS_WORDS[dialect][row.status],
        _safe_text(row.evidence),
    ]


def _money(value: Decimal | None, dialect: str) -> str:
    if value is None:
        return ""
    text = f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"
    return text.replace(".", ",") if dialect == "pt" else text


def _safe_text(value: str) -> str:
    """Prefix a quote so spreadsheet apps do not evaluate the cell as a formula."""
    return f"'{value}" if value.startswith(_FORMULA_START) else value
