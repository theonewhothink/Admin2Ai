"""Who an invoice is made out to, besides the tax number: the billing name and address (checklist H2, H3).

Read only from labelled lines, never guessed from position: the customer label
("Cliente:", "Adquirente:", "Customer:", "Bill to:" ...) gives the billing name,
on the same line or, for a bare header, on the next line. The billing address is
a labelled address line ("Morada:", "Address:") after it, or the street and
postal-code lines right under the name. Anything before the customer label (the
supplier's own name and address at the top of the page) is never taken.

"Consumidor final" (a sale to a final consumer) names nobody: no billing name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["BillingParty", "read_billing_party"]


@dataclass(frozen=True)
class BillingParty:
    name: str | None = None
    address: str | None = None


_NAME_LABEL = re.compile(
    r"^\s*(?:dados\s+do\s+)?(?:cliente|adquirente|comprador|destinat[aá]rio|customer|client|bill(?:ed)?\s+to|"
    r"invoice\s+to|sold\s+to|facturar\s+a|facturado\s+a|faturado\s+a)(?![a-z])(?:\s+(?:name|nome))?\s*(?::|-|–)?\s*"
    r"(?P<value>.*)$", re.IGNORECASE)
_ADDRESS_LABEL = re.compile(
    r"^\s*(?:morada|endere[cç]o|address|billing\s+address|domic[ií]lio|direcci[oó]n)(?:\s+de\s+fatura[cç][aã]o)?"
    r"\s*:\s*(?P<value>.+)$", re.IGNORECASE)
_TAX = re.compile(r"(?<![a-z])(?:nif|nipc|n\.?\s?i\.?\s?f\.?|vat(?:\s+(?:no|number))?|contribuinte|tax\s*id|cif)"
                  r"(?![a-z])", re.IGNORECASE)
_STREET = re.compile(
    r"^\s*(?:rua|r\.|av\.?|avenida|pra[cç]a|largo|travessa|tv\.|estrada|alameda|cal[cç]ada|beco|rotunda|"
    r"urbaniza[cç][aã]o|quinta|street|st\.|road|rd\.|lane|calle|c/|paseo|plaza)(?![a-z])", re.IGNORECASE)
_NUMBERED_STREET = re.compile(
    r"^\s*\d+[a-z]?,?\s+.*(?<![a-z])(?:street|st|road|rd|avenue|ave|lane|ln|drive|dr|way|place|square|court|"
    r"boulevard|blvd)(?![a-z])", re.IGNORECASE)
_POSTAL = re.compile(r"(?<!\d)\d{4}\s*-\s*\d{3}(?!\d)")
_NOT_A_NAME = re.compile(r"^(?:consumidor\s+final|final\s+consumer)\b", re.IGNORECASE)
_NOT_A_VALUE = re.compile(r"^(?:id|no\.?|n\.?\s?º|number|code|c[oó]digo|ref\.?|reference)(?![a-z])",
                          re.IGNORECASE)
_TRIM = " \t,;:-–|"


def _clean(value: str) -> str:
    value = _TAX.split(value, maxsplit=1)[0]  # "Hazel Tree, Lda. - NIF 516123459" -> the name only
    return " ".join(value.split()).strip(_TRIM)


def _is_address_line(line: str) -> bool:
    return bool(_STREET.match(line) or _NUMBERED_STREET.match(line) or _POSTAL.search(line))


def read_billing_party(text: str) -> BillingParty:
    """The billing name and address printed on a document (module docstring); empty when not labelled."""
    lines = [line for line in (text or "").splitlines()]
    for i, line in enumerate(lines):
        found = _NAME_LABEL.match(line)
        if found is None:
            continue
        rest = [l for l in lines[i + 1:i + 6] if l.strip()]
        value = _clean(found.group("value"))
        if not value and rest and not _TAX.match(rest[0].strip()) and not _is_address_line(rest[0]):
            value, rest = _clean(rest[0]), rest[1:]
        if not value or _NOT_A_VALUE.match(value):
            continue
        name = None if _NOT_A_NAME.match(value) else value
        return BillingParty(name=name, address=_address_after(rest))
    return BillingParty()


def _address_after(lines: list[str]) -> str | None:
    """A labelled address among the next lines, else the street and postal-code lines right under the name."""
    for line in lines[:4]:
        labelled = _ADDRESS_LABEL.match(line)
        if labelled is not None:
            return " ".join(labelled.group("value").split()) or None
    parts: list[str] = []
    for line in lines[:3]:
        if _NAME_LABEL.match(line) or _TAX.match(line.strip()):
            break
        if not _is_address_line(line):
            if parts:
                break
            continue
        parts.append(" ".join(line.split()).strip(_TRIM))
        if _POSTAL.search(line):
            break
    return ", ".join(parts) if parts else None
