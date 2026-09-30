"""Small plain-language and value helpers shared by the closure package.

Owner-facing wording follows §36, §48 and §69–70: short, calm, precise, no
accounting jargon, no raw errors, no internal ids. These helpers are private to
the closure package; the integration layer may swap them for the shared
phrase layer without changing any output.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from datetime import date, datetime, timezone, tzinfo
from decimal import ROUND_HALF_UP, Decimal, localcontext

MONTH_NAMES: tuple[str, ...] = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
WEEKDAYS: tuple[str, ...] = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)  # fmt: skip
MIDDLE_DOT = " · "
MINUS = "−"  # true minus sign, same width as digits in tabular fonts (§32)
CENT = Decimal("0.01")

_CURRENCY_SYMBOLS = {"EUR": "€", "GBP": "£", "USD": "$", "ILS": "₪"}
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")


# --------------------------------------------------------------------------- validation


def require_aware(value: datetime, name: str) -> datetime:
    """Refuse naive datetimes: every instant in the product is timezone-aware."""
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def require_money(value: object, name: str) -> Decimal:
    """Money is Decimal (ints allowed), never float or bool."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise TypeError(f"{name} must be Decimal, never float")
    amount = Decimal(value)
    if not amount.is_finite():
        raise ValueError(f"{name} must be a finite amount")
    return amount


def require_count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


def cents(amount: Decimal) -> Decimal:
    """Round to cents, half up (display and comparison of money)."""
    with localcontext() as ctx:
        ctx.prec = max(28, amount.adjusted() + 4)
        return amount.quantize(CENT, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- wording


def count_phrase(n: int, singular: str, plural: str | None = None) -> str:
    """``'1 supplier'`` / ``'7 suppliers'``."""
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def join_and(parts: Sequence[str]) -> str:
    """``'a, b and c'``."""
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def format_money(amount: Decimal | int, currency: str = "EUR") -> str:
    """``'€1,492.30'`` / ``'−£12.00'`` / ``'CHF 1,492.30'``."""
    value = cents(require_money(amount, "amount"))
    code = currency.strip().upper()
    if not _CURRENCY_CODE.match(code):
        raise ValueError(f"not a currency code: {currency!r}")
    sign = MINUS if value < 0 else ""
    digits = f"{abs(value):,.2f}"
    symbol = _CURRENCY_SYMBOLS.get(code)
    return f"{sign}{symbol}{digits}" if symbol else f"{sign}{code} {digits}"


def day_month(day: date, today: date | None = None) -> str:
    """``'20 October'``; the year is added when it differs from ``today``'s."""
    text = f"{day.day} {MONTH_NAMES[day.month - 1]}"
    if today is None or today.year != day.year:
        return f"{text} {day.year}"
    return text


def since_phrase(then: datetime, now: datetime, tz: tzinfo | None = None) -> str:
    """``'14:42'``, ``'14:42 yesterday'``, ``'Monday at 14:42'``, ``'3 September'`` (§48)."""
    zone = tz or require_aware(now, "now").tzinfo or timezone.utc
    local_now = require_aware(now, "now").astimezone(zone)
    local_then = min(require_aware(then, "then").astimezone(zone), local_now)
    days = (local_now.date() - local_then.date()).days
    clock = f"{local_then:%H:%M}"
    if days == 0:
        return clock
    if days == 1:
        return f"{clock} yesterday"
    if days < 7:
        return f"{WEEKDAYS[local_then.weekday()]} at {clock}"
    return day_month(local_then.date(), local_now.date())


def minutes_from_seconds(seconds: int) -> int:
    """Whole minutes, rounded up: owner time is never understated (§2, §59)."""
    return math.ceil(require_count(seconds, "seconds") / 60)


# --------------------------------------------------------------------------- text matching


def fold(text: str) -> str:
    """Lowercase, accents removed, whitespace collapsed: 'Autoridade  Tributária' -> 'autoridade tributaria'."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped.casefold()).strip()


def phrase_pattern(phrases: Sequence[str]) -> re.Pattern[str]:
    """Whole-word alternation over already-folded phrases, longest first."""
    ordered = sorted({p for p in phrases if p}, key=len, reverse=True)
    body = "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in ordered)
    return re.compile(rf"(?<![0-9a-z])(?:{body})(?![0-9a-z])")
