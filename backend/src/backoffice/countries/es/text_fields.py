"""Reading Spanish invoice text: "Factura nº", "Fecha", "Base imponible", "IVA", "Total", NIF/CIF (§50).

Rules, as for Portugal (§19, never guess):

* a value is read only after a known label at the start of a line, or of a segment separated
  from the one before by a wide gap ("Factura nº: F-2026/118     Fecha: 18/09/2026");
* labels are matched longest first ("fecha de vencimiento" before "fecha");
* dates are day first (dd/mm/aaaa, and Spanish month names); amounts use the comma for
  decimals ("1.234,56 €") or the point, whichever the value's own shape says;
* a rate printed with the VAT or the base ("IVA 21%: 42,00", "Base imponible (10%)") is not
  the amount;
* tax numbers (NIF, NIE, CIF, with or without "ES") are kept only when their check character is
  right; their role comes from the business's known numbers, then the words around them
  ("Cliente", "Emisor"). An invoice must name its issuer (Real Decreto 1619/2012, art. 6-7), so
  the one number left when the buyer is known, or the only one printed, is the issuer's;
  otherwise it stays unassigned.

Every value keeps its line ("text:line 7"); verification decides what is confirmed.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

from backoffice.countries.base import NamedObservation, TextReading
from backoffice.domain.models import CriticalField, ExtractionMethod
from backoffice.extraction.values import iban_is_valid, parse_amount, parse_date

from . import nif as es_nif
from ._text import clean, fold

__all__ = ["ES_LABELS", "read_text"]

F = CriticalField

# Folded labels by field.
ES_LABELS: dict[CriticalField, tuple[str, ...]] = {
    F.INVOICE_NUMBER: (
        "numero de factura", "n.º de factura", "nº de factura", "no. de factura", "num. factura",
        "num factura", "numero factura", "factura n.º", "factura nº", "factura n°", "factura no.",
        "factura num.", "factura numero", "nº factura", "n.º factura", "n° factura", "serie y numero",
        "factura simplificada n.º", "factura simplificada nº", "factura rectificativa n.º",
        "factura rectificativa nº", "ticket n.º", "ticket nº",
    ),
    F.ISSUE_DATE: ("fecha de expedicion", "fecha de emision", "fecha de factura", "fecha factura", "fecha"),
    F.DUE_DATE: ("fecha de vencimiento", "vencimiento"),
    F.NET_AMOUNT: ("base imponible", "base imp.", "importe neto", "total sin iva", "subtotal"),
    F.VAT_AMOUNT: ("cuota de iva", "cuota iva", "importe del iva", "importe iva", "total iva", "iva"),
    F.GROSS_AMOUNT: ("total factura", "importe total", "total a pagar", "total con iva", "total iva incluido",
                     "total"),
    F.IBAN: ("iban",),
    F.PAYMENT_REFERENCE: ("referencia de pago",),
}  # fmt: skip

_BY_LABEL = sorted(((label, f) for f, labels in ES_LABELS.items() for label in labels), key=lambda x: -len(x[0]))
_LABEL_RE = [(re.compile(rf"\s*{re.escape(label)}(?![a-z0-9])"), f) for label, f in _BY_LABEL]
_GAP = re.compile(r"\t+|\s{3,}|\s\|\s")
_RATE = re.compile(r"\s*\(?\s*(\d{1,2}(?:[.,]\d{1,2})?)\s*%\s*\)?")
_SEP = re.compile(r"^[\s:#.=]*")
_AMOUNT_TOKEN = re.compile(r"-?(?:€\s?)?\d[\d.,  ]*\d(?:\s?(?:€|EUR))?|-?\d(?:\s?(?:€|EUR))?", re.I)
_NUMBER_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9/\-_.]*[A-Za-z0-9]|[A-Za-z0-9]")
# NIF, NIE and CIF as printed: "B12345674", "B-1234567-4", "ES B12345674", "12.345.678-Z", "X1234567L".
_TAX_ID = re.compile(
    r"(?<![A-Za-z0-9])(?:ES[\s-]?)?([A-Z]-?\d{7}-?[0-9A-Z]|\d{8}-?[A-Z]|\d{2}\.\d{3}\.\d{3}-?[A-Z]|[XYZ]-?\d{7}-?[A-Z])"
    r"(?![A-Za-z0-9])", re.I)
_CUSTOMER_WORDS = re.compile(r"(?<![a-z])(?:cliente|destinatario|adquirente|comprador|facturar\s+a|receptor)(?![a-z])")
_SUPPLIER_WORDS = re.compile(r"(?<![a-z])(?:emisor|proveedor|vendedor|expedidor|prestador)(?![a-z])")
_CUSTOMER_HEADER = re.compile(r"^\s*(?:datos\s+del\s+)?(?:cliente|destinatario|adquirente|facturar\s+a|receptor)\s*:?\s*$")
_SUPPLIER_HEADER = re.compile(r"^\s*(?:datos\s+del\s+)?(?:emisor|proveedor|vendedor|expedidor)\s*:?\s*$")
_TEXT = 0.5  # a labelled value in the document's own text
_TAX = 0.6  # a tax number whose check character is right
_OWN = 0.7  # the business's own number: the customer


@dataclass(frozen=True)
class _TaxHit:
    number: str
    line: int
    role: str | None


def _value(field: CriticalField, raw: str) -> object | None:
    raw = raw.strip()
    if not raw:
        return None
    if field in (F.GROSS_AMOUNT, F.NET_AMOUNT, F.VAT_AMOUNT):
        m = _AMOUNT_TOKEN.search(raw)
        return parse_amount(m.group(0).strip()) if m else None
    if field in (F.ISSUE_DATE, F.DUE_DATE):
        m = re.match(r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{4}-\d{2}-\d{2}", raw)
        text = m.group(0) if m else raw.split("  ")[0]
        return parse_date(text, day_first=True)
    if field is F.IBAN:
        compact = re.sub(r"[\s-]", "", raw).upper()
        m = re.match(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", compact)
        return m.group(0) if m and iban_is_valid(m.group(0)) else None
    m = _NUMBER_TOKEN.match(raw)
    if m is None or not any(c.isdigit() for c in m.group(0)):
        return None
    return m.group(0)


def _starts_with_label(text: str) -> bool:
    low = fold(text)
    return any(p.match(low) for p, _ in _LABEL_RE)


def _segments(line: str) -> list[str]:
    """A line cut where a wide gap is followed by another label; a value after a gap stays with its label."""
    out: list[str] = []
    for piece in _GAP.split(line):
        if not piece.strip():
            continue
        if out and not _starts_with_label(piece):
            out[-1] = f"{out[-1]}   {piece}"
        else:
            out.append(piece)
    return out


def _role(folded_lines: list[str], index: int, before: str) -> str | None:
    customer, supplier = bool(_CUSTOMER_WORDS.search(before)), bool(_SUPPLIER_WORDS.search(before))
    if customer != supplier:
        return "customer" if customer else "supplier"
    for back in range(index - 1, max(index - 5, -1), -1):
        line = folded_lines[back]
        if not line.strip():
            break
        if _CUSTOMER_HEADER.match(line):
            return "customer"
        if _SUPPLIER_HEADER.match(line):
            return "supplier"
    return None


def _tax_ids(lines: list[str], folded: list[str]) -> list[_TaxHit]:
    found: dict[str, _TaxHit] = {}
    for i, line in enumerate(lines):
        for m in _TAX_ID.finditer(line):
            check = es_nif.validate_nif(m.group(1))
            if not check.valid or check.normalized is None:
                continue
            found.setdefault(check.normalized, _TaxHit(check.normalized, i + 1, _role(folded, i, folded[i][:m.start()])))
    return list(found.values())


def read_text(
    text: str,
    source: str,
    *,
    method: ExtractionMethod = ExtractionMethod.OCR,
    known_customer_tax_ids: Collection[str] = (),
    known_supplier_tax_ids: Collection[str] = (),
) -> TextReading:
    """Candidate observations from a Spanish invoice's text (module docstring)."""
    lines = clean(text or "").split("\n")
    folded = [fold(line) for line in lines]
    out: list[NamedObservation] = []
    seen: set[tuple[CriticalField, str]] = set()

    def add(field: CriticalField, value: object, line: int, confidence: float) -> None:
        key = (field, str(value))
        if key in seen:
            return
        seen.add(key)
        out.append(NamedObservation(field=field, value=value, source=source, method=method, confidence=confidence,
                                    location=f"text:line {line}"))

    for number, line in enumerate(lines, start=1):
        for segment in _segments(line):
            low = fold(segment)
            for pattern, field in _LABEL_RE:
                m = pattern.match(low)
                if m is None:
                    continue
                rest = segment[m.end():]
                rate = _RATE.match(rest)
                if rate is not None and field in (F.VAT_AMOUNT, F.NET_AMOUNT):
                    rest = rest[rate.end():]
                rest = rest[_SEP.match(rest).end():].strip()  # type: ignore[union-attr]
                value = _value(field, rest)
                if value is not None:
                    add(field, value, number, _TEXT)
                break
    customers = {es_nif.normalize_nif(t) for t in known_customer_tax_ids} - {None}
    suppliers = {es_nif.normalize_nif(t) for t in known_supplier_tax_ids} - {None}
    unassigned: list[_TaxHit] = []
    have_customer = have_supplier = False
    for hit in _tax_ids(lines, folded):
        if hit.number in suppliers or (hit.role == "supplier" and hit.number not in customers):
            add(F.SUPPLIER_TAX_ID, hit.number, hit.line, _TAX)
            have_supplier = True
        elif hit.number in customers or hit.role == "customer":
            add(F.CUSTOMER_TAX_ID, hit.number, hit.line, _OWN if hit.number in customers else _TAX)
            have_customer = True
        else:
            unassigned.append(hit)
    if len(unassigned) == 1 and not have_supplier:
        # The issuer must be named (RD 1619/2012): the one number left is theirs.
        add(F.SUPPLIER_TAX_ID, unassigned[0].number, unassigned[0].line, _TEXT)
        unassigned = []
    del have_customer
    return TextReading(observations=tuple(out), unassigned_tax_ids=tuple(h.number for h in unassigned))
