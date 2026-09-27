"""Bank identifiers seen on Portuguese documents: IBAN and Multibanco (§18, §20).

IBAN validation is ISO 13616 mod-97 plus the registered length per country.
Multibanco payment references are "Entidade" (5 digits) + "Referência"
(9 digits); their optional check digits are provider-specific, so only the
format is validated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

# IBAN lengths from the SWIFT IBAN registry (ISO 13616) for the countries a
# Portuguese SME commonly pays. verified_as_of: 2026-09-27. Countries not
# listed are not recognised by find_ibans() rather than guessed.
IBAN_LENGTHS: dict[str, int] = {
    "AT": 20, "BE": 16, "BG": 22, "CH": 21, "CY": 28, "CZ": 24, "DE": 22, "DK": 18,
    "EE": 20, "ES": 24, "FI": 18, "FR": 27, "GB": 22, "GR": 27, "HR": 21, "HU": 28,
    "IE": 22, "IL": 23, "IS": 26, "IT": 27, "LI": 21, "LT": 20, "LU": 20, "LV": 21,
    "MT": 31, "NL": 18, "NO": 15, "PL": 28, "PT": 25, "RO": 24, "SE": 24, "SI": 19,
    "SK": 24,
}

# Country code + check digits, then alphanumerics optionally grouped by spaces.
_IBAN_RUN = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]){8,34}")


def normalize_iban(raw: str) -> str:
    """Remove spaces and upper-case. Does not validate."""
    return re.sub(r"\s+", "", raw or "").upper()


def _mod97(iban: str) -> int:
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97


def is_valid_iban(raw: str) -> bool:
    """Registered country, exact length and mod-97 == 1."""
    iban = normalize_iban(raw)
    expected = IBAN_LENGTHS.get(iban[:2])
    if expected is None or len(iban) != expected or not iban.isalnum():
        return False
    if not iban[2:4].isdigit():
        return False
    return _mod97(iban) == 1


def find_ibans(text: str) -> list[str]:
    """Valid IBANs in ``text``, in order of appearance, without duplicates.

    A run is cut at the country's registered length only when that point is a
    real boundary in the text (end, space or punctuation); otherwise it is
    rejected rather than trimmed to fit.
    """
    found: list[str] = []
    upper = (text or "").upper()
    for match in _IBAN_RUN.finditer(upper):
        iban = _cut_at_length(match.group(0), upper, match.start())
        if iban and is_valid_iban(iban) and iban not in found:
            found.append(iban)
    return found


def _cut_at_length(run: str, text: str, start: int) -> str | None:
    length = IBAN_LENGTHS.get(run[:2])
    if length is None:
        return None
    count = 0
    for offset, char in enumerate(run):
        if char != " ":
            count += 1
        if count == length:
            end = start + offset + 1
            nxt = text[end] if end < len(text) else ""
            if nxt.isalnum():
                return None
            return normalize_iban(run[: offset + 1])
    return None


@dataclass(frozen=True)
class MultibancoReference:
    entity: str  # 5 digits
    reference: str  # 9 digits
    amount: Decimal | None = None

    @property
    def payment_reference(self) -> str:
        """Canonical text used for the core PAYMENT_REFERENCE field."""
        return f"{self.entity} {self.reference}"


class MultibancoError(ValueError):
    """Malformed Multibanco entity or reference."""


def parse_multibanco(entity: str, reference: str, amount: Decimal | None = None) -> MultibancoReference:
    """Validate the format of an entity/reference pair (spaces are ignored)."""
    ent = re.sub(r"\s+", "", entity or "")
    ref = re.sub(r"\s+", "", reference or "")
    if len(ent) != 5 or not ent.isdigit():
        raise MultibancoError("Multibanco entity must have 5 digits")
    if len(ref) != 9 or not ref.isdigit():
        raise MultibancoError("Multibanco reference must have 9 digits")
    if amount is not None and amount <= 0:
        raise MultibancoError("Multibanco amount must be positive")
    return MultibancoReference(entity=ent, reference=ref, amount=amount)
