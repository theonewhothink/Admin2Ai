"""UBL 2.1 and UN/CEFACT CII e-invoices as Stage 0 evidence (§13, §18).

Structured invoices are the strongest document evidence we get: values come
from typed elements, never from pixels. Parsing is namespace-aware and uses
:func:`parse_xml`, which refuses DTDs and entities.

Rules that keep the "never guess" promise (§19):

* amounts must be xsd:decimal; anything else is noted and skipped;
* an amount whose ``currencyID`` differs from the document currency adds a
  second currency observation, so the inconsistency surfaces as a conflict;
  without a document currency, the amounts' mandatory ``currencyID`` is the
  currency evidence;
* a credit note (by root or type code) carries its sign in its type, so its
  amounts should be positive; negative ones are noted
  (``credit_note_with_negative_amounts``) because negating them again
  downstream would turn a refund into a charge;
* tax totals in another (tax accounting) currency are ignored, with a note;
* a payee account that is not a valid IBAN is noted, never emitted;
* repeated elements (several payment means) yield one observation each.

Supported roots: UBL ``Invoice`` / ``CreditNote`` (optionally wrapped in a
Peppol StandardBusinessDocument) and CII ``CrossIndustryInvoice`` (D16B,
as used by EN 16931, Factur-X and ZUGFeRD).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from types import MappingProxyType

from backoffice.domain.models import CriticalField, DocumentType, ExtractionMethod

from ._collect import Collector
from .fields import Stage0Result, StructuredDataError
from .safexml import parse_xml
from .values import iban_is_valid

__all__ = [
    "CII_NS",
    "UBL_NS",
    "XML_CONFIDENCE",
    "StructuredFormatError",
    "parse_einvoice",
]

F = CriticalField

UBL_NS: Mapping[str, str] = MappingProxyType(
    {
        "inv": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
        "cn": "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2",
        "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
        "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    }
)
CII_NS: Mapping[str, str] = MappingProxyType(
    {
        "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
        "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
        "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
    }
)
_SBDH_NS = "http://www.unece.org/cefact/namespaces/StandardBusinessDocumentHeader"
# ElementTree wants plain dicts for prefix lookups.
_UBL_PREFIXES = dict(UBL_NS)
_CII_PREFIXES = dict(CII_NS)

XML_CONFIDENCE = 0.98

# UNTDID 1001 document name codes used by EN 16931 (UBL InvoiceTypeCode /
# CreditNoteTypeCode, CII TypeCode). Only the codes we map with certainty;
# unknown codes fall back to the root element's kind.
# Source: UN/EDIFACT code list 1001 as referenced by EN 16931-1.
# verified_as_of: 2026-09 (author knowledge).
_TYPE_CODES: Mapping[str, DocumentType] = MappingProxyType(
    {
        "380": DocumentType.INVOICE,  # commercial invoice
        "384": DocumentType.INVOICE,  # corrected invoice
        "386": DocumentType.INVOICE,  # prepayment invoice
        "389": DocumentType.INVOICE,  # self-billed invoice
        "381": DocumentType.CREDIT_NOTE,  # credit note
        "261": DocumentType.CREDIT_NOTE,  # self-billed credit note
        "383": DocumentType.DEBIT_NOTE,  # debit note
    }
)

_XSD_DECIMAL = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_XSD_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:Z|[+-]\d{2}:\d{2})?")


class StructuredFormatError(StructuredDataError):
    """Well-formed XML, but not an e-invoice format we read."""


def parse_einvoice(data: bytes | str, *, source: str) -> Stage0Result:
    """Critical fields from a UBL or CII e-invoice.

    Raises :class:`~backoffice.extraction.safexml.UnsafeXMLError` for hostile
    XML and :class:`StructuredFormatError` for other XML vocabularies.
    """
    root = _unwrap(parse_xml(data))
    if root.tag in (f"{{{UBL_NS['inv']}}}Invoice", f"{{{UBL_NS['cn']}}}CreditNote"):
        return _UBLReader(root, source).read()
    if root.tag == f"{{{CII_NS['rsm']}}}CrossIndustryInvoice":
        return _CIIReader(root, source).read()
    raise StructuredFormatError("not_einvoice", root.tag)


def _unwrap(root: ET.Element) -> ET.Element:
    """The business document inside a Peppol StandardBusinessDocument envelope."""
    if root.tag != f"{{{_SBDH_NS}}}StandardBusinessDocument":
        return root
    for child in root:
        if not child.tag.startswith(f"{{{_SBDH_NS}}}"):
            return child
    raise StructuredFormatError("empty_envelope")


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    text = " ".join(element.text.split())
    return text or None


class _Reader:
    """Shared helpers: strict xsd values with notes on anything skipped."""

    def __init__(self, kind: str, source: str) -> None:
        self.c = Collector(kind, source, ExtractionMethod.STRUCTURED_XML, XML_CONFIDENCE)
        self.negative_amounts = False

    def decimal(self, element: ET.Element | None, location: str) -> Decimal | None:
        text = _text(element)
        if text is None:
            return None
        if not _XSD_DECIMAL.fullmatch(text):
            self.c.note(f"invalid_amount:{location}")
            return None
        return Decimal(text)

    def xsd_date(self, element: ET.Element | None, location: str) -> date | None:
        text = _text(element)
        if text is None:
            return None
        m = _XSD_DATE.fullmatch(text)
        if m:
            try:
                return date(int(m[1]), int(m[2]), int(m[3]))
            except ValueError:
                pass
        self.c.note(f"invalid_date:{location}")
        return None

    def amount(
        self,
        field: CriticalField | None,
        element: ET.Element | None,
        location: str,
        currency: str | None,
    ) -> Decimal | None:
        """Record an amount; a differing currencyID becomes a currency observation."""
        value = self.decimal(element, location)
        if value is None or element is None:
            return value
        if field is not None:
            self.c.add(field, value, location)
            self.negative_amounts |= value < 0
        unit = (element.get("currencyID") or "").strip().upper()
        if unit and currency and unit != currency.upper():
            self.c.note("amount_currency_mismatch")
            self.c.add(F.CURRENCY, unit, f"{location}/@currencyID")
        elif unit and not currency:
            self.c.add(F.CURRENCY, unit, f"{location}/@currencyID")
        return value

    def iban(self, text: str | None, location: str) -> None:
        if text is None:
            return
        if iban_is_valid(text):
            self.c.add(F.IBAN, text, location)
        else:
            self.c.note("payee_account_not_iban")

    def doc_type(self, code: str | None, default: DocumentType) -> DocumentType:
        if code and code not in _TYPE_CODES:
            self.c.note(f"unknown_type_code:{code}")
        kind = _TYPE_CODES.get(code or "", default)
        if kind is DocumentType.CREDIT_NOTE and self.negative_amounts:
            self.c.note("credit_note_with_negative_amounts")
        return kind


class _UBLReader(_Reader):
    def __init__(self, root: ET.Element, source: str) -> None:
        self.credit = root.tag.endswith("}CreditNote")
        super().__init__("ubl_credit_note" if self.credit else "ubl_invoice", source)
        self.root = root
        self.name = "CreditNote" if self.credit else "Invoice"

    def find(self, path: str, element: ET.Element | None = None) -> ET.Element | None:
        return (self.root if element is None else element).find(path, _UBL_PREFIXES)

    def findall(self, path: str, element: ET.Element | None = None) -> list[ET.Element]:
        return (self.root if element is None else element).findall(path, _UBL_PREFIXES)

    def loc(self, path: str) -> str:
        return f"/{self.name}/{path}"

    def read(self) -> Stage0Result:
        c = self.c
        c.add(F.INVOICE_NUMBER, _text(self.find("cbc:ID")), self.loc("cbc:ID"))
        c.add(F.ISSUE_DATE, self.xsd_date(self.find("cbc:IssueDate"), "IssueDate"), self.loc("cbc:IssueDate"))
        currency = _text(self.find("cbc:DocumentCurrencyCode"))
        c.add(F.CURRENCY, currency, self.loc("cbc:DocumentCurrencyCode"))
        self._due_dates()
        self._party("supplier", F.SUPPLIER_TAX_ID, "cac:AccountingSupplierParty/cac:Party")
        self._party("customer", F.CUSTOMER_TAX_ID, "cac:AccountingCustomerParty/cac:Party")
        self._totals(currency)
        self._tax_totals(currency)
        self._payment_means()
        # The invoice a credit note corrects (EN 16931 BT-25, "preceding invoice reference").
        c.extra("invoice_reference", _text(self.find("cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID")))
        type_code = _text(self.find("cbc:CreditNoteTypeCode" if self.credit else "cbc:InvoiceTypeCode"))
        default = DocumentType.CREDIT_NOTE if self.credit else DocumentType.INVOICE
        return c.result(self.doc_type(type_code, default))

    def _due_dates(self) -> None:
        if not self.credit:
            due = self.xsd_date(self.find("cbc:DueDate"), "DueDate")
            self.c.add(F.DUE_DATE, due, self.loc("cbc:DueDate"))
        for i, means in enumerate(self.findall("cac:PaymentMeans"), start=1):
            path = f"cac:PaymentMeans[{i}]/cbc:PaymentDueDate"
            due = self.xsd_date(self.find("cbc:PaymentDueDate", means), "PaymentDueDate")
            self.c.add(F.DUE_DATE, due, self.loc(path))

    def _party(self, role: str, field: CriticalField, path: str) -> None:
        party = self.find(path)
        if party is None:
            return
        self.c.extra(
            f"{role}_name",
            _text(self.find("cac:PartyName/cbc:Name", party))
            or _text(self.find("cac:PartyLegalEntity/cbc:RegistrationName", party)),
        )
        for i, scheme in enumerate(self.findall("cac:PartyTaxScheme", party), start=1):
            company_id = _text(self.find("cbc:CompanyID", scheme))
            scheme_id = (_text(self.find("cac:TaxScheme/cbc:ID", scheme)) or "VAT").upper()
            if scheme_id != "VAT":
                self.c.note(f"{role}_non_vat_tax_scheme")
                continue
            self.c.add(field, company_id, self.loc(f"{path}/cac:PartyTaxScheme[{i}]/cbc:CompanyID"))

    def _totals(self, currency: str | None) -> None:
        totals = self.find("cac:LegalMonetaryTotal")
        if totals is None:
            return
        base = "cac:LegalMonetaryTotal/cbc:"
        for field, tag in ((F.GROSS_AMOUNT, "TaxInclusiveAmount"), (F.NET_AMOUNT, "TaxExclusiveAmount")):
            self.amount(field, self.find(f"cbc:{tag}", totals), self.loc(base + tag), currency)
        payable = self.amount(None, self.find("cbc:PayableAmount", totals), self.loc(base + "PayableAmount"), currency)
        if payable is not None:
            self.c.extra("amount_payable", payable)

    def _tax_totals(self, currency: str | None) -> None:
        for i, total in enumerate(self.findall("cac:TaxTotal"), start=1):
            element = self.find("cbc:TaxAmount", total)
            unit = element.get("currencyID") if element is not None else None
            if unit and currency and unit.strip().upper() != currency.upper():
                self.c.note("tax_total_in_other_currency")
                continue
            self.amount(F.VAT_AMOUNT, element, self.loc(f"cac:TaxTotal[{i}]/cbc:TaxAmount"), currency)

    def _payment_means(self) -> None:
        for i, means in enumerate(self.findall("cac:PaymentMeans"), start=1):
            base = f"cac:PaymentMeans[{i}]/"
            self.c.add(F.PAYMENT_REFERENCE, _text(self.find("cbc:PaymentID", means)), self.loc(base + "cbc:PaymentID"))
            account = _text(self.find("cac:PayeeFinancialAccount/cbc:ID", means))
            self.iban(account, self.loc(base + "cac:PayeeFinancialAccount/cbc:ID"))


class _CIIReader(_Reader):
    _TX = "rsm:SupplyChainTradeTransaction/"
    _AGREEMENT = _TX + "ram:ApplicableHeaderTradeAgreement/"
    _SETTLEMENT = _TX + "ram:ApplicableHeaderTradeSettlement/"

    def __init__(self, root: ET.Element, source: str) -> None:
        super().__init__("cii", source)
        self.root = root

    def find(self, path: str, element: ET.Element | None = None) -> ET.Element | None:
        return (self.root if element is None else element).find(path, _CII_PREFIXES)

    def findall(self, path: str, element: ET.Element | None = None) -> list[ET.Element]:
        return (self.root if element is None else element).findall(path, _CII_PREFIXES)

    @staticmethod
    def loc(path: str) -> str:
        return f"/rsm:CrossIndustryInvoice/{path}"

    def cii_date(self, element: ET.Element | None, location: str) -> date | None:
        """udt:DateTimeString; only format 102 (CCYYMMDD) is a full date."""
        text = _text(element)
        if element is None or text is None:
            return None
        if element.get("format", "102") != "102" or not re.fullmatch(r"\d{8}", text):
            self.c.note(f"unsupported_date:{location}")
            return None
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
        except ValueError:
            self.c.note(f"invalid_date:{location}")
            return None

    def read(self) -> Stage0Result:
        c = self.c
        doc = "rsm:ExchangedDocument/"
        c.add(F.INVOICE_NUMBER, _text(self.find(doc + "ram:ID")), self.loc(doc + "ram:ID"))
        issue_path = doc + "ram:IssueDateTime/udt:DateTimeString"
        c.add(F.ISSUE_DATE, self.cii_date(self.find(issue_path), "IssueDateTime"), self.loc(issue_path))
        currency = _text(self.find(self._SETTLEMENT + "ram:InvoiceCurrencyCode"))
        c.add(F.CURRENCY, currency, self.loc(self._SETTLEMENT + "ram:InvoiceCurrencyCode"))
        self._party("supplier", F.SUPPLIER_TAX_ID, self._AGREEMENT + "ram:SellerTradeParty")
        self._party("customer", F.CUSTOMER_TAX_ID, self._AGREEMENT + "ram:BuyerTradeParty")
        self._settlement(currency)
        return c.result(self.doc_type(_text(self.find(doc + "ram:TypeCode")), DocumentType.INVOICE))

    def _party(self, role: str, field: CriticalField, path: str) -> None:
        party = self.find(path)
        if party is None:
            return
        self.c.extra(f"{role}_name", _text(self.find("ram:Name", party)))
        for i, reg in enumerate(self.findall("ram:SpecifiedTaxRegistration", party), start=1):
            element = self.find("ram:ID", reg)
            scheme = (element.get("schemeID") or "").upper() if element is not None else ""
            if scheme == "VA":  # VAT number
                self.c.add(field, _text(element), self.loc(f"{path}/ram:SpecifiedTaxRegistration[{i}]/ram:ID"))
            elif scheme == "FC":  # national fiscal number, not a VAT number
                self.c.extra(f"{role}_fiscal_number", _text(element))

    def _settlement(self, currency: str | None) -> None:
        settlement = self.find(self._SETTLEMENT.rstrip("/"))
        if settlement is None:
            return

        def loc(path: str) -> str:
            return self.loc(self._SETTLEMENT + path)

        reference = _text(self.find("ram:PaymentReference", settlement))
        self.c.add(F.PAYMENT_REFERENCE, reference, loc("ram:PaymentReference"))
        account = "ram:PayeePartyCreditorFinancialAccount/ram:IBANID"
        for i, means in enumerate(self.findall("ram:SpecifiedTradeSettlementPaymentMeans", settlement), start=1):
            self.iban(_text(self.find(account, means)), loc(f"ram:SpecifiedTradeSettlementPaymentMeans[{i}]/{account}"))
        due_path = "ram:DueDateDateTime/udt:DateTimeString"
        for i, terms in enumerate(self.findall("ram:SpecifiedTradePaymentTerms", settlement), start=1):
            due = self.cii_date(self.find(due_path, terms), "DueDateDateTime")
            self.c.add(F.DUE_DATE, due, loc(f"ram:SpecifiedTradePaymentTerms[{i}]/{due_path}"))
        self._summation(settlement, currency)

    def _summation(self, settlement: ET.Element, currency: str | None) -> None:
        summation = self.find("ram:SpecifiedTradeSettlementHeaderMonetarySummation", settlement)
        if summation is None:
            return
        base = self._SETTLEMENT + "ram:SpecifiedTradeSettlementHeaderMonetarySummation/ram:"

        def amount(field: CriticalField | None, tag: str) -> Decimal | None:
            return self.amount(field, self.find(f"ram:{tag}", summation), self.loc(base + tag), currency)

        amount(F.NET_AMOUNT, "TaxBasisTotalAmount")
        amount(F.GROSS_AMOUNT, "GrandTotalAmount")
        payable = amount(None, "DuePayableAmount")
        if payable is not None:
            self.c.extra("amount_payable", payable)
        for i, element in enumerate(self.findall("ram:TaxTotalAmount", summation), start=1):
            unit = element.get("currencyID")
            if unit and currency and unit.strip().upper() != currency.upper():
                self.c.note("tax_total_in_other_currency")
                continue
            self.amount(F.VAT_AMOUNT, element, self.loc(f"{base}TaxTotalAmount[{i}]"), currency)
