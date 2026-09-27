"""Redaction before anything goes to an external AI (§52–53).

Only uncertain evidence leaves our infrastructure, and only after bank accounts,
card numbers, contact details and addresses are replaced by placeholder tokens
such as ``[IBAN_1]``. The token map stays local in a :class:`TokenVault`, so the
external answer can be restored on our side.

Detection is deliberately biased toward over-redaction, but avoids eating the
numbers extraction needs (tax ids, invoice numbers, dates, amounts).
Address and phone patterns are best-effort for Portuguese and English text.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType


class PiiKind(str, Enum):
    IBAN = "IBAN"
    CARD = "CARD"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    ADDRESS = "ADDRESS"


ALL_KINDS: frozenset[PiiKind] = frozenset(PiiKind)


@dataclass(frozen=True)
class PiiMatch:
    kind: PiiKind
    start: int
    end: int
    text: str


# --------------------------------------------------------------------------- IBAN

# Fallback lengths used only for IBAN-shaped strings whose checksum fails
# (typically an OCR misread), so they are still redacted.
# Source: SWIFT IBAN Registry. verified_as_of: 2026-09 (author knowledge; re-check
# against the current registry release before extending).
IBAN_LENGTHS: Mapping[str, int] = MappingProxyType(
    {
        "AT": 20, "BE": 16, "CH": 21, "DE": 22, "ES": 24, "FR": 27, "GB": 22,
        "IE": 22, "IL": 23, "IT": 27, "LU": 20, "NL": 18, "PT": 25,
    }
)  # fmt: skip

_IBAN_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,32}")


def iban_is_valid(iban: str) -> bool:
    """ISO 13616 mod-97 check on a compact or spaced IBAN."""
    compact = iban.replace(" ", "").upper()
    if not 15 <= len(compact) <= 34 or not compact.isalnum() or not compact.isascii():
        return False
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


def _find_ibans(text: str) -> Iterable[tuple[int, int]]:
    for m in _IBAN_CANDIDATE.finditer(text):
        span = _best_iban_prefix(m.group(), m.start())
        if span:
            yield span


def _best_iban_prefix(candidate: str, offset: int) -> tuple[int, int] | None:
    """Longest prefix that is a valid IBAN, else one with the registry length."""
    ends = [i + 1 for i, c in enumerate(candidate) if c != " "]
    ends.reverse()
    for end in ends:
        if iban_is_valid(candidate[:end]):
            return offset, offset + end
    expected = IBAN_LENGTHS.get(candidate[:2])
    for end in ends:
        if expected and len(candidate[:end].replace(" ", "")) == expected:
            return offset, offset + end
    return None


# --------------------------------------------------------------------------- cards

_CARD_CANDIDATE = re.compile(r"(?<![\d])\d(?:[ -]?\d){12,24}(?![\d])")


def luhn_is_valid(digits: str) -> bool:
    """Luhn checksum over a digit string."""
    if not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _find_cards(text: str) -> Iterable[tuple[int, int]]:
    for m in _CARD_CANDIDATE.finditer(text):
        span = _best_card_run(m.group(), m.start())
        if span:
            yield span


def _best_card_run(candidate: str, offset: int) -> tuple[int, int] | None:
    """Longest run of whole digit groups with 13–19 digits that passes Luhn."""
    groups = [(g.start(), g.end()) for g in re.finditer(r"\d+", candidate)]
    best: tuple[int, int] | None = None
    for i in range(len(groups)):
        for j in range(i, len(groups)):
            start, end = groups[i][0], groups[j][1]
            digits = re.sub(r"\D", "", candidate[start:end])
            if 13 <= len(digits) <= 19 and luhn_is_valid(digits):
                if best is None or end - start > best[1] - best[0]:
                    best = (start, end)
    return (offset + best[0], offset + best[1]) if best else None


# --------------------------------------------------------------------------- email

_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
)


def _find_emails(text: str) -> Iterable[tuple[int, int]]:
    return (m.span() for m in _EMAIL.finditer(text))


# --------------------------------------------------------------------------- phone

_PHONE_CANDIDATE = re.compile(r"(?<![\w+])\+?\(?\d[\d \t().-]{5,24}\d(?!\w)")
_PHONE_KEYWORD = re.compile(
    r"(?i)\b(?:tel|tlf|tlm|telef\w*|telem\w*|phone|mobile|mob|cell|fax|contacto|"
    r"contact|whatsapp|call)\b[^\n\d]{0,6}$"
)
# Tax identifiers look like phone numbers; never treat them as one.
_TAX_LABEL = re.compile(
    r"(?i)(?:\b(?:nif|nipc|vat|iva|contribuinte|tax\s*id|n\.?\s*º?\s*fiscal)\b[^\n\d]{0,8}"
    r"|(?<![A-Za-z])[A-Z]{2}[ ]?)$"
)
_PT_NATIONAL = re.compile(r"^(?:[29]\d{2}[ .-]?\d{3}[ .-]?\d{3}|2\d[ .-]\d{3}[ .-]\d{2}[ .-]\d{2})$")
_UK_NATIONAL = re.compile(r"^0\d{2,4}[ -]?\d{3,4}[ -]?\d{3,4}$")
_US_NATIONAL = re.compile(r"^\(?\d{3}\)?[ .-]?\d{3}[ .-]\d{4}$")
_INTL_00 = re.compile(r"^00\d{1,3}[ .-]")


def _find_phones(text: str) -> Iterable[tuple[int, int]]:
    for m in _PHONE_CANDIDATE.finditer(text):
        before = text[max(0, m.start() - 30) : m.start()]
        if _is_phone(m.group(), before):
            yield m.span()


def _is_phone(candidate: str, before: str) -> bool:
    digits = re.sub(r"\D", "", candidate)
    if candidate.startswith("+"):
        return 8 <= len(digits) <= 15
    if _TAX_LABEL.search(before):
        return False
    if _PHONE_KEYWORD.search(before):
        return 7 <= len(digits) <= 17
    if digits.startswith("00"):
        return 10 <= len(digits) <= 17 and bool(_INTL_00.match(candidate))
    has_separator = candidate != digits
    if not has_separator:
        return False
    if len(digits) == 9 and _PT_NATIONAL.match(candidate):
        return True
    if len(digits) in (10, 11) and _UK_NATIONAL.match(candidate):
        return True
    return len(digits) == 10 and bool(_US_NATIONAL.match(candidate))


# --------------------------------------------------------------------------- address

_WORD = r"[A-Za-zÀ-ÿ0-9][\wÀ-ÿ'.ºª-]*"
_PT_STREET = re.compile(
    r"(?<![\w])(?:Rua|R\.|Avenida|Av\.|Avª|Travessa|Trav\.|Tv\.|Largo|Lg\.|Praça|Pç\.|"
    r"Praceta|Estrada|Estr\.|Alameda|Calçada|Beco|Rotunda|Urbanização|Urb\.|Bairro|"
    r"Quinta|Caminho)"
    rf"[ \t]+{_WORD}(?:[ \t]+{_WORD}){{0,7}}?"
    r",?[ \t]*(?:n\.?[ \t]?º|nº|n\.|no\.|número)?[ \t]*\d{1,5}[A-Za-z]?\b"
    r"(?:,?[ \t]*\d{1,2}[ \t]?[ºª°.]?[ \t]*(?:andar|esq\.?|esquerdo|dto\.?|dt\.?|direito|"
    r"frente|frt\.?|[A-D]\b)?)?",
    re.IGNORECASE,
)
_PT_POSTAL = re.compile(
    r"(?<![\w/-])\d{4}-\d{3}(?![\w-])"
    r"(?P<locality>[ \t]+[A-ZÀ-Ý][A-Za-zÀ-ÿ'.-]{2,}"
    r"(?:[ \t]+(?:de|do|da|dos|das|e|[A-ZÀ-Ý][A-Za-zÀ-ÿ'.-]+)){0,4})?"
)
_PT_POSTAL_LABEL = re.compile(r"(?i)(?:c[óo]digo\s+postal|c\.?\s?p\.?)\s*:?\s*$")
_DOC_SERIES = re.compile(r"(?i)(?:\b(?:FT|FR|FS|NC|ND|RC|FA|GT|OR|no|nº|n\.º|ref)\.?|#)\s*$")
_LOCALITY_NOT = re.compile(r"(?i)^\s*(?:total|iva|vat|nif|data|date|eur|valor|invoice|fatura|factura)\b")
_EN_STREET = re.compile(
    r"(?<![\w])\d{1,5}[A-Za-z]?,?[ \t]+(?:[A-Z][\w'.-]*[ \t]+){1,4}"
    r"(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct|Place|Pl|"
    r"Square|Sq|Terrace|Way|Close|Crescent|Gardens|Grove|Row|Mews|Parkway|Highway|Hwy)"
    r"\b\.?"
)
_UK_POSTCODE = re.compile(
    r"(?<![\w])(?:[A-Z]{1,2}\d[A-Z\d]?|GIR)[ ]?\d[ABD-HJLNP-UW-Z]{2}(?![\w])"
)
_US_STATES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|"
    "NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
_US_STATE_ZIP = re.compile(
    rf"(?:[A-Z][a-z]+(?:[ ][A-Z][a-z]+)*,[ ])?\b(?:{_US_STATES})[ ]+\d{{5}}(?:-\d{{4}})?(?![\w-])"
)


def _find_addresses(text: str) -> Iterable[tuple[int, int]]:
    for pattern in (_PT_STREET, _EN_STREET, _UK_POSTCODE, _US_STATE_ZIP):
        yield from (m.span() for m in pattern.finditer(text))
    for m in _PT_POSTAL.finditer(text):
        span = _pt_postal_span(text, m)
        if span:
            yield span


def _pt_postal_span(text: str, m: re.Match[str]) -> tuple[int, int] | None:
    """A PT postal code needs a locality after it or a 'Código Postal' label."""
    before = text[max(0, m.start() - 20) : m.start()]
    if _DOC_SERIES.search(before):
        return None
    locality = m.group("locality")
    if locality and not _LOCALITY_NOT.match(locality):
        return m.span()
    if _PT_POSTAL_LABEL.search(before):
        return m.start(), m.start() + 8
    return None


# --------------------------------------------------------------------------- engine

_Finder = Callable[[str], Iterable[tuple[int, int]]]

# Priority order: earlier detectors win overlapping spans.
_DETECTORS: tuple[tuple[PiiKind, _Finder], ...] = (
    (PiiKind.EMAIL, _find_emails),
    (PiiKind.IBAN, _find_ibans),
    (PiiKind.CARD, _find_cards),
    (PiiKind.PHONE, _find_phones),
    (PiiKind.ADDRESS, _find_addresses),
)


def find_pii(text: str, kinds: Iterable[PiiKind] = ALL_KINDS) -> list[PiiMatch]:
    """Non-overlapping sensitive spans in ``text``, sorted by position."""
    wanted = frozenset(kinds)
    taken: list[tuple[int, int]] = []
    found: list[PiiMatch] = []
    for kind, finder in _DETECTORS:
        if kind not in wanted:
            continue
        for start, end in sorted(finder(text), key=lambda s: (s[0], -s[1])):
            if any(start < e and s < end for s, e in taken):
                continue
            taken.append((start, end))
            found.append(PiiMatch(kind, start, end, text[start:end]))
    return sorted(found, key=lambda m: m.start)


def _normalize(kind: PiiKind, value: str) -> str:
    """Key under which equal values share one token."""
    if kind in (PiiKind.IBAN, PiiKind.CARD):
        return re.sub(r"[^0-9A-Za-z]", "", value).upper()
    if kind is PiiKind.PHONE:
        digits = re.sub(r"\D", "", value)
        return digits[2:] if digits.startswith("00") else digits
    return " ".join(value.split()).casefold()


_TOKEN = re.compile(r"\[(?:IBAN|CARD|EMAIL|PHONE|ADDRESS)_\d+\]")


class TokenVault:
    """Local, never-transmitted map from placeholder tokens to original values.

    The same value always gets the same token within one vault, so an external
    model can still tell that two mentions refer to the same account.
    """

    def __init__(self) -> None:
        self._originals: dict[str, str] = {}
        self._by_key: dict[tuple[PiiKind, str], str] = {}
        self._counters: dict[PiiKind, int] = {}

    def token_for(self, kind: PiiKind, value: str, *, avoid: str = "") -> str:
        """Token for ``value``; never one that already appears in ``avoid``."""
        key = (kind, _normalize(kind, value))
        if key in self._by_key:
            return self._by_key[key]
        n = self._counters.get(kind, 0)
        while True:
            n += 1
            token = f"[{kind.value}_{n}]"
            if token not in avoid:
                break
        self._counters[kind] = n
        self._by_key[key] = token
        self._originals[token] = value
        return token

    def restore(self, text: str) -> str:
        """Replace known tokens with originals; unknown tokens are left as they are."""
        return _TOKEN.sub(lambda m: self._originals.get(m.group(), m.group()), text)

    def originals(self) -> Mapping[str, str]:
        """Read-only view of token -> original value."""
        return MappingProxyType(self._originals)

    def __len__(self) -> int:
        return len(self._originals)


@dataclass(frozen=True)
class Redaction:
    """Redacted text safe to send, plus what was removed (kept locally)."""

    text: str
    vault: TokenVault = field(repr=False)
    matches: tuple[PiiMatch, ...] = ()

    def restore(self, external_text: str) -> str:
        return self.vault.restore(external_text)


def redact(
    text: str, *, vault: TokenVault | None = None, kinds: Iterable[PiiKind] = ALL_KINDS
) -> Redaction:
    """Replace sensitive spans with tokens (§53). Pass a shared ``vault`` to keep
    tokens consistent across several texts of one request."""
    vault = vault if vault is not None else TokenVault()
    matches = find_pii(text, kinds)
    parts: list[str] = []
    cursor = 0
    for m in matches:
        parts.append(text[cursor : m.start])
        parts.append(vault.token_for(m.kind, m.text, avoid=text))
        cursor = m.end
    parts.append(text[cursor:])
    return Redaction(text="".join(parts), vault=vault, matches=tuple(matches))


def is_clean(text: str, kinds: Iterable[PiiKind] = ALL_KINDS) -> bool:
    """True when no sensitive span is detected: a last check before sending."""
    return not find_pii(text, kinds)
