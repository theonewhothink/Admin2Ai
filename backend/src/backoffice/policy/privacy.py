"""Redaction before anything goes to an external AI (§52–53).

Only uncertain evidence leaves our infrastructure, and only after bank accounts,
card numbers, contact details and addresses are replaced by placeholder tokens
such as ``[IBAN_1]``. The token map stays local in a :class:`TokenVault`, so the
external answer can be restored on our side.

Detection is deliberately biased toward over-redaction, but avoids eating the
numbers extraction needs (tax ids, invoice numbers, dates, amounts).
Address and phone patterns are best-effort for English text and, from the country packs ("privacy.<concept>"),
Portuguese addresses, postal codes and phone numbers (backoffice.countries.wording).

Text from PDFs and OCR often uses no-break or thin spaces, typographic hyphens
and invisible characters inside numbers; detection runs on a normalized copy
and reports spans of the original text. Token-shaped text already present in
the input (``[IBAN_1]``) is itself replaced by a vault token, so every token
that leaves is one the vault issued and can never alias another value.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from functools import cache
from types import MappingProxyType

from backoffice.countries import LazyPattern, pack_alternatives, pack_words


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

# IBAN lengths by country: they fix where an IBAN ends when words follow it, and
# let an uppercase, four-grouped IBAN with a failed checksum (typically an OCR
# misread) still be redacted. Other countries rely on the checksum alone.
# Source: SWIFT IBAN Registry. verified_as_of: 2026-09 (author knowledge; re-check
# against the current registry release before extending).
IBAN_LENGTHS: Mapping[str, int] = MappingProxyType(
    {
        "AT": 20, "BE": 16, "CH": 21, "DE": 22, "ES": 24, "FR": 27, "GB": 22,
        "IE": 22, "IL": 23, "IT": 27, "LU": 20, "NL": 18, "PT": 25,
    }
)  # fmt: skip

# Groups separated by single spaces or hyphens ("DE89-3704-0044-...").
_IBAN_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?:[ -]?[A-Z0-9]){11,32}", re.IGNORECASE
)


def iban_is_valid(iban: str) -> bool:
    """ISO 13616 mod-97 check on a compact or spaced IBAN."""
    compact = iban.replace(" ", "").upper()
    if not 15 <= len(compact) <= 34 or not compact.isalnum() or not compact.isascii():
        return False
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


def _checksum_ends(candidate: str) -> frozenset[int]:
    """End offsets at which the compact prefix of ``candidate`` passes mod-97.

    One pass instead of one full check per prefix: the check value is the part
    after the first four characters followed by those four, so a running
    remainder of the rest is combined with the head at every end. ``candidate``
    holds only ASCII letters, digits and spaces.
    """
    positions = [i for i, c in enumerate(candidate) if c != " "]
    if len(positions) < 15:
        return frozenset()
    head = "".join(str(int(candidate[i], 36)) for i in positions[:4])
    head_value, head_scale = int(head), 10 ** len(head)
    ends: set[int] = set()
    remainder = 0
    for count, i in enumerate(positions[4:34], start=5):
        value = int(candidate[i], 36)
        remainder = (remainder * (100 if value > 9 else 10) + value) % 97
        if count >= 15 and (remainder * head_scale + head_value) % 97 == 1:
            ends.add(i + 1)
    return frozenset(ends)


def _find_ibans(text: str) -> Iterable[tuple[int, int]]:
    pos = 0
    while m := _IBAN_CANDIDATE.search(text, pos):
        end = _iban_end(m.group())
        if end:
            yield m.start(), m.start() + end
        # resume right after the IBAN: the candidate may reach into the next one
        pos = m.start() + (end or 1)


def _group_shape(spaced: str) -> tuple[int, ...]:
    return tuple(len(g) for g in spaced.split(" "))


def _iban_shape_ok(shape: tuple[int, ...]) -> bool:
    """Compact, or spaced with the country code and check digits as the first group."""
    return len(shape) == 1 or shape[0] == 4


def _fours(shape: tuple[int, ...]) -> bool:
    """Compact, or the standard print format: groups of four, last one shorter."""
    return len(shape) == 1 or (all(n == 4 for n in shape[:-1]) and 1 <= shape[-1] <= 4)


def _iban_end(candidate: str) -> int | None:
    """Where the IBAN at the start of ``candidate`` ends, if there is one.

    The candidate may run into following words ("... 9015 4 EUR"), so letters
    must keep the country code's case, and then:
    known country: exactly the registry length, checksum-valid, or (for an
    uppercase, four-grouped print, e.g. an OCR misread) the length alone;
    other countries: the longest checksum-valid prefix, preferring the
    four-grouped print and then word ends.
    """
    candidate = candidate.replace("-", " ")  # same length: offsets stay valid
    country = candidate[:2]
    upper = country.isupper()
    limit = next(
        (
            i
            for i, c in enumerate(candidate)
            if not c.isascii() or (c.isalpha() and c.isupper() != upper)
        ),
        len(candidate),
    )
    candidate = candidate[:limit]
    ends = [i + 1 for i, c in enumerate(candidate) if c != " "]  # ends[k]: k+1 characters
    valid = _checksum_ends(candidate)
    expected = IBAN_LENGTHS.get(country.upper())
    if expected:
        if len(ends) < expected:
            return None
        end = ends[expected - 1]
        shape = _group_shape(candidate[:end])
        if _iban_shape_ok(shape) and end in valid:
            return end
        return end if upper and _fours(shape) else None
    ok = [
        e
        for e in sorted(valid, reverse=True)
        if _iban_shape_ok(_group_shape(candidate[:e]))
    ]
    at_word_end = [e for e in ok if e == len(candidate) or candidate[e] == " "]
    printed = [e for e in at_word_end if _fours(_group_shape(candidate[:e]))]
    return next(iter(printed + at_word_end + ok), None)


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
    """Runs of whole digit groups with a plausible printed grouping (``shape_ok``
    on the group lengths, one separator throughout) whose digits pass ``check``.

    Scans each digit run left to right; at each start the best window wins
    (``preferred_digits`` long if possible, else the longest), so neighbouring
    numbers ("Order 12 4111 1111 1111 1111 2027") are left out and several
    values in one run are all found. At most ``max_groups`` groups are joined.
    """
    for run in _DIGIT_RUN.finditer(text):
        chunk = run.group()
        groups = [g.span() for g in re.finditer(r"\d+", chunk)]
        # separator in front of each group ("" for the first group)
        seps = [""] + [
            chunk[a_end:b_start] for (_, a_end), (b_start, _) in zip(groups, groups[1:])
        ]
        i = 0
        while i < len(groups):
            best = _best_window(
                chunk, groups, seps, i, min_digits, max_digits, max_groups,
                shape_ok, check, preferred_digits,
            )  # fmt: skip
            if best is None:
                i += 1
                continue
            end_group, end = best
            yield run.start() + groups[i][0], run.start() + end
            i = end_group + 1


def _best_window(
    chunk: str,
    groups: list[tuple[int, int]],
    seps: list[str],
    i: int,
    min_digits: int,
    max_digits: int,
    max_groups: int,
    shape_ok: Callable[[tuple[int, ...]], bool],
    check: Callable[[str], bool],
    preferred_digits: int | None,
) -> tuple[int, int] | None:
    """Best valid window starting at group ``i``: (index of last group, end offset)."""
    best: tuple[int, int] | None = None
    best_key: tuple[bool, int] = (False, -1)
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
            key = (len(digits) == preferred_digits, end)
            if key > best_key:
                best, best_key = (j, end), key
    return best


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

# Unicode letters are allowed on both sides ("joão@café.pt"): over-redaction is safer.
_EMAIL = re.compile(r"(?<![\w.+-])[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,}\b")


def _find_emails(text: str) -> Iterable[tuple[int, int]]:
    return (m.span() for m in _EMAIL.finditer(text))


# --------------------------------------------------------------------------- phone

# Never starts inside a code such as "INV-00123" or "2026/183".
_PHONE_CANDIDATE = re.compile(r"(?<![\w+])(?<!\w[-/.])\+?\(?\d[\d \t().-]{5,}\d(?!\w)")
# The last group belongs to another token: "678 14:42", "678 14,50", "678 23%".
_GLUED_AFTER = re.compile(r"[:%€/]|[.,]\d")
_PHONE_KEYWORD = LazyPattern(lambda: (
    r"(?i)\b(?:tel|tlf|telef\w*|phone|mobile|mob|cell|fax|contacto|"
    rf"contact|whatsapp|call{_or('phone_keyword')})\b[^\n\d]{{0,6}}$"
))
# Tax identifiers look like phone numbers; never treat them as one.
_TAX_LABEL = LazyPattern(lambda: (
    rf"(?:(?i:\b(?:nif|vat|iva|tax\s*id|n\.?\s*º?\s*fiscal{_or('tax_label')})\b)[^\n\d]{{0,8}}"
    r"|(?<![A-Za-z])[A-Z]{2}[ ]?)$"  # country prefix such as "PT 509 123 456": uppercase only
))
_UK_NATIONAL = re.compile(r"^0\d{2,4}[ -]?\d{3,4}[ -]?\d{3,4}$")
# NANP: area code and exchange start with 2-9; "(555) 123-4567" is accepted as written.
_US_NATIONAL = re.compile(
    r"^(?:\(\d{3}\)[ .-]?\d{3}|[2-9]\d{2}[ .-][2-9]\d{2})[ .-]\d{4}$"
)
_INTL_00 = re.compile(r"^00[1-9]\d{0,2}[ .-]")  # country codes never start with 0
_DATE_SHAPE = re.compile(
    r"^(?:\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}[-./]\d{1,2}[-./]\d{4})(?!\d)"
)


def _find_phones(text: str) -> Iterable[tuple[int, int]]:
    for m in _PHONE_CANDIDATE.finditer(text):
        yield from _phones_in(text, m)


_PHONE_MAX_WORDS = 6  # "+351 21 345 67 89" is five


def _phones_in(text: str, m: re.Match[str]) -> Iterable[tuple[int, int]]:
    """Phone numbers among the whitespace-separated words of one candidate.

    Leftmost, longest first, so "qty 3 - (555) 123-4567" and
    "+351 912 345 678 18.09.2026" yield just the phone number. Windows never
    include a date, and the digits of a labelled tax id are skipped.
    """
    words = [w.span() for w in re.finditer(r"[^ \t]+", m.group())]
    texts = [m.group()[a:b] for a, b in words]
    digits = [sum(c.isdigit() for c in t) for t in texts]
    dates = [bool(_DATE_SHAPE.match(t)) for t in texts]
    last = len(words) - (1 if _GLUED_AFTER.match(text, m.end()) else 0)
    i = 0
    while i < last:
        start = m.start() + words[i][0]
        lead = text[start]
        before = text[max(0, start - 30) : start]
        if lead != "+" and _TAX_LABEL.search(before):
            i = _skip_tax_id(digits, i)
            continue
        found = None
        if lead in "+(0123456789":
            keyword = bool(_PHONE_KEYWORD.search(before))
            low, high = _phone_digit_range(lead, texts[i], keyword)
            for j in range(min(last, i + _PHONE_MAX_WORDS), i, -1):
                if any(dates[i:j]) or not low <= sum(digits[i:j]) <= high:
                    continue
                end = m.start() + words[j - 1][0] + len(texts[j - 1].rstrip(".-("))
                if _is_phone(text[start:end], keyword=keyword):
                    found = j
                    yield start, end
                    break
        i = found if found else i + 1


def _phone_digit_range(lead: str, first_word: str, keyword: bool) -> tuple[int, int]:
    """How many digits a phone number starting like this can have."""
    if lead == "+":
        return 8, 15
    if keyword:
        return 7, 15
    if first_word.startswith("00"):
        return 10, 17
    return 9, 11  # PT, UK and US national formats


def _skip_tax_id(digits: list[int], i: int) -> int:
    """Index of the first word after a tax id starting at word ``i`` (9+ digits)."""
    seen = 0
    while i < len(digits) and seen < 9:
        seen += digits[i]
        i += 1
    return i


def _is_phone(candidate: str, *, keyword: bool) -> bool:
    """Whether ``candidate`` reads as a phone number (tax labels are checked by
    the caller). ``keyword``: a word such as "Tel" precedes it."""
    digits = re.sub(r"\D", "", candidate)
    if candidate.startswith("+"):
        return 8 <= len(digits) <= 15 and not digits.startswith("0")
    if _DATE_SHAPE.match(candidate):
        return False
    if keyword:
        return 7 <= len(digits) <= 15  # E.164 maximum
    if digits.startswith("00"):
        return 10 <= len(digits) <= 17 and bool(_INTL_00.match(candidate))
    if candidate == digits:
        return False  # bare national numbers need a separator or a keyword
    if any(national.match(candidate) for national in _national_phones()):  # as a pack's country writes them
        return True
    if len(digits) in (10, 11) and _UK_NATIONAL.match(candidate):
        return True
    return len(digits) == 10 and bool(_US_NATIONAL.match(candidate))


# --------------------------------------------------------------------------- address

# A pack's own addresses ("privacy.street", any case: Portugal's "Rua ... 12, 3.º Esq."), postal codes
# ("privacy.postal": its "code" group the code, its "locality" group the place after it), what a postal code needs
# before it ("privacy.postal_label") or must not have before it ("privacy.doc_series": a document number such as
# "FT 2026-183"), and words that are never a place ("privacy.locality_not"). Full patterns, one per entry.


def _or(concept: str) -> str:
    return "".join(f"|{w}" for w in pack_words(f"privacy.{concept}"))


@cache
def _patterns(concept: str, flags: int = 0) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, flags) for p in pack_words(f"privacy.{concept}"))


def _national_phones() -> tuple[re.Pattern[str], ...]:
    return _patterns("national_phone")


def _streets() -> tuple[re.Pattern[str], ...]:
    return _patterns("street", re.IGNORECASE)


def _postal_codes() -> tuple[re.Pattern[str], ...]:
    return _patterns("postal")


_POSTAL_LABEL = LazyPattern(lambda: rf"(?i)(?:{pack_alternatives('privacy.postal_label')})\s*:?\s*$")
# Document series codes (uppercase) and number labels right before "2026-183".
_DOC_SERIES = LazyPattern(lambda: (
    rf"(?:\b(?:{pack_alternatives('privacy.doc_series')})|(?i:\b(?:no|nº|n\.º|ref)\.?)|#)[ \t]*$"
))
_LOCALITY_NOT = LazyPattern(lambda: (
    rf"(?i)^\s*(?:total|iva|vat|nif|data|date|eur|valor|invoice|factura{_or('locality_not')})\b"
))
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
# "City, ST 12345": state and ZIP after a comma, so "OR 12345" (a quote number)
# and the words before the city are left alone.
_US_STATE_ZIP = re.compile(rf"(?<=, )(?:{_US_STATES})[ ]+\d{{5}}(?:-\d{{4}})?(?![\w-])")


def _find_addresses(text: str) -> Iterable[tuple[int, int]]:
    for pattern in (*_streets(), _EN_STREET, _UK_POSTCODE, _US_STATE_ZIP):
        yield from (m.span() for m in pattern.finditer(text))
    for postal in _postal_codes():
        for m in postal.finditer(text):
            span = _postal_span(text, m)
            if span:
                yield span


def _postal_span(text: str, m: re.Match[str]) -> tuple[int, int] | None:
    """A postal code needs a locality after it or a label ('Código Postal') before it."""
    before = text[max(0, m.start() - 20) : m.start()]
    if _DOC_SERIES.search(before):
        return None
    locality = m.group("locality")
    if locality and not _LOCALITY_NOT.match(locality):
        return m.span()
    if _POSTAL_LABEL.search(before):
        return m.start("code"), m.end("code")
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

# Characters that PDF text layers and OCR put where ASCII separators belong.
_SPACE_LIKE = frozenset(
    "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009"
    "\u200a\u202f\u205f\u3000"
)
_DASH_LIKE = frozenset("\u2010\u2011\u2012\u2013\u2212\ufe63\uff0d")
# Soft hyphen, zero-width space / joiners, word joiner, BOM: dropped entirely.
_INVISIBLE = frozenset("\u00ad\u200b\u200c\u200d\u2060\ufeff")
_UNUSUAL = re.compile("[" + "".join(sorted(_SPACE_LIKE | _DASH_LIKE | _INVISIBLE)) + "]")

_Span = tuple[PiiKind, int, int]


def _normalized(text: str) -> tuple[str, list[int] | None]:
    """ASCII-separator copy of ``text`` and, if it differs, each character's original index."""
    if not _UNUSUAL.search(text):
        return text, None
    chars: list[str] = []
    index: list[int] = []
    for i, c in enumerate(text):
        if c in _INVISIBLE:
            continue
        chars.append(" " if c in _SPACE_LIKE else "-" if c in _DASH_LIKE else c)
        index.append(i)
    return "".join(chars), index


def find_pii(text: str, kinds: Iterable[PiiKind] = ALL_KINDS) -> list[PiiMatch]:
    """Non-overlapping sensitive spans in ``text``, sorted by position.

    Offsets and ``PiiMatch.text`` always refer to the original ``text``.
    """
    wanted = frozenset(kinds)
    scanned, index = _normalized(text)
    matches: list[PiiMatch] = []
    for kind, start, end in _find_spans(scanned, wanted):
        if index is not None:
            start, end = index[start], index[end - 1] + 1
        matches.append(PiiMatch(kind, start, end, text[start:end]))
    return matches


def _find_spans(text: str, wanted: frozenset[PiiKind]) -> list[_Span]:
    found = _scan(text, wanted)
    for _ in range(_MAX_PASSES - 1):
        if not found:
            break
        masked = _masked(text, found)
        extra = [
            (kind, start, end)
            for kind, start, end in _scan(masked, wanted)
            if _MASK not in masked[start:end]
        ]
        if not extra:
            break
        found = sorted(found + extra, key=lambda span: span[1])
    return found


def _masked(text: str, spans: list[_Span]) -> str:
    parts: list[str] = []
    cursor = 0
    for _, start, end in spans:
        parts += [text[cursor:start], _MASK * (end - start)]
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _scan(text: str, wanted: frozenset[PiiKind]) -> list[_Span]:
    """One pass of every wanted detector; earlier detectors win overlaps."""
    taken: list[tuple[int, int]] = []  # sorted, non-overlapping
    found: list[_Span] = []
    for kind, finder in _DETECTORS:
        if kind not in wanted:
            continue
        for start, end in sorted(finder(text), key=lambda s: (s[0], -s[1])):
            if _overlaps(taken, start, end):
                continue
            bisect.insort(taken, (start, end))
            found.append((kind, start, end))
    return sorted(found, key=lambda span: span[1])


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
    model can still tell that two mentions refer to the same account (§26).
    Restoring gives back the first spelling seen for that value.
    """

    def __init__(self) -> None:
        self._originals: dict[str, str] = {}
        self._by_key: dict[tuple[PiiKind, str], str] = {}
        self._counters: dict[PiiKind, int] = {}

    def token_for(self, kind: PiiKind, value: str, *, avoid: str = "") -> str:
        """Token for ``value``; never one that already appears in ``avoid``."""
        return self._issue(kind, _normalize(kind, value), value, avoid)

    def _token_for_literal(self, literal: str, *, avoid: str) -> str:
        """A fresh token standing for token-shaped text found in the input.

        Its key lives in a namespace no normalized value can reach ("\\x00..."),
        so it never shares a token with a real value.
        """
        kind = PiiKind(literal[1:-1].rsplit("_", 1)[0])
        return self._issue(kind, "\x00" + literal, literal, avoid)

    def _issue(self, kind: PiiKind, normalized: str, value: str, avoid: str) -> str:
        key = (kind, normalized)
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
    tokens consistent across several texts of one request.

    Token-shaped text already in ``text`` is replaced by a fresh token too (and
    restored to itself), so an injected "[IBAN_1]" cannot make the external
    answer restore to a real value from another text. ``matches`` lists only
    the sensitive values.
    """
    vault = vault if vault is not None else TokenVault()
    matches = find_pii(text, kinds)
    taken = [(m.start, m.end) for m in matches]
    edits: list[tuple[int, int, PiiMatch | None]] = [(m.start, m.end, m) for m in matches]
    edits += [
        (*literal.span(), None)
        for literal in _TOKEN.finditer(text)
        if not _overlaps(taken, *literal.span())
    ]
    parts: list[str] = []
    cursor = 0
    for start, end, match in sorted(edits, key=lambda e: e[0]):  # tokens numbered in text order
        if match is None:
            token = vault._token_for_literal(text[start:end], avoid=text)
        else:
            token = vault.token_for(match.kind, match.text, avoid=text)
        parts += [text[cursor:start], token]
        cursor = end
    parts.append(text[cursor:])
    return Redaction(text="".join(parts), vault=vault, matches=tuple(matches))


def is_clean(text: str, kinds: Iterable[PiiKind] = ALL_KINDS) -> bool:
    """True when no sensitive span is detected: a last check before sending."""
    return not find_pii(text, kinds)
