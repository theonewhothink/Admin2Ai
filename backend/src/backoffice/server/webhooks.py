"""Push notifications from mail providers, received by the API (§47: "If webhook misses an event: backfill").

* **Gmail** pushes through Google Cloud Pub/Sub (``POST /api/webhooks/gmail``). Pub/Sub signs each push with a
  Google OIDC token (``Authorization: Bearer``); :class:`GooglePushVerifier` checks its RS256 signature against
  Google's published keys, its issuer, its audience (this endpoint) and, when configured, the push service
  account. The message names the mailbox and its new ``historyId``.
* **Microsoft Graph** posts change notifications (``POST /api/webhooks/microsoft``) and lifecycle notifications
  (``POST /api/webhooks/microsoft/lifecycle``). A new subscription is validated first: Graph sends
  ``validationToken`` and expects it back as plain text. Every notification must echo the subscription's
  ``clientState`` secret (compared in constant time with the SHA-256 kept for it); lifecycle notifications ask
  for a renewal (``reauthorizationRequired``), say the subscription is gone (``subscriptionRemoved``) or that
  notifications were lost (``missed``).
* **GoCardless** (bank data): the Bank Account Data API this product uses offers no push notifications, so banks
  stay on polling (at most every 6 hours, the provider's limit).

A notification is a hint, never data: nothing from it is recorded in a business's log. It finds the business and
connection through ``webhook_routes`` (written by the sync worker when it subscribes), and queues one job (server/
jobs.py) for the sync worker: read that mailbox now from its saved cursor. Receipt, queueing and the route's
"last notified" time happen in one transaction, keyed on the provider's notification id (or a hash of the
notification) and kept for a week, so a duplicate or replayed notification does nothing. A notification for an
unknown mailbox, or with a wrong secret, is acknowledged and ignored (a provider would otherwise resend it
forever); refusals are logged without their content.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .jobs import SUBSCRIPTION_RENEW, SYNC_CONNECTION
from .store import WebhookRoute

__all__ = ["GOOGLE_CERTS_URL", "GoogleCerts", "GooglePushVerifier", "RECEIPT_KEPT", "WebhookOutcome",
           "WebhookRefused", "client_state_hash", "receive_gmail", "receive_graph"]

log = logging.getLogger("backoffice.server.webhooks")

GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
RECEIPT_KEPT = timedelta(days=7)  # a notification id is remembered this long (Pub/Sub retries for up to 7 days)
MAX_VALIDATION_TOKEN = 1024


class WebhookRefused(Exception):
    """A push that is not provably from the provider. ``code`` is internal (logs only)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def client_state_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- Google's signature


def _b64url(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise WebhookRefused("jwt_bad_encoding") from None


class GoogleCerts:
    """Google's OIDC signing keys (JWK), fetched over HTTPS and kept until the response says they expire."""

    def __init__(self, *, client: Any = None, url: str = GOOGLE_CERTS_URL, ttl: timedelta = timedelta(hours=1)) -> None:
        self._client = client
        self.url = url
        self.ttl = ttl
        self._keys: dict[str, Mapping[str, Any]] = {}
        self._until = 0.0
        self._lock = threading.Lock()

    def __call__(self, *, refresh: bool = False) -> Mapping[str, Mapping[str, Any]]:
        with self._lock:
            if refresh or not self._keys or time.monotonic() >= self._until:
                import httpx  # lazy: server-only dependency

                client = self._client or httpx.Client(timeout=httpx.Timeout(10.0))
                try:
                    response = client.get(self.url)
                    response.raise_for_status()
                    keys = response.json().get("keys") or []
                except (httpx.HTTPError, ValueError, AttributeError):
                    raise WebhookRefused("google_keys_unavailable") from None
                self._keys = {str(k["kid"]): k for k in keys if isinstance(k, Mapping) and k.get("kid")}
                self._until = time.monotonic() + self.ttl.total_seconds()
            return dict(self._keys)


class GooglePushVerifier:
    """Checks the OIDC token Pub/Sub sends with every push (RS256 signature, issuer, audience, expiry, sender).

    ``keys`` returns Google's JWKs by key id (:class:`GoogleCerts`); tests pass their own.
    """

    def __init__(self, audience: str, *, service_account: str | None = None,
                 keys: Callable[..., Mapping[str, Mapping[str, Any]]] | None = None,
                 clock: Callable[[], datetime] | None = None, leeway: timedelta = timedelta(minutes=5)) -> None:
        if not audience:
            raise ValueError("the push audience (this endpoint's URL) is required")
        self.audience = audience
        self.service_account = (service_account or "").strip().lower() or None
        self._keys = keys or GoogleCerts()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.leeway = leeway

    def verify(self, authorization: str | None) -> dict[str, Any]:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        if not authorization or not authorization.lower().startswith("bearer "):
            raise WebhookRefused("no_token")
        token = authorization[7:].strip()
        parts = token.split(".")
        if len(parts) != 3 or len(token) > 8192:
            raise WebhookRefused("jwt_malformed")
        try:
            header = json.loads(_b64url(parts[0]))
            claims = json.loads(_b64url(parts[1]))
        except ValueError:
            raise WebhookRefused("jwt_malformed") from None
        if not isinstance(header, dict) or not isinstance(claims, dict) or header.get("alg") != "RS256":
            raise WebhookRefused("jwt_alg")
        kid = str(header.get("kid") or "")
        jwk = self._keys().get(kid)
        if jwk is None:  # Google rotates its keys: look once more before refusing
            try:
                jwk = self._keys(refresh=True).get(kid)
            except TypeError:
                jwk = None
        if jwk is None:
            raise WebhookRefused("jwt_unknown_key")
        try:
            public = rsa.RSAPublicNumbers(int.from_bytes(_b64url(str(jwk["e"])), "big"),
                                          int.from_bytes(_b64url(str(jwk["n"])), "big")).public_key()
            public.verify(_b64url(parts[2]), f"{parts[0]}.{parts[1]}".encode("ascii"), padding.PKCS1v15(),
                          hashes.SHA256())
        except (InvalidSignature, KeyError, ValueError, TypeError):
            raise WebhookRefused("jwt_signature") from None
        now = self._clock().timestamp()
        leeway = self.leeway.total_seconds()
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise WebhookRefused("jwt_issuer")
        audience = claims.get("aud")
        if audience != self.audience and not (isinstance(audience, list) and self.audience in audience):
            raise WebhookRefused("jwt_audience")
        try:
            if float(claims["exp"]) + leeway < now or float(claims.get("iat", now)) - leeway > now:
                raise WebhookRefused("jwt_expired")
        except (KeyError, TypeError, ValueError):
            raise WebhookRefused("jwt_expired") from None
        if self.service_account is not None and (str(claims.get("email") or "").lower() != self.service_account
                                                 or claims.get("email_verified") is not True):
            raise WebhookRefused("jwt_sender")
        return claims


# --------------------------------------------------------------------------- receivers


@dataclass(frozen=True)
class WebhookOutcome:
    status: int
    body: Any  # a JSON object, or the validation text for Graph's handshake
    queued: int = 0
    duplicates: int = 0
    ignored: int = 0

    @property
    def text(self) -> bool:
        return isinstance(self.body, str)


def receive_gmail(store: Any, body: bytes, *, now: datetime) -> WebhookOutcome:
    """A verified Pub/Sub push: queue a sync of every connection reading that mailbox (once per message id)."""
    from backoffice.connectors.base import ProviderError
    from backoffice.connectors.gmail import GmailConnector

    try:
        envelope = json.loads(body or b"{}")
        if not isinstance(envelope, dict):
            raise ValueError
        push = GmailConnector.parse_push(envelope)
    except (ValueError, ProviderError):
        log.warning("gmail_push_unreadable")
        return WebhookOutcome(204, None, ignored=1)  # acknowledged: Pub/Sub would resend it forever
    message = envelope.get("message") if isinstance(envelope.get("message"), dict) else {}
    message_id = str(message.get("messageId") or message.get("message_id") or "")[:200]
    notification = message_id or hashlib.sha256(str(message.get("data") or "").encode()).hexdigest()
    routes = store.webhook_routes("gmail", push.email_address.strip().lower())
    queued = duplicates = 0
    for route in routes:
        payload = {"connectionId": route.connection_id, "reason": "push", "notifiedAt": now.isoformat(),
                   "historyId": push.history_id[:40]}
        if store.accept_notification(route, notification, now, now + RECEIPT_KEPT, kind=SYNC_CONNECTION,
                                     key=route.connection_id, payload=payload):
            queued += 1
        else:
            duplicates += 1
    if not routes:
        log.info("gmail_push_unknown_mailbox")
    return WebhookOutcome(204, None, queued=queued, duplicates=duplicates, ignored=0 if routes else 1)


def _graph_notes(payload: Any) -> list[Mapping[str, Any]]:
    values = payload.get("value") if isinstance(payload, Mapping) else None
    return [n for n in values if isinstance(n, Mapping)] if isinstance(values, list) else []


def receive_graph(store: Any, query: Mapping[str, str], body: bytes, *, now: datetime,
                  lifecycle: bool = False) -> WebhookOutcome:
    """Microsoft Graph: the validation handshake, change notifications and lifecycle notifications."""
    token = query.get("validationToken")
    if token is not None:  # a new subscription: Graph wants its token back, as plain text, within 10 seconds
        return WebhookOutcome(200, token[:MAX_VALIDATION_TOKEN])
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return WebhookOutcome(400, {"error": "bad_request", "message": "I couldn't read that request."})
    queued = duplicates = ignored = 0
    for note in _graph_notes(payload):
        subscription = str(note.get("subscriptionId") or "")[:320]
        routes = store.webhook_routes("microsoft", subscription) if subscription else []
        route: WebhookRoute | None = routes[0] if routes else None
        given = client_state_hash(str(note.get("clientState") or ""))
        if route is None or not route.secret_hash or not hmac.compare_digest(given, route.secret_hash):
            ignored += 1  # forged, or for a subscription we no longer have
            continue
        if lifecycle:
            event = str(note.get("lifecycleEvent") or "")
            if event in ("reauthorizationRequired", "subscriptionRemoved"):
                kind, reason = SUBSCRIPTION_RENEW, event
            elif event == "missed":
                kind, reason = SYNC_CONNECTION, "missed"  # notifications were lost: read from the cursor now
            else:
                ignored += 1
                continue
        else:
            kind, reason = SYNC_CONNECTION, "push"
        seen = {k: v for k, v in note.items() if k != "clientState"}
        notification = hashlib.sha256(json.dumps(seen, sort_keys=True, default=str).encode()).hexdigest()
        job = {"connectionId": route.connection_id, "reason": reason, "notifiedAt": now.isoformat()}
        if store.accept_notification(route, notification, now, now + RECEIPT_KEPT, kind=kind,
                                     key=route.connection_id, payload=job):
            queued += 1
        else:
            duplicates += 1
    if ignored:
        log.warning("graph_notifications_ignored", extra={"reason": str(ignored)})
    return WebhookOutcome(202, {"received": True}, queued=queued, duplicates=duplicates, ignored=ignored)
