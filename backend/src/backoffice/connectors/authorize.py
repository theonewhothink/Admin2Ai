"""First sign-in for Google and Microsoft mailboxes (OAuth 2.0 code flow + PKCE).

Flow (§4 "connect email", §47 "stay connected"):

1. :meth:`OAuthAuthorizer.begin` returns the provider's consent URL and a
   signed, expiring ``state`` holding the tenant, connection id and PKCE
   verifier hash. We ask for offline access so the provider issues a refresh
   token.
2. The provider redirects to our callback with ``code`` and ``state``;
   :meth:`OAuthAuthorizer.complete` checks the state, exchanges the code and
   stores the refresh token in the :class:`~.vault.TokenVault`.

From then on the connector renews access tokens by itself; the owner only
reconnects if they revoke access, change their password, or the provider ends
the grant.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

from .vault import TokenVault

__all__ = ["AuthorizationError", "NonceStore", "OAuthApp", "OAuthAuthorizer", "PROVIDERS"]


class AuthorizationError(Exception):
    """The sign-in could not be completed; the owner is asked to try again."""


@dataclass(frozen=True)
class OAuthApp:
    provider: str
    client_id: str
    client_secret: str = field(repr=False)
    authorize_url: str = ""
    token_url: str = ""
    scopes: tuple[str, ...] = ()
    extra: tuple[tuple[str, str], ...] = ()


PROVIDERS: dict[str, dict[str, Any]] = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ("openid", "email", "https://www.googleapis.com/auth/gmail.readonly"),
        # offline + consent: Google returns a refresh token on every first sign-in.
        "extra": (("access_type", "offline"), ("prompt", "consent"), ("include_granted_scopes", "true")),
    },
    "microsoft": {
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": ("openid", "email", "offline_access", "https://graph.microsoft.com/Mail.Read"),
        "extra": (("response_mode", "query"),),
    },
}


def app_from_env(provider: str) -> OAuthApp | None:
    """Build an app from BACKOFFICE_<PROVIDER>_CLIENT_ID / _CLIENT_SECRET, if set."""
    spec = PROVIDERS.get(provider)
    cid = os.environ.get(f"BACKOFFICE_{provider.upper()}_CLIENT_ID")
    secret = os.environ.get(f"BACKOFFICE_{provider.upper()}_CLIENT_SECRET")
    if not spec or not cid or not secret:
        return None
    return OAuthApp(provider=provider, client_id=cid, client_secret=secret, **spec)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class NonceStore(Protocol):
    """Shared single-use record of sign-ins in progress (several API processes, §47).

    ``save`` notes a nonce until ``expires_at`` (epoch seconds); ``take``
    returns True exactly once for a saved, unexpired nonce.
    """

    def save(self, tenant_id: str, nonce: str, expires_at: float) -> None: ...

    def take(self, tenant_id: str, nonce: str) -> bool: ...


class OAuthAuthorizer:
    def __init__(
        self,
        apps: dict[str, OAuthApp],
        vault: TokenVault,
        *,
        redirect_uri: str,
        state_key: bytes,
        http: Any = None,
        state_ttl_seconds: int = 600,
        clock: Any = time.time,
        nonces: NonceStore | None = None,
    ) -> None:
        if len(state_key) < 32:
            raise ValueError("state_key must be at least 32 bytes")
        self._apps = apps
        self._vault = vault
        self._redirect = redirect_uri
        self._key = state_key
        self._http = http
        self._ttl = state_ttl_seconds
        self._clock = clock
        # Without a shared nonce store the PKCE verifier stays in this process
        # (one API process). With one, the verifier is derived from the nonce
        # with the state key, so any process can finish the sign-in and the
        # store only has to remember that the nonce was not used yet.
        self._nonces = nonces
        self._verifiers: dict[str, str] = {}  # nonce -> PKCE verifier (server side, short-lived)

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._apps))

    def _derived_verifier(self, nonce: str) -> str:
        return _b64(hmac.new(self._key, b"pkce:" + nonce.encode(), hashlib.sha256).digest())

    def begin(self, provider: str, tenant_id: str, connection_id: str, login_hint: str | None = None) -> str:
        app = self._apps.get(provider)
        if app is None:
            raise AuthorizationError(f"{provider} sign-in is not configured")
        nonce = _b64(os.urandom(16))
        expires = int(self._clock()) + self._ttl
        if self._nonces is not None:
            verifier = self._derived_verifier(nonce)
            self._nonces.save(tenant_id, nonce, expires)
        else:
            verifier = _b64(os.urandom(32))
            self._verifiers[nonce] = verifier
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        payload = {"p": provider, "t": tenant_id, "c": connection_id, "n": nonce, "e": expires}
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        state = f"{body}.{_b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())}"
        params = {
            "client_id": app.client_id, "redirect_uri": self._redirect, "response_type": "code",
            "scope": " ".join(app.scopes), "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", **dict(app.extra),
        }
        if login_hint:
            params["login_hint"] = login_hint
        return f"{app.authorize_url}?{urlencode(params)}"

    def _read_state(self, state: str) -> dict[str, Any]:
        try:
            body, sig = state.split(".", 1)
            expected = _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(sig, expected):
                raise ValueError
            payload = json.loads(_unb64(body))
        except (ValueError, json.JSONDecodeError):
            raise AuthorizationError("sign-in link is not valid") from None
        if int(payload.get("e", 0)) < self._clock():
            raise AuthorizationError("sign-in took too long")
        return payload

    def complete(self, code: str, state: str) -> dict[str, str]:
        """Exchange ``code`` and seal the refresh token. Returns tenant, connection and provider."""
        payload = self._read_state(state)
        if self._nonces is not None:
            verifier = self._derived_verifier(payload["n"]) if self._nonces.take(payload["t"], payload["n"]) else None
        else:
            verifier = self._verifiers.pop(payload["n"], None)
        if verifier is None:
            raise AuthorizationError("sign-in was already used")
        app = self._apps[payload["p"]]
        http = self._http
        if http is None:
            import httpx  # lazy: optional in the browser build

            http = httpx.Client(timeout=20.0)
        form = {
            "grant_type": "authorization_code", "code": code, "redirect_uri": self._redirect,
            "client_id": app.client_id, "client_secret": app.client_secret, "code_verifier": verifier,
        }
        response = http.post(app.token_url, data=form, headers={"Accept": "application/json"})
        if response.status_code != 200:
            raise AuthorizationError("the provider refused the sign-in")
        token = response.json()
        refresh = token.get("refresh_token")
        if not refresh:
            raise AuthorizationError("the provider did not allow offline access")
        self._vault.store(payload["t"], payload["c"], payload["p"],
                          {"refresh_token": refresh, "scope": token.get("scope", "")})
        done = {"tenant_id": payload["t"], "connection_id": payload["c"], "provider": payload["p"]}
        email = _id_token_email(token.get("id_token"))
        if email:
            done["email"] = email
        return done


def _id_token_email(id_token: Any) -> str | None:
    """The mailbox address in an ID token received directly from the provider's token endpoint.

    The token came over TLS from the token URL in exchange for our own code, so
    its issuer is the provider (OpenID Connect Core §3.1.3.7 allows TLS
    validation in place of the signature here). Only the address is used.
    """
    if not isinstance(id_token, str) or id_token.count(".") != 2:
        return None
    try:
        claims = json.loads(_unb64(id_token.split(".")[1]))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    for name in ("email", "preferred_username", "upn"):
        value = claims.get(name)
        if isinstance(value, str) and "@" in value and len(value) <= 254 and " " not in value:
            return value.strip().lower()
    return None
