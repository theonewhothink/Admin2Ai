"""Shared HTTP plumbing for engine adapters.

One place maps transport failures to typed :class:`OCRError` subclasses, so
the router can tell "try again later" from "this request will never work".
Errors carry the status code, never the response body (which may echo
document content). There are no retries here: the caller (a Temporal
activity, §45) owns retry policy.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..base import OCRRejected, OCRResponseError, OCRUnavailable

__all__ = ["EndpointConfig", "JSONEndpoint", "b64"]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@dataclass(frozen=True)
class EndpointConfig:
    """Where an engine lives and how to authenticate.

    ``api_key`` is sent as ``Authorization: Bearer <key>`` by default, or raw
    in ``api_key_header`` when a vendor uses another header.
    """

    base_url: str
    timeout_seconds: float = 60.0
    api_key: str | None = field(default=None, repr=False)
    api_key_header: str = "Authorization"
    headers: Mapping[str, str] = field(default_factory=dict)
    verify_tls: bool = True

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) URL")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def url(self, path: str) -> str:
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    def request_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", **self.headers}
        if self.api_key:
            if self.api_key_header.lower() == "authorization":
                headers["Authorization"] = f"Bearer {self.api_key}"
            else:
                headers[self.api_key_header] = self.api_key
        return headers


class JSONEndpoint:
    """POST JSON, get JSON, with typed failures.

    Pass a shared ``httpx.AsyncClient`` to reuse connections (and in tests,
    one built on ``httpx.MockTransport``); otherwise one is created lazily
    and closed by :meth:`aclose`.
    """

    def __init__(self, config: EndpointConfig, *, engine: str, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._engine = engine
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(verify=self._config.verify_tls)
        return self._client

    async def post(self, path: str, payload: Mapping[str, Any]) -> Any:
        engine = self._engine
        try:
            response = await self._http().post(
                self._config.url(path),
                json=payload,
                headers=self._config.request_headers(),
                timeout=self._config.timeout_seconds,
            )
        except httpx.TimeoutException:
            raise OCRUnavailable(engine, "timeout") from None
        except httpx.TransportError as exc:
            raise OCRUnavailable(engine, type(exc).__name__) from None
        status = response.status_code
        if status == 429 or status >= 500:
            raise OCRUnavailable(engine, f"HTTP {status}")
        if status >= 400:
            raise OCRRejected(engine, f"HTTP {status}")
        try:
            return response.json()
        except ValueError:
            raise OCRResponseError(engine, "response is not JSON") from None

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
