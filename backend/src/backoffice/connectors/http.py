"""Authorised HTTP for connectors: bearer tokens, one refresh on 401, typed errors.

Responses become :class:`ConnectorError` subclasses; nothing raw leaks upward.
JSON is decoded with ``parse_float=Decimal`` so amounts never pass through a
binary float. Absolute follow-up links (``nextLink``/``deltaLink``) are only
followed on the provider's own origin, so a token is never sent elsewhere.

Payloads are validated where they are read (:func:`json_object`,
:func:`object_list`, :func:`required_str`): an unexpected shape is a
:class:`ProviderError` recorded on the connector, never a ``KeyError``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from .base import ConnectorError, ProviderError, ReconnectRequired, TransientError

if TYPE_CHECKING:
    from .oauth import TokenProvider

__all__ = [
    "AuthorizedHttp",
    "error_for_status",
    "json_body",
    "json_object",
    "object_list",
    "required_str",
    "retry_after_seconds",
    "same_origin",
]

Classifier = Callable[[httpx.Response], ConnectorError | None]


def json_body(response: httpx.Response, provider: str) -> Any:
    try:
        return json.loads(response.content or b"null", parse_float=Decimal)
    except ValueError:
        raise ProviderError(f"{provider}_bad_json") from None


def json_object(response: httpx.Response, provider: str) -> dict[str, Any]:
    """The response body as a JSON object, or :class:`ProviderError`."""
    payload = json_body(response, provider)
    if not isinstance(payload, dict):
        raise ProviderError(f"{provider}_unexpected_json")
    return payload


def object_list(payload: Mapping[str, Any], key: str, provider: str) -> list[dict[str, Any]]:
    """``payload[key]`` as a list of JSON objects (empty when absent), or :class:`ProviderError`."""
    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ProviderError(f"{provider}_unexpected_{key}")
    return value


def required_str(obj: Mapping[str, Any], key: str, provider: str) -> str:
    """A non-empty scalar field as text, or :class:`ProviderError`."""
    value = obj.get(key) if isinstance(obj, Mapping) else None
    if value is None or isinstance(value, (dict, list, bool)) or str(value) == "":
        raise ProviderError(f"{provider}_missing_{key}")
    return str(value)


def retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)


def error_for_status(response: httpx.Response, provider: str) -> ConnectorError | None:
    status = response.status_code
    if status < 400:
        return None
    code = f"{provider}_http_{status}"
    if status in (408, 425, 429) or status >= 500:
        return TransientError(code, retry_after=retry_after_seconds(response))
    if status in (401, 403):
        return ReconnectRequired(code)
    return ProviderError(code)


def same_origin(url: str, base: str) -> bool:
    a, b = urlsplit(url), urlsplit(base)
    return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port) and a.scheme == "https"


class AuthorizedHttp:
    def __init__(
        self,
        client: httpx.Client,
        tokens: TokenProvider,
        provider: str,
        *,
        classify: Classifier | None = None,
    ) -> None:
        self.client = client
        self.tokens = tokens
        self.provider = provider
        self._classify = classify

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_payload: Any = None,
        headers: Mapping[str, str] | None = None,
        allow: Iterable[int] = (),
    ) -> httpx.Response:
        """Send with a bearer token; statuses in ``allow`` are returned, not raised."""
        allowed = set(allow)
        for attempt in (1, 2):
            token = self.tokens.access_token()
            merged = {**(headers or {}), "Authorization": f"Bearer {token}"}
            try:
                response = self.client.request(method, url, params=params, json=json_payload, headers=merged)
            except httpx.TimeoutException:
                raise TransientError(f"{self.provider}_timeout") from None
            except httpx.HTTPError:
                raise TransientError(f"{self.provider}_network") from None
            if response.status_code in allowed:
                return response
            if response.status_code == 401 and attempt == 1:
                self.tokens.invalidate()  # maybe just expired: refresh once and retry
                continue
            if response.status_code == 401:
                raise ReconnectRequired(f"{self.provider}_unauthorized")
            error = (self._classify(response) if self._classify else None) or error_for_status(
                response, self.provider
            )
            if error is not None:
                raise error
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    def get_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        """GET a JSON object (every endpoint the connectors read returns one)."""
        return json_object(self.request("GET", url, **kwargs), self.provider)
