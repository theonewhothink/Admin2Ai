"""Payments for the plans (backoffice.billing), through a payment provider: Stripe.

The owner never types a card number into the back office. ``POST /api/billing/checkout`` answers with the
address of a Stripe Checkout page for the chosen plan (or, for a business that already pays, Stripe's own page
to change plan), ``POST /api/billing/portal`` with the address of Stripe's customer portal (card, invoices,
cancel). Card data only ever goes to Stripe: nothing here receives, keeps or logs it.

Stripe tells the back office what happened through ``POST /api/billing/webhook``. Every call is checked before
anything is read from it: the ``Stripe-Signature`` header must be Stripe's HMAC-SHA256 of the exact body with the
endpoint's signing secret, made less than :data:`TOLERANCE_S` seconds ago (an old, replayed call is refused).
Then only what the plan needs is kept (backoffice.billing.reduce_event) and recorded as one event in the
business's log, applied once, however often Stripe sends it.

Settings (documented in .env.example): ``STRIPE_SECRET_KEY``, ``STRIPE_WEBHOOK_SECRET``, and the Stripe Price id
of each plan: ``STRIPE_PRICE_SOLO``, ``STRIPE_PRICE_BUSINESS``, ``STRIPE_PRICE_MULTI``,
``STRIPE_PRICE_ACCOUNTANT`` and ``STRIPE_PRICE_ACCOUNTANT_CLIENT`` (per active client). Without the two keys
there are no payments and nobody is held to a plan. ``STRIPE_API_URL`` points the client elsewhere (tests).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = ["BillingError", "BillingProvider", "STRIPE_API", "StripeBilling", "TOLERANCE_S", "WebhookRefused",
           "billing_from_env", "sign_payload", "verify_signature"]

STRIPE_API = "https://api.stripe.com"
STRIPE_VERSION = "2024-06-20"  # the API version our requests are read with; set the webhook endpoint to it too
TOLERANCE_S = 300  # a signed webhook older (or newer) than this is refused: it may be a replay
PRICE_ENV = {"solo": "STRIPE_PRICE_SOLO", "business": "STRIPE_PRICE_BUSINESS", "multi": "STRIPE_PRICE_MULTI",
             "accountant": "STRIPE_PRICE_ACCOUNTANT"}
CLIENT_PRICE_ENV = "STRIPE_PRICE_ACCOUNTANT_CLIENT"


class BillingError(RuntimeError):
    """The payment provider refused or could not be reached (developer-facing; the owner reads a plain line)."""


class WebhookRefused(ValueError):
    """A webhook call that is not provably the provider's, or is too old (developer-facing)."""


class BillingProvider(Protocol):
    def price_plans(self) -> dict[str, str]: ...
    def offers(self, plan_id: str) -> bool: ...
    def checkout(self, *, tenant_id: str, plan_id: str, customer: str | None, email: str, clients: int,
                 success_url: str, cancel_url: str) -> str: ...
    def portal(self, *, customer: str, return_url: str, subscription: str | None = None) -> str: ...
    def verify(self, payload: bytes, header: str | None, now: float) -> dict[str, Any]: ...


def sign_payload(payload: bytes, secret: str, timestamp: int) -> str:
    """The ``Stripe-Signature`` header value Stripe sends for ``payload`` at ``timestamp`` (tests use it too)."""
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_signature(payload: bytes, header: str | None, secret: str, now: float, *,
                     tolerance: int = TOLERANCE_S) -> None:
    """Raise :class:`WebhookRefused` unless ``header`` signs ``payload`` with ``secret``, recently.

    Stripe's scheme: ``t=<unix time>,v1=<hex HMAC-SHA256 of "<t>.<body>">[,v1=...]``; any ``v1`` may match
    (while a secret is being rolled). Compared in constant time.
    """
    if not secret:
        raise WebhookRefused("no signing secret")
    if not header or len(header) > 4096:
        raise WebhookRefused("no signature")
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                raise WebhookRefused("bad timestamp") from None
        elif key == "v1" and value:
            signatures.append(value)
    if timestamp is None or not signatures:
        raise WebhookRefused("malformed signature")
    if abs(now - timestamp) > tolerance:
        raise WebhookRefused("too old")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        raise WebhookRefused("signature does not match")


@dataclass
class StripeBilling:
    """:class:`BillingProvider` on Stripe's API (form-encoded requests with the secret key, over httpx)."""

    secret_key: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    prices: Mapping[str, str] = field(default_factory=dict)  # plan id -> Stripe Price id
    client_price: str | None = None  # the accountant plan's price per active client
    api_url: str = STRIPE_API
    transport: Any = None
    timeout: float = 15.0

    def price_plans(self) -> dict[str, str]:
        return {price: plan for plan, price in self.prices.items() if price}

    def offers(self, plan_id: str) -> bool:
        return bool(self.prices.get(plan_id)) and (plan_id != "accountant" or bool(self.client_price))

    def _post(self, path: str, form: list[tuple[str, str]], *, idempotency: str | None = None) -> dict[str, Any]:
        import httpx  # lazy: server-only dependency

        headers = {"Authorization": f"Bearer {self.secret_key}", "Stripe-Version": STRIPE_VERSION,
                   "Content-Type": "application/x-www-form-urlencoded"}
        if idempotency:
            headers["Idempotency-Key"] = idempotency
        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
                response = client.post(f"{self.api_url.rstrip('/')}{path}", data=dict(form),
                                       headers=headers)
        except httpx.HTTPError as exc:
            raise BillingError(f"Stripe unreachable: {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            raise BillingError(f"Stripe answered {response.status_code} with something unreadable") from None
        if response.status_code != 200 or not isinstance(body, dict):
            code = (body.get("error") or {}).get("code") if isinstance(body, dict) else None
            raise BillingError(f"Stripe answered {response.status_code} ({code or 'error'})")
        return body

    def checkout(self, *, tenant_id: str, plan_id: str, customer: str | None, email: str, clients: int,
                 success_url: str, cancel_url: str) -> str:
        """A Checkout Session for ``plan_id`` (a subscription); the owner pays on Stripe's page."""
        if not self.offers(plan_id):
            raise BillingError(f"no Stripe price for {plan_id}")
        form = [("mode", "subscription"), ("success_url", success_url), ("cancel_url", cancel_url),
                ("client_reference_id", tenant_id), ("metadata[tenant]", tenant_id), ("metadata[plan]", plan_id),
                ("subscription_data[metadata][tenant]", tenant_id), ("subscription_data[metadata][plan]", plan_id),
                ("line_items[0][price]", self.prices[plan_id]), ("line_items[0][quantity]", "1"),
                ("allow_promotion_codes", "true"), ("billing_address_collection", "required"),
                ("tax_id_collection[enabled]", "true")]
        if plan_id == "accountant" and self.client_price:
            form += [("line_items[1][price]", self.client_price), ("line_items[1][quantity]", str(max(clients, 1)))]
        if customer:
            form += [("customer", customer), ("customer_update[address]", "auto"), ("customer_update[name]", "auto")]
        elif email:
            form.append(("customer_email", email))
        body = self._post("/v1/checkout/sessions", form, idempotency=f"checkout-{tenant_id}-{secrets.token_hex(8)}")
        url = body.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise BillingError("Stripe gave no checkout address")
        return url

    def portal(self, *, customer: str, return_url: str, subscription: str | None = None) -> str:
        """Stripe's customer portal (card, invoices, cancel); with ``subscription``, straight to changing plan."""
        form = [("customer", customer), ("return_url", return_url)]
        if subscription:
            form += [("flow_data[type]", "subscription_update"),
                     ("flow_data[subscription_update][subscription]", subscription)]
        url = self._post("/v1/billing_portal/sessions", form).get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise BillingError("Stripe gave no portal address")
        return url

    def verify(self, payload: bytes, header: str | None, now: float) -> dict[str, Any]:
        """The webhook's event, once its signature and time are checked."""
        verify_signature(payload, header, self.webhook_secret, now)
        try:
            event = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise WebhookRefused("not JSON") from None
        if not isinstance(event, dict) or event.get("object") != "event":
            raise WebhookRefused("not an event")
        return event


def billing_from_env(env: Mapping[str, str] | None = None) -> StripeBilling | None:
    """Stripe from the environment, or None (no payments) without ``STRIPE_SECRET_KEY`` and the webhook secret."""
    e = os.environ if env is None else env
    key = e.get("STRIPE_SECRET_KEY", "").strip()
    webhook = e.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not key or not webhook:
        return None
    return StripeBilling(secret_key=key, webhook_secret=webhook,
                         prices={plan: e.get(name, "").strip() for plan, name in PRICE_ENV.items()
                                 if e.get(name, "").strip()},
                         client_price=e.get(CLIENT_PRICE_ENV, "").strip() or None,
                         api_url=(e.get("STRIPE_API_URL") or STRIPE_API).strip())
