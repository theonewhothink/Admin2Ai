"""Plans and billing (§60, §61): what each plan includes, what a business uses, and what happens over a limit.

The plans are data (:data:`PLANS`), straight from the product specification:

* **Free**: the historical business audit (the last 90 days, always free) and limited scanning after it;
* **Solo** €9 a month, **Business** €19 a month;
* **Multi-company** €29 a month, with an allowance of companies;
* **Accountant**: a practice plan plus a low price per active client.

Each plan has limits: companies, documents a month and people with access (accountants are always free: they
bring their clients, §62). The static demo is on its own plan, "Demo", with nothing to pay.

Limits never block evidence and never lose data (§3, §52):

* over a limit, the owner sees one plain upgrade line (``GET /api/billing``, and once in Activity) and everything
  keeps being processed for a grace period (:data:`GRACE_DAYS`);
* after the grace period, what arrives is still received and kept (the file, the email, the shared link), but it
  waits, unread, until the plan covers it again: the owner upgrades or a new month starts. Then it is processed
  in the order it arrived, exactly as if it had just come in;
* documents from before the business joined (the free audit's history) never count;
* a failed payment is a plain notice and the same grace period; after it the business is on the Free plan's
  limits until the payment goes through. Nothing is deleted.

Only a business the payment provider can bill is held to its limits (``BillingState.enforced``): a new business
on a server with payments set up, or one that chose a plan. Everyone else just sees their usage.

The payment provider (Stripe, :mod:`backoffice.server.billing`) tells the back office what changed through
webhooks; :func:`reduce_event` keeps only what the plan needs from each (no names, emails, addresses or card
details) and :meth:`BillingState.apply` applies it once, whatever order and however many times it arrives.
Pure standard library: this module runs in the browser build too.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

__all__ = [
    "BillingState",
    "DEMO_PLAN",
    "GRACE_DAYS",
    "HOLDABLE_PATHS",
    "PAID_PLANS",
    "PLANS",
    "Plan",
    "Usage",
    "plan_for",
    "reduce_event",
]

GRACE_DAYS = 14  # over a limit, or after a failed payment: everything keeps working this long
# Where evidence arrives from the owner: held (kept, not read) after the grace period. Mailbox syncs too.
HOLDABLE_PATHS = frozenset({"/api/evidence", "/api/evidence/upload", "/api/receipts", "/api/share"})
HANDLED_EVENTS = frozenset({"checkout.session.completed", "customer.subscription.created",
                            "customer.subscription.updated", "customer.subscription.deleted",
                            "invoice.payment_failed", "invoice.paid"})
MAX_REMEMBERED_EVENTS = 500  # provider event ids remembered for idempotency (webhooks are retried for 3 days)


@dataclass(frozen=True)
class Plan:
    """One plan. ``None`` limits are unlimited. Prices in euros a month, VAT on top."""

    id: str
    name: str
    monthly: Decimal
    companies: int | None
    documents: int | None  # documents a month (the free audit's history never counts)
    users: int | None  # people with access; accountants are always free
    summary: str
    per_client: Decimal | None = None  # the accountant plan: each active client, a month
    self_serve: bool = True  # bought through the payment provider's checkout

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "monthly": float(self.monthly),
                "perClient": float(self.per_client) if self.per_client is not None else None,
                "limits": {"companies": self.companies, "documentsPerMonth": self.documents, "users": self.users},
                "summary": self.summary, "price": self.price_words()}

    def price_words(self) -> str:
        if self.id == "demo":
            return "Nothing to pay"
        if not self.monthly:
            return "Free"
        price = f"€{self.monthly:f} a month".replace(".00 ", " ")
        if self.per_client is not None:
            price += f" plus €{self.per_client:f} for each active client"
        return price


PLANS: dict[str, Plan] = {p.id: p for p in (
    Plan("free", "Free", Decimal("0"), companies=1, documents=25, users=1, self_serve=False,
         summary="The audit of your last 90 days, free. Then up to 25 documents a month for one company."),
    Plan("solo", "Solo", Decimal("9"), companies=1, documents=150, users=2,
         summary="One company, up to 150 documents a month."),
    Plan("business", "Business", Decimal("19"), companies=1, documents=600, users=5,
         summary="One company, up to 600 documents a month, five people."),
    Plan("multi", "Multi-company", Decimal("29"), companies=5, documents=1500, users=10,
         summary="Up to 5 companies and 1,500 documents a month, ten people."),
    Plan("accountant", "Accountant", Decimal("39"), companies=None, documents=None, users=10,
         per_client=Decimal("2"), summary="Your practice, plus €2 a month for each active client."),
)}
PAID_PLANS = tuple(p.id for p in PLANS.values() if p.self_serve)
DEMO_PLAN = Plan("demo", "Demo", Decimal("0"), companies=None, documents=None, users=None, self_serve=False,
                 summary="The demo business. Nothing here is billed.")
_ORDER = {p: i for i, p in enumerate(("free", "solo", "business", "multi", "accountant"))}


def plan_for(plan_id: str | None) -> Plan | None:
    """The plan called ``plan_id`` ("demo" included), or None."""
    if plan_id == "demo":
        return DEMO_PLAN
    return PLANS.get(str(plan_id or ""))


@dataclass(frozen=True)
class Usage:
    """What a business uses this month, against its plan's limits."""

    month: str  # "2026-10"
    companies: int
    documents: int  # documents that arrived this month, not counting the free audit's history
    users: int | None = None  # people with access (the server knows; None in the engine alone)
    clients: int = 0  # active client businesses (the accountant plan)

    def public(self) -> dict[str, Any]:
        return {"month": self.month, "companies": self.companies, "documents": self.documents, "users": self.users,
                "clients": self.clients}


def _limit_words(name: str, plan: Plan) -> str:
    return {"companies": f"{_n(plan.companies)} {'company' if plan.companies == 1 else 'companies'}",
            "documents": f"{_n(plan.documents)} documents a month",
            "users": f"{_n(plan.users)} {'person' if plan.users == 1 else 'people'}"}[name]


def _n(value: int | None) -> str:
    return f"{value:,}" if value is not None else "unlimited"


def _day(value: date) -> str:
    return f"{value.day} {value:%B}"


def _next_up(plan: Plan, over: list[str], usage: Usage) -> Plan | None:
    """The cheapest self-serve plan that covers ``usage`` (the accountant plan only for accountants)."""
    for candidate in sorted((p for p in PLANS.values() if p.self_serve), key=lambda p: _ORDER[p.id]):
        if _ORDER[candidate.id] <= _ORDER.get(plan.id, -1) or (candidate.id == "accountant" and not usage.clients):
            continue
        if all(getattr(candidate, name) is None or getattr(usage, name) is None
               or getattr(usage, name) <= getattr(candidate, name) for name in ("companies", "documents", "users")):
            return candidate
    return None


@dataclass
class BillingState:
    """One business's plan and what the payment provider told us, as the event log rebuilds it."""

    plan: str = "free"
    status: str = "active"  # active | past_due | canceled
    enforced: bool = False  # held to its plan's limits (see the module docstring)
    started_on: date | None = None  # the day the business joined: earlier documents are the free audit's
    customer: str | None = None  # the payment provider's customer id
    subscription: str | None = None
    renews_on: date | None = None
    payment_failed_on: date | None = None
    over_since: date | None = None  # the first day over a limit (the grace period starts)
    events: list[str] = field(default_factory=list)  # provider event ids applied (each applies once)
    subscription_at: int = 0  # the provider's time of the latest subscription change applied (older ones ignored)
    waiting: list[dict[str, Any]] = field(default_factory=list)  # evidence kept, waiting for the plan to cover it

    # -- the plan in force --------------------------------------------------------------------------------

    @property
    def is_demo(self) -> bool:
        return self.plan == "demo"

    def chosen(self) -> Plan:
        return plan_for(self.plan) or PLANS["free"]

    def payment_grace_until(self) -> date | None:
        return self.payment_failed_on + timedelta(days=GRACE_DAYS) if self.payment_failed_on else None

    def effective(self, today: date) -> Plan:
        """The plan whose limits apply today: the chosen one, or Free once a failed payment's grace is over."""
        until = self.payment_grace_until()
        if self.status == "past_due" and until is not None and today > until:
            return PLANS["free"]
        return self.chosen()

    def over(self, usage: Usage, today: date) -> list[str]:
        """The limits ``usage`` is over ("companies", "documents", "users"), against today's plan."""
        plan = self.effective(today)
        return [name for name in ("companies", "documents", "users")
                if getattr(plan, name) is not None and getattr(usage, name) is not None
                and getattr(usage, name) > getattr(plan, name)]

    def grace_until(self, usage: Usage, today: date) -> date | None:
        """The last day everything keeps being processed although the business is over a limit it is held to."""
        if not self.over(usage, today):
            return None
        starts = [d for d in (self.over_since or today,
                              self.payment_failed_on if self.status == "past_due" else None) if d is not None]
        return min(starts) + timedelta(days=GRACE_DAYS)

    def holding(self, usage: Usage, today: date) -> bool:
        """New evidence waits (kept, unread) instead of being processed: held to the plan, over a limit it counts
        for evidence (companies, documents), and past the grace period."""
        if not self.enforced or self.is_demo:
            return False
        engine_usage = Usage(usage.month, usage.companies, usage.documents, None, usage.clients)
        until = self.grace_until(engine_usage, today)
        return until is not None and today > until

    # -- what the owner reads ------------------------------------------------------------------------------

    def prompt(self, usage: Usage, today: date) -> str | None:
        """One plain upgrade line when over a limit (none otherwise)."""
        over = self.over(usage, today)
        if not over or self.is_demo:
            return None
        plan = self.effective(today)
        what = " and ".join(_limit_words(name, plan) for name in over)
        head = f"You are over the {plan.name} plan's {what}."
        better = _next_up(plan, over, usage)
        offer = f" The {better.name} plan ({better.price_words()}) covers it." if better is not None else ""
        if not self.enforced:
            return head + offer
        until = self.grace_until(Usage(usage.month, usage.companies, usage.documents, None, usage.clients), today)
        if until is None or not ({"companies", "documents"} & set(over)):
            return head + offer
        if today > until:
            return (head + " New documents are kept safely and wait until you upgrade or the month turns. "
                    "Nothing is lost." + offer)
        return (head + f" Everything keeps being processed until {_day(until)}. After that, new documents are "
                "kept safely and wait until you upgrade or the month turns." + offer)

    def notice(self, today: date) -> str | None:
        """The plain line after a failed payment (none otherwise)."""
        if self.status != "past_due" or self.payment_failed_on is None:
            return None
        until = self.payment_grace_until()
        plan = self.chosen().name
        if until is not None and today <= until:
            return (f"The payment for your {plan} plan did not go through. Everything keeps working until "
                    f"{_day(until)}. Update your card to keep your plan.")
        return (f"The payment for your {plan} plan did not go through, so the Free plan's limits apply until it "
                "does. Nothing is deleted. Update your card to go back to your plan.")

    # -- the provider's events ------------------------------------------------------------------------------

    def seen(self, event_id: str) -> bool:
        return event_id in self.events

    def apply(self, event: Mapping[str, Any], today: date) -> dict[str, Any]:
        """Apply one reduced provider event (:func:`reduce_event`), once. Returns what changed, in plain words."""
        event_id = str(event.get("id") or "")
        kind = str(event.get("type") or "")
        if not event_id or self.seen(event_id):
            return {"applied": False, "reason": "already applied"}
        self.events.append(event_id)
        del self.events[:-MAX_REMEMBERED_EVENTS]
        obj = event.get("object") if isinstance(event.get("object"), Mapping) else {}
        created = int(event.get("created") or 0)
        before = (self.plan, self.status)
        if obj.get("customer"):
            self.customer = str(obj["customer"])
        if kind == "checkout.session.completed":
            if obj.get("status") != "complete" or obj.get("payment_status") not in ("paid", "no_payment_required"):
                return {"applied": True, "changed": False}
            plan = plan_for(obj.get("plan"))
            if plan is None or not plan.self_serve:
                return {"applied": True, "changed": False}
            self.plan, self.status, self.enforced = plan.id, "active", True
            self.subscription = str(obj.get("subscription") or "") or self.subscription
            self.payment_failed_on = None
        elif kind.startswith("customer.subscription."):
            if created and created < self.subscription_at:
                return {"applied": True, "changed": False, "reason": "older than what we know"}
            self.subscription_at = max(self.subscription_at, created)
            status = str(obj.get("status") or "")
            plan = plan_for(obj.get("plan"))
            if kind == "customer.subscription.deleted" or status in ("canceled", "incomplete_expired"):
                if obj.get("id") and self.subscription and str(obj["id"]) != self.subscription:
                    return {"applied": True, "changed": False, "reason": "another subscription"}
                self.plan, self.status, self.subscription, self.renews_on = "free", "canceled", None, None
                self.payment_failed_on = None
            elif status in ("active", "trialing"):
                if plan is not None and plan.self_serve:
                    self.plan = plan.id
                self.status, self.enforced = "active", True
                self.subscription = str(obj.get("id") or "") or self.subscription
                self.payment_failed_on = None
            elif status in ("past_due", "unpaid"):
                self.status = "past_due"
                self.payment_failed_on = self.payment_failed_on or _event_day(created, today)
            if obj.get("current_period_end"):
                self.renews_on = _event_day(int(obj["current_period_end"]), today)
        elif kind == "invoice.payment_failed":
            if self.plan != "free":
                self.status = "past_due"
                self.payment_failed_on = self.payment_failed_on or _event_day(created, today)
        elif kind == "invoice.paid":
            if self.status == "past_due":
                self.status = "active"
            self.payment_failed_on = None
        return {"applied": True, "changed": (self.plan, self.status) != before, "plan": self.plan,
                "status": self.status}


def _event_day(created: int, today: date) -> date:
    if not created:
        return today
    return datetime.fromtimestamp(created, tz=timezone.utc).date()


def _get(data: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


def reduce_event(event: Mapping[str, Any], price_plans: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """What the back office keeps from one provider event: its id, type, time and the plan facts of its object.

    Names, emails, addresses and anything about the card are dropped: they never reach the event log. The plan
    comes from the metadata the checkout set (``plan``), else from the price (``price_plans``: price id -> plan).
    None for events that do not change a plan.
    """
    kind = str(event.get("type") or "")
    if kind not in HANDLED_EVENTS or not event.get("id"):
        return None
    obj = _get(event, "data", "object")
    if not isinstance(obj, Mapping):
        return None
    prices = price_plans or {}
    plan = _get(obj, "metadata", "plan")
    items = _get(obj, "items", "data") if kind.startswith("customer.subscription.") else None
    items = items if isinstance(items, list) else []
    for item in items:
        price = _get(item, "price", "id")
        if isinstance(price, str) and price in prices:
            plan = prices[price]  # the price paid says the plan, even after a change made on the provider's page
            break
    period_end = obj.get("current_period_end")
    if not isinstance(period_end, int) and items:  # newer API versions keep it on the subscription's items
        period_end = _get(items[0], "current_period_end")
    out: dict[str, Any] = {
        "id": str(event["id"]), "type": kind, "created": int(event.get("created") or 0),
        "livemode": bool(event.get("livemode")),
        "object": {k: v for k, v in {
            "id": obj.get("id") if isinstance(obj.get("id"), str) else None,
            "customer": obj.get("customer") if isinstance(obj.get("customer"), str) else None,
            "subscription": obj.get("subscription") if isinstance(obj.get("subscription"), str) else None,
            "status": obj.get("status") if isinstance(obj.get("status"), str) else None,
            "payment_status": obj.get("payment_status") if isinstance(obj.get("payment_status"), str) else None,
            "plan": plan if isinstance(plan, str) and plan in PLANS else None,
            "current_period_end": period_end if isinstance(period_end, int) else None,
        }.items() if v is not None},
    }
    return out


def event_tenant(event: Mapping[str, Any]) -> str | None:
    """The business a provider event is about, from what our checkout put on it (else None)."""
    obj = _get(event, "data", "object")
    for value in (_get(obj, "client_reference_id"), _get(obj, "metadata", "tenant"),
                  _get(obj, "subscription_details", "metadata", "tenant"),
                  _get(obj, "parent", "subscription_details", "metadata", "tenant")):
        if isinstance(value, str) and value:
            return value
    return None


def event_customer(event: Mapping[str, Any]) -> str | None:
    value = _get(event, "data", "object", "customer")
    return value if isinstance(value, str) and value else None
