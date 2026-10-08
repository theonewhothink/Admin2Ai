"""OAuth refresh: failures become reconnect-needed, never raw errors (§47-48, §52)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from backoffice.connectors.base import ProviderError, ReconnectRequired, TransientError
from backoffice.connectors.oauth import (
    GOOGLE_TOKEN_URL,
    OAuthClientConfig,
    OAuthRefresher,
    RefreshingTokenProvider,
    TokenProvider,
    microsoft_token_url,
)

T0 = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def refresher(handler, *, scopes=(), clock=None) -> tuple[OAuthRefresher, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request):
        seen.append(request)
        return handler(request)

    config = OAuthClientConfig("client-id", "client-secret", GOOGLE_TOKEN_URL, scopes)
    client = httpx.Client(transport=httpx.MockTransport(record))
    return OAuthRefresher(config, client=client, provider="google", clock=clock or Clock()), seen


def ok(access="at-1", refresh=None, expires=3600):
    body = {"access_token": access, "expires_in": expires, "token_type": "Bearer"}
    if refresh:
        body["refresh_token"] = refresh
    return lambda request: httpx.Response(200, json=body)


def test_refresh_posts_the_standard_form():
    r, seen = refresher(ok(), scopes=("offline_access", "Mail.Read"))
    token = r.refresh("rt-1")
    form = parse_qs(seen[0].content.decode())
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["rt-1"]
    assert form["client_id"] == ["client-id"] and form["scope"] == ["offline_access Mail.Read"]
    assert token.access_token == "at-1" and token.expires_at == T0 + timedelta(hours=1)
    assert token.refresh_token == "rt-1"  # Google keeps the same refresh token
    assert "at-1" not in repr(token) and "client-secret" not in repr(r.config)


@pytest.mark.parametrize("error", ["invalid_grant", "interaction_required", "consent_required", "login_required"])
def test_refused_refresh_means_reconnect(error):
    r, _ = refresher(lambda req: httpx.Response(400, json={"error": error, "error_description": "AADSTS70008 ..."}))
    with pytest.raises(ReconnectRequired) as info:
        r.refresh("rt-1")
    assert info.value.code == f"google_{error}" and info.value.needs_reconnect
    assert "AADSTS" not in str(info.value)


def test_our_own_misconfiguration_is_not_the_owners_problem():
    r, _ = refresher(lambda req: httpx.Response(401, json={"error": "invalid_client"}))
    with pytest.raises(ProviderError) as info:
        r.refresh("rt-1")
    assert not info.value.needs_reconnect


@pytest.mark.parametrize(
    "handler",
    [
        lambda req: httpx.Response(503),
        lambda req: httpx.Response(429, headers={"retry-after": "30"}),
    ],
)
def test_provider_outages_are_transient(handler):
    r, _ = refresher(handler)
    with pytest.raises(TransientError):
        r.refresh("rt-1")


def test_network_errors_and_garbage_are_typed():
    def down(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(TransientError):
        refresher(down)[0].refresh("rt-1")
    with pytest.raises(ProviderError):
        refresher(lambda req: httpx.Response(200, content=b"<html>"))[0].refresh("rt-1")
    with pytest.raises(ProviderError):
        refresher(lambda req: httpx.Response(200, json={"token_type": "Bearer"}))[0].refresh("rt-1")
    with pytest.raises(ProviderError):
        refresher(lambda req: httpx.Response(400, json={"error": "weird"}))[0].refresh("rt-1")
    with pytest.raises(ReconnectRequired):
        refresher(ok())[0].refresh("")


def test_provider_caches_and_refreshes_before_expiry():
    clock = Clock()
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"access_token": f"at-{len(calls)}", "expires_in": 600})

    r, _ = refresher(handler, clock=clock)
    provider = RefreshingTokenProvider(r, "rt-1", clock=clock, skew=timedelta(seconds=60))
    assert isinstance(provider, TokenProvider)
    assert provider.access_token() == "at-1" and provider.access_token() == "at-1"
    clock.now = T0 + timedelta(seconds=541)  # inside the skew window
    assert provider.access_token() == "at-2"
    provider.invalidate()
    assert provider.access_token() == "at-3" and len(calls) == 3


def test_rotated_refresh_tokens_are_reported_for_the_vault():
    rotated = []
    r, seen = refresher(ok(refresh="rt-2"))
    provider = RefreshingTokenProvider(r, "rt-1", on_rotate=rotated.append)
    provider.access_token()
    provider.invalidate()
    provider.access_token()
    assert [t.refresh_token for t in rotated] == ["rt-2"]
    assert parse_qs(seen[1].content.decode())["refresh_token"] == ["rt-2"]


def test_microsoft_token_url():
    assert microsoft_token_url("contoso.onmicrosoft.com") == (
        "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/token")
