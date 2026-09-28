"""A generic, conservative "Label: value" text extractor (§17, §18).

Country packs provide the real text extractors (the Portuguese one knows
"Total do documento", NIF check digits, ATCUD...). This one is language
neutral given its label table. It is the fallback for countries without a
pack, and the extractor used by tests and the golden-dataset harness.

Rules (§19, never guess):

* a value is taken only when a known label starts the line, followed by
  ``:``, ``#``, ``.`` or spaces, and then the value;
* labels are matched longest first across all fields, so "invoice date" wins
  over "date" and "due date" over "due";
* the value must parse for the field's type; an IBAN must pass mod-97;
* every distinct value is emitted: two different totals become a
  disagreement for verification to see, never a silent choice.

Day order is configuration, not inference: ``day_first=True`` (the default)
reads "05/09/2026" as 5 September, the convention of every EU market this
product serves. Pass ``day_first=None`` for sources of unknown convention,
so such dates are refused instead of guessed (§19); ``False`` for US dates.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from backoffice.domain.models import CriticalField, ExtractionMethod, FieldObservation

from .fields import FieldMap
from .values import (
    AMOUNT_FIELDS,
    DATE_FIELDS,
    TAX_ID_FIELDS,
    comparison_key,
    find_currency,
    iban_is_valid,
    normalize_currency,
    normalize_tax_id,
    parse_amount,
    parse_date,
)

__all__ = ["DEFAULT_LABELS", "LabelledFieldExtractor"]

F = CriticalField

# English labels. Deliberately explicit: an unlabelled number is never read.
DEFAULT_LABELS: Mapping[CriticalField, tuple[str, ...]] = MappingProxyType(
    {
        F.INVOICE_NUMBER: ("invoice number", "invoice no", "invoice #", "document number"),
        F.SUPPLIER_TAX_ID: (
            "supplier vat", "supplier vat number", "supplier tax id", "seller vat",
            "vat number", "vat no", "vat id",
        ),
        F.CUSTOMER_TAX_ID: ("customer vat", "customer vat number", "customer tax id", "buyer vat"),
        F.GROSS_AMOUNT: (
            "total", "total amount", "grand total", "total due", "amount due",
            "total incl. vat", "total (incl. vat)",
        ),
        F.NET_AMOUNT: ("subtotal", "net amount", "net total", "total excl. vat", "total (excl. vat)"),
        F.VAT_AMOUNT: ("vat", "vat amount", "vat total", "total vat", "tax", "tax amount", "total tax"),
        F.CURRENCY: ("currency",),
        F.ISSUE_DATE: ("invoice date", "issue date", "date of issue", "date"),
        F.DUE_DATE: ("due date", "payment due", "due"),
        F.IBAN: ("iban",),
        F.PAYMENT_REFERENCE: ("payment reference", "reference"),
    }
)  # fmt: skip

_MAX_REFERENCE_LENGTH = 40


class LabelledFieldExtractor:
    """``FieldExtractor`` for text where values follow explicit labels."""

    def __init__(
        self,
        labels: Mapping[CriticalField, Sequence[str]] = DEFAULT_LABELS,
        *,
        confidence: float = 0.5,
        day_first: bool | None = True,
    ) -> None:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        pairs = [(label.strip(), CriticalField(f)) for f, ls in labels.items() for label in ls]
        pairs.sort(key=lambda p: -len(p[0]))
        self._patterns = [(_label_pattern(label), f) for label, f in pairs if label]
        self._confidence = confidence
        self._day_first = day_first

    def __call__(self, text: str, source: str, method: ExtractionMethod) -> FieldMap:
        found: dict[CriticalField, dict[str, FieldObservation]] = {}
        for line in text.splitlines():
            match = self._match(line)
            if match is None:
                continue
            field, raw = match
            for f, value, confidence in self._values(field, raw):
                observation = FieldObservation(
                    value=value, source=source, method=method, confidence=confidence
                )
                found.setdefault(f, {}).setdefault(comparison_key(f, value), observation)
        return {f: tuple(by_key.values()) for f, by_key in found.items()}

    def _match(self, line: str) -> tuple[CriticalField, str] | None:
        for pattern, field in self._patterns:
            m = pattern.match(line)
            if m and m["value"]:
                return field, m["value"]
        return None

    def _values(self, field: CriticalField, raw: str) -> list[tuple[CriticalField, object, float]]:
        value = self._parse(field, raw)
        if value is None:
            return []
        out: list[tuple[CriticalField, object, float]] = [(field, value, self._confidence)]
        if field in AMOUNT_FIELDS and (currency := find_currency(raw)):
            out.append((F.CURRENCY, currency, max(0.0, self._confidence - 0.1)))
        return out

    def _parse(self, field: CriticalField, raw: str) -> object | None:
        if field in AMOUNT_FIELDS:
            return parse_amount(raw)
        if field in DATE_FIELDS:
            return parse_date(raw, day_first=self._day_first)
        if field in TAX_ID_FIELDS:
            compact = normalize_tax_id(raw)
            plausible = compact and 8 <= len(compact) <= 15 and sum(c.isdigit() for c in compact) >= 6
            return raw if plausible else None
        if field is F.IBAN:
            return raw if iban_is_valid(raw) else None
        if field is F.CURRENCY:
            return normalize_currency(raw)
        return raw if len(raw) <= _MAX_REFERENCE_LENGTH and any(c.isalnum() for c in raw) else None


def _label_pattern(label: str) -> re.Pattern[str]:
    boundary = r"(?![A-Za-z0-9])" if label[-1].isalnum() else ""
    return re.compile(
        rf"^\s*{re.escape(label)}{boundary}[\s:#.]*(?P<value>.*?)\s*$", re.IGNORECASE
    )
