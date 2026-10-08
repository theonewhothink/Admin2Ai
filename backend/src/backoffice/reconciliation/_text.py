"""Text, identifier and money helpers shared by the reconciliation package.

Internal module: nothing here is owner-facing on its own. Money is Decimal
throughout; floats are refused (§ standards, §54 "Why?" lines show exact money).
"""

from __future__ import annotations

import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

__all__ = [
    "MINUS",
    "compile_identifier",
    "currency_code",
    "days_phrase",
    "is_currency_code",
    "fold",
    "format_money",
    "normalize_iban",
    "normalize_tax_id",
    "same_tax_id",
    "scale_to_int",
    "squash",
    "tokens",
]

MINUS = "−"  # true minus sign, same width as digits in tabular fonts (§30-33)
_CURRENCY_SYMBOLS = {"EUR": "€", "GBP": "£", "USD": "$"}
_TOKEN = re.compile(r"[A-Z0-9]+")
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")


def fold(text: str | None) -> str:
    """Uppercase ASCII with accents removed: 'Comunicações' -> 'COMUNICACOES'."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.upper()


def currency_code(value: str | None) -> str:
    """ISO currency code as compared everywhere: ' eur' -> 'EUR' ('' when absent).

    Feeds and extractors disagree on case and padding; the domain model stores
    whatever they sent, so every comparison goes through this.
    """
    return (value or "").strip().upper()


def is_currency_code(value: str | None) -> bool:
    """True for a three-letter code after :func:`currency_code` ('usd' yes, 'US$' no)."""
    return bool(_CURRENCY_CODE.match(currency_code(value)))


def tokens(text: str | None) -> list[str]:
    """Alphanumeric tokens of the folded text."""
    return _TOKEN.findall(fold(text))


def squash(text: str | None) -> str:
    """Folded text with every separator removed: 'FT 2026/183' -> 'FT2026183'."""
    return "".join(tokens(text))


def normalize_iban(value: str | None) -> str:
    """IBAN without spaces or punctuation, uppercased ('' when absent)."""
    return squash(value)


def normalize_tax_id(value: str | None) -> str:
    """Tax id uppercased without separators: 'PT 123 456 789' -> 'PT123456789'."""
    return squash(value)


def same_tax_id(a: str | None, b: str | None) -> bool:
    """True when two tax ids are equal, ignoring an optional 2-letter country prefix.

    'PT123456789' == '123456789'. Only a leading alphabetic pair is ever dropped,
    so 'B12345678' and 'ESB12345678' also compare equal.
    """
    na, nb = normalize_tax_id(a), normalize_tax_id(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return _without_country(na) == nb or na == _without_country(nb)


def _without_country(value: str) -> str:
    if len(value) > 4 and value[:2].isalpha():
        return value[2:]
    return value


@lru_cache(maxsize=4096)
def compile_identifier(identifier: str) -> re.Pattern[str] | None:
    """Pattern finding an invoice number / payment reference inside bank text.

    Separators may differ ('FT 2026/183' vs 'FT2026-183') but the identifier must
    stand alone: 'FT 2026/183' does not match inside 'FT 2026/1834'. Returns None
    when the identifier is too weak to search for (short or digit-free), so a
    stray '183' never counts as evidence.
    """
    core = squash(identifier)
    if not _strong_identifier(core):
        return None
    gap = r"[^A-Z0-9]*"
    body = gap.join(re.escape(ch) for ch in core)
    return re.compile(rf"(?<![A-Z0-9]){body}(?![A-Z0-9])")


def _strong_identifier(core: str) -> bool:
    if not any(ch.isdigit() for ch in core):
        return False
    if core.isdigit():
        return len(core) >= 6
    return len(core) >= 4


def _check_money(amount: Decimal | int) -> Decimal:
    if isinstance(amount, (bool, float)) or not isinstance(amount, (Decimal, int)):
        raise TypeError("money must be Decimal (or int), never float")
    value = Decimal(amount)
    if not value.is_finite():
        raise ValueError("money must be finite")
    return value


def format_money(amount: Decimal | int, currency: str = "EUR") -> str:
    """'€83.21', '−£12.00', 'CHF 1,492.30'. Two decimals, exact Decimal rounding."""
    value = _check_money(amount)
    code = currency.strip().upper()
    if not _CURRENCY_CODE.match(code):
        raise ValueError(f"not a currency code: {currency!r}")
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = MINUS if rounded < 0 else ""
    digits = f"{abs(rounded):,.2f}"
    symbol = _CURRENCY_SYMBOLS.get(code)
    return f"{sign}{symbol}{digits}" if symbol else f"{sign}{code} {digits}"


def days_phrase(days: int) -> str:
    """'same day' / '1 day apart' / '12 days apart'."""
    days = abs(days)
    if days == 0:
        return "same day"
    return f"{days} day apart" if days == 1 else f"{days} days apart"


def scale_to_int(values: list[Decimal]) -> tuple[list[int], int]:
    """Scale Decimals to exact integers with one shared power of ten.

    Returns (integers, scale). Works for any number of decimals (JPY, BHD,
    FX-derived amounts), so subset sums stay exact.
    """
    places = 2
    for value in values:
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int):
            places = max(places, -exponent)
    scale = 10**places
    return [int(value * scale) for value in values], scale
