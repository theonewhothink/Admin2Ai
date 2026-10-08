"""Plain-language building blocks for owner-facing copy (§36, §48, §69–70).

Mirrors the conventions of the product's phrase layer (true minus sign, two
decimals, '€1,492.30', '18 September') so strings built by the autopilot
modules read the same as the rest of the product. Money is Decimal; floats
are refused.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext

__all__ = [
    "CURRENCY_SYMBOLS",
    "MINUS",
    "MONTHS",
    "WEEKDAYS",
    "card_mask",
    "count_phrase",
    "day_month",
    "format_money",
    "join_and",
    "ordinal",
    "quantize_money",
]

CURRENCY_SYMBOLS: dict[str, str] = {"EUR": "€", "GBP": "£", "USD": "$", "ILS": "₪"}
MINUS = "−"
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")
_CENT = Decimal("0.01")


def _check_money(amount: Decimal | int) -> Decimal:
    if isinstance(amount, (bool, float)) or not isinstance(amount, (Decimal, int)):
        raise TypeError("money must be Decimal (or int), never float")
    value = Decimal(amount)
    if not value.is_finite():
        raise ValueError("money must be a finite amount")
    return value


def quantize_money(amount: Decimal | int) -> Decimal:
    """Round half-up to cents without ever losing integer digits."""
    value = _check_money(amount)
    with localcontext() as ctx:
        ctx.prec = max(28, value.adjusted() + 4)
        return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _currency(code: str) -> str:
    normalized = code.strip().upper()
    if not _CURRENCY_CODE.match(normalized):
        raise ValueError(f"not a currency code: {code!r}")
    return normalized


def format_money(amount: Decimal | int, currency: str = "EUR") -> str:
    """'€1,492.30' / '−£12.00' / 'CHF 1,492.30'."""
    rounded = quantize_money(amount)
    code = _currency(currency)
    sign = MINUS if rounded < 0 else ""
    digits = f"{abs(rounded):,.2f}"
    symbol = CURRENCY_SYMBOLS.get(code)
    return f"{sign}{symbol}{digits}" if symbol else f"{sign}{code} {digits}"


def ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 11 -> '11th', 26 -> '26th'."""
    if n < 0:
        raise ValueError("ordinal needs a non-negative number")
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def day_month(value: date, today: date | None = None) -> str:
    """'18 September', or '18 September 2025' when not in ``today``'s year."""
    text = f"{value.day} {MONTHS[value.month - 1]}"
    if today is not None and value.year != today.year:
        text = f"{text} {value.year}"
    return text


def card_mask(last4: str) -> str:
    """'4817' -> '•••• 4817' (§38: 'card •••• 4817')."""
    digits = last4.strip()
    if not re.fullmatch(r"\d{4}", digits):
        raise ValueError("card ending must be exactly 4 digits")
    return f"•••• {digits}"


def join_and(items: Sequence[str]) -> str:
    """['a'] -> 'a'; ['a', 'b'] -> 'a and b'; ['a', 'b', 'c'] -> 'a, b and c'."""
    parts = [p for p in items if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def count_phrase(n: int, singular: str, plural: str | None = None) -> str:
    """(1, 'thing') -> 'one thing'; (4, 'thing') -> '4 things'."""
    if n < 0:
        raise ValueError("count cannot be negative")
    if n == 1:
        return f"one {singular}"
    return f"{n} {plural or singular + 's'}"
