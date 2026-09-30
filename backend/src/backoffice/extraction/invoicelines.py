"""What an e-invoice says beyond its critical fields: references, lines and VAT per rate (§13 Stage 0).

Used to put costs on the right job, property, vehicle or client (cost
centers): a UBL invoice can name a project (``ProjectReference``), a cost
reference (``AccountingCost``), the delivery site's address, and on each line
what was bought, for whom and at which VAT rate. A reseller's licence invoice
lists one line per end customer.

Only structure is read here; nothing is decided. Hostile XML is refused by
:mod:`.safexml`. CII invoices are not read yet (their critical fields still
are, by :mod:`.einvoice`).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from backoffice.domain.models import DocumentLine, VatPart

from .einvoice import _UBL_PREFIXES, UBL_NS, _unwrap
from .fields import StructuredDataError
from .safexml import parse_xml

__all__ = ["InvoiceDetails", "read_invoice_details"]

_DECIMAL = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")


@dataclass(frozen=True)
class InvoiceDetails:
    references: tuple[str, ...]  # header texts: notes, project, order and cost references, delivery address
    lines: tuple[DocumentLine, ...]
    vat_parts: tuple[VatPart, ...]  # per VAT rate: taxable amount and VAT, as the invoice states them


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    text = " ".join(element.text.split())
    return text or None


def _decimal(element: ET.Element | None) -> Decimal | None:
    text = _text(element)
    if text is None or not _DECIMAL.fullmatch(text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def read_invoice_details(data: bytes | str) -> InvoiceDetails | None:
    """References, lines and VAT per rate of a UBL invoice or credit note; None for anything else."""
    try:
        root = _unwrap(parse_xml(data))
    except (StructuredDataError, ValueError):
        return None
    if root.tag not in (f"{{{UBL_NS['inv']}}}Invoice", f"{{{UBL_NS['cn']}}}CreditNote"):
        return None
    credit = root.tag.endswith("}CreditNote")

    def find(path: str, element: ET.Element | None = None) -> ET.Element | None:
        return (root if element is None else element).find(path, _UBL_PREFIXES)

    def findall(path: str, element: ET.Element | None = None) -> list[ET.Element]:
        return (root if element is None else element).findall(path, _UBL_PREFIXES)

    currency = _text(find("cbc:DocumentCurrencyCode"))
    references: list[str] = []
    for path in ("cbc:Note", "cbc:AccountingCost", "cbc:BuyerReference", "cac:ProjectReference/cbc:ID",
                 "cac:OrderReference/cbc:ID", "cac:ContractDocumentReference/cbc:ID",
                 "cac:AdditionalDocumentReference/cbc:ID", "cac:Delivery/cac:DeliveryLocation/cbc:ID"):
        for element in findall(path):
            text = _text(element)
            if text:
                references.append(text)
    for address in findall("cac:Delivery/cac:DeliveryLocation/cac:Address") + findall("cac:Delivery/cac:Address"):
        words = [_text(find(f"cbc:{tag}", address)) for tag in ("StreetName", "BuildingNumber",
                                                                 "AdditionalStreetName", "PostalZone", "CityName")]
        words += [_text(line) for line in findall("cac:AddressLine/cbc:Line", address)]
        text = " ".join(w for w in words if w)
        if text:
            references.append(text)

    lines: list[DocumentLine] = []
    for i, line in enumerate(findall("cac:CreditNoteLine" if credit else "cac:InvoiceLine"), start=1):
        amount = find("cbc:LineExtensionAmount", line)
        unit = amount.get("currencyID") if amount is not None else None
        if unit and currency and unit.strip().upper() != currency.upper():
            return InvoiceDetails(tuple(references), (), ())  # lines in another currency: not used
        words = [_text(n) for n in findall("cbc:Note", line)]
        words += [_text(find("cac:Item/cbc:Name", line)), _text(find("cac:Item/cbc:Description", line))]
        for prop in findall("cac:Item/cac:AdditionalItemProperty", line):
            name, value = _text(find("cbc:Name", prop)), _text(find("cbc:Value", prop))
            if value:
                words.append(f"{name}: {value}" if name else value)
        refs = [_text(find(path, line)) for path in ("cbc:AccountingCost", "cac:OrderLineReference/cbc:LineID",
                                                     "cac:DocumentReference/cbc:ID")]
        lines.append(DocumentLine(
            id=_text(find("cbc:ID", line)) or str(i),
            description=" · ".join(w for w in words if w),
            net_amount=_decimal(amount),
            vat_rate=_decimal(find("cac:Item/cac:ClassifiedTaxCategory/cbc:Percent", line)),
            quantity=_decimal(find("cbc:CreditedQuantity" if credit else "cbc:InvoicedQuantity", line)),
            reference=" ".join(r for r in refs if r) or None,
        ))

    parts: list[VatPart] = []
    for total in findall("cac:TaxTotal"):
        subtotals = findall("cac:TaxSubtotal", total)
        if not subtotals:
            continue
        for sub in subtotals:
            base, tax = _decimal(find("cbc:TaxableAmount", sub)), _decimal(find("cbc:TaxAmount", sub))
            if base is None or tax is None:
                parts = []
                break
            parts.append(VatPart(rate=_decimal(find("cac:TaxCategory/cbc:Percent", sub)), net=base, vat=tax))
        break
    return InvoiceDetails(tuple(references), tuple(lines), tuple(parts))
