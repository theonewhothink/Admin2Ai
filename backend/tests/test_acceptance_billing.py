"""Plans and billing (production readiness "Payments and billing"; QA X35 "low-volume plan and billing").

* The plans of the product specification are data, with limits: Free (the historical audit, then limited
  scanning), Solo €9, Business €19, Multi-company €29 with an allowance of companies, Accountant (a practice
  plan plus a low price per active client). The static demo is on "Demo" and never calls a payment provider.
* Limits never block evidence and never lose data: over a limit the owner reads one plain upgrade line and
  everything keeps being processed for a grace period; after it, new evidence is kept and waits, and it is read
  as soon as the plan covers it (an upgrade, a new month). The free audit's history never counts.
* Payments go through Stripe (a fake here: no network). Checkout and the customer portal are Stripe's pages; the
  webhook is read only once its signature is checked (valid, invalid, replayed), and each event updates the plan
  once, recorded in the business's log and replay-safe. A failed payment is a plain notice and a grace period.
* The billing routes are the owner's only.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
import pytest
from _server_support import NIF_B, PASSWORD, bearer, harness, signup
from test_acceptance_accountant import pg_store  # noqa: F401  (its PostgreSQL store fixture, with 0015 applied)
from test_server_push import TOKEN_A, FakeExpo

from backoffice import billing as B
from backoffice.language import find_jargon
from backoffice.orchestrator import TZ
from backoffice.readiness import READINESS
from backoffice.server.billing import (
    StripeBilling,
    WebhookRefused,
    billing_from_env,
    sign_payload,
    verify_signature,
)
from backoffice.server.events import Event, state_digest
from backoffice.server.notify import ExpoPushClient, PushNotifier
from backoffice.server.runtime import TenantManager
from backoffice.server.store import MemoryStore
from backoffice.service import BackOfficeService

D = Decimal
SECRET = "whsec_test_signing_secret"
PRICES = {"solo": "price_solo", "business": "price_business", "multi": "price_multi",
          "accountant": "price_accountant"}
PADARIA = "516123459"


# --------------------------------------------------------------------------- a fake Stripe (no network)


class FakeStripe:
    """Stripe's API as the back office uses it: Checkout Sessions and customer portal sessions."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            return httpx.Response(500, json={"error": {"code": "api_error"}})
        assert request.headers["authorization"] == "Bearer sk_test_back_office"
        n = len(self.requests)
        if request.url.path == "/v1/checkout/sessions":
            return httpx.Response(200, json={"id": f"cs_test_{n}", "object": "checkout.session",
                                             "url": f"https://checkout.stripe.com/c/pay/cs_test_{n}"})
        if request.url.path == "/v1/billing_portal/sessions":
            return httpx.Response(200, json={"id": f"bps_{n}", "url": f"https://billing.stripe.com/p/session/{n}"})
        return httpx.Response(404, json={"error": {"code": "resource_missing"}})

    def form(self, i: int = -1) -> dict[str, str]:
        return dict(httpx.QueryParams(self.requests[i].content.decode()))


def stripe(fake: FakeStripe | None = None) -> StripeBilling:
    return StripeBilling(secret_key="sk_test_back_office", webhook_secret=SECRET, prices=PRICES,
                         client_price="price_accountant_client",
                         transport=httpx.MockTransport(fake or FakeStripe()))


def paying(tmp_path: Path, **services: Any) -> tuple[Any, FakeStripe]:
    fake = FakeStripe()
    return harness(tmp_path, billing=stripe(fake), **services), fake


def events(h: Any, tenant: str, kind: str | None = None) -> list[Event]:
    return [e for e in (Event.parse(r) for r in h.store.events(tenant)) if kind is None or e.kind == kind]


def digest(manager: TenantManager, tenant: str) -> str:
    with manager.open(tenant) as rt:
        return state_digest(rt.service)


def replayed(h: Any, tenant: str) -> str:
    """The business another process rebuilds from its log alone: no payment provider, no reader, no mailer."""
    return digest(TenantManager(h.store, h.objects, now=h.clock, strict_reads=True), tenant)


def ok(res: Any, status: int = 200) -> Any:
    assert res.status_code == status, (res.request.url, res.status_code, res.text)
    return res.json() if res.content else None


def ts(h: Any) -> int:
    return int(h.clock.now_.timestamp())


def checkout_completed(tenant: str, plan: str, *, event_id: str = "evt_checkout_1", created: int,
                       customer: str = "cus_padaria", subscription: str = "sub_padaria") -> dict[str, Any]:
    return {"id": event_id, "object": "event", "type": "checkout.session.completed", "created": created,
            "livemode": False, "data": {"object": {
                "id": "cs_test_1", "object": "checkout.session", "mode": "subscription", "status": "complete",
                "payment_status": "paid", "client_reference_id": tenant, "customer": customer,
                "subscription": subscription, "metadata": {"tenant": tenant, "plan": plan},
                "customer_details": {"email": "ana@example.pt", "name": "Ana Silva",
                                     "address": {"line1": "Rua das Flores 12", "city": "Porto"}}}}}


def subscription(kind: str, price: str, status: str, *, event_id: str, created: int,
                 customer: str = "cus_padaria", sub: str = "sub_padaria") -> dict[str, Any]:
    """customer.subscription.<kind>: after a plan change on Stripe's own page the metadata is the old plan's;
    the price says which plan it is now."""
    return {"id": event_id, "object": "event", "type": f"customer.subscription.{kind}", "created": created,
            "data": {"object": {"id": sub, "object": "subscription", "customer": customer, "status": status,
                                "metadata": {"plan": "solo"},
                                "items": {"data": [{"price": {"id": price}, "current_period_end": created + 30 * 86400}]}}}}


def invoice_event(kind: str, *, event_id: str, created: int, customer: str = "cus_padaria") -> dict[str, Any]:
    """An invoice event names only its customer (no tenant): the customer link finds the business."""
    return {"id": event_id, "object": "event", "type": kind, "created": created,
            "data": {"object": {"id": "in_1", "object": "invoice", "customer": customer, "subscription": "sub_padaria",
                                "customer_email": "ana@example.pt", "attempt_count": 1,
                                "payment_intent": {"last_payment_error": {"card": {"last4": "4242"}}}}}}


def webhook(h: Any, event: dict[str, Any], *, secret: str = SECRET, at: int | None = None,
            signature: str | None = None) -> Any:
    payload = json.dumps(event).encode()
    header = signature if signature is not None else sign_payload(payload, secret, ts(h) if at is None else at)
    return h.client.post("/api/billing/webhook", content=payload,
                         headers={"Stripe-Signature": header, "Content-Type": "application/json"})


def invoice_text(number: int, day: date, total: str = "123,00") -> bytes:
    """A supplier's invoice to the bakery issued after it joined (it counts towards the month's documents)."""
    return (f"Moagem do Norte, Lda.\nNIF: 508111226\nFatura n.º FT MN2026/{number}\nData de emissão: "
            f"{day:%d/%m/%Y}\nCliente: Padaria Lda\nNIF: {PADARIA}\nFarinha de trigo T65\nBase tributável (6%): "
            f"116,04\nIVA 6%: 6,96\nTotal: {total} €\n").encode()


def send(h: Any, H: dict[str, str], number: int, day: date) -> dict[str, Any]:
    return ok(h.client.post("/api/evidence", files={"file": (f"ft-{number}.txt", invoice_text(number, day),
                                                            "text/plain")}, headers=H))


def plain(text: str | None) -> None:
    assert text and not find_jargon(text), text


# =========================================================================== the plans, as data


def test_plans_are_data_with_the_specification_prices_and_limits() -> None:
    plans = B.PLANS
    assert [(p.id, p.name, p.monthly) for p in plans.values()] == [
        ("free", "Free", D("0")), ("solo", "Solo", D("9")), ("business", "Business", D("19")),
        ("multi", "Multi-company", D("29")), ("accountant", "Accountant", D("39"))]
    assert (plans["free"].companies, plans["free"].documents, plans["free"].users) == (1, 25, 1)
    assert plans["multi"].companies == 5 and plans["accountant"].per_client == D("2")
    assert B.PAID_PLANS == ("solo", "business", "multi", "accountant") and not plans["free"].self_serve
    assert [p.price_words() for p in plans.values()] == [
        "Free", "€9 a month", "€19 a month", "€29 a month", "€39 a month plus €2 for each active client"]
    for p in plans.values():  # every plan's limits, in the shape the owner's page reads
        assert set(p.public()["limits"]) == {"companies", "documentsPerMonth", "users"}
        plain(p.summary)
    assert B.plan_for("demo") is B.DEMO_PLAN and B.DEMO_PLAN.price_words() == "Nothing to pay"


def test_the_static_demo_shows_the_demo_plan_and_never_calls_stripe(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the demo called a payment provider")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    svc = BackOfficeService.demo()
    status, view = svc.dispatch("GET", "/api/billing")
    assert status == 200 and view["demo"] is True
    assert (view["plan"]["id"], view["plan"]["name"], view["canUpgrade"], view["prompt"]) == \
        ("demo", "Demo", False, None)
    assert view["message"] == "This is the demo business, so nothing here is billed."
    for path in ("/api/billing/checkout", "/api/billing/portal"):
        assert svc.dispatch("POST", path, {"plan": "solo"}) == (409, {
            "error": "conflict", "message": "This is the demo business, so there is nothing to pay."})
    with pytest.raises(Exception, match="demo"):
        svc.billing_event(B.reduce_event(checkout_completed("t", "solo", created=1)) or {})
    # The demo's outcomes: nothing is ever held, whatever arrives.
    assert not svc.intake_held() and svc.billing.waiting == []
    assert billing_from_env({}) is None  # no keys, no payments


# =========================================================================== the webhook: signed, once


def test_webhook_signatures_are_verified_valid_invalid_and_replayed(tmp_path: Path) -> None:
    payload = b'{"id": "evt_1", "object": "event"}'
    now = 1_790_000_000
    verify_signature(payload, sign_payload(payload, SECRET, now), SECRET, now)  # valid
    header = f"t={now},v1=deadbeef,v1={sign_payload(payload, SECRET, now).split('v1=')[1]}"
    verify_signature(payload, header, SECRET, now + 60)  # one of several signatures (a secret being rolled)
    for bad, when in ((sign_payload(payload, "whsec_other", now), now),  # another secret
                      (sign_payload(payload + b" ", SECRET, now), now),  # the body was changed
                      (sign_payload(payload, SECRET, now), now + 301),  # replayed later
                      (f"t={now}", now), ("", now), ("t=x,v1=00", now)):
        with pytest.raises(WebhookRefused):
            verify_signature(payload, bad, SECRET, when)

    h, _ = paying(tmp_path)
    ana = signup(h.client)
    tenant = ana["tenant"]["id"]
    event = checkout_completed(tenant, "solo", created=ts(h))
    before = len(events(h, tenant))
    refused = webhook(h, event, secret="whsec_forged")
    assert refused.status_code == 400 and refused.json() == {
        "error": "bad_signature", "message": "This request is not signed by the payment provider."}
    replay = webhook(h, event, at=ts(h) - 3600)  # a real signature from an hour ago: replayed
    assert replay.status_code == 400
    assert webhook(h, event, signature="").status_code == 400
    assert len(events(h, tenant)) == before  # nothing refused was recorded
    assert ok(webhook(h, event)) == {"received": True}
    assert ok(webhook(h, event)) == {"received": True}  # Stripe sends it again: applied once
    assert [e.data["event"]["id"] for e in events(h, tenant, "billing.event")] == ["evt_checkout_1"]


def test_each_webhook_updates_the_plan_once(tmp_path: Path) -> None:
    h, _ = paying(tmp_path)
    ana = signup(h.client)
    H, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    plan = lambda: ok(h.client.get("/api/billing", headers=H))["plan"]  # noqa: E731
    assert (plan()["id"], plan()["status"]) == ("free", "active")
    t0 = ts(h)
    steps = [
        (checkout_completed(tenant, "solo", created=t0), ("solo", "active")),
        (subscription("updated", "price_business", "active", event_id="evt_sub_2", created=t0 + 60),
         ("business", "active")),
        (invoice_event("invoice.payment_failed", event_id="evt_inv_3", created=t0 + 120), ("business", "past_due")),
        (invoice_event("invoice.paid", event_id="evt_inv_4", created=t0 + 180), ("business", "active")),
        (subscription("deleted", "price_business", "canceled", event_id="evt_sub_5", created=t0 + 240),
         ("free", "canceled")),
    ]
    for event, expected in steps:
        for _ in range(3):  # retried by Stripe: each changes the plan once
            ok(webhook(h, event))
        assert (plan()["id"], plan()["status"]) == expected, event["type"]
    assert [e.data["event"]["id"] for e in events(h, tenant, "billing.event")] == \
        ["evt_checkout_1", "evt_sub_2", "evt_inv_3", "evt_inv_4", "evt_sub_5"]
    # The customer is linked to the business with the first event that named it: an invoice names only its customer.
    assert h.store.billing_tenant("cus_padaria") == tenant
    # An older subscription change arriving late never undoes a newer one.
    ok(webhook(h, subscription("updated", "price_multi", "active", event_id="evt_sub_old", created=t0 + 100)))
    assert plan()["id"] == "free"
    # What was recorded is what the plan needs: no name, email, address or card.
    recorded = json.dumps([e.data for e in events(h, tenant, "billing.event")])
    for private in ("ana@example.pt", "Ana Silva", "Rua das Flores", "4242", "customer_details"):
        assert private not in recorded
    # An event for nobody we know is acknowledged and ignored.
    stranger = invoice_event("invoice.payment_failed", event_id="evt_x", created=t0, customer="cus_stranger")
    assert ok(webhook(h, stranger)) == {"received": True, "ignored": True}
    assert ok(webhook(h, {"id": "evt_y", "object": "event", "type": "charge.succeeded", "created": t0,
                          "data": {"object": {"id": "ch_1"}}})) == {"received": True}


# =========================================================================== checkout and portal: Stripe's pages


def test_checkout_and_portal_are_stripe_pages_and_no_card_data_touches_the_server(tmp_path: Path) -> None:
    h, fake = paying(tmp_path)
    ana = signup(h.client)
    H, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    view = ok(h.client.get("/api/billing", headers=H))
    assert (view["canUpgrade"], view["canManage"], view["usage"]["users"]) == (True, False, 1)
    assert ok(h.client.post("/api/billing/portal", json={}, headers=H), 409)["message"] == \
        "You don't have a paid plan yet. Choose a plan first."
    before = len(events(h, tenant))
    started = ok(h.client.post("/api/billing/checkout", json={"plan": "solo"}, headers=H))
    assert started == {"url": "https://checkout.stripe.com/c/pay/cs_test_1", "via": "checkout", "plan": "solo"}
    form = fake.form()
    assert (form["mode"], form["client_reference_id"], form["metadata[plan]"], form["line_items[0][price]"],
            form["subscription_data[metadata][tenant]"], form["customer_email"]) == (
        "subscription", tenant, "solo", "price_solo", tenant, "ana@example.pt")
    assert fake.requests[-1].headers["idempotency-key"].startswith(f"checkout-{tenant}-")
    assert not [k for k in form if "card" in k or "number" in k or "cvc" in k]
    assert len(events(h, tenant)) == before  # opening the payment page changes nothing: the webhook does
    assert ok(h.client.get("/api/billing", headers=H))["plan"]["id"] == "free"
    assert ok(h.client.post("/api/billing/checkout", json={"plan": "platinum"}, headers=H), 400)["message"] == \
        "Choose Solo, Business, Multi-company or Accountant."
    ok(webhook(h, checkout_completed(tenant, "solo", created=ts(h))))
    view = ok(h.client.get("/api/billing", headers=H))
    assert (view["plan"]["id"], view["plan"]["name"], view["canManage"]) == ("solo", "Solo", True)
    assert ok(h.client.post("/api/billing/checkout", json={"plan": "solo"}, headers=H), 409)["message"] == \
        "You are already on the Solo plan."
    # Changing plan while paying: Stripe's own page to change it (never a second subscription).
    change = ok(h.client.post("/api/billing/checkout", json={"plan": "business"}, headers=H))
    assert change["via"] == "portal" and change["url"].startswith("https://billing.stripe.com/")
    assert fake.form()["flow_data[subscription_update][subscription]"] == "sub_padaria"
    portal = ok(h.client.post("/api/billing/portal", json={}, headers=H))
    assert portal["url"].startswith("https://billing.stripe.com/") and fake.form()["customer"] == "cus_padaria"
    # The accountant plan: the practice, and each active client.
    StripeBilling(secret_key="sk_test_back_office", webhook_secret=SECRET, prices=PRICES,
                  client_price="price_accountant_client", transport=httpx.MockTransport(fake)).checkout(
        tenant_id=tenant, plan_id="accountant", customer=None, email="carla@contas.pt", clients=7,
        success_url="https://app/ok", cancel_url="https://app/no")
    assert (fake.form()["line_items[1][price]"], fake.form()["line_items[1][quantity]"]) == \
        ("price_accountant_client", "7")
    fake.down = True
    assert ok(h.client.post("/api/billing/portal", json={}, headers=H), 502)["message"] == \
        "The billing page did not open. Please try again in a few minutes."
    # A server without payments says so plainly.
    bare = harness(tmp_path / "bare")
    X = bearer(signup(bare.client, "rui@oficina.pt", company="Oficina Rui", tax_id=None, name="Rui")["token"])
    assert ok(bare.client.post("/api/billing/checkout", json={"plan": "solo"}, headers=X), 503)["message"] == \
        "Payments are not set up on this server yet."
    assert ok(bare.client.get("/api/billing", headers=X))["canUpgrade"] is False
    assert bare.client.post("/api/billing/webhook", content=b"{}").status_code == 404


# =========================================================================== a failed payment


def test_a_failed_payment_is_a_plain_owner_notice_and_a_grace_period(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(B.PLANS, "free", replace(B.PLANS["free"], documents=1))
    expo = FakeExpo()
    store = MemoryStore()
    h, _ = paying(tmp_path, store=store,
                  notifier=PushNotifier(store, ExpoPushClient(transport=httpx.MockTransport(expo))))
    ana = signup(h.client)
    H, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    ok(h.client.post("/api/devices", json={"expoPushToken": TOKEN_A, "platform": "ios"}, headers=H), 204)
    ok(webhook(h, checkout_completed(tenant, "solo", created=ts(h))))
    for n in (1, 2):
        send(h, H, n, date(2026, 10, 2))  # within Solo's documents
    assert expo.sent == []  # a plan that starts is quiet success
    ok(webhook(h, invoice_event("invoice.payment_failed", event_id="evt_fail", created=ts(h))))
    notice = ("The payment for your Solo plan did not go through. Everything keeps working until 16 October. "
              "Update your card to keep your plan.")
    view = ok(h.client.get("/api/billing", headers=H))
    assert (view["plan"]["status"], view["notice"], view["over"], view["held"]) == ("past_due", notice, [], False)
    assert [(m["title"], m["body"]) for m in expo.sent] == [("Payment did not go through", notice)]
    assert notice in [a["text"] for a in ok(h.client.get("/api/activity", headers=H))["items"]]
    plain(notice)
    ok(webhook(h, invoice_event("invoice.payment_failed", event_id="evt_fail_2", created=ts(h))))
    assert len(expo.sent) == 1  # Stripe's next attempt fails too: told once
    # Still within the grace period: everything is processed.
    h.clock.advance(days=10)
    assert send(h, H, 3, date(2026, 10, 12))["documents"]
    # After it: the Free plan's limits, nothing deleted, new documents wait.
    h.clock.advance(days=5)
    view = ok(h.client.get("/api/billing", headers=H))
    assert view["notice"] == ("The payment for your Solo plan did not go through, so the Free plan's limits apply "
                              "until it does. Nothing is deleted. Update your card to go back to your plan.")
    assert (view["limits"]["documentsPerMonth"], view["over"], view["held"]) == (1, ["documents"], True)
    held = send(h, H, 4, date(2026, 10, 17))
    assert (held["held"], held["documents"]) == (True, [])
    documents = len(ok(h.client.get("/api/documents", headers=H))["items"])
    # The card is updated: the plan is back and what waited is read.
    ok(webhook(h, invoice_event("invoice.paid", event_id="evt_paid", created=ts(h))))
    view = ok(h.client.get("/api/billing", headers=H))
    assert (view["plan"]["status"], view["notice"], view["waiting"], view["held"]) == ("active", None, 0, False)
    assert len(ok(h.client.get("/api/documents", headers=H))["items"]) == documents + 1
    h.clock.step = h.clock.step * 0
    assert replayed(h, tenant) == digest(h.manager, tenant)
    assert len(expo.sent) == 1  # a replay never notifies


# =========================================================================== limits, without losing anything


def test_limits_are_enforced_without_losing_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(B.PLANS, "free", replace(B.PLANS["free"], documents=2))
    h, _ = paying(tmp_path)
    ana = signup(h.client)
    H, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    for n in (1, 2):
        send(h, H, n, date(2026, 10, 2))
    assert ok(h.client.get("/api/billing", headers=H))["prompt"] is None
    # Over the limit: one plain line, and everything keeps being processed for the grace period.
    third = send(h, H, 3, date(2026, 10, 2))
    assert third["documents"] and "held" not in third
    prompt = ("You are over the Free plan's 2 documents a month. Everything keeps being processed until "
              "16 October. After that, new documents are kept safely and wait until you upgrade or the month turns. "
              "The Solo plan (€9 a month) covers it.")
    view = ok(h.client.get("/api/billing", headers=H))
    assert (view["over"], view["prompt"], view["graceUntil"], view["held"]) == (["documents"], prompt,
                                                                               "2026-10-16", False)
    assert prompt in [a["text"] for a in ok(h.client.get("/api/activity", headers=H))["items"]]
    plain(prompt)
    # A second company is never refused either: it is only over the plan's allowance.
    ok(h.client.post("/api/onboarding/company", json={"name": "Second Company", "taxId": NIF_B}, headers=H))
    assert ok(h.client.get("/api/billing", headers=H))["over"] == ["companies", "documents"]
    # After the grace period: what arrives is kept (stored, its hash checked) and waits, unread.
    h.clock.advance(days=15)
    count = len(ok(h.client.get("/api/documents", headers=H))["items"])
    held = send(h, H, 4, date(2026, 10, 17))
    assert held == {"ok": True, "message": "Saved. It waits for your plan: it is read as soon as you upgrade or the "
                    "month turns.", "evidenceIds": [], "documents": [], "transactions": [], "pendingLinks": [],
                    "storedOnly": True, "held": True}
    scan = invoice_text(5, date(2026, 10, 17))
    phone = ok(h.client.post("/api/evidence/upload", data={"sha256": hashlib.sha256(scan).hexdigest(),
                                                          "source": "mobile_scan"},
                             files={"file": ("scan.txt", scan, "text/plain")},
                             headers={**H, "Idempotency-Key": "scan-000000000777"}))
    assert (phone["status"], phone["delete_local"], phone["held"], phone["sha256"]) == \
        ("stored", True, True, hashlib.sha256(scan).hexdigest())  # the phone may delete its copy: we have it
    bad = ok(h.client.post("/api/evidence/upload", data={"sha256": "0" * 64, "source": "mobile_scan"},
                           files={"file": ("scan.txt", scan, "text/plain")},
                           headers={**H, "Idempotency-Key": "scan-000000000778"}), 422)
    assert bad["delete_local"] is False  # a damaged upload is never kept as if it were fine
    assert len(ok(h.client.get("/api/documents", headers=H))["items"]) == count
    recorded = events(h, tenant, "request")[-3:-1]
    assert all(e.data.get("held") is True and "reads" not in e.data for e in recorded)
    for e in recorded:  # the files themselves are in the evidence store
        ref = e.data["body"]["dataBase64"]["$object"]
        assert h.objects.get(ref["key"]) in (invoice_text(4, date(2026, 10, 17)), scan)
    view = ok(h.client.get("/api/billing", headers=H))
    assert (view["waiting"], view["held"]) == (2, True)
    assert "New documents are kept safely and wait until you upgrade or the month turns. Nothing is lost." in \
        view["prompt"]
    # The owner upgrades: everything that waited is read, in the order it arrived.
    ok(webhook(h, checkout_completed(tenant, "multi", created=ts(h))))
    view = ok(h.client.get("/api/billing", headers=H))
    assert (view["plan"]["id"], view["waiting"], view["held"], view["over"]) == ("multi", 0, False, [])
    numbers = [d["number"] for d in ok(h.client.get("/api/documents", headers=H))["items"]]
    assert len(numbers) == count + 2 and {"FT MN2026/4", "FT MN2026/5"} <= set(numbers)
    assert "Your plan covers it again: I read the 2 items that were waiting." in \
        [a["text"] for a in ok(h.client.get("/api/activity", headers=H))["items"]]
    h.clock.step = h.clock.step * 0
    assert replayed(h, tenant) == digest(h.manager, tenant)


def test_evidence_that_waited_is_read_when_the_month_turns_and_mail_is_never_lost(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(B.PLANS, "free", replace(B.PLANS["free"], documents=1))
    h, _ = paying(tmp_path)
    ana = signup(h.client)
    H, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    ok(h.client.post("/api/sources", json={"kind": "email", "provider": "imap", "address": "ana@padaria.pt",
                                           "host": "imap.padaria.pt", "password": "app-password-123"}, headers=H))

    def mail(n: int, day: date) -> bytes:
        message = EmailMessage()
        message["From"], message["To"] = "faturas@moagem.pt", "ana@padaria.pt"
        message["Subject"], message["Message-ID"] = f"Fatura FT MN2026/{n}", f"<ft-{n}@moagem.pt>"
        message["Date"] = f"{day:%a, %d %b %Y} 10:00:00 +0100"
        message.set_content("Segue a fatura.")
        message.add_attachment(invoice_text(n, day), maintype="text", subtype="plain", filename=f"FT{n}.txt")
        return message.as_bytes()

    for n in (1, 2):  # the second is over the limit: processed, the grace period starts
        h.manager.record_mail(tenant, "mail-ana-padaria-pt", [mail(n, date(2026, 10, 2))], None)
    h.clock.advance(days=15)
    h.manager.record_mail(tenant, "mail-ana-padaria-pt", [mail(3, date(2026, 10, 17))], {"cursor": "3"})
    last = events(h, tenant, "sync.mail")[-1]
    assert last.data["held"] is True and "reads" not in last.data
    view = ok(h.client.get("/api/billing", headers=H))
    assert (view["waiting"], view["held"]) == (1, True)
    documents = len(ok(h.client.get("/api/documents", headers=H))["items"])
    assert documents == 2
    # November: the month's allowance starts again, and the email that waited is read first thing.
    h.clock.advance(days=15)
    view = ok(h.client.get("/api/billing", headers=H))
    assert (view["usage"]["month"], view["waiting"], view["held"]) == ("2026-11", 0, False)
    assert len(ok(h.client.get("/api/documents", headers=H))["items"]) == 3
    tick = events(h, tenant, "tick")[-1]
    assert tick.data["env"]["reader"] is False  # read through what was recorded before the tick (no reader here)
    h.clock.step = h.clock.step * 0
    assert replayed(h, tenant) == digest(h.manager, tenant)


def test_the_free_audit_never_counts_and_stays_free() -> None:
    now = datetime(2026, 10, 2, 9, 30, tzinfo=TZ)
    svc = BackOfficeService.new_tenant("t-audit", owner_name="Ana Silva", owner_email="ana@padaria.pt", now=now)
    svc.add_company("Padaria Lda", PADARIA, "Padaria Lda")
    svc.billing.enforced = True
    assert svc.billing.plan == "free" and svc.billing.started_on == date(2026, 10, 2)
    for n in range(40):  # the last 90 days, read at sign-up: the free audit
        svc.upload_evidence(f"old-{n}.txt", "text/plain", invoice_text(n, date(2026, 9, 1) + timedelta(days=n % 30),
                                                                         total=f"{100 + n},00"))
    view = svc.billing_view()
    assert (view["usage"]["documents"], view["over"], view["prompt"], view["held"]) == (0, [], None, False)
    assert svc.dispatch("GET", "/api/audit")[1]["findings"][1]["value"] == "40"  # the audit itself, free
    svc.upload_evidence("new.txt", "text/plain", invoice_text(99, date(2026, 10, 2)))
    assert svc.billing_view()["usage"]["documents"] == 1


# =========================================================================== owner only


def test_billing_routes_are_the_owners_only(tmp_path: Path) -> None:
    h, _ = paying(tmp_path)
    ana = signup(h.client)
    H, tenant = bearer(ana["token"]), ana["tenant"]["id"]

    def member(email: str, role: str) -> dict[str, str]:
        other = signup(h.client, email, company="Other", tax_id=None, name="Other Person")
        h.store._d.memberships.discard((other["tenant"]["id"], other["user"]["id"], "owner"))
        h.store.add_membership(tenant, other["user"]["id"], role)
        return bearer(ok(h.client.post("/api/auth/login", json={"email": email, "password": PASSWORD}))["token"])

    accountant, employee = member("marc@vidal.pt", "accountant"), member("rui@padaria.pt", "employee")
    for who in (accountant, employee):
        assert h.client.get("/api/billing", headers=who).status_code == 403
        for path in ("/api/billing/checkout", "/api/billing/portal"):
            assert h.client.post(path, json={"plan": "solo"}, headers=who).status_code == 403
    h.client.cookies.clear()
    assert h.client.get("/api/billing").status_code == 401
    # People who count: the owner and the employee; the accountant is always free.
    assert ok(h.client.get("/api/billing", headers=H))["usage"]["users"] == 2
    assert ok(h.client.get("/api/billing", headers=H))["over"] == ["users"]
    # The webhook needs no sign-in, only Stripe's signature.
    assert ok(webhook(h, checkout_completed(tenant, "business", created=ts(h)))) == {"received": True}
    assert ok(h.client.get("/api/billing", headers=H))["over"] == []


def test_billing_is_replay_safe_and_reading_it_changes_nothing(tmp_path: Path) -> None:
    h, _ = paying(tmp_path)  # strict reads: a read that changed the business fails the test
    ana = signup(h.client)
    H, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    created = events(h, tenant, "tenant.created")[0]
    assert created.data["billing"] == {"enforced": True}  # recorded: a replay holds the business to the same plan
    ok(webhook(h, checkout_completed(tenant, "solo", created=ts(h))))
    ok(webhook(h, subscription("updated", "price_business", "active", event_id="evt_2", created=ts(h) + 5)))
    h.clock.step = h.clock.step * 0
    before, count = digest(h.manager, tenant), len(h.store.events(tenant))
    for _ in range(2):
        ok(h.client.get("/api/billing", headers=H))
    assert digest(h.manager, tenant) == before and len(h.store.events(tenant)) == count
    assert replayed(h, tenant) == before
    fresh = TenantManager(h.store, h.objects, now=h.clock, strict_reads=True)
    with fresh.open(tenant) as rt:
        state = rt.service.billing
        assert (state.plan, state.status, state.customer, state.enforced, state.events) == (
            "business", "active", "cus_padaria", True, ["evt_checkout_1", "evt_2"])
    # A business created before payments were set up replays as it always did: never held to a plan.
    old = harness(tmp_path / "old")
    rui = signup(old.client, "rui@oficina.pt", company="Oficina Rui", tax_id=None, name="Rui")
    assert "billing" not in events(old, rui["tenant"]["id"], "tenant.created")[0].data
    with old.manager.open(rui["tenant"]["id"]) as rt:
        assert rt.service.billing.enforced is False and rt.service.decides_holds is False


def test_billing_customers_on_postgresql_are_found_by_the_customer_a_webhook_names(tmp_path: Path,
                                                                                   pg_store: Any) -> None:
    fake = FakeStripe()
    h = harness(tmp_path, store=pg_store, billing=stripe(fake))
    ana = signup(h.client, "ana.pg@example.pt")
    H, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    ok(webhook(h, checkout_completed(tenant, "solo", created=ts(h), customer="cus_pg_1")))
    assert pg_store.billing_tenant("cus_pg_1") == tenant and pg_store.billing_tenant("cus_nobody") is None
    assert pg_store.member_count(tenant) == 1
    ok(webhook(h, invoice_event("invoice.payment_failed", event_id="evt_pg_2", created=ts(h), customer="cus_pg_1")))
    assert ok(h.client.get("/api/billing", headers=H))["plan"]["status"] == "past_due"
    # Row-level security: another business's scope sees no customer of this one.
    other = signup(h.client, "rui.pg@example.pt", company="Oficina Rui", tax_id=None, name="Rui")
    with pg_store._tx(tenant=other["tenant"]["id"]) as cur:
        cur.execute("SELECT count(*) FROM billing_customers")
        assert cur.fetchone()[0] == 0
    with pg_store._tx() as cur:
        cur.execute("SELECT count(*) FROM billing_customers")
        assert cur.fetchone()[0] == 0  # nothing presented, nothing visible
    # Erasing the business removes its customer link.
    ok(h.client.post("/api/account/delete", json={"confirm": "DELETE", "password": PASSWORD}, headers=H), 202)
    assert pg_store.billing_tenant("cus_pg_1") is None


def test_readiness_says_billing_is_built_and_needs_a_stripe_account() -> None:
    item = next(i for i in READINESS if i.id == "billing")
    assert (item.title, item.status, item.area) == ("Payments and billing", "pending", "billing")
    assert 0 < item.percent < 100 and "Stripe" in item.detail and "live keys" in item.detail


def test_reduced_events_keep_only_the_plan_facts() -> None:
    event = subscription("updated", "price_multi", "active", event_id="evt_r", created=1_790_000_000)
    event["data"]["object"]["default_payment_method"] = {"card": {"last4": "4242", "exp_month": 12}}
    reduced = B.reduce_event(event, {v: k for k, v in PRICES.items()})
    assert reduced == {"id": "evt_r", "type": "customer.subscription.updated", "created": 1_790_000_000,
                       "livemode": False, "object": {"id": "sub_padaria", "customer": "cus_padaria",
                                                     "status": "active", "plan": "multi",
                                                     "current_period_end": 1_790_000_000 + 30 * 86400}}
    assert B.reduce_event({"id": "evt_z", "type": "charge.refunded", "data": {"object": {}}}) is None
    state = B.BillingState(plan="free", started_on=date(2026, 10, 2))
    assert state.apply(reduced, date(2026, 10, 2))["plan"] == "multi"
    assert state.renews_on == datetime.fromtimestamp(1_790_000_000 + 30 * 86400, tz=timezone.utc).date()
    assert state.apply(reduced, date(2026, 10, 2)) == {"applied": False, "reason": "already applied"}
    encoded = base64.b64encode(json.dumps(reduced).encode())
    assert b"4242" not in base64.b64decode(encoded)
