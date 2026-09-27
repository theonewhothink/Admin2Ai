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

import bisect
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType


class PiiKind(str, Enum):
    IBAN = "IBAN"
    BANK_ACCOUNT = "ACCOUNT"  # domestic formats: PT NIB, UK sort code / account number
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
    text: str = field(repr=False)  # the sensitive value: keep it out of logs


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

_IBAN_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,32}", re.IGNORECASE
)


def iban_is_valid(iban: str) -> bool:
    """ISO 13616 mod-97 check on a compact or spaced IBAN."""
    compact = iban.replace(" ", "").upper()
    if not 15 <= len(compact) <= 34 or not compact.isalnum() or not compact.isascii():
        return False
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


def _find_ibans(text: str) -> Iterable[tuple[int, int]]:
    for m in _IBAN_CANDIDATE.finditer(text):
        end = _iban_end(m.group())
        if end:
            yield m.start(), m.start() + end


def _iban_end(candidate: str) -> int | None:
    """Where the IBAN at the start of ``candidate`` ends, if there is one.

    The candidate may run into following words ("... 9015 4 EUR"), so:
    known country: take exactly the registry length (redacted even when the
    checksum fails, e.g. an OCR misread, if the country code is uppercase);
    other countries: the longest checksum-valid prefix, preferring word ends.
    """
    ends = [i + 1 for i, c in enumerate(candidate) if c != " "][::-1]
    country = candidate[:2]
    expected = IBAN_LENGTHS.get(country.upper())
    if expected:
        for end in ends:
            if len(candidate[:end].replace(" ", "")) == expected:
                valid = iban_is_valid(candidate[:end])
                return end if valid or country.isupper() else None
        return None
    at_word_end = [e for e in ends if e == len(candidate) or candidate[e] == " "]
    return next((e for e in at_word_end + ends if iban_is_valid(candidate[:e])), None)


# --------------------------------------------------------------------------- domestic accounts

# Digits separated by at most one space, dot or hyphen: "4111 1111 1111 1111".
_DIGIT_RUN = re.compile(r"(?<![\d])\d(?:[ .-]?\d)*(?![\d])")


def _checked_digit_spans(
    text: str,
    min_digits: int,
    max_digits: int,
    max_groups: int,
    shape_ok: Callable[[tuple[int, ...]], bool],
    check: Callable[[str], bool],
    preferred_digits: int | None = None,
) -> Iterable[tuple[int, int]]:
    """Per digit run, the best run of whole groups with a plausible printed
    grouping (``shape_ok`` on the group lengths, one separator throughout)
    whose digits pass ``check``.

    Best means ``preferred_digits`` long if possible, else the longest.
    Neighbouring numbers ("Order 12 4111 1111 1111 1111 2027") are left out.
    At most ``max_groups`` groups are joined, so long numeric tables stay cheap.
    """
    for run in _DIGIT_RUN.finditer(text):
        chunk = run.group()
        groups = [g.span() for g in re.finditer(r"\d+", chunk)]
        # separator in front of each group ("" for the first group)
        seps = [""] + [
            chunk[a_end:b_start] for (_, a_end), (b_start, _) in zip(groups, groups[1:])
        ]
        best: tuple[int, int] | None = None
        best_key: tuple[bool, int] = (False, -1)
        for i, (start, _) in enumerate(groups):
            digits = ""
            shape: tuple[int, ...] = ()
            for j, (g_start, end) in enumerate(groups[i : i + max_groups], start=i):
                if j > i + 1 and seps[j] != seps[i + 1]:
                    break  # a printed number uses one separator throughout
                digits += chunk[g_start:end]
                shape += (end - g_start,)
                if len(digits) > max_digits:
                    break
                if len(digits) >= min_digits and shape_ok(shape) and check(digits):
                    key = (len(digits) == preferred_digits, end - start)
                    if key > best_key:
                        best, best_key = (start, end), key
        if best:
            yield run.start() + best[0], run.start() + best[1]


def nib_is_valid(digits: str) -> bool:
    """PT NIB: 21 digits whose check digits make the number ≡ 1 (mod 97).

    That property is why every Portuguese IBAN starts with "PT50".
    """
    return len(digits) == 21 and digits.isdigit() and int(digits) % 97 == 1


_UK_SORT_CODE = re.compile(
    r"(?i)\bsort[ \t]*code\b[ \t]*:?[ \t]*(\d{2}[ -]?\d{2}[ -]?\d{2})(?![\d])"
)
_UK_ACCOUNT_NO = re.compile(
    r"(?i)\b(?:account[ \t]*(?:number|no\.?)|acc(?:t|ount)?[ \t]*no\.?|a/c)[ \t]*:?[ \t]*(\d{8})(?![\d])"
)


# How a NIB is printed: compact, bank-branch-account-check, or IBAN-style fours.
_NIB_SHAPES = frozenset({(21,), (4, 4, 11, 2), (4, 4, 4, 4, 4, 1)})


def _find_bank_accounts(text: str) -> Iterable[tuple[int, int]]:
    yield from _checked_digit_spans(
        text, 21, 21, 6, _NIB_SHAPES.__contains__, nib_is_valid
    )
    for pattern in (_UK_SORT_CODE, _UK_ACCOUNT_NO):
        yield from (m.span(1) for m in pattern.finditer(text))


# --------------------------------------------------------------------------- cards


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


def _card_shape(lengths: tuple[int, ...]) -> bool:
    """Compact, fours (last group 1–4 digits), or Amex/Diners 4-6-5 / 4-6-4."""
    if len(lengths) == 1 or lengths in ((4, 6, 5), (4, 6, 4)):
        return True
    return all(n == 4 for n in lengths[:-1]) and 1 <= lengths[-1] <= 4


def _find_cards(text: str) -> Iterable[tuple[int, int]]:
    """Card numbers: 13–19 digits passing Luhn, in a printed card grouping.

    A 16-digit reading wins over a longer one, so a number printed right after
    the card is not swallowed; the next scan pass picks that number up.
    """
    return _checked_digit_spans(text, 13, 19, 5, _card_shape, luhn_is_valid, 16)


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
    r"(?:(?i:\b(?:nif|nipc|vat|iva|contribuinte|tax\s*id|n\.?\s*º?\s*fiscal)\b)[^\n\d]{0,8}"
    r"|(?<![A-Za-z])[A-Z]{2}[ ]?)$"  # country prefix such as "PT 509 123 456": uppercase only
)
_PT_NATIONAL = re.compile(
    r"^(?:[29]\d{2}[ .-]?\d{3}[ .-]?\d{3}|2\d[ .-]\d{3}[ .-]\d{2}[ .-]\d{2})$"
)
_UK_NATIONAL = re.compile(r"^0\d{2,4}[ -]?\d{3,4}[ -]?\d{3,4}$")
_US_NATIONAL = re.compile(r"^\(?\d{3}\)?[ .-]?\d{3}[ .-]\d{4}$")
_INTL_00 = re.compile(r"^00[1-9]\d{0,2}[ .-]")  # country codes never start with 0
_DATE_SHAPE = re.compile(
    r"^(?:\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}[-./]\d{1,2}[-./]\d{4})(?!\d)"
)


def _find_phones(text: str) -> Iterable[tuple[int, int]]:
    for m in _PHONE_CANDIDATE.finditer(text):
        before = text[max(0, m.start() - 30) : m.start()]
        end = _phone_end(m.group(), before)
        if end:
            yield m.start(), m.start() + end


def _phone_end(candidate: str, before: str) -> int | None:
    """Longest whitespace-bounded prefix that reads as a phone number, so a
    date or amount printed right after it ("... 0958 18.09.2026") is left out."""
    ends = [len(candidate)] + [w.start() for w in re.finditer(r"[ \t]+", candidate)][
        ::-1
    ]
    for end in ends:
        prefix = candidate[:end].rstrip(" \t().-")
        if prefix and _is_phone(prefix, before):
            return len(prefix)
    return None


def _is_phone(candidate: str, before: str) -> bool:
    digits = re.sub(r"\D", "", candidate)
    if candidate.startswith("+"):
        return 8 <= len(digits) <= 15 and not digits.startswith("0")
    if _TAX_LABEL.search(before) or _DATE_SHAPE.match(candidate):
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
    r"(?:[ \t]+(?:de|do|da|dos|das)[ \t]+[A-ZÀ-Ý][A-Za-zÀ-ÿ'.-]+){0,2})?"
)
_PT_POSTAL_LABEL = re.compile(r"(?i)(?:c[óo]digo\s+postal|c\.?\s?p\.?)\s*:?\s*$")
# Document series codes (uppercase) and number labels right before "2026-183".
_DOC_SERIES = re.compile(
    r"(?:\b(?:FT|FR|FS|NC|ND|RC|FA|GT|OR)|(?i:\b(?:no|nº|n\.º|ref)\.?)|#)[ \t]*$"
)
_LOCALITY_NOT = re.compile(
    r"(?i)^\s*(?:total|iva|vat|nif|data|date|eur|valor|invoice|fatura|factura)\b"
)
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
# "City, ST 12345": the city is required so codes like "OR 12345" are left alone.
_US_STATE_ZIP = re.compile(
    rf"\b[A-Z][a-z]+(?:[ ][A-Z][a-z]+)*,[ ](?:{_US_STATES})[ ]+\d{{5}}(?:-\d{{4}})?(?![\w-])"
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
    (PiiKind.BANK_ACCOUNT, _find_bank_accounts),
    (PiiKind.PHONE, _find_phones),
    (PiiKind.CARD, _find_cards),
    (PiiKind.ADDRESS, _find_addresses),
)


# Found spans are masked with this (same length, matched by no detector) and the
# text is scanned again, so values glued together ("+44 20 7946 0958 4111 1111
# 1111 1111") are all caught. Two re-scans settle every case we have seen.
_MASK = "\x00"
_MAX_PASSES = 3


def find_pii(text: str, kinds: Iterable[PiiKind] = ALL_KINDS) -> list[PiiMatch]:
    """Non-overlapping sensitive spans in ``text``, sorted by position."""
    wanted = frozenset(kinds)
    found = _scan(text, wanted)
    for _ in range(_MAX_PASSES - 1):
        if not found:
            break
        masked = _masked(text, found)
        extra = [m for m in _scan(masked, wanted) if _MASK not in m.text]
        if not extra:
            break
        found = sorted(found + extra, key=lambda m: m.start)
    return found


def _masked(text: str, matches: list[PiiMatch]) -> str:
    parts: list[str] = []
    cursor = 0
    for m in matches:
        parts += [text[cursor : m.start], _MASK * (m.end - m.start)]
        cursor = m.end
    parts.append(text[cursor:])
    return "".join(parts)


def _scan(text: str, wanted: frozenset[PiiKind]) -> list[PiiMatch]:
    """One pass of every wanted detector; earlier detectors win overlaps."""
    taken: list[tuple[int, int]] = []  # sorted, non-overlapping
    found: list[PiiMatch] = []
    for kind, finder in _DETECTORS:
        if kind not in wanted:
            continue
        for start, end in sorted(finder(text), key=lambda s: (s[0], -s[1])):
            if _overlaps(taken, start, end):
                continue
            bisect.insort(taken, (start, end))
            found.append(PiiMatch(kind, start, end, text[start:end]))
    return sorted(found, key=lambda m: m.start)


def _overlaps(taken: list[tuple[int, int]], start: int, end: int) -> bool:
    """Whether [start, end) overlaps a span in the sorted, disjoint ``taken``."""
    i = bisect.bisect_left(taken, (start, end))
    before = i > 0 and taken[i - 1][1] > start
    after = i < len(taken) and taken[i][0] < end
    return before or after


def _normalize(kind: PiiKind, value: str) -> str:
    """Key under which equal values share one token."""
    if kind in (PiiKind.IBAN, PiiKind.BANK_ACCOUNT, PiiKind.CARD):
        return re.sub(r"[^0-9A-Za-z]", "", value).upper()
    if kind is PiiKind.PHONE:
        digits = re.sub(r"\D", "", value)
        return digits[2:] if digits.startswith("00") else digits
    return " ".join(value.split()).casefold()


_TOKEN = re.compile(r"\[(?:" + "|".join(k.value for k in PiiKind) + r")_\d+\]")


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
    matches: tuple[PiiMatch, ...] = field(default=(), repr=False)

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
