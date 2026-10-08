"""Costs recharged to clients, and client money (reimbursable costs, disbursements, pass-through costs).

A cost on a client's cost center can be the client's to pay back: an interior designer's purchase for a
client, a law firm's court fee, media an agency buys for a client, a licence resold at cost, a hotel a
travel agency pays for a client's trip. The cost center agent decides it with a reason (a rule the owner
taught, the cost center's setting, the invoice itself, a consistent history, or the owner's one answer:
``AllocationShare.recharge``). This module is read-only: for each client, what was bought for them, what
they paid back, what is still to recover, and money held for them.

Money received from a client is matched to their recharged costs, oldest first, when:

* the client's costs are all theirs (the cost center's ``recharge`` setting: a travel agency's trip, a
  law firm's client account): everything received from them is client money, not revenue; what has not
  been spent on them yet is held for them;
* otherwise, when the bank line says it pays costs back ("reembolso", "refaturação", "despesas",
  "reimbursement", "expenses", "media" ...), or its amount is exactly what is outstanding (all of it, one
  cost, or the oldest ones together). Anything else received from the client stays their payment for the
  business's own work (revenue).

Pure Python over the engine's records; changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from backoffice.countries import LazyPattern, pack_alternatives
from backoffice.domain.cost_centers import CostCenter
from backoffice.learning.keys import fold

__all__ = ["ClientRecharge", "RechargeBook", "RechargeSummary"]

_ZERO = Decimal("0.00")
_PAYS_BACK = LazyPattern(lambda: (  # a pack's own words in "recharges.pays_back" (Portugal's "refaturação")
    rf"(?<![a-z])(?:{pack_alternatives('recharges.pays_back')}|re-?invoic\w*|reimburs\w*|recharg\w*|rebill\w*|"
    r"expenses|disbursements?|media|advance|pass[\s-]+through)(?![a-z])"))


@dataclass
class RechargeSummary:
    """One client's recharged costs over a period (costs dated in it), and what came back for them."""

    bought: Decimal = _ZERO  # recharged costs dated in the period
    paid_back: Decimal = _ZERO  # the part of those costs the client has paid back (whenever they paid)
    received: Decimal = _ZERO  # money from the client in the period counted as paying back / client money
    held: Decimal = _ZERO  # client money not spent on them yet (client-money cost centers only)
    costs: list[tuple[Any, Decimal, Decimal]] = field(default_factory=list)  # (payment, part, recovered part)
    receipts: list[tuple[Any, Decimal]] = field(default_factory=list)  # (payment in, part)

    @property
    def outstanding(self) -> Decimal:
        return self.bought - self.paid_back


@dataclass
class ClientRecharge:
    center: CostCenter
    costs: list[tuple[Any, Decimal]] = field(default_factory=list)  # (payment out, part recharged), oldest first
    receipts: list[tuple[Any, Decimal]] = field(default_factory=list)  # (payment in, part paying back)
    recovered: dict[str, Decimal] = field(default_factory=dict)  # payment out id -> part paid back so far
    held: Decimal = _ZERO  # client money not spent on them yet

    @property
    def client_money(self) -> bool:
        return self.center.recharge

    def summary(self, start: date | None = None, end: date | None = None) -> RechargeSummary:
        def inside(day: date) -> bool:
            return (start is None or day >= start) and (end is None or day <= end)

        out = RechargeSummary(held=self.held)
        for rec, part in self.costs:
            if inside(rec.tx.booked_on):
                back = self.recovered.get(rec.id, _ZERO)
                out.bought += part
                out.paid_back += back
                out.costs.append((rec, part, back))
        for rec, part in self.receipts:
            if inside(rec.tx.booked_on):
                out.received += part
                out.receipts.append((rec, part))
        return out


class RechargeBook:
    """Every client's recharged costs and what came back for them (read-only)."""

    def __init__(self, repo: Any) -> None:
        self.repo = repo
        self.clients: dict[str, ClientRecharge] = {}
        self.out_parts: dict[str, list[tuple[str, Decimal]]] = {}  # payment out -> [(cost center, recharged part)]
        self.in_parts: dict[str, list[tuple[str, Decimal]]] = {}  # payment in -> [(cost center, paid-back part)]
        self._build()

    def _build(self) -> None:
        repo = self.repo
        records = sorted(repo.transactions.values(), key=lambda r: (r.tx.booked_on, r.id))
        costs: dict[str, list[tuple[Any, Decimal]]] = {}
        receipts: dict[str, list[tuple[Any, Decimal]]] = {}
        for rec in records:
            allocation = rec.tx.cost_allocation
            if allocation is None or allocation.general or rec.private:
                continue
            for share in allocation.shares:
                if rec.tx.amount < 0 and share.recharge:
                    costs.setdefault(share.cost_center_id, []).append((rec, share.amount))
                elif rec.tx.amount > 0:
                    receipts.setdefault(share.cost_center_id, []).append((rec, share.amount))
        for cid in sorted(set(costs) | {c.id for c in repo.cost_centers.values() if c.recharge}):
            center = repo.cost_centers.get(cid)
            if center is None:
                continue
            client = ClientRecharge(center=center, costs=costs.get(cid, []))
            self._match(client, receipts.get(cid, []))
            self.clients[cid] = client
            for rec, part in client.costs:
                self.out_parts.setdefault(rec.id, []).append((cid, part))
            for rec, part in client.receipts:
                self.in_parts.setdefault(rec.id, []).append((cid, part))

    @staticmethod
    def _match(client: ClientRecharge, money_in: list[tuple[Any, Decimal]]) -> None:
        """Money received from the client, matched to their recharged costs oldest first (module docstring)."""
        open_costs = [[rec, part] for rec, part in client.costs]

        def outstanding() -> Decimal:
            return sum((c[1] for c in open_costs), _ZERO)

        def pay(amount: Decimal) -> Decimal:
            left = amount
            for cost in open_costs:
                if left <= 0:
                    break
                take = min(cost[1], left)
                if take <= 0:
                    continue
                cost[1] -= take
                client.recovered[cost[0].id] = client.recovered.get(cost[0].id, _ZERO) + take
                left -= take
            return amount - left

        if client.client_money:
            for rec, part in money_in:
                client.receipts.append((rec, part))
            total = sum((part for _, part in money_in), _ZERO)
            client.held = total - pay(total)
            return
        for rec, part in money_in:
            due = outstanding()
            if due <= 0:
                continue
            said = f"{rec.tx.counterparty} {rec.tx.description} {rec.tx.reference or ''}"
            words = bool(_PAYS_BACK.search(fold(said)))
            remaining = [c for c in open_costs if c[1] > 0]
            oldest = _ZERO
            exact = part == due or any(c[1] == part for c in remaining)
            for c in remaining:
                oldest += c[1]
                if oldest == part:
                    exact = True
                    break
            if not (words or exact):
                continue  # a payment for the business's own work: revenue
            if exact and not words and part != due:
                single = next((c for c in remaining if c[1] == part), None)
                if single is not None and oldest != part:
                    single[1] -= part
                    client.recovered[single[0].id] = client.recovered.get(single[0].id, _ZERO) + part
                    client.receipts.append((rec, part))
                    continue
            paid = pay(min(part, due))
            if paid > 0:
                client.receipts.append((rec, paid))

    # ----------------------------------------------------------------- queries

    def for_center(self, cost_center_id: str) -> ClientRecharge | None:
        return self.clients.get(cost_center_id)

    def recharged(self, tx_id: str) -> Decimal:
        return sum((part for _, part in self.out_parts.get(tx_id, [])), _ZERO)

    def paid_back(self, tx_id: str) -> Decimal:
        return sum((part for _, part in self.in_parts.get(tx_id, [])), _ZERO)
