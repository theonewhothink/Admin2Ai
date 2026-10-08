"""Exact money parameters for ``numeric(18,2)`` columns.

PostgreSQL ROUNDS a value with more decimals than a ``numeric(18,2)`` column
holds (10.005 is stored as 10.01) without any error. Rounding someone's money
silently is a critical silent error (§59), so amounts pass through
:func:`db_amount`, which refuses instead of rounding.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

__all__ = ["MAX_ABS_AMOUNT", "db_amount", "db_currency"]

_CENT = Decimal("0.01")
# numeric(18,2): 16 digits before the decimal point.
MAX_ABS_AMOUNT = Decimal("9999999999999999.99")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def db_amount(value: Decimal | int) -> Decimal:
    """``value`` as an exact two-decimal Decimal, or ``ValueError``/``TypeError``.

    Floats are refused outright: a binary float is never money.
    """
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise TypeError(f"money must be Decimal or int, not {type(value).__name__}")
    amount = Decimal(value)
    if not amount.is_finite():
        raise ValueError("money must be a finite number")
    try:
        exact = amount.quantize(_CENT)
    except InvalidOperation:
        raise ValueError("amount is too large for numeric(18,2)") from None
    if exact != amount:
        raise ValueError(f"{amount} has more than two decimal places; round it explicitly first")
    if abs(exact) > MAX_ABS_AMOUNT:
        raise ValueError("amount is too large for numeric(18,2)")
    return exact


def db_currency(code: str) -> str:
    """An ISO 4217 style currency code (three upper-case letters), else ``ValueError``."""
    if not isinstance(code, str) or not _CURRENCY.fullmatch(code):
        raise ValueError("currency must be three upper-case letters, e.g. EUR")
    return code
