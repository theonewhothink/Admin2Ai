"""Plain-language rendering for verification reasons (§36, §48, §70).

Owner-facing text never shows engine names, evidence ids, method codes or
raw errors: an OCR engine is "the scan", arithmetic is "the amounts adding
up", an IBAN is "the account ending 1234" (§53: no full bank details in
passing text).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Any

from backoffice.domain.models import CriticalField, ExtractionMethod

from .normalize import AMOUNT_FIELDS, CENT, DATE_FIELDS, as_critical_field

_FIELD_LABELS: Mapping[CriticalField, str] = MappingProxyType({
    CriticalField.INVOICE_NUMBER: "the invoice number",
    CriticalField.SUPPLIER_TAX_ID: "the supplier's VAT number",
    CriticalField.CUSTOMER_TAX_ID: "the customer's VAT number",
    CriticalField.GROSS_AMOUNT: "the total",
    CriticalField.NET_AMOUNT: "the amount before VAT",
    CriticalField.VAT_AMOUNT: "the VAT",
    CriticalField.CURRENCY: "the currency",
    CriticalField.ISSUE_DATE: "the date",
    CriticalField.DUE_DATE: "the due date",
    CriticalField.IBAN: "the bank details",
    CriticalField.PAYMENT_REFERENCE: "the payment reference",
})  # fmt: skip

METHOD_LABELS: Mapping[ExtractionMethod, str] = MappingProxyType({
    ExtractionMethod.STRUCTURED_XML: "the e-invoice file",
    ExtractionMethod.EMBEDDED_TEXT: "the document text",
    ExtractionMethod.QR: "the QR code",
    ExtractionMethod.BARCODE: "the barcode",
    ExtractionMethod.API: "the supplier's system",
    ExtractionMethod.HTML_STRUCTURED: "the web page",
    ExtractionMethod.OCR: "the scan",
    ExtractionMethod.VLM: "the scan",
    ExtractionMethod.ARITHMETIC: "the amounts adding up",
    ExtractionMethod.BANK: "the bank",
    ExtractionMethod.HUMAN: "a person's check",
})  # fmt: skip

_SYMBOLS: Mapping[str, str] = MappingProxyType({"EUR": "€", "GBP": "£", "USD": "$", "ILS": "₪"})
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
MINUS = "−"


def field_label(name: CriticalField | str) -> str:
    field = as_critical_field(name)
    if field is not None:
        return _FIELD_LABELS[field]
    return "the " + str(name).replace("_", " ").strip()


def method_label(method: ExtractionMethod) -> str:
    return METHOD_LABELS[method]


def join(parts: Iterable[str]) -> str:
    """'a', 'a and b', 'a, b and c' (duplicates dropped, order kept)."""
    items = list(dict.fromkeys(p for p in parts if p))
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def money(amount: Decimal, currency: str | None = None) -> str:
    """'€1,492.30', '−£12.00', 'CHF 1,492.30', or '1,492.30' when the currency is unknown."""
    rounded = amount.quantize(CENT, rounding=ROUND_HALF_UP)
    sign = MINUS if rounded < 0 else ""
    digits = f"{abs(rounded):,.2f}"
    if currency and currency in _SYMBOLS:
        return f"{sign}{_SYMBOLS[currency]}{digits}"
    return f"{sign}{currency} {digits}" if currency else f"{sign}{digits}"


def day(value: date) -> str:
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


def percent(rate: Decimal) -> str:
    """Decimal("0.23") -> '23%', Decimal("0.055") -> '5.5%'."""
    text = format((rate * 100).normalize(), "f")
    return f"{text}%"


MAX_TEXT = 40  # longer document text is cut in owner copy; the full value stays in the result
_IBAN_ENDING = 4
_IBAN_ENDING_MAX = 8  # §53: never more of an account number than needed to tell two apart


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1].rstrip() + "…"


def show(name: CriticalField | str, value: Any, currency: str | None = None) -> str:
    """A canonical value as an owner would read it."""
    field = as_critical_field(name)
    if field in AMOUNT_FIELDS and isinstance(value, Decimal):
        return money(value, currency)
    if field in DATE_FIELDS and isinstance(value, date):
        return day(value)
    if field is CriticalField.IBAN and isinstance(value, str):
        return f"the account ending {value[-_IBAN_ENDING:]}"
    return _short(str(value))


def _iban_endings(values: list[str]) -> list[str]:
    """Shortest common ending length (4..8) that tells every account apart."""
    distinct = set(values)
    for size in range(_IBAN_ENDING, _IBAN_ENDING_MAX + 1):
        if len({v[-size:] for v in distinct}) == len(distinct):
            return [f"the account ending {v[-size:]}" for v in values]
    return [f"the {v[:2]} account ending {v[-_IBAN_ENDING_MAX:]}" for v in values]


def show_many(name: CriticalField | str, values: Iterable[Any], currency: str | None = None) -> list[str]:
    """Like :func:`show`, but values shown side by side never look identical when they differ."""
    items = list(values)
    if as_critical_field(name) is CriticalField.IBAN and all(isinstance(v, str) for v in items):
        return _iban_endings(items)
    return [show(name, v, currency) for v in items]
