"""Push notifications to the owner's phone, through Expo, for the few things that need them (§42).

Only six things ever notify:

* a new hard approval: a payment on hold because the bank details changed
  or the invoice needs checking (§25, §26);
* a connection that needs reconnecting (§47, §48);
* a connection whose access ends within a week (a bank consent, a sign-in
  given until a stated day): once per end date, a week before (checklist R4);
* a supplier's site that needs the owner to sign in (or a code) before the
  invoice behind its link can be fetched (§9: "Supplier X needs authentication.");
* a month that closed;
* a payment for the business's plan that did not go through (backoffice.billing).

Nothing else: "invoice processed" is quiet success. Messages are computed
from the tenant's state before and after each live change (never during a
replay), and delivered in the background so a slow push service never slows
the owner down. Phones Expo reports as gone are forgotten.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass
from typing import Any

from backoffice.closure import Month
from backoffice.learning import display_name, format_money

from .store import Store

__all__ = ["EXPO_PUSH_URL", "ExpoPushClient", "Facts", "PushError", "PushMessage", "PushNotifier", "facts",
           "messages_for"]

log = logging.getLogger("backoffice.server.push")

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
EXPO_BATCH = 100  # Expo accepts at most 100 messages per request


class PushError(RuntimeError):
    """The push service refused or could not be reached (developer-facing)."""


@dataclass(frozen=True)
class PushMessage:
    title: str
    body: str
    url: str  # where the app opens


@dataclass(frozen=True)
class Facts:
    approvals: frozenset[str]
    stale: frozenset[str]
    closed: frozenset[tuple[str, str]]
    sign_ins: frozenset[str] = frozenset()  # invoice links whose site asks the owner to sign in
    payment_failed: str | None = None  # the day a payment for the plan failed (backoffice.billing), until it goes through
    renewals: frozenset[str] = frozenset()  # "<connection>:<end date>": access ending within a week, noted (R4)


def facts(svc: Any) -> Facts:
    repo = svc.repo
    return Facts(
        approvals=frozenset(n.id for n in repo.open_needs() if n.kind == "approval"),
        stale=frozenset(c.id for c in repo.connectors.values() if not c.healthy),
        closed=frozenset(repo.closed_months),
        sign_ins=frozenset(url for url, link in getattr(repo, "links_seen", {}).items() if link.status == "sign_in"),
        payment_failed=_payment_failed(svc),
        renewals=frozenset(f"{cid}:{info['warned']}" for cid, info in (getattr(svc, "sign_in", None) or {}).items()
                           if isinstance(info, dict) and info.get("warned")),
    )


def _payment_failed(svc: Any) -> str | None:
    billing = getattr(svc, "billing", None)
    if billing is None or billing.status != "past_due" or billing.payment_failed_on is None:
        return None
    return billing.payment_failed_on.isoformat()


def messages_for(svc: Any, before: Facts, after: Facts) -> list[PushMessage]:
    """What changed that the owner must hear about, in plain words."""
    repo = svc.repo
    out: list[PushMessage] = []
    for needs_id in sorted(after.approvals - before.approvals):
        needs = repo.needs.get(needs_id)
        doc = repo.documents.get(needs.subject_id) if needs else None
        if doc is None:
            continue
        who = display_name(doc.document.supplier_name)
        if svc.orchestrator._iban_changed(doc):
            text = f"{who} changed the bank details on its invoice. Payment blocked."
        elif doc.document.gross_amount is not None:
            amount = format_money(doc.document.gross_amount, doc.document.currency)
            text = f"The {who} payment of {amount} needs your approval."
        else:
            text = f"The {who} payment needs your approval."
        out.append(PushMessage("Payment on hold", text, f"/needs-you#{needs_id}"))
    for connector_id in sorted(after.stale - before.stale):
        connector = repo.connectors.get(connector_id)
        if connector is not None:
            out.append(PushMessage("Connection needs you", f"{connector.name} needs reconnecting.",
                                   "/settings#connections"))
    notices = {n["id"]: n for n in svc.access_notices()} if hasattr(svc, "access_notices") else {}
    for key in sorted(after.renewals - before.renewals):
        notice = notices.get(key.rsplit(":", 1)[0])
        if notice is not None:  # once per end date: the push that a week-before reminder promises
            out.append(PushMessage("Access ends soon", notice["note"], f"/needs-you#renew_{notice['id']}"))
    for url in sorted(after.sign_ins - before.sign_ins):
        link = repo.links_seen.get(url)
        if link is not None and link.message:
            out.append(PushMessage("Sign-in needed", link.message, "/activity"))
    by_month: dict[str, list[str]] = {}
    for company_id, month in sorted(after.closed - before.closed):
        by_month.setdefault(month, []).append(repo.company_name(company_id) or company_id)
    for month, names in sorted(by_month.items()):
        try:
            label = Month.parse(month).name
        except (ValueError, TypeError):
            label = month
        who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
        out.append(PushMessage("Month closed", f"{label} is closed for {who}.", "/"))
    if after.payment_failed and after.payment_failed != before.payment_failed:
        notice = svc.billing.notice(svc.repo.clock.today())
        if notice:
            out.append(PushMessage("Payment did not go through", notice, "/settings#billing"))
    return out


class ExpoPushClient:
    """``POST https://exp.host/--/api/v2/push/send`` with httpx; ``transport`` lets tests fake it."""

    def __init__(self, *, access_token: str = "", transport: Any = None, timeout: float = 10.0,
                 url: str = EXPO_PUSH_URL) -> None:
        self._token = access_token
        self._transport = transport
        self._timeout = timeout
        self.url = url

    def __repr__(self) -> str:
        return f"ExpoPushClient(url={self.url!r})"

    def send(self, messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Send ``messages`` (Expo's JSON format); returns one ticket per message, in order."""
        import httpx  # lazy: server-only dependency

        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        tickets: list[dict[str, Any]] = []
        with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
            for start in range(0, len(messages), EXPO_BATCH):
                batch = list(messages[start:start + EXPO_BATCH])
                try:
                    response = client.post(self.url, json=batch, headers=headers)
                except httpx.HTTPError as exc:
                    raise PushError(f"push service unreachable: {type(exc).__name__}") from None
                if response.status_code != 200:
                    raise PushError(f"push service answered {response.status_code}")
                try:
                    data = response.json().get("data")
                except ValueError:
                    raise PushError("push service sent something unreadable") from None
                if isinstance(data, dict):  # a single message gets a single ticket
                    data = [data]
                if not isinstance(data, list) or len(data) != len(batch):
                    raise PushError("push service answered with the wrong number of tickets")
                tickets += [t if isinstance(t, dict) else {"status": "error"} for t in data]
        return tickets


class PushNotifier:
    """Decides what to tell the owner after a change and delivers it to their phones."""

    def __init__(self, store: Store, client: ExpoPushClient, *, executor: Executor | None = None,
                 describe: Callable[[Any, Facts, Facts], list[PushMessage]] = messages_for) -> None:
        self.store = store
        self.client = client
        self.executor = executor  # None: deliver immediately (tests)
        self.describe = describe
        self.facts = facts

    def changed(self, tenant_id: str, svc: Any, before: Facts, after: Facts) -> list[PushMessage]:
        if before == after:
            return []
        messages = self.describe(svc, before, after)
        if messages:
            if self.executor is None:
                self.deliver(tenant_id, messages)
            else:
                self.executor.submit(self._deliver_quietly, tenant_id, messages)
        return messages

    def _deliver_quietly(self, tenant_id: str, messages: list[PushMessage]) -> None:
        try:
            self.deliver(tenant_id, messages)
        except Exception:
            log.exception("push_failed", extra={"tenant": tenant_id})

    def deliver(self, tenant_id: str, messages: Sequence[PushMessage]) -> int:
        """Send ``messages`` to every owner phone of the tenant; returns how many were accepted."""
        devices = self.store.owner_devices(tenant_id)
        if not devices or not messages:
            return 0
        payload, targets = [], []
        for device in devices:
            for m in messages:
                payload.append({"to": device.token, "title": m.title, "body": m.body, "sound": "default",
                                "priority": "high", "data": {"url": m.url}})
                targets.append(device.token)
        tickets = self.client.send(payload)
        accepted = 0
        gone: set[str] = set()
        for token, ticket in zip(targets, tickets, strict=True):
            if ticket.get("status") == "ok":
                accepted += 1
            elif (ticket.get("details") or {}).get("error") == "DeviceNotRegistered":
                gone.add(token)
        for token in sorted(gone):
            self.store.forget_device(tenant_id, token)
        return accepted
