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

from backoffice.countries import LazyPattern, pack_alternatives, pack_words

__all__ = ["BillingParty", "read_billing_party"]


@dataclass(frozen=True)
class BillingParty:
    name: str | None = None
    address: str | None = None


# English and Spanish labels; a pack's own are in "addressee.<concept>" (Portugal's "Dados do cliente", "Morada de
# faturação", "Rua", its postal codes "4000-123"), alternatives in any case.


def _pt(concept: str) -> str:
    return pack_alternatives(f"addressee.{concept}")


def _or(concept: str) -> str:
    return "".join(f"|{w}" for w in pack_words(f"addressee.{concept}"))


_NAME_LABEL = LazyPattern(lambda: (
    rf"^\s*(?:{_pt('name_prefix')})?(?:cliente|comprador|destinat[aá]rio|customer|client|bill(?:ed)?\s+to|"
    rf"invoice\s+to|sold\s+to|facturar\s+a|facturado\s+a{_or('name')})(?![a-z])(?:\s+(?:name{_or('name_word')}))?"
    r"\s*(?::|-|–)?\s*(?P<value>.*)$"), re.IGNORECASE)
_ADDRESS_LABEL = LazyPattern(lambda: (
    rf"^\s*(?:address|billing\s+address|domic[ií]lio|direcci[oó]n{_or('address')})(?:{_pt('address_suffix')})?"
    r"\s*:\s*(?P<value>.+)$"), re.IGNORECASE)
_TAX = LazyPattern(lambda: (rf"(?<![a-z])(?:{pack_alternatives('tax_id_label')}|n\.?\s?i\.?\s?f\.?|"
                            r"vat(?:\s+(?:no|number))?|tax\s*id|cif)(?![a-z])"), re.IGNORECASE)
_STREET = LazyPattern(lambda: (
    r"^\s*(?:av\.?|avenida|street|st\.|road|rd\.|lane|calle|c/|paseo|plaza"
    rf"{_or('street')})(?![a-z])"), re.IGNORECASE)
_NUMBERED_STREET = re.compile(
    r"^\s*\d+[a-z]?,?\s+.*(?<![a-z])(?:street|st|road|rd|avenue|ave|lane|ln|drive|dr|way|place|square|court|"
    r"boulevard|blvd)(?![a-z])", re.IGNORECASE)
_POSTAL = LazyPattern(lambda: _pt("postal_code"))  # a postal code as the packs' countries write it
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
