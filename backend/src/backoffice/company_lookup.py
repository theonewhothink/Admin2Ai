"""Company identity from its tax number (§4 step 2, QA A2): the EU VAT register (VIES).

When the owner enters the company's VAT number at onboarding, its check digits are checked first by the
company's country pack: a number that fails them is refused, and nothing is sent anywhere. A number that
passes is looked up in VIES, the European Commission's VAT Information Exchange System, which answers
whether the number is registered for VAT and, where the member state shares them, the registered name and
address. Portugal shares them; Spain (like Germany) only confirms the number and answers "---" for both
("Due to data protection, the national authorities will not supply the name and address", Your Europe,
https://europa.eu/youreurope/business/finance-and-tax/vat/check-vat-number-vies/index_en.htm, read 2026-10-08).

What comes back is only ever a *suggestion*: it is shown to the owner, who confirms it with one tap; what the
owner typed is never overwritten on its own. An invalid answer is a plain message, never a refusal (a valid
Portuguese NIF that is not registered for trade with other EU countries is "invalid" in VIES). When the
register is down, slow or busy, the owner types the details and onboarding goes on.

Endpoint (European Commission VIES REST API; response shapes as the service returns them, read 2026-10-08:
https://stackoverflow.com/questions/77422403/using-eu-vies-rest-service-to-check-vat-number)::

    GET https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{CC}/vat/{number}
    -> {"isValid": true, "requestDate": "2026-10-08T09:30:02.124Z", "userError": "VALID",
        "name": "HAZEL TREE INTERIORES LDA", "address": "RUA DA ROSA 57\\n1200-384 LISBOA",
        "requestIdentifier": "", "originalVatNumber": "516123459", "vatNumber": "516123459",
        "viesApproximate": {...}}

The equivalent ``POST .../rest-api/check-vat-number`` with ``{"countryCode": "PT", "vatNumber": "516123459"}``
answers ``{"valid": true, "name": ..., "address": ..., ...}``; both shapes are read. A failure is a
``userError`` other than VALID / INVALID, or ``{"actionSucceed": false, "errorWrappers": [{"error": ...}]}``:
INVALID_INPUT, SERVICE_UNAVAILABLE, MS_UNAVAILABLE, TIMEOUT, VAT_BLOCKED, IP_BLOCKED,
GLOBAL_MAX_CONCURRENT_REQ(_TIME), MS_MAX_CONCURRENT_REQ(_TIME) (the last four, and HTTP 429: too many requests).

Timeouts: 3 seconds to connect, 6 to read, one attempt and no retry while the owner waits (:data:`TIMEOUT`).

The production server looks the number up *before* it records the onboarding event and keeps the answer in the
event (``server/runtime.py``): a replay reads the recorded answer and never calls the register. The browser demo
has no client (``BackOfficeService.company_lookup`` is None): nothing is looked up there.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

__all__ = ["STATUSES", "TIMEOUT", "VIES_API", "CompanyLookup", "ViesClient", "company_lookup_from_env",
           "lookup_company", "parse_vies_response"]

VIES_API = "https://ec.europa.eu/taxation_customs/vies/rest-api"
TIMEOUT = {"connect": 3.0, "read": 6.0, "write": 3.0, "pool": 3.0}  # seconds; one attempt, no retry
SOURCE = "VIES"

# found: valid, with the registered name and address; valid: registered, no details shared; invalid: not
# registered; rate_limited: the register is busy; unavailable: it could not be reached or did not answer.
STATUSES = ("found", "valid", "invalid", "rate_limited", "unavailable")
_BUSY = frozenset({"GLOBAL_MAX_CONCURRENT_REQ", "GLOBAL_MAX_CONCURRENT_REQ_TIME", "MS_MAX_CONCURRENT_REQ",
                   "MS_MAX_CONCURRENT_REQ_TIME"})
_NOT_GIVEN = re.compile(r"^\s*-*\s*$")


@dataclass(frozen=True)
class CompanyLookup:
    """What the EU VAT register said about one VAT number.

    ``detail`` is the register's own code ("MS_MAX_CONCURRENT_REQ"), for the audit trail only, never owner copy.
    ``checked_at`` is the register's request date; ``consultation`` its request identifier, when it gave one."""

    status: str
    country: str
    number: str
    legal_name: str | None = None
    address: str | None = None
    checked_at: str | None = None
    consultation: str | None = None
    detail: str = ""
    source: str = SOURCE

    @property
    def vat_number(self) -> str:
        return f"{self.country}{self.number}"

    @property
    def has_details(self) -> bool:
        return self.status == "found" and bool(self.legal_name or self.address)

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}

    @classmethod
    def from_json(cls, data: Any) -> CompanyLookup | None:
        """A recorded answer (an event's ``lookup``) back; None when it is not one."""
        if not isinstance(data, Mapping) or data.get("status") not in STATUSES:
            return None
        country, number = str(data.get("country") or ""), str(data.get("number") or "")
        if not re.fullmatch(r"[A-Z]{2}", country) or not number:
            return None

        def text(key: str, limit: int) -> str | None:
            value = data.get(key)
            return " ".join(str(value).split())[:limit] or None if value not in (None, "") else None

        return cls(status=str(data["status"]), country=country, number=number[:20],
                   legal_name=text("legal_name", 160), address=text("address", 200),
                   checked_at=text("checked_at", 40), consultation=text("consultation", 40),
                   detail=text("detail", 60) or "", source=text("source", 20) or SOURCE)


def _clean(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or _NOT_GIVEN.match(value):
        return None
    text = ", ".join(part.strip() for part in value.replace("\r", "\n").split("\n") if part.strip())
    return " ".join(text.split())[:limit] or None


def parse_vies_response(country: str, number: str, data: Any, *, http_status: int = 200) -> CompanyLookup:
    """One VIES answer (either endpoint's shape, module docstring) as a :class:`CompanyLookup`."""
    base = {"country": country, "number": number}
    if http_status == 429:
        return CompanyLookup("rate_limited", **base, detail="HTTP_429")
    if not isinstance(data, Mapping):
        return CompanyLookup("unavailable", **base, detail=f"HTTP_{http_status}")
    if data.get("actionSucceed") is False or data.get("errorWrappers"):
        errors = [str(w.get("error") or "") for w in data.get("errorWrappers") or () if isinstance(w, Mapping)]
        code = next((e for e in errors if e), "SERVICE_UNAVAILABLE")
        return CompanyLookup("rate_limited" if code in _BUSY else "unavailable", **base, detail=code)
    code = str(data.get("userError") or "")
    if code and code not in ("VALID", "INVALID"):
        return CompanyLookup("rate_limited" if code in _BUSY else "unavailable", **base, detail=code)
    valid = data.get("isValid", data.get("valid"))
    if not isinstance(valid, bool) or http_status >= 400:
        return CompanyLookup("unavailable", **base, detail=code or f"HTTP_{http_status}")
    checked = _clean(data.get("requestDate"), 40)
    consultation = _clean(data.get("requestIdentifier"), 40)
    if not valid:
        return CompanyLookup("invalid", **base, checked_at=checked, consultation=consultation, detail=code or "INVALID")
    name = _clean(data.get("name") or data.get("traderName"), 160)
    address = _clean(data.get("address"), 200)
    return CompanyLookup("found" if name or address else "valid", **base, legal_name=name, address=address,
                         checked_at=checked, consultation=consultation, detail=code or "VALID")


class ViesClient:
    """The EU VAT register over HTTPS (httpx). ``transport`` (tests: ``httpx.MockTransport``) or ``client`` is
    injected; nothing else reaches the network. One attempt within :data:`TIMEOUT`, never an exception: every
    failure is a :class:`CompanyLookup` the owner reads as a plain message."""

    def __init__(self, *, client: Any = None, transport: Any = None, base_url: str = VIES_API,
                 timeout: Mapping[str, float] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._transport = transport
        self._timeout = dict(timeout or TIMEOUT)

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(transport=self._transport, timeout=httpx.Timeout(**self._timeout),
                                        headers={"Accept": "application/json"}, follow_redirects=False)
        return self._client

    def lookup(self, country: str, number: str) -> CompanyLookup:
        import httpx

        url = f"{self.base_url}/ms/{country}/vat/{number}"
        try:
            response = self._http().get(url)
        except httpx.TimeoutException:
            return CompanyLookup("unavailable", country, number, detail="TIMEOUT")
        except httpx.HTTPError:
            return CompanyLookup("unavailable", country, number, detail="NETWORK")
        try:
            data = response.json()
        except ValueError:
            data = None
        return parse_vies_response(country, number, data, http_status=response.status_code)

    def __call__(self, country: str, number: str) -> CompanyLookup:
        return self.lookup(country, number)


def lookup_company(tax_id: Any, country: Any, client: Callable[[str, str], CompanyLookup] | None
                   ) -> CompanyLookup | None:
    """Check digits first (the company's country pack), then the register. None when there is nothing to look up:
    no client, or a number its pack refuses (the onboarding refuses it with the pack's own message)."""
    from backoffice.countries import CountryPackError, company_pack

    if client is None or tax_id in (None, ""):
        return None
    try:
        pack = company_pack(str(country or "PT"))
    except CountryPackError:
        return None
    check = pack.validate_tax_id(str(tax_id))
    if not check.valid or not check.normalized:
        return None
    number = re.sub(rf"^{pack.country_code}", "", check.normalized.upper())
    try:
        found = client(pack.country_code, number)
    except Exception:  # a client that fails is an unreachable register, never a blocked onboarding
        return CompanyLookup("unavailable", pack.country_code, number, detail="CLIENT_ERROR")
    return found if isinstance(found, CompanyLookup) else CompanyLookup("unavailable", pack.country_code, number)


def company_lookup_from_env() -> ViesClient | None:
    """The production register client: on unless ``BACKOFFICE_COMPANY_LOOKUP=off``; ``BACKOFFICE_VIES_URL``
    points it elsewhere (a proxy)."""
    if os.environ.get("BACKOFFICE_COMPANY_LOOKUP", "on").strip().lower() in ("off", "0", "false", "no"):
        return None
    return ViesClient(base_url=os.environ.get("BACKOFFICE_VIES_URL") or VIES_API)
