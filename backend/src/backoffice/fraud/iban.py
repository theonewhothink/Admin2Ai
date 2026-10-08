"""IBAN normalization and ISO 13616 mod-97 validation, done locally (§26).

No network lookups: the check digits alone tell a mistyped or doctored IBAN
from a well-formed one. A valid checksum proves nothing about who owns the
account; that is what the fraud engine's known-IBAN comparison is for.

IBAN lengths per country come from the SWIFT IBAN Registry (ISO 13616).
verified_as_of: not re-checked against the live registry in this build; only
countries whose length is long-established are listed. Countries not listed
fall back to the generic 15..34 character bounds, so a missing entry can never
reject a valid IBAN; mod-97 remains the authoritative check.
"""

from __future__ import annotations

import re

__all__ = [
    "IBAN_LENGTHS",
    "find_ibans",
    "iban_country",
    "is_valid_iban",
    "mask_iban",
    "normalize_iban",
]

IBAN_LENGTHS: dict[str, int] = {
    "AT": 20, "BE": 16, "BG": 22, "CH": 21, "CY": 28, "CZ": 24, "DE": 22, "DK": 18,
    "EE": 20, "ES": 24, "FI": 18, "FR": 27, "GB": 22, "GR": 27, "HR": 21, "HU": 28,
    "IE": 22, "IL": 23, "IS": 26, "IT": 27, "LI": 21, "LT": 20, "LU": 20, "LV": 21,
    "MC": 27, "MT": 31, "NL": 18, "NO": 15, "PL": 28, "PT": 25, "RO": 24, "SE": 24,
    "SI": 19, "SK": 24, "SM": 27,
}  # fmt: skip

_SHAPE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
_MAX_BBAN = 30
# Invisible characters a copy/paste (or a fraudster) can put inside an IBAN.
_INVISIBLE = "\u00ad\u200b\u200c\u200d\u2060\ufeff"
_SEPARATORS = re.compile(rf"[\s\-.{_INVISIBLE}]+")
# An IBAN starts at a word boundary with a country code and two check digits.
_START = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2}\d{2}")


def _is_separator(ch: str) -> bool:
    return ch.isspace() or ch in "-." or ch in _INVISIBLE


def normalize_iban(raw: str) -> str:
    """'pt50 0002.0123-1234 5678 9015 4' -> 'PT50000201231234567890154'; drops an 'IBAN' label.

    Any whitespace (non-breaking, thin, line breaks) and invisible characters
    (zero-width space, soft hyphen, BOM) are separators, never part of the number.
    """
    text = _SEPARATORS.sub(" ", raw).strip().upper()
    if text.startswith("IBAN"):
        text = text[4:].lstrip(":# ")
    return _SEPARATORS.sub("", text)


def _mod97(iban: str) -> int:
    rearranged = iban[4:] + iban[:4]
    remainder = 0
    for ch in rearranged:
        digits = str(int(ch, 36))  # 'A' -> 10 ... 'Z' -> 35
        for d in digits:
            remainder = (remainder * 10 + int(d)) % 97
    return remainder


def is_valid_iban(raw: str | None) -> bool:
    """Shape, registered length (when known) and mod-97 check digits."""
    if not raw:
        return False
    iban = normalize_iban(raw)
    if not _SHAPE.match(iban):
        return False
    expected = IBAN_LENGTHS.get(iban[:2])
    if expected is not None and len(iban) != expected:
        return False
    return _mod97(iban) == 1


def iban_country(raw: str) -> str:
    """ISO country code of an IBAN ('PT50…' -> 'PT')."""
    return normalize_iban(raw)[:2]


def mask_iban(raw: str) -> str:
    """'PT50 •••• 0154': enough to recognise, not enough to copy."""
    iban = normalize_iban(raw)
    return f"{iban[:4]} •••• {iban[-4:]}"


def _groups_after(text: str, start: int) -> list[str]:
    """Alphanumeric groups following ``start``, each separated by one run of separators."""
    groups: list[str] = []
    current: list[str] = []
    length, i = 0, start
    while i < len(text) and length < _MAX_BBAN:
        ch = text[i]
        if ch.isascii() and ch.isalnum():
            current.append(ch)
            length += 1
            i += 1
            continue
        if not _is_separator(ch):
            break
        if current:
            groups.append("".join(current))
            current = []
        while i < len(text) and _is_separator(text[i]):
            i += 1
    if current:
        groups.append("".join(current))
    return groups


def _candidates(head: str, groups: list[str]) -> list[str]:
    """Longest first: the registered length when known, else every whole-group prefix."""
    body = "".join(groups)
    expected = IBAN_LENGTHS.get(head[:2])
    if expected is not None:
        return [head + body[: expected - 4]]
    cuts, total = [], 0
    for group in groups:
        total += len(group)
        cuts.append(total)
    return [head + body[:cut] for cut in reversed(cuts)]


def find_ibans(text: str) -> list[str]:
    """Valid IBANs mentioned in free text (email bodies), normalized, in order, unique.

    Every possible start is examined, so one IBAN can never hide the next
    ("old: PT50 … new: GB82 …"). For countries without a registered length,
    the longest run of whole groups that passes mod-97 wins, so a trailing word
    is not glued onto the number.
    """
    found: list[str] = []
    source = text or ""
    for match in _START.finditer(source):
        head = match.group(0).upper()
        groups = _groups_after(source, match.end())
        for candidate in _candidates(head, groups):
            if is_valid_iban(candidate):
                normalized = normalize_iban(candidate)
                if normalized not in found:
                    found.append(normalized)
                break
    return found
