"""Documents from suppliers outside Portugal (checklist P7, X31).

Portugal is the only country pack, but a Portuguese company still buys from
Ireland (Meta, Google), the Netherlands (Booking.com), the United States
(AWS, GitHub, OpenAI), the United Kingdom or Spain. Their invoices follow
their own country's conventions: no AT fiscal QR code, no ATCUD, no NIF,
other VAT rates, other date and number formats. This module gives the core
what it needs to read and check them without assuming Portuguese formatting
merely because the *company* is Portuguese:

* :func:`detect_issuer` says which country issued a document: from the
  issuer's VAT number prefix (IE, NL, DE, FR, ES, IT, GB...), a US EIN, the
  country named in its address, or, weakest, the currency. A Portuguese
  fiscal QR code or a labelled NIF means Portugal. When the signals are
  absent or disagree the country is None and callers keep their Portuguese
  rules (§19: never guess);
* :func:`check_tax_number` checks a VAT number by its own country's format
  and, where the country has them, its check digits;
* :func:`vat_rates` lists the standard and reduced VAT rates of EU countries
  and the UK (a small dated table);
* :func:`read_foreign_text` reads English and Spanish invoice labels
  ("Invoice number", "Factura nº", "Amount due", "Importe total",
  "Base imponible", "VAT (21%)"...) with
  :class:`~backoffice.extraction.labelled.LabelledFieldExtractor`, adding
  line locations, stated VAT rates, the tax numbers' roles and the currency;
* :func:`mentions_reverse_charge` spots "reverse charge", "autoliquidação",
  "VAT to be accounted for by the recipient"...

Everything here is a *reading*: verification decides what is confirmed.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from types import MappingProxyType

from backoffice.domain.models import CriticalField, ExtractionMethod, FieldObservation
from backoffice.extraction.labelled import LabelledFieldExtractor
from backoffice.extraction.values import COMMON_CURRENCIES, comparison_key, find_currency
from backoffice.learning.keys import same_tax_id
from backoffice.verification.normalize import iban_is_valid

__all__ = [
    "EU_MEMBERS",
    "FOREIGN_LABELS",
    "ForeignFields",
    "IssuerProfile",
    "TaxNumber",
    "check_tax_number",
    "detect_issuer",
    "document_language",
    "find_tax_numbers",
    "mentions_reverse_charge",
    "read_foreign_text",
    "vat_rates",
]

F = CriticalField

# EU member states (ISO 3166-1 alpha-2; Greece is GR here, EL on VAT numbers).
EU_MEMBERS: frozenset[str] = frozenset(
    "AT BE BG CY CZ DE DK EE ES FI FR GR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK".split()
)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


# --------------------------------------------------------------------------- VAT rates

# Standard and reduced VAT rates of EU countries and the UK, as fractions. Zero is
# never listed: it is only acceptable when the VAT is reverse-charged or the
# supplier is outside the EU, which the caller decides. Dated where a rate changed
# recently. verified_as_of: 2026-09 (author knowledge of the European Commission's
# VAT rates list and HMRC; not re-checked online). Extend, don't guess.


@dataclass(frozen=True)
class _Rate:
    rate: Decimal
    valid_from: date | None = None
    valid_to: date | None = None  # inclusive

    def applies_on(self, day: date | None) -> bool:
        if day is None:  # clock-free: the rates the table lists as still in force
            return self.valid_to is None
        return (self.valid_from is None or self.valid_from <= day) and (
            self.valid_to is None or day <= self.valid_to
        )


def _rates(*entries: str | tuple[str, str | None, str | None]) -> tuple[_Rate, ...]:
    out = []
    for entry in entries:
        if isinstance(entry, str):
            out.append(_Rate(Decimal(entry)))
        else:
            rate, start, end = entry
            out.append(_Rate(Decimal(rate), date.fromisoformat(start) if start else None,
                             date.fromisoformat(end) if end else None))  # fmt: skip
    return tuple(out)


VAT_RATES: Mapping[str, tuple[_Rate, ...]] = MappingProxyType({
    "AT": _rates("0.20", "0.13", "0.10"),
    "BE": _rates("0.21", "0.12", "0.06"),
    "BG": _rates("0.20", "0.09"),
    "CY": _rates("0.19", "0.09", "0.05", "0.03"),
    "CZ": _rates("0.21", ("0.12", "2024-01-01", None), ("0.15", None, "2023-12-31"), ("0.10", None, "2023-12-31")),
    "DE": _rates("0.19", "0.07"),
    "DK": _rates("0.25"),
    "EE": _rates(("0.24", "2025-07-01", None), ("0.22", "2024-01-01", "2025-06-30"), ("0.20", None, "2023-12-31"),
                 ("0.13", "2025-01-01", None), "0.09", "0.05"),
    "ES": _rates("0.21", "0.10", "0.04"),
    "FI": _rates(("0.255", "2024-09-01", None), ("0.24", None, "2024-08-31"), ("0.14", None, "2025-12-31"),
                 ("0.135", "2026-01-01", None), "0.10"),
    "FR": _rates("0.20", "0.10", "0.055", "0.021"),
    "GR": _rates("0.24", "0.13", "0.06", "0.17", "0.09", "0.04"),  # the last three: some Aegean islands
    "HR": _rates("0.25", "0.13", "0.05"),
    "HU": _rates("0.27", "0.18", "0.05"),
    "IE": _rates("0.23", "0.135", "0.09", "0.048"),
    "IT": _rates("0.22", "0.10", "0.05", "0.04"),
    "LT": _rates("0.21", "0.09", "0.05"),
    "LU": _rates("0.17", "0.14", "0.08", "0.03", ("0.16", "2023-01-01", "2023-12-31"),
                 ("0.13", "2023-01-01", "2023-12-31"), ("0.07", "2023-01-01", "2023-12-31")),
    "LV": _rates("0.21", "0.12", "0.05"),
    "MT": _rates("0.18", "0.07", "0.05"),
    "NL": _rates("0.21", "0.09"),
    "PL": _rates("0.23", "0.08", "0.05"),
    "RO": _rates(("0.21", "2025-08-01", None), ("0.11", "2025-08-01", None), ("0.19", None, "2025-07-31"),
                 ("0.09", None, "2025-07-31"), ("0.05", None, "2025-07-31")),
    "SE": _rates("0.25", "0.12", "0.06"),
    "SI": _rates("0.22", "0.095", "0.05"),
    "SK": _rates(("0.23", "2025-01-01", None), ("0.19", "2025-01-01", None), "0.05", ("0.20", None, "2024-12-31"),
                 ("0.10", None, "2024-12-31")),
    "GB": _rates("0.20", "0.05"),
})  # fmt: skip


def _iso(country: str | None) -> str | None:
    if not country:
        return None
    code = country.strip().upper()
    return {"EL": "GR", "XI": "GB", "UK": "GB"}.get(code, code)


def vat_rates(country: str | None, on: date | None = None) -> tuple[Decimal, ...]:
    """Positive VAT rates in force in ``country`` on ``on`` (sorted); empty when the table does not cover it.

    Without ``on`` the rates the table lists as still in force are used (never the clock).
    """
    code = _iso(country)
    entries = VAT_RATES.get(code or "", ())
    return tuple(sorted({e.rate for e in entries if e.applies_on(on)}))


# --------------------------------------------------------------------------- tax numbers

# Printed VAT prefix -> ISO country (Greece prints EL; XI is Northern Ireland).
_VAT_PREFIX_COUNTRY: Mapping[str, str] = MappingProxyType({
    **{c: c for c in EU_MEMBERS if c != "GR"}, "EL": "GR", "GB": "GB", "XI": "GB",
})  # fmt: skip

_GB_FORMAT = r"\d{9}|\d{12}|GD[0-4]\d{2}|HA[5-9]\d{2}"
# The number after the prefix, without separators. Source: European Commission VIES
# formats and HMRC. verified_as_of: 2026-09 (author knowledge).
_FORMATS: Mapping[str, re.Pattern[str]] = MappingProxyType({k: re.compile(v) for k, v in {
    "AT": r"U\d{8}", "BE": r"[01]\d{9}", "BG": r"\d{9,10}", "CY": r"\d{8}[A-Z]", "CZ": r"\d{8,10}",
    "DE": r"[1-9]\d{8}", "DK": r"\d{8}", "EE": r"10\d{7}", "EL": r"\d{9}", "ES": r"[A-Z0-9]\d{7}[A-Z0-9]",
    "FI": r"\d{8}", "FR": r"[0-9A-HJ-NP-Z]{2}\d{9}", "GB": _GB_FORMAT, "HR": r"\d{11}", "HU": r"\d{8}",
    "IE": r"\d{7}[A-W][A-IW]?|\d[A-Z+*]\d{5}[A-W]", "IT": r"\d{11}", "LT": r"\d{9}|\d{12}", "LU": r"\d{8}",
    "LV": r"\d{11}", "MT": r"\d{8}", "NL": r"\d{9}B\d{2}", "PL": r"\d{10}", "PT": r"\d{9}", "RO": r"[1-9]\d{1,9}",
    "SE": r"\d{10}01", "SI": r"\d{8}", "SK": r"\d{10}", "XI": _GB_FORMAT,
}.items()})  # fmt: skip

# US employer identification numbers: the first two digits are an IRS campus prefix.
_EIN_PREFIXES = frozenset(
    [*range(1, 7), *range(10, 17), *range(20, 28), *range(30, 49), *range(50, 69), *range(71, 78),
     *range(80, 89), *range(90, 96), 98, 99]
)  # fmt: skip


@dataclass(frozen=True)
class TaxNumber:
    """A tax number found on a document, checked by its own country's rules.

    ``checksum`` is True or False when the country's numbers carry check
    digits (and this module knows them), None when only the format can be
    checked. ``role`` is what the words around it say ("customer",
    "supplier") or None.
    """

    country: str  # ISO alpha-2: "IE", "GR" (printed EL), "GB" (printed GB or XI), "US"
    prefix: str  # as printed on VAT numbers ("EL", "XI"); "" for a US EIN
    number: str  # compact, without prefix; an EIN keeps its hyphen ("20-4938068")
    kind: str = "vat"  # "vat" | "ein"
    format_ok: bool = True
    checksum: bool | None = None
    line: int = 0
    role: str | None = None

    @property
    def valid(self) -> bool:
        return self.format_ok and self.checksum is not False

    @property
    def printed(self) -> str:
        return f"{self.prefix}{self.number}"


def _digits(text: str) -> list[int]:
    return [int(c) for c in text]


def _luhn_sum(text: str) -> int:
    """ISO/IEC 7812 Luhn checksum of a digit string (0 means a valid number)."""
    total = 0
    for i, d in enumerate(reversed(_digits(text))):
        total += d if i % 2 == 0 else sum(divmod(d * 2, 10))
    return total % 10


def _mod11_10(text: str) -> int:
    """ISO 7064 MOD 11,10 check digit (German USt-IdNr., Croatian OIB)."""
    product = 10
    for d in _digits(text):
        total = (d + product) % 10 or 10
        product = (2 * total) % 11
    return (11 - product) % 10


def _check_at(n: str) -> bool:
    return (6 - _luhn_sum(n[1:8])) % 10 == int(n[8])


def _check_be(n: str) -> bool:
    return 97 - int(n[:8]) % 97 == int(n[8:])


def _check_de(n: str) -> bool:
    return _mod11_10(n[:8]) == int(n[8])


def _check_hr(n: str) -> bool:
    return _mod11_10(n[:10]) == int(n[10])


def _weighted(n: str, weights: Sequence[int]) -> int:
    return sum(w * d for w, d in zip(weights, _digits(n), strict=False))


def _check_dk(n: str) -> bool:
    return _weighted(n, (2, 7, 6, 5, 4, 3, 2, 1)) % 11 == 0


def _check_fi(n: str) -> bool:
    return _weighted(n, (7, 9, 10, 5, 8, 4, 2, 1)) % 11 == 0


def _check_pl(n: str) -> bool:
    check = _weighted(n[:9], (6, 5, 7, 2, 3, 4, 5, 6, 7)) % 11
    return check != 10 and check == int(n[9])


def _check_it(n: str) -> bool:
    return _luhn_sum(n) == 0


def _check_lu(n: str) -> bool:
    return int(n[:6]) % 89 == int(n[6:])


def _check_se(n: str) -> bool:
    return _luhn_sum(n[:10]) == 0


def _check_sk(n: str) -> bool:
    return int(n) % 11 == 0


def _check_ro(n: str) -> bool:
    body = n[:-1].rjust(9, "0")
    return (10 * _weighted(body, (7, 5, 3, 2, 1, 7, 5, 3, 2))) % 11 % 10 == int(n[-1])


def _check_nl(n: str) -> bool:
    eleven = _weighted(n[:8], (9, 8, 7, 6, 5, 4, 3, 2)) % 11
    if eleven != 10 and eleven == int(n[8]):
        return True
    # Sole traders' numbers since 2020: ISO 7064 MOD 97-10 over "NL" + number (letters as 10..35).
    return int("".join(str(int(c, 36)) for c in "NL" + n)) % 97 == 1


def _check_ie(n: str) -> bool:
    if n[1].isalpha() or n[1] in "+*":  # old format 1X23456X -> 0234561X
        n = "0" + n[2:7] + n[0] + n[7]
    total = _weighted(n[:7], (8, 7, 6, 5, 4, 3, 2))
    if len(n) == 9:
        total += 9 * ("WABCDEFGHI".index(n[8]))
    return "WABCDEFGHIJKLMNOPQRSTUV"[total % 23] == n[7]


def _check_gb(n: str) -> bool | None:
    if not n[:9].isdigit():
        return None  # government departments and health authorities: no check digits
    return _weighted(n[:9], (8, 7, 6, 5, 4, 3, 2, 10, 1)) % 97 in (0, 42)


def _check_fr(n: str) -> bool | None:
    if not n[:2].isdigit():
        return None  # the newer alphanumeric keys: format only
    return int(n[:2]) == (12 + 3 * (int(n[2:]) % 97)) % 97


_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"


def _check_es(n: str) -> bool | None:
    if n[:8].isdigit() and n[8].isalpha():  # DNI: a person
        return _DNI_LETTERS[int(n[:8]) % 23] == n[8]
    if n[0] in "XYZ" and n[1:8].isdigit() and n[8].isalpha():  # NIE: a foreign resident
        return _DNI_LETTERS[int(str("XYZ".index(n[0])) + n[1:8]) % 23] == n[8]
    if n[0].isalpha() and n[1:8].isdigit():  # CIF: a company
        digits = _digits(n[1:8])
        odd = sum(sum(divmod(d * 2, 10)) for d in digits[0::2])
        control = (10 - (odd + sum(digits[1::2])) % 10) % 10
        letter = "JABCDEFGHI"[control]
        if n[0] in "KPQSNW":
            return n[8] == letter
        if n[0] in "ABEH":
            return n[8] == str(control)
        return n[8] in (str(control), letter)
    return False


def _check_pt(n: str) -> bool:
    from backoffice.countries.pt.nif import validate_nif

    return validate_nif(n).valid


_CHECKS = MappingProxyType({
    "AT": _check_at, "BE": _check_be, "DE": _check_de, "DK": _check_dk, "ES": _check_es, "FI": _check_fi,
    "FR": _check_fr, "GB": _check_gb, "HR": _check_hr, "IE": _check_ie, "IT": _check_it, "LU": _check_lu,
    "NL": _check_nl, "PL": _check_pl, "PT": _check_pt, "RO": _check_ro, "SE": _check_se, "SK": _check_sk,
    "XI": _check_gb,
})  # fmt: skip


def _judge(prefix: str, number: str, *, line: int = 0, role: str | None = None) -> TaxNumber:
    pattern = _FORMATS.get(prefix)
    country = _VAT_PREFIX_COUNTRY.get(prefix, prefix)
    if pattern is None or not pattern.fullmatch(number):
        return TaxNumber(country, prefix, number, format_ok=False, line=line, role=role)
    check = _CHECKS.get(prefix)
    return TaxNumber(country, prefix, number, checksum=check(number) if check else None, line=line, role=role)


def _compact(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z+*]", "", text).upper()


def check_tax_number(raw: str) -> TaxNumber | None:
    """A VAT number with its country prefix ("IE 9692928F", "NL805734958B01", "EL 094259216"), or a US EIN
    ("20-4938068"), checked by its country's rules. None when it is neither."""
    text = (raw or "").strip()
    ein = re.fullmatch(r"(?:US\s*)?(\d{2})-(\d{7})", text)
    if ein:
        return TaxNumber("US", "", f"{ein[1]}-{ein[2]}", kind="ein", format_ok=int(ein[1]) in _EIN_PREFIXES)
    compact = _compact(text)
    if compact.startswith("CHE") and compact[3:].isdigit():
        return TaxNumber("CH", "CHE", compact[3:12], format_ok=len(compact[3:]) >= 9)
    prefix, number = compact[:2], compact[2:]
    if prefix not in _FORMATS or not any(c.isdigit() for c in number):
        return None
    return _judge(prefix, number)


# ---- finding tax numbers in text

_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(sorted(_FORMATS)) + r")(?=[\s.\-:]?[0-9A-Z])")
_MAX_NUMBER = 14  # the longest VAT number body (without prefix and separators)
_IBAN_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}")
_NIF_LABEL = re.compile(
    r"(?<![a-z])(?:nif|nipc|contribuinte)(?![a-z])[^0-9\n]{0,15}?(\d{3}\s?\d{3}\s?\d{3})(?![0-9])", re.I
)
_ES_LABEL = re.compile(
    r"(?<![A-Za-z])(?:C\.?\s?I\.?\s?F\.?|N\.?\s?I\.?\s?F\.?)(?![A-Za-z])\s*[:.#]?\s*(?:ES[\s-]?)?"
    r"([A-Z]-?\d{7}-?[0-9A-Z]|\d{8}-?[A-Z]|[XYZ]-?\d{7}-?[A-Z])(?![A-Za-z0-9])",
    re.I,
)
_EIN_LABEL = re.compile(
    r"(?<![a-z])(?:f?ein|federal\s+(?:tax\s+)?(?:id|identification)(?:\s+(?:no\.?|number))?"
    r"|employer\s+identification\s+number|(?:us\s+)?tax\s+id(?:\s+(?:no\.?|number))?|tin)(?![a-z])"
    r"[^0-9\n]{0,6}(\d{2}-\d{7})(?![0-9])",
    re.I,
)
_CUSTOMER_WORDS = re.compile(
    r"(?<![a-z])(?:customer|client|bill(?:ed)?\s+to|invoice\s+to|sold\s+to|buyer|your|recipient"
    r"|cliente|destinatario|adquirente|comprador|facturar\s+a)(?![a-z])"
)
_SUPPLIER_WORDS = re.compile(
    r"(?<![a-z])(?:supplier|seller|vendor|issued\s+by|our|proveedor|emisor|vendedor|fornecedor|emitente)(?![a-z])"
)
_CUSTOMER_HEADER = re.compile(
    r"^\s*(?:bill(?:ed)?\s+to|invoice\s+to|sold\s+to|ship\s+to|customer|client|buyer|cliente|facturar\s+a"
    r"|datos\s+del\s+cliente)\s*:?\s*$"
)
_SUPPLIER_HEADER = re.compile(r"^\s*(?:from|seller|supplier|vendor|proveedor|emisor)\s*:?\s*$")


def _iban_spans(line: str) -> list[tuple[int, int]]:
    spans = []
    for m in _IBAN_RE.finditer(line):
        text = m.group(0)
        for end in range(len(text), 14, -1):  # the longest prefix that is a valid IBAN
            if iban_is_valid(text[:end]):
                spans.append((m.start(), m.start() + end))
                break
    return spans


def _number_after(line: str, start: int) -> list[tuple[str, int]]:
    """Alphanumerics after a prefix, single separators allowed: [(compact so far, end index in line)]."""
    out: list[tuple[str, int]] = []
    compact = ""
    i = start
    if i < len(line) and line[i] in " .-:":
        i += 1
    while i < len(line) and len(compact) < _MAX_NUMBER:
        ch = line[i]
        if (ch.isalnum() and ch.isascii()) or ch in "+*":
            compact += ch.upper()
            out.append((compact, i + 1))
            i += 1
        elif ch in " .-" and i + 1 < len(line) and line[i + 1].isalnum():
            i += 1
        else:
            break
    return out


def _role(folded: list[str], index: int, before: str) -> str | None:
    customer = bool(_CUSTOMER_WORDS.search(before))
    supplier = bool(_SUPPLIER_WORDS.search(before))
    if customer != supplier:
        return "customer" if customer else "supplier"
    for back in range(index - 1, max(index - 5, -1), -1):
        line = folded[back]
        if not line.strip():
            break
        if _CUSTOMER_HEADER.match(line):
            return "customer"
        if _SUPPLIER_HEADER.match(line):
            return "supplier"
    return None


def find_tax_numbers(text: str) -> list[TaxNumber]:
    """Tax numbers printed in ``text``: VAT numbers with their country prefix anywhere, a labelled
    Portuguese NIF or Spanish NIF/CIF without one, and a labelled US EIN. Parts of an IBAN are never
    read as a VAT number. Only numbers whose format fits are returned (check digits may still fail)."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    folded = [_fold(line) for line in lines]
    found: dict[tuple[str, str], TaxNumber] = {}

    def keep(tn: TaxNumber) -> None:
        found.setdefault((tn.country, tn.number), tn)

    for index, line in enumerate(lines):
        ibans = _iban_spans(line)
        for m in _PREFIX_RE.finditer(line):
            if any(s <= m.start() < e for s, e in ibans):
                continue
            prefix = m.group(1)
            pattern = _FORMATS[prefix]
            for compact, end in reversed(_number_after(line, m.end())):
                if pattern.fullmatch(compact) and not (end < len(line) and line[end].isalnum()):
                    keep(_judge(prefix, compact, line=index + 1, role=_role(folded, index, folded[index][:m.start()])))
                    break
        for m in _NIF_LABEL.finditer(line):
            tn = _judge("PT", re.sub(r"\s", "", m.group(1)), line=index + 1,
                        role=_role(folded, index, folded[index][:m.start()]))
            if tn.valid:
                keep(tn)
        for m in _ES_LABEL.finditer(line):
            tn = _judge("ES", m.group(1).replace("-", "").upper(), line=index + 1,
                        role=_role(folded, index, folded[index][:m.start()]))
            if tn.valid:
                keep(tn)
        for m in _EIN_LABEL.finditer(line):
            ein = check_tax_number(m.group(1))
            if ein is not None and ein.format_ok:
                keep(replace(ein, line=index + 1, role=_role(folded, index, folded[index][:m.start()])))
    return [tn for tn in found.values() if tn.format_ok]


# --------------------------------------------------------------------------- the issuer's country

# Country names printed in addresses (folded). Portugal is left out of detection:
# on a purchase invoice it is usually the customer's address.
_COUNTRY_WORDS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "GB": ("united kingdom", "great britain", "northern ireland", "england", "scotland", "wales"),
    "US": ("united states of america", "united states"),
    "IE": ("ireland", "eire"),
    "NL": ("the netherlands", "netherlands", "nederland"),
    "DE": ("germany", "deutschland"),
    "FR": ("france",),
    "ES": ("spain", "espana"),
    "IT": ("italy", "italia"),
    "BE": ("belgium", "belgique", "belgie"),
    "LU": ("luxembourg", "luxemburg"),
    "AT": ("austria", "osterreich"),
    "SE": ("sweden", "sverige"),
    "DK": ("denmark", "danmark"),
    "FI": ("finland", "suomi"),
    "PL": ("poland", "polska"),
    "CH": ("switzerland", "schweiz", "suisse"),
    "PT": ("portugal",),
})  # fmt: skip
_COUNTRY_RE = re.compile(
    r"(?<![a-z])(" + "|".join(sorted((re.escape(w) for ws in _COUNTRY_WORDS.values() for w in ws),
                                     key=len, reverse=True)) + r")(?![a-z])"
)  # fmt: skip
_WORD_COUNTRY = {w: c for c, ws in _COUNTRY_WORDS.items() for w in ws}
_US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK "
    "OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
_US_ADDRESS = re.compile(r"(?<![A-Za-z])(?:USA|U\.S\.A\.|(?:" + "|".join(_US_STATES) + r")\s+\d{5}(?:-\d{4})?)(?![A-Za-z0-9])")

# A currency named on its own points (weakly) at its country.
_CURRENCY_COUNTRY: Mapping[str, str] = MappingProxyType({
    "USD": "US", "GBP": "GB", "CHF": "CH", "SEK": "SE", "DKK": "DK", "NOK": "NO", "PLN": "PL", "CZK": "CZ",
    "HUF": "HU", "RON": "RO", "CAD": "CA", "AUD": "AU", "NZD": "NZ", "JPY": "JP", "ILS": "IL", "BRL": "BR",
    "MXN": "MX", "CNY": "CN",
})  # fmt: skip

_REVERSE_CHARGE = re.compile(
    r"(?<![a-z])(?:reverse[\s-]?charge[ds]?|autoliquida\w*|auto-liquida\w*|autoliquidation"
    r"|(?:vat|tax|iva)\s+(?:is\s+)?(?:to\s+be\s+)?(?:accounted\s+for|payable|due|self[\s-]?assessed)\s+by\s+the\s+"
    r"(?:recipient|customer|buyer|client)"
    r"|inversion\s+del\s+sujeto\s+pasivo|sujeto\s+pasivo\s+inverso|steuerschuldnerschaft\s+des\s+leistungsempfangers"
    r"|btw\s+verlegd|inversione\s+contabile|article\s+196|art\.?\s*196)(?![a-z])"
)


def mentions_reverse_charge(text: str) -> bool:
    """The document says the customer accounts for the VAT (EN, PT, ES, FR, DE, NL, IT wording)."""
    return _REVERSE_CHARGE.search(_fold(text or "")) is not None


_LANGUAGE_MARKERS: Mapping[str, re.Pattern[str]] = MappingProxyType({
    "pt": re.compile(r"(?<![a-z])(fatura|faturas|nif|contribuinte|data de emissao|base tributavel|atcud|morada"
                     r"|iva incluido|recibo|valor)(?![a-z])"),
    "es": re.compile(r"(?<![a-z])(factura|fecha|base imponible|importe|cif|vencimiento|cuota|domicilio|forma de pago"
                     r"|numero de factura)(?![a-z])"),
    "en": re.compile(r"(?<![a-z])(invoice|amount due|subtotal|bill to|billed to|date of issue|vat number|total due"
                     r"|due date|receipt|quantity|unit price|description|balance due|amount paid)(?![a-z])"),
})  # fmt: skip


def document_language(text: str) -> str | None:
    """"pt", "es" or "en" by the invoice words the text uses (distinct markers); None when unclear."""
    folded = _fold(text or "")
    counts = {lang: len(set(p.findall(folded))) for lang, p in _LANGUAGE_MARKERS.items()}
    best = max(counts.values())
    leaders = [lang for lang, n in counts.items() if n == best]
    return leaders[0] if best and len(leaders) == 1 else None


@dataclass(frozen=True)
class IssuerProfile:
    """Which country issued a document, and why (plain words), with what the rest of the core needs.

    ``country`` is None when nothing says, or the signals disagree: callers
    then keep their Portuguese rules. ``stated_rates`` are the VAT rates the
    document prints ("VAT (21%)"), filled in by the text reader.
    """

    country: str | None = None
    basis: tuple[str, ...] = ()
    tax_number: TaxNumber | None = None
    reverse_charge: bool = False
    language: str | None = None
    stated_rates: tuple[Decimal, ...] = ()

    @property
    def is_foreign(self) -> bool:
        return self.country is not None and self.country != "PT"

    @property
    def in_eu(self) -> bool:
        return self.country in EU_MEMBERS

    @property
    def reads_foreign(self) -> bool:
        """Read with the international labels: a foreign issuer, or an unknown one writing English or Spanish."""
        return self.is_foreign or (self.country is None and self.language in ("en", "es"))

    @property
    def has_valid_vat_number(self) -> bool:
        tn = self.tax_number
        return tn is not None and tn.kind == "vat" and tn.valid and tn.country == self.country

    @property
    def needs_vat_number(self) -> bool:
        """EU and UK invoices must show the supplier's VAT number."""
        return self.in_eu or self.country == "GB"

    @property
    def zero_vat_reason(self) -> str | None:
        """Why an invoice from this issuer may charge no VAT (plain words), or None when it may not."""
        if not self.is_foreign:
            return None
        if self.reverse_charge:
            return "the invoice says you account for the VAT yourself"
        if self.in_eu and self.has_valid_vat_number:
            return "a business buying from another EU country usually accounts for the VAT itself"
        if not self.in_eu:
            return "suppliers outside the EU don't charge EU VAT"
        return None

    @property
    def reverse_charge_candidate(self) -> bool:
        """No VAT charged by a foreign supplier may mean the business must declare it (for the accountant)."""
        return self.is_foreign and (self.reverse_charge or not self.in_eu or self.has_valid_vat_number)


def _is_own(tn: TaxNumber, own_tax_ids: Collection[str]) -> bool:
    return tn.kind == "vat" and any(same_tax_id(own, tn.printed) for own in own_tax_ids)


def _named_countries(text: str) -> set[str]:
    folded = _fold(text or "")
    found = {_WORD_COUNTRY[m.group(1)] for m in _COUNTRY_RE.finditer(folded)}
    if _US_ADDRESS.search(text or ""):
        found.add("US")
    return found


def detect_issuer(
    text: str,
    *,
    own_tax_ids: Collection[str] = (),
    fiscal_qr: bool = False,
    structured_tax_ids: Iterable[str] = (),
) -> IssuerProfile:
    """The issuing country of a document, strongest signal first (module docstring).

    ``own_tax_ids`` are the business's own numbers, *with* their country
    ("PT516123459"): on a purchase invoice they name the customer, never the
    issuer. ``structured_tax_ids`` are supplier numbers from a structured copy
    (an e-invoice), which count like printed ones.
    """
    language = document_language(text)
    reverse = mentions_reverse_charge(text)
    base = IssuerProfile(reverse_charge=reverse, language=language)
    if fiscal_qr:
        return replace(base, country="PT", basis=("a Portuguese fiscal QR code",))
    numbers = find_tax_numbers(text)
    for raw in structured_tax_ids:
        tn = check_tax_number(raw)
        if tn is not None and tn.format_ok:
            numbers.append(replace(tn, role="supplier"))
    issuers = [n for n in numbers if n.valid and n.role != "customer" and not _is_own(n, own_tax_ids)]
    countries = sorted({n.country for n in issuers})
    if len(countries) == 1:
        number = issuers[0]
        what = "its US tax number" if number.kind == "ein" else "its VAT number"
        return replace(base, country=countries[0], basis=(f"{what} {number.printed}",), tax_number=number)
    if len(countries) > 1:
        return replace(base, basis=("tax numbers from more than one country",))
    named = _named_countries(text) - {"PT"}
    if len(named) == 1:
        country = named.pop()
        return replace(base, country=country, basis=("the country in its address",))
    if len(named) > 1:
        return replace(base, basis=("more than one country in its address",))
    currency = find_currency(text or "")
    if currency in _CURRENCY_COUNTRY:
        return replace(base, country=_CURRENCY_COUNTRY[currency], basis=(f"amounts in {currency}",))
    return base


# --------------------------------------------------------------------------- reading English and Spanish text

# Labels (matched longest first, case-insensitive, at the start of a line or of a
# segment separated by a wide gap). An unlabelled number is never read. Tax numbers
# are found by :func:`find_tax_numbers` instead of labels, so their role is known.
FOREIGN_LABELS: Mapping[CriticalField, tuple[str, ...]] = MappingProxyType({
    F.INVOICE_NUMBER: (
        "invoice number", "invoice no.", "invoice no", "invoice nr.", "invoice nr", "invoice #", "invoice id",
        "invoice ref", "tax invoice number", "tax invoice no.", "receipt number", "receipt no.", "receipt #",
        "document number", "bill number",
        "número de factura", "numero de factura", "nº de factura", "n.º de factura", "no. de factura",
        "núm. factura", "num. factura", "factura nº", "factura n.º", "factura n°", "factura no.", "factura núm.",
        "factura num.", "factura número", "factura numero", "nº factura", "n.º factura", "n° factura",
    ),
    F.ISSUE_DATE: (
        "invoice date", "date of issue", "issue date", "date issued", "issued on", "billing date", "document date",
        "receipt date", "date paid", "date",
        "fecha de emisión", "fecha de emision", "fecha de expedición", "fecha de expedicion", "fecha de factura",
        "fecha factura", "fecha",
    ),
    F.DUE_DATE: ("due date", "payment due date", "payment due", "due", "fecha de vencimiento", "vencimiento"),
    F.NET_AMOUNT: (
        "subtotal", "sub-total", "sub total", "net amount", "net total", "total net", "total excl. vat",
        "total excluding vat", "total excl. tax", "total before tax", "amount excl. vat",
        "base imponible", "importe neto", "total sin iva", "subtotal sin iva",
    ),
    F.VAT_AMOUNT: (
        "vat", "vat amount", "total vat", "vat total", "tax", "tax amount", "total tax", "sales tax",
        "iva", "cuota iva", "cuota de iva", "importe iva", "importe del iva", "total iva",
    ),
    F.GROSS_AMOUNT: (
        "total", "total amount", "total amount due", "grand total", "total due", "amount due", "balance due",
        "invoice total",
        "total incl. vat", "total including vat", "total inc. vat", "total incl. tax", "amount paid", "total paid",
        "total to pay", "importe total", "total factura", "total a pagar", "total con iva", "total iva incluido",
    ),
    F.CURRENCY: ("currency", "moneda", "divisa"),
    F.IBAN: ("iban",),
    F.PAYMENT_REFERENCE: ("payment reference", "referencia de pago"),
})  # fmt: skip

_VAT_LABELS = frozenset(_fold(label) for label in FOREIGN_LABELS[F.VAT_AMOUNT])
_DUE_LABELS = frozenset({"amount due", "balance due", "total due", "total amount due"})
_ALL_LABELS = sorted({_fold(label) for labels in FOREIGN_LABELS.values() for label in labels}, key=len, reverse=True)
_LABEL_START = re.compile(r"^\s*(" + "|".join(re.escape(label) for label in _ALL_LABELS) + r")(?![a-z0-9])")
# "VAT (21%): 21.00", "IVA 21 %: 42,00", "VAT @ 20%: 40.00", "Total (EUR): 121.00"
_QUALIFIED = re.compile(
    r"^(?P<label>\s*[^\W\d_][^\d():@%]*?)\s*"
    r"(?:\((?P<paren>[^()]{1,24})\)|@\s*(?P<at>\d{1,2}(?:[.,]\d{1,3})?)\s*%|(?P<pct>\d{1,2}(?:[.,]\d{1,3})?)\s*%)"
    r"(?P<rest>\s*[:#]?.*)$"
)
_PERCENT = re.compile(r"^\s*(\d{1,2}(?:[.,]\d{1,3})?)\s*%\s*$")
_GAP = re.compile(r"\t+|\s{3,}|\s\|\s")
_TEXT_CONFIDENCE = 0.5  # a labelled value in the document's own text (verification decides)
_TAX_NUMBER_CONFIDENCE = 0.6  # its format (and check digits, where they exist) passed
_OWN_NUMBER_CONFIDENCE = 0.7  # the business's own number: the customer


@dataclass(frozen=True)
class ForeignFields:
    """What :func:`read_foreign_text` found: observations per field, and the VAT rates printed."""

    observations: Mapping[CriticalField, tuple[FieldObservation, ...]]
    stated_rates: tuple[Decimal, ...] = ()


@lru_cache(maxsize=3)
def _extractor(day_first: bool | None) -> LabelledFieldExtractor:
    return LabelledFieldExtractor(FOREIGN_LABELS, confidence=_TEXT_CONFIDENCE, day_first=day_first)


def _segments(line: str) -> list[str]:
    """A line cut where a wide gap is followed by another label ("Invoice no: 12    Date: ...")."""
    pieces = _GAP.split(line)
    out: list[str] = []
    for piece in pieces:
        if out and not _LABEL_START.match(_fold(piece)):
            out[-1] = f"{out[-1]}   {piece}"
        else:
            out.append(piece)
    return [p for p in out if p.strip()]


def _rate(text: str) -> Decimal | None:
    try:
        value = Decimal(text.replace(",", ".")) / 100
    except InvalidOperation:
        return None
    return value if 0 <= value < 1 else None


def _qualified(segment: str) -> tuple[str, str, Decimal | None, str | None]:
    """(segment without a qualifier after its label, the label, a stated rate, a currency in brackets)."""
    m = _QUALIFIED.match(segment)
    if m is None:
        return segment, _label_of(segment), None, None
    label = " ".join(_fold(m["label"]).split()).rstrip(" :")
    if label not in _ALL_LABELS:
        return segment, _label_of(segment), None, None
    rate = currency = None
    if m["at"] or m["pct"]:
        rate = _rate(m["at"] or m["pct"])
    elif m["paren"]:
        inner = m["paren"].strip()
        percent = _PERCENT.match(inner)
        if percent:
            rate = _rate(percent.group(1))
        elif inner.upper() in COMMON_CURRENCIES or inner in ("€", "£"):
            currency = {"€": "EUR", "£": "GBP"}.get(inner, inner.upper())
    return f"{m['label'].rstrip()}{m['rest']}", label, (rate if label in _VAT_LABELS else None), currency


def _label_of(segment: str) -> str:
    m = _LABEL_START.match(_fold(segment))
    return m.group(1) if m else ""


def read_foreign_text(
    text: str,
    source: str,
    *,
    method: ExtractionMethod = ExtractionMethod.OCR,
    issuer: IssuerProfile | None = None,
    own_tax_ids: Collection[str] = (),
) -> ForeignFields:
    """Candidate observations from an English or Spanish invoice text (or any text with those labels).

    Dates are read in the issuer's convention: month first for the United
    States, day first elsewhere; for an unknown issuer writing English, only
    dates that cannot be read two ways. An "amount due" of zero on a paid
    invoice is not its total. Every value keeps its line ("text:line 7").
    """
    issuer = issuer or IssuerProfile()
    if issuer.country == "US":
        day_first: bool | None = False
    elif issuer.country is not None or issuer.language == "es":
        day_first = True
    else:
        day_first = None
    extractor = _extractor(day_first)
    found: dict[CriticalField, dict[str, FieldObservation]] = {}
    rates: list[Decimal] = []
    dollar_line: int | None = None

    def add(field: CriticalField, obs: FieldObservation) -> None:
        found.setdefault(field, {}).setdefault(str(comparison_key(field, obs.value)), obs)

    for number, line in enumerate((text or "").replace("\r\n", "\n").split("\n"), start=1):
        where = f"text:line {number}"
        for raw in _segments(line):
            segment, label, rate, bracket = _qualified(raw)
            if rate is not None:
                rates.append(rate)
            if bracket is not None:
                add(F.CURRENCY, FieldObservation(value=bracket, source=source, method=method,
                                                 confidence=_TEXT_CONFIDENCE, location=where))  # fmt: skip
            for field, observations in extractor(segment, source, method).items():
                for obs in observations:
                    if field is F.GROSS_AMOUNT and label in _DUE_LABELS and obs.value == 0:
                        continue  # "Amount due: $0.00" on a paid invoice: not its total
                    if field is F.GROSS_AMOUNT and "$" in segment:
                        dollar_line = dollar_line or number
                    add(field, obs.model_copy(update={"location": where}))
    if F.CURRENCY not in found and issuer.country == "US" and dollar_line is not None:
        add(F.CURRENCY, FieldObservation(value="USD", source=source, method=method, confidence=_TEXT_CONFIDENCE,
                                         location=f"text:line {dollar_line} ($ on a US invoice)"))  # fmt: skip
    for tn in find_tax_numbers(text):
        if not tn.valid:
            continue
        own = _is_own(tn, own_tax_ids)
        field = F.CUSTOMER_TAX_ID if own or tn.role == "customer" else F.SUPPLIER_TAX_ID
        add(field, FieldObservation(value=tn.printed, source=source, method=method,
                                    confidence=_OWN_NUMBER_CONFIDENCE if own else _TAX_NUMBER_CONFIDENCE,
                                    location=f"text:line {tn.line}"))  # fmt: skip
    return ForeignFields(
        observations=MappingProxyType({f: tuple(by_key.values()) for f, by_key in found.items()}),
        stated_rates=tuple(dict.fromkeys(rates)),
    )
