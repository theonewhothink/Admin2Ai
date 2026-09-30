"""One payout as the provider's settlement statement describes it (§7, §18, §54).

A :class:`SettlementReport` is what a card terminal's or a payment / sales
platform's payout report says about ONE payout into the bank:

    sales (gross) − fees and commissions − refunds − disputed payments ± adjustments = paid out (net)

Every figure keeps its provenance (:class:`~backoffice.domain.models.FieldObservation`:
value, source evidence, method, confidence, location), like every other value
in the engine (§18). Figures the report states come from the provider's own
system (``ExtractionMethod.API``); totals this module adds up from the
report's rows are ``ARITHMETIC`` observations saying so.

:attr:`SettlementReport.adds_up` is the report's own check: the components
must give the stated payout to the cent, and every row whose own figures the
provider states must add up too. A report that does not add up is a conflict
(§19): it is never used to close anything.

Money is Decimal throughout. Plain-language lines (:meth:`SettlementReport.breakdown`)
carry no ids or jargon (§36).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

from backoffice.domain.models import ExtractionMethod, FieldObservation
from backoffice.reconciliation import PayoutProvider
from backoffice.reconciliation._text import format_money

__all__ = ["REPORT_FIELDS", "LineKind", "SettlementLine", "SettlementReport"]

_ZERO = Decimal("0")

# The figures a payout report is read into (observation keys, in display order).
REPORT_FIELDS = ("payout_id", "payout_date", "currency", "gross_sales", "fees", "refunds", "chargebacks",
                 "adjustments", "net_amount")


class LineKind(str, Enum):
    SALE = "sale"  # an order, booking or card payment
    REFUND = "refund"  # money given back to a customer
    CHARGEBACK = "chargeback"  # a disputed card payment taken back
    FEE = "fee"  # a fee or commission on its own row
    ADJUSTMENT = "adjustment"  # anything else that changes the payout (promotions, corrections, reserves)
    PAYOUT = "payout"  # the payout itself (its amount is the stated net)


@dataclass(frozen=True)
class SettlementLine:
    """One row of a payout report, as contributions to the payout.

    ``sales``, ``fees``, ``refunds`` and ``chargebacks`` are positive when they
    are what their name says (a fee given back is a negative fee);
    ``adjustments`` is signed (+ adds to the payout). ``stated_net`` is the
    row's own net as the provider prints it, when it does.
    """

    kind: LineKind
    reference: str | None = None
    on: date | None = None
    sales: Decimal = _ZERO
    fees: Decimal = _ZERO
    refunds: Decimal = _ZERO
    chargebacks: Decimal = _ZERO
    adjustments: Decimal = _ZERO
    stated_net: Decimal | None = None
    location: str = ""

    @property
    def net(self) -> Decimal:
        return self.sales - self.fees - self.refunds - self.chargebacks + self.adjustments

    @property
    def adds_up(self) -> bool:
        return self.stated_net is None or self.stated_net == self.net


@dataclass(frozen=True)
class SettlementReport:
    """What one payout report says about one payout."""

    provider: PayoutProvider
    source: str  # evidence id of the report file
    format: str  # which layout was read (developer-facing)
    currency: str
    gross_sales: Decimal
    fees: Decimal
    refunds: Decimal
    chargebacks: Decimal
    adjustments: Decimal
    net: Decimal  # what the report says was paid out (or, when it states nothing, what its rows add up to)
    net_stated: bool  # the provider states the payout (a total, a payout row, or every row's net)
    payout_id: str | None = None
    payout_date: date | None = None
    lines: tuple[SettlementLine, ...] = ()
    observations: Mapping[str, tuple[FieldObservation, ...]] = field(default_factory=dict)
    # Plain sentences: totals the report states that its own rows contradict.
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("gross_sales", "fees", "refunds", "chargebacks", "adjustments", "net"):
            value = getattr(self, name)
            if isinstance(value, float) or not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal, never float")
        object.__setattr__(self, "currency", (self.currency or "").strip().upper() or "EUR")
        object.__setattr__(self, "observations", MappingProxyType({k: tuple(v) for k, v in self.observations.items()}))

    # -- the check -------------------------------------------------------------

    @property
    def computed_net(self) -> Decimal:
        return self.gross_sales - self.fees - self.refunds - self.chargebacks + self.adjustments

    @property
    def difference(self) -> Decimal:
        """Stated payout minus what the components give (0 when it adds up)."""
        return self.net - self.computed_net

    @property
    def rows_that_do_not_add_up(self) -> tuple[SettlementLine, ...]:
        return tuple(line for line in self.lines if not line.adds_up)

    @property
    def adds_up(self) -> bool:
        return self.difference == 0 and not self.rows_that_do_not_add_up and not self.problems

    @property
    def order_count(self) -> int:
        """Orders, bookings or card payments in this payout."""
        return sum(1 for line in self.lines if line.kind is LineKind.SALE)

    @property
    def identity(self) -> tuple[str, str, str, Decimal]:
        """What makes two copies of a report the same payout."""
        return (self.provider.key, (self.payout_id or "").strip().upper(), self.currency, self.net)

    # -- plain language ----------------------------------------------------------

    def money(self, value: Decimal) -> str:
        return format_money(value, self.currency)

    def breakdown(self) -> tuple[str, ...]:
        """The report's figures as short plain lines for "Why?" (§54)."""
        count = self.order_count
        sales = f"Sales: {self.money(self.gross_sales)}"
        if count > 1:
            noun = {"accommodation": "bookings", "card_terminal": "card payments",
                    "payments": "payments"}.get(self.provider.kind.value, "orders")
            sales += f" from {count} {noun}"
        lines = [sales]
        fee_label = "Commission" if self.provider.fee_word == "commission" else "Fees"
        if self.fees:
            lines.append(f"{fee_label}: {self.money(self.fees)}")
        if self.refunds:
            lines.append(f"Refunded to customers: {self.money(self.refunds)}")
        if self.chargebacks:
            lines.append(f"Disputed card payments taken back: {self.money(self.chargebacks)}")
        if self.adjustments:
            sign = "added" if self.adjustments > 0 else "taken off"
            lines.append(f"Other changes {sign}: {self.money(abs(self.adjustments))}")
        lines.append(f"Paid out, as the report says: {self.money(self.net)}")
        return tuple(lines)

    def mismatch_sentence(self) -> str:
        """Why the report does not add up, in one plain sentence (§36)."""
        parts = [f"{self.money(self.gross_sales)} in sales"]
        taken = []
        if self.fees:
            taken.append(f"{self.money(self.fees)} in {self.provider.fee_word}")
        if self.refunds:
            taken.append(f"{self.money(self.refunds)} in refunds")
        if self.chargebacks:
            taken.append(f"{self.money(self.chargebacks)} in disputed payments")
        text = parts[0]
        if taken:
            text += " minus " + _join(taken)
        if self.adjustments:
            word = "plus" if self.adjustments > 0 else "minus"
            text += f" {word} {self.money(abs(self.adjustments))} in other changes"
        if self.difference == 0 and self.problems:
            return self.problems[0]
        if self.difference == 0 and self.rows_that_do_not_add_up:
            n = len(self.rows_that_do_not_add_up)
            if n == 1:
                return "One of its rows does not add up on its own."
            return f"{n} of its rows do not add up on their own."
        return f"{text} is {self.money(self.computed_net)}, but it says {self.money(self.net)} was paid out."


def observation(value: object, source: str, location: str, *, method: ExtractionMethod = ExtractionMethod.API,
                confidence: float = 0.99) -> FieldObservation:
    return FieldObservation(value=value, source=source, method=method, confidence=confidence, location=location)


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]
