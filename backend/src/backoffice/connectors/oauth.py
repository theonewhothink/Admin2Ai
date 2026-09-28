"""OAuth 2.0 refresh for Google and Microsoft (§47-48, §52 secure OAuth).

A refused refresh (revoked grant, expired consent, new MFA or consent demand)
becomes :class:`ReconnectRequired`: the owner sees "Gmail needs reconnecting."
and one Reconnect button, never the provider's error text. Our own
misconfiguration (bad client secret) is a :class:`ProviderError` for
engineers; reconnecting would not fix it.

Refresh tokens live in the secrets vault; :class:`RefreshingTokenProvider`
reports rotated refresh tokens (Microsoft rotates them) through ``on_rotate``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

import httpx

from backoffice.domain.models import utcnow

from .base import ConnectorError, ProviderError, ReconnectRequired, TransientError
from .http import json_body, retry_after_seconds

__all__ = [
    "GOOGLE_TOKEN_URL",
    "OAuthClientConfig",
    "OAuthRefresher",
    "OAuthToken",
    "RefreshingTokenProvider",
    "TokenProvider",
    "microsoft_token_url",
]

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def microsoft_token_url(tenant: str = "common") -> str:
    """Microsoft identity platform v2 token endpoint for ``tenant``."""
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


# OAuth 2.0 / OpenID error codes meaning "the user must authorise again"
# (RFC 6749 §5.2 invalid_grant; OIDC Core §3.1.2.6 interaction/login/consent).
_RECONNECT_ERRORS = frozenset({"invalid_grant", "interaction_required", "login_required", "consent_required"})
_CONFIG_ERRORS = frozenset({"invalid_client", "unauthorized_client", "invalid_scope", "unsupported_grant_type"})


@runtime_checkable
class TokenProvider(Protocol):
    def access_token(self) -> str:
        """A currently valid access token (refreshing if needed)."""
        ...

    def invalidate(self) -> None:
        """Forget the cached access token (called after a 401)."""
        ...


@dataclass(frozen=True)
class OAuthClientConfig:
    client_id: str
    client_secret: str = field(repr=False)
    token_url: str
    scopes: tuple[str, ...] = ()  # Microsoft wants scopes on refresh; Google does not


@dataclass(frozen=True)
class OAuthToken:
    access_token: str = field(repr=False)
    expires_at: datetime
    refresh_token: str | None = field(default=None, repr=False)
    scope: str | None = None


class OAuthRefresher:
    """Exchanges a refresh token for an access token over HTTPS (RFC 6749 §6)."""

    def __init__(
        self,
        config: OAuthClientConfig,
        *,
        client: httpx.Client | None = None,
        provider: str = "oauth",
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config
        self._client = client or httpx.Client(timeout=httpx.Timeout(20.0))
        self._provider = provider
        self._clock = clock

    def refresh(self, refresh_token: str) -> OAuthToken:
        if not refresh_token:
            raise ReconnectRequired(f"{self._provider}_no_refresh_token")
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
        }
        if self.config.scopes:
            form["scope"] = " ".join(self.config.scopes)
        try:
            response = self._client.post(self.config.token_url, data=form, headers={"Accept": "application/json"})
        except httpx.TimeoutException:
            raise TransientError(f"{self._provider}_token_timeout") from None
        except httpx.HTTPError:
            raise TransientError(f"{self._provider}_token_network") from None
        if response.status_code != 200:
            raise self._refusal(response)
        return self._token(response, refresh_token)

    def _refusal(self, response: httpx.Response) -> ConnectorError:
        status = response.status_code
        if status == 429 or status >= 500:
            return TransientError(f"{self._provider}_token_http_{status}", retry_after=retry_after_seconds(response))
        try:
            payload = json_body(response, self._provider)
        except ProviderError:
            payload = None
        error = str(payload.get("error", "")) if isinstance(payload, dict) else ""
        if error in _RECONNECT_ERRORS:
            return ReconnectRequired(f"{self._provider}_{error}")
        if error in _CONFIG_ERRORS:
            return ProviderError(f"{self._provider}_{error}")
        return ProviderError(f"{self._provider}_token_http_{status}")

    def _token(self, response: httpx.Response, previous_refresh: str) -> OAuthToken:
        payload = json_body(response, self._provider)
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise ProviderError(f"{self._provider}_token_malformed")
        try:
            lifetime = int(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            raise ProviderError(f"{self._provider}_token_malformed") from None
        return OAuthToken(
            access_token=str(payload["access_token"]),
            expires_at=self._clock() + timedelta(seconds=max(lifetime, 0)),
            refresh_token=str(payload.get("refresh_token") or previous_refresh),
            scope=payload.get("scope"),
        )


class RefreshingTokenProvider:
    """Caches an access token and refreshes it shortly before expiry. Thread-safe."""

    def __init__(
        self,
        refresher: OAuthRefresher,
        refresh_token: str,
        *,
        on_rotate: Callable[[OAuthToken], None] | None = None,
        skew: timedelta = timedelta(seconds=60),
        initial: OAuthToken | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._refresher = refresher
        self._refresh_token = refresh_token
        self._on_rotate = on_rotate
        self._skew = skew
        self._token = initial
        self._clock = clock
        self._lock = threading.Lock()

    def access_token(self) -> str:
        with self._lock:
            if self._token is None or self._token.expires_at - self._skew <= self._clock():
                token = self._refresher.refresh(self._refresh_token)
                if token.refresh_token and token.refresh_token != self._refresh_token:
                    self._refresh_token = token.refresh_token
                    if self._on_rotate is not None:
                        self._on_rotate(token)
                self._token = token
            return self._token.access_token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
