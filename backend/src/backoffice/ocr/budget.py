"""Per-tenant OCR spending ceilings (§17: paid OCR is the exception).

The router reserves the estimated cost of a paid step before calling the
engine and settles the actual cost afterwards, so concurrent documents of
one tenant cannot overshoot the ceiling together. Ceilings apply per period
(calendar month in UTC by default).

A reservation is a :class:`BudgetHold` that remembers the period it was
taken in: a call reserved at 23:59:59 on 30 September and settled after
midnight is settled against September, never against October (which would
otherwise go negative and hand the tenant extra budget).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol, runtime_checkable

__all__ = ["BudgetHold", "BudgetLedger", "InMemoryBudgetLedger", "monthly_period"]


@dataclass(frozen=True)
class BudgetHold:
    """Money set aside for one pending call. ``period`` is ledger-specific."""

    tenant_id: str
    amount: Decimal
    period: str


@runtime_checkable
class BudgetLedger(Protocol):
    def reserve(self, tenant_id: str, amount: Decimal) -> BudgetHold | None:
        """Hold ``amount`` for a pending call; None when it would exceed the ceiling.

        A zero reservation always succeeds (it is used to record the cost
        of a call that was expected to be free).
        """
        ...

    def settle(self, hold: BudgetHold, actual: Decimal) -> None:
        """Replace a reservation with what the call actually cost, in the hold's period."""
        ...

    def remaining(self, tenant_id: str) -> Decimal | None:
        """What is left this period; None when the tenant has no ceiling."""
        ...


def monthly_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


class InMemoryBudgetLedger:
    """Process-local ledger (tests, single worker). Production uses a shared store."""

    def __init__(
        self,
        ceilings: Mapping[str, Decimal] | None = None,
        *,
        default_ceiling: Decimal | None = None,
        period: Callable[[], str] = monthly_period,
    ) -> None:
        for value in [*(ceilings or {}).values(), default_ceiling]:
            if value is not None and value < 0:
                raise ValueError("ceilings cannot be negative")
        self._ceilings = dict(ceilings or {})
        self._default = default_ceiling
        self._period = period
        self._spent: dict[tuple[str, str], Decimal] = {}
        self._lock = threading.Lock()

    def ceiling(self, tenant_id: str) -> Decimal | None:
        return self._ceilings.get(tenant_id, self._default)

    def spent(self, tenant_id: str, period: str | None = None) -> Decimal:
        with self._lock:
            return self._spent.get((tenant_id, period or self._period()), Decimal("0"))

    def reserve(self, tenant_id: str, amount: Decimal) -> BudgetHold | None:
        if amount < 0:
            raise ValueError("amount cannot be negative")
        period = self._period()
        key = (tenant_id, period)
        ceiling = self.ceiling(tenant_id)
        with self._lock:
            spent = self._spent.get(key, Decimal("0"))
            if amount > 0 and ceiling is not None and spent + amount > ceiling:
                return None
            self._spent[key] = spent + amount
        return BudgetHold(tenant_id=tenant_id, amount=amount, period=period)

    def settle(self, hold: BudgetHold, actual: Decimal) -> None:
        if actual < 0:
            raise ValueError("amounts cannot be negative")
        key = (hold.tenant_id, hold.period)
        with self._lock:
            self._spent[key] = self._spent.get(key, Decimal("0")) - hold.amount + actual

    def remaining(self, tenant_id: str) -> Decimal | None:
        ceiling = self.ceiling(tenant_id)
        if ceiling is None:
            return None
        return max(Decimal("0"), ceiling - self.spent(tenant_id))
