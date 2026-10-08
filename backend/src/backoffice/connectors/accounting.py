"""Accounting software connectors: TOConline, Moloni and InvoiceXpress (§22 search 5, §28, §50 Portugal pack).

One interface, :class:`AccountingConnector`, keeps the providers' specifics behind it:

* **read** the company's own sales documents (invoices, invoice-receipts, simplified invoices, receipts, credit and
  debit notes) and the purchase documents recorded there (:meth:`~AccountingConnector.list_documents`,
  :meth:`~AccountingConnector.search`), with each document's PDF (:meth:`~AccountingConnector.fetch`). Found
  files go through the normal pipeline, where a document already on file is recognised as the same one (never
  counted twice);
* **export a month** (:meth:`~AccountingConnector.export_month`): the documents booked there, their PDFs where the
  API gives them, and a ledger CSV (one line per document: date, direction, type, number, counterparty, tax
  number, net, VAT, gross, currency), as far as the provider's API allows;
* **sync** (:meth:`~AccountingConnector.sync`): the company's own sales documents issued since the last run, with
  the usual §47 state (last sync, failures, a sign-in that ended), so the connection's health shows like a
  mailbox's and a month never closes green while it stopped syncing.

Providers (request and response shapes from each provider's public API documentation):

* **InvoiceXpress** (``https://{account}.app.invoicexpress.com``): the customer's own account name and API key
  (``api_key`` on every request, kept in the vault). ``GET /invoices.json`` with ``type[]``, ``date[from]`` /
  ``date[to]`` (dd/mm/yyyy), ``page`` and ``per_page``; ``GET /api/pdf/{id}.json`` answers 202 while the PDF is
  being made, then ``{"output": {"pdfUrl"}}``. Sales documents only: InvoiceXpress keeps no purchases.
* **Moloni** (``https://api.moloni.pt/v1``): OAuth 2.0 with the developer's client id (Developer ID) and client
  secret from a Moloni developer registration (``BACKOFFICE_MOLONI_CLIENT_ID`` / ``_SECRET``; redirect URI
  ``<BACKOFFICE_API_URL>/api/oauth/callback``). Tokens come from ``GET /grant/`` (authorization code, then
  refresh: the access token lasts an hour, the refresh token 14 days, so a daily sync keeps it alive).
  Every call is ``POST /{endpoint}/?access_token=...`` with form fields: ``companies/getAll``,
  ``documents/getAll`` (sales), ``supplierInvoices/getAll`` and ``supplierCreditNotes/getAll`` (purchases),
  ``documents/getPDFLink`` (``{"url"}``); at most 50 per page (``qty`` / ``offset``).
* **TOConline** (JSON:API): the company's own API data from TOConline (Empresa > Configurações > Dados API:
  client id, secret, OAuth address, API address). ``GET {oauth}/auth`` answers 302 with the authorization code,
  ``POST {oauth}/token`` (HTTP Basic client id:secret, ``scope=commercial``) gives an access token (4 hours) and a
  refresh token (8 hours). ``GET {api}/api/v1/commercial_sales_documents`` and ``.../commercial_purchases_documents``
  (``page[size]``, ``page[number]``, ``sort=-date``); ``GET {api}/api/url_for_print/{id}`` gives the PDF address.

Downloads from addresses a provider returns are fetched without credentials and only over https to a public host.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import re
import threading
import zipfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from backoffice.domain.models import utcnow

from .choices import TOConlineCredentials, _toc_url
from .base import (
    ConnectorError,
    ConnectorKind,
    ConnectorState,
    CountingSink,
    ProviderError,
    ReconnectRequired,
    SyncOutcome,
    TransientError,
    record_failure,
    record_success,
)
from .http import AuthorizedHttp, error_for_status, json_body, retry_after_seconds
from .oauth import OAuthToken, TokenProvider

__all__ = [
    "MOLONI_API",
    "MOLONI_AUTHORIZE_URL",
    "SALES",
    "PURCHASES",
    "AccountingConnector",
    "AccountingDocument",
    "AccountingExport",
    "AccountingFile",
    "DocumentLookup",
    "InvoiceXpressConnector",
    "MoloniConnector",
    "MoloniRefresher",
    "TOConlineConnector",
    "TOConlineCredentials",
    "TOConlineTokens",
]

SALES = "sales"
PURCHASES = "purchases"
MOLONI_API = "https://api.moloni.pt/v1"
MOLONI_AUTHORIZE_URL = "https://www.moloni.pt/ac/root/oauth/"
MOLONI_REFRESH_DAYS = 14
TOCONLINE_REFRESH_HOURS = 8
_CREDIT = frozenset({"NC"})
_SALES_TYPES = frozenset({"FT", "FR", "FS", "RC", "NC", "ND"})


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount.quantize(Decimal("0.01")) if amount.is_finite() else None


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _number_key(value: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", (value or "").upper())


def _safe_download_url(url: str) -> str:
    """An address a provider returned for a PDF: https to a public host only (never an internal address)."""
    from backoffice.evidence.links import UrlSafety, UrlSafetyConfig

    check = UrlSafety(UrlSafetyConfig(allow_http=False)).check_static(url)
    if not check.safe:
        raise ProviderError("accounting_unsafe_download_address")
    return url


@dataclass(frozen=True)
class AccountingDocument:
    """One document as the accounting software records it."""

    provider: str
    provider_id: str
    direction: str  # "sales" (issued by the company) | "purchases" (a supplier's, recorded there)
    doc_type: str  # SAF-T code: FT, FR, FS, RC, NC, ND (TOConline purchases: FC, DSP)
    number: str | None
    issue_date: date | None
    counterparty: str
    counterparty_tax_id: str | None
    net: Decimal | None
    vat: Decimal | None
    gross: Decimal | None
    currency: str = "EUR"
    final: bool = True  # finalised (never a draft or a cancelled document)

    @property
    def credit(self) -> bool:
        return self.doc_type in _CREDIT

    def provenance(self) -> dict[str, Any]:
        """Where the evidence came from (§55): the software, its document id, direction, type, number, date."""
        return {"source": "accounting", "provider": self.provider, "documentId": self.provider_id,
                "direction": self.direction, "type": self.doc_type, "number": self.number,
                "date": self.issue_date.isoformat() if self.issue_date else None}

    def signed(self, value: Decimal | None) -> Decimal | None:
        return -value if value is not None and self.credit else value

    def filename(self) -> str:
        """``FT_2026_70.pdf``: the number as printed (its series already names the type), else type and id."""
        number = (self.number or "").strip()
        label = number if number and number.upper().startswith(self.doc_type.upper()) else \
            f"{self.doc_type} {number or self.provider_id}"
        name = re.sub(r"[^0-9A-Za-z._-]+", "_", label).strip("_")
        return f"{name or 'document'}.pdf"


@dataclass(frozen=True)
class AccountingFile:
    document: AccountingDocument
    data: bytes = field(repr=False)
    filename: str
    content_type: str = "application/pdf"

    def provenance(self) -> dict[str, Any]:
        return self.document.provenance()


@dataclass(frozen=True)
class DocumentLookup:
    """A missing document: issued from ``since`` to ``until``, of ``amount`` (as printed: the gross total), or with
    ``number``; ``tax_id`` (the other party's) and ``direction`` narrow it."""

    since: date
    until: date
    amount: Decimal | None = None
    currency: str = "EUR"
    number: str | None = None
    tax_id: str | None = None
    direction: str | None = None  # None: sales and purchases
    limit: int = 5

    def matches(self, doc: AccountingDocument) -> bool:
        if not doc.final or (self.direction is not None and doc.direction != self.direction):
            return False
        if doc.issue_date is not None and not self.since <= doc.issue_date <= self.until:
            return False
        if self.tax_id and doc.counterparty_tax_id and _digits(self.tax_id)[-9:] != _digits(doc.counterparty_tax_id)[-9:]:
            return False
        number = bool(self.number) and _number_key(self.number) == _number_key(doc.number)
        amount = (self.amount is not None and doc.gross is not None and abs(doc.gross) == abs(self.amount)
                  and doc.currency.upper() == self.currency.upper())
        return number or amount


LEDGER_COLUMNS = ("date", "direction", "type", "number", "counterparty", "tax_number", "net", "vat", "gross",
                  "currency", "software", "document_id")


@dataclass(frozen=True)
class AccountingExport:
    """One month as the accounting software has it: documents booked, their PDFs, a ledger CSV."""

    provider: str
    year: int
    month: int
    documents: tuple[AccountingDocument, ...]
    files: tuple[AccountingFile, ...]
    ledger_csv: bytes = field(repr=False)
    missing_files: tuple[str, ...] = ()  # documents whose PDF the API did not give (provider ids)

    def zip_bytes(self) -> bytes:
        """``ledger.csv`` and every PDF (``sales/`` and ``purchases/``), ready for the accountant."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{self.year:04d}-{self.month:02d}/ledger.csv", self.ledger_csv)
            used: set[str] = set()
            for f in self.files:
                name = f"{self.year:04d}-{self.month:02d}/{f.document.direction}/{f.filename}"
                n = 2
                while name in used:
                    stem, _, ext = f.filename.rpartition(".")
                    name = f"{self.year:04d}-{self.month:02d}/{f.document.direction}/{stem}-{n}.{ext}"
                    n += 1
                used.add(name)
                archive.writestr(name, f.data)
        return buffer.getvalue()


def ledger_csv(documents: Iterable[AccountingDocument]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(LEDGER_COLUMNS)
    for d in sorted(documents, key=lambda d: (d.issue_date or date.min, d.direction, d.number or "", d.provider_id)):
        def fmt(v: Decimal | None) -> str:
            return "" if v is None else f"{d.signed(v):.2f}"

        writer.writerow([d.issue_date.isoformat() if d.issue_date else "", d.direction, d.doc_type, d.number or "",
                         d.counterparty, d.counterparty_tax_id or "", fmt(d.net), fmt(d.vat), fmt(d.gross),
                         d.currency, d.provider, d.provider_id])
    return out.getvalue().encode("utf-8")


AccountingSink = Callable[[AccountingFile], None]


def _sync_cursor(state: ConnectorState) -> tuple[date | None, set[str]]:
    if not state.cursor:
        return None, set()
    try:
        data = json.loads(state.cursor)
        if isinstance(data, dict) and data.get("v") == 1:
            return date.fromisoformat(str(data["through"])), {str(i) for i in data.get("ids") or ()}
    except (ValueError, KeyError, TypeError):
        pass
    return None, set()


class AccountingConnector(ABC):
    """What every accounting connector does (module docstring); subclasses talk to one provider."""

    kind = ConnectorKind.ACCOUNTING
    provider: ClassVar[str]
    display_name: ClassVar[str]
    directions: ClassVar[tuple[str, ...]] = (SALES, PURCHASES)  # what the provider's API offers
    history_window: timedelta = timedelta(days=90)

    def __init__(self, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._clock = clock

    @abstractmethod
    def list_documents(self, since: date, until: date, direction: str) -> list[AccountingDocument]:
        """Documents of ``direction`` issued from ``since`` to ``until`` (inclusive)."""

    @abstractmethod
    def pdf(self, document: AccountingDocument) -> bytes | None:
        """The document's PDF, or None when the API does not give one."""

    # ----------------------------------------------------------------- shared behaviour

    def search(self, lookup: DocumentLookup) -> list[AccountingDocument]:
        """Documents matching a missing one (by number, or by amount), newest first."""
        found: list[AccountingDocument] = []
        for direction in self.directions:
            if lookup.direction is not None and direction != lookup.direction:
                continue
            found += [d for d in self.list_documents(lookup.since, lookup.until, direction) if lookup.matches(d)]
        found.sort(key=lambda d: (d.issue_date or date.min, d.provider_id), reverse=True)
        return found[: lookup.limit]

    def fetch(self, document: AccountingDocument) -> AccountingFile | None:
        data = self.pdf(document)
        return AccountingFile(document, data, document.filename()) if data else None

    def export_month(self, year: int, month: int, *, files: bool = True) -> AccountingExport:
        """The month's documents booked in the software, their PDFs where the API gives them, a ledger CSV."""
        first = date(year, month, 1)
        last = (date(year + (month == 12), month % 12 + 1, 1)) - timedelta(days=1)
        documents = [d for direction in self.directions for d in self.list_documents(first, last, direction)
                     if d.final]
        fetched: list[AccountingFile] = []
        missing: list[str] = []
        if files:
            for d in documents:
                got = self.fetch(d)
                if got is None:
                    missing.append(d.provider_id)
                else:
                    fetched.append(got)
        return AccountingExport(self.provider, year, month, tuple(documents), tuple(fetched), ledger_csv(documents),
                                tuple(missing))

    def sync(self, state: ConnectorState, sink: AccountingSink, *, now: datetime | None = None) -> SyncOutcome:
        """The company's own sales documents issued since the last run (the first run: the history window), each
        with its PDF: they prove money in and refunds given (§20). Purchases are read when a payment misses its
        document (:meth:`search`), never copied in bulk."""
        now = now or self._clock()
        counted = CountingSink(sink)
        through, seen = _sync_cursor(state)
        first = through is None
        today = now.astimezone(timezone.utc).date()
        since = through if through is not None else (now - self.history_window).date()
        latest, latest_ids = through or since, set(seen) if through is not None else set()
        try:
            if SALES in self.directions:
                documents = sorted(self.list_documents(since, today, SALES),
                                   key=lambda d: (d.issue_date or since, d.provider_id))
                for d in documents:
                    if not d.final:
                        continue
                    if through is not None and d.issue_date is not None and (
                            d.issue_date < through or (d.issue_date == through and d.provider_id in seen)):
                        continue  # read on an earlier run (a software that filters loosely by date)
                    got = self.fetch(d)
                    if got is not None:
                        counted(got)
                    day = d.issue_date or since
                    if day > latest:
                        latest, latest_ids = day, set()
                    if day == latest:
                        latest_ids.add(d.provider_id)
        except ConnectorError as exc:
            return SyncOutcome(record_failure(state, at=now, error=exc), counted.count, error=exc)
        cursor = json.dumps({"v": 1, "through": latest.isoformat(), "ids": sorted(latest_ids)}, sort_keys=True)
        new_state = record_success(state, at=now, cursor=cursor,
                                   coverage_start=now - self.history_window if first else None)
        return SyncOutcome(new_state, counted.count, full_sync=first)

    # ----------------------------------------------------------------- helpers for subclasses

    def _download(self, client: httpx.Client, url: str, provider: str) -> bytes | None:
        try:
            response = client.get(_safe_download_url(url))
        except httpx.TimeoutException:
            raise TransientError(f"{provider}_download_timeout") from None
        except httpx.HTTPError:
            raise TransientError(f"{provider}_download_network") from None
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise (TransientError if response.status_code >= 500 else ProviderError)(
                f"{provider}_download_http_{response.status_code}")
        return response.content or None


# --------------------------------------------------------------------------- InvoiceXpress


_IXP_TYPES = {"Invoice": "FT", "InvoiceReceipt": "FR", "SimplifiedInvoice": "FS", "CreditNote": "NC",
              "DebitNote": "ND", "Receipt": "RC"}
_IXP_NOT_FINAL = frozenset({"draft", "canceled", "cancelled", "deleted"})
_CURRENCY_NAMES = {"euro": "EUR", "eur": "EUR", "dólar americano": "USD", "us dollar": "USD", "dollar": "USD",
                   "libra esterlina": "GBP", "british pound": "GBP", "pound sterling": "GBP"}


def _currency(value: Any) -> str:
    text = " ".join(str(value or "EUR").split())
    if re.fullmatch(r"[A-Za-z]{3}", text):
        return text.upper()
    return _CURRENCY_NAMES.get(text.lower(), text.upper())


def _pt_date(value: Any) -> date | None:
    try:
        return datetime.strptime(str(value), "%d/%m/%Y").date()
    except (TypeError, ValueError):
        return None


class InvoiceXpressConnector(AccountingConnector):
    """InvoiceXpress with the customer's own API key (module docstring). Sales documents only."""

    provider = "invoicexpress"
    display_name = "InvoiceXpress"
    directions = (SALES,)
    PER_PAGE = 50
    MAX_PAGES = 200

    def __init__(self, account: str, api_key: str, *, client: httpx.Client | None = None,
                 base_url: str | None = None, clock: Callable[[], datetime] = utcnow, pdf_attempts: int = 3) -> None:
        super().__init__(clock=clock)
        account = (account or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", account):
            raise ValueError("an InvoiceXpress account name is letters, digits and dashes")
        if not api_key:
            raise ValueError("an InvoiceXpress API key is needed")
        self.base_url = (base_url or f"https://{account}.app.invoicexpress.com").rstrip("/")
        self._key = api_key
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0))
        self._pdf_attempts = max(1, pdf_attempts)

    def __repr__(self) -> str:  # the key never reaches a log
        return f"InvoiceXpressConnector({self.base_url!r})"

    def _get(self, path: str, params: Sequence[tuple[str, Any]] = (), *, allow: Iterable[int] = ()) -> httpx.Response:
        try:
            response = self._client.get(f"{self.base_url}{path}", params=[("api_key", self._key), *params],
                                        headers={"Accept": "application/json"})
        except httpx.TimeoutException:
            raise TransientError("invoicexpress_timeout") from None
        except httpx.HTTPError:
            raise TransientError("invoicexpress_network") from None
        if response.status_code in set(allow):
            return response
        if response.status_code == 401:
            raise ReconnectRequired("invoicexpress_unauthorized")  # the key was regenerated: add it again
        error = error_for_status(response, "invoicexpress")
        if error is not None:
            raise error
        return response

    def list_documents(self, since: date, until: date, direction: str) -> list[AccountingDocument]:
        if direction != SALES:
            return []
        params: list[tuple[str, Any]] = [("type[]", t) for t in _IXP_TYPES if t != "Receipt"]
        params += [("date[from]", f"{since:%d/%m/%Y}"), ("date[to]", f"{until:%d/%m/%Y}"),
                   ("per_page", self.PER_PAGE)]
        out: list[AccountingDocument] = []
        for page in range(1, self.MAX_PAGES + 1):
            payload = json_body(self._get("/invoices.json", [*params, ("page", page)]), "invoicexpress")
            if not isinstance(payload, dict):
                raise ProviderError("invoicexpress_unexpected_json")
            items = payload.get("invoices") or []
            if not isinstance(items, list):
                raise ProviderError("invoicexpress_unexpected_invoices")
            out += [d for item in items if isinstance(item, dict) and (d := self._doc(item)) is not None]
            pages = (payload.get("pagination") or {}).get("total_pages") if isinstance(payload.get("pagination"),
                                                                                       dict) else None
            if not items or not isinstance(pages, int) or page >= pages:
                break
        return out

    def _doc(self, item: dict[str, Any]) -> AccountingDocument | None:
        kind = _IXP_TYPES.get(str(item.get("type") or ""))
        if kind is None or item.get("id") is None:
            return None
        client = item.get("client") if isinstance(item.get("client"), dict) else {}
        return AccountingDocument(
            provider=self.provider, provider_id=str(item["id"]), direction=SALES, doc_type=kind,
            number=str(item.get("inverted_sequence_number") or item.get("sequence_number") or "") or None,
            issue_date=_pt_date(item.get("date")), counterparty=str(client.get("name") or ""),
            counterparty_tax_id=str(client.get("fiscal_id") or "") or None, net=_money(item.get("before_taxes")),
            vat=_money(item.get("taxes")), gross=_money(item.get("total")), currency=_currency(item.get("currency")),
            final=str(item.get("status") or "").lower() not in _IXP_NOT_FINAL)

    def pdf(self, document: AccountingDocument) -> bytes | None:
        """``/api/pdf/{id}.json``: 202 while the PDF is being made (asked again a few times), then its address."""
        for _ in range(self._pdf_attempts):
            response = self._get(f"/api/pdf/{quote(document.provider_id, safe='')}.json", allow=(202, 404))
            if response.status_code == 404:
                return None
            if response.status_code == 202:
                continue
            payload = json_body(response, "invoicexpress")
            url = (payload.get("output") or {}).get("pdfUrl") if isinstance(payload, dict) else None
            if not isinstance(url, str) or not url:
                raise ProviderError("invoicexpress_pdf_without_url")
            return self._download(self._client, url, "invoicexpress")
        raise TransientError("invoicexpress_pdf_not_ready")


# --------------------------------------------------------------------------- Moloni


class MoloniRefresher:
    """Moloni tokens (``GET /grant/``): an authorization code or a refresh token for an access token (1 hour) and a
    new refresh token (14 days). A refused grant means the owner signs in to Moloni again."""

    def __init__(self, client_id: str, client_secret: str, *, client: httpx.Client | None = None,
                 token_url: str = f"{MOLONI_API}/grant/", clock: Callable[[], datetime] = utcnow) -> None:
        self.client_id = client_id
        self._secret = client_secret
        self._client = client or httpx.Client(timeout=httpx.Timeout(20.0))
        self.token_url = token_url
        self._clock = clock

    def __repr__(self) -> str:
        return f"MoloniRefresher({self.client_id!r})"

    def refresh(self, refresh_token: str) -> OAuthToken:
        if not refresh_token:
            raise ReconnectRequired("moloni_no_refresh_token")
        return self._grant({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def exchange(self, code: str, redirect_uri: str) -> OAuthToken:
        return self._grant({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})

    def _grant(self, params: dict[str, str]) -> OAuthToken:
        query = {**params, "client_id": self.client_id, "client_secret": self._secret}
        try:
            response = self._client.get(self.token_url, params=query, headers={"Accept": "application/json"})
        except httpx.TimeoutException:
            raise TransientError("moloni_token_timeout") from None
        except httpx.HTTPError:
            raise TransientError("moloni_token_network") from None
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientError(f"moloni_token_http_{response.status_code}", retry_after=retry_after_seconds(response))
        payload = json_body(response, "moloni") if response.content else None
        if response.status_code != 200 or not isinstance(payload, dict) or not payload.get("access_token"):
            error = str(payload.get("error", "")) if isinstance(payload, dict) else ""
            if error in ("invalid_client", "unauthorized_client"):
                raise ProviderError(f"moloni_{error}")  # our developer registration: engineering, not the owner
            raise ReconnectRequired(f"moloni_{error or 'grant_refused'}")
        now = self._clock()
        try:
            lifetime = int(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            lifetime = 3600
        # Each refresh gives a new refresh token for 14 more days (MOLONI_REFRESH_DAYS): the grant has no fixed end
        # while the connection syncs, so none is stated (an owner reminder would be wrong).
        return OAuthToken(access_token=str(payload["access_token"]), expires_at=now + timedelta(seconds=lifetime),
                          refresh_token=str(payload.get("refresh_token") or params.get("refresh_token") or "") or None)


class MoloniConnector(AccountingConnector):
    """Moloni through its REST API (module docstring)."""

    provider = "moloni"
    display_name = "Moloni"
    QTY = 50  # the API's maximum per page
    MAX_PAGES = 400
    PURCHASE_ENDPOINTS = ("supplierInvoices", "supplierCreditNotes")

    def __init__(self, tokens: TokenProvider, *, company_id: int | None = None, company_tax_id: str | None = None,
                 client: httpx.Client | None = None, base_url: str = MOLONI_API,
                 clock: Callable[[], datetime] = utcnow) -> None:
        super().__init__(clock=clock)
        self._tokens = tokens
        self._company_id = company_id
        self._tax_id = _digits(company_tax_id)[-9:] or None
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0))
        self.base_url = base_url.rstrip("/")
        self._lock = threading.Lock()

    def _post(self, endpoint: str, data: dict[str, Any]) -> Any:
        url = f"{self.base_url}/{endpoint}/"
        for attempt in (1, 2):
            try:
                response = self._client.post(url, params={"access_token": self._tokens.access_token()},
                                             data={k: str(v) for k, v in data.items()},
                                             headers={"Accept": "application/json"})
            except httpx.TimeoutException:
                raise TransientError("moloni_timeout") from None
            except httpx.HTTPError:
                raise TransientError("moloni_network") from None
            payload = json_body(response, "moloni") if response.content else None
            expired = response.status_code == 401 or (
                isinstance(payload, dict) and payload.get("error") in ("invalid_token", "expired_token"))
            if expired and attempt == 1:
                self._tokens.invalidate()  # maybe just expired: a new access token, once
                continue
            if expired:
                raise ReconnectRequired("moloni_unauthorized")
            error = error_for_status(response, "moloni")
            if error is not None:
                raise error
            if isinstance(payload, dict) and payload.get("error"):
                raise ProviderError(f"moloni_{str(payload['error'])[:40]}")
            return payload
        raise AssertionError("unreachable")  # pragma: no cover

    def company_id(self) -> int:
        """The Moloni company to read: the one given, else the one whose tax number is the company's."""
        with self._lock:
            if self._company_id is not None:
                return self._company_id
            companies = self._post("companies/getAll", {})
            if not isinstance(companies, list):
                raise ProviderError("moloni_unexpected_companies")
            matching = [c for c in companies if isinstance(c, dict)
                        and (self._tax_id is None or _digits(str(c.get("vat") or ""))[-9:] == self._tax_id)]
            if len(matching) != 1 or matching[0].get("company_id") is None:
                raise ProviderError("moloni_company_not_found")
            self._company_id = int(matching[0]["company_id"])
            return self._company_id

    def _all(self, endpoint: str, year: int) -> list[dict[str, Any]]:
        company = self.company_id()
        out: list[dict[str, Any]] = []
        for page in range(self.MAX_PAGES):
            items = self._post(endpoint, {"company_id": company, "qty": self.QTY, "offset": page * self.QTY,
                                          "year": year})
            if not isinstance(items, list):
                raise ProviderError("moloni_unexpected_documents")
            out += [i for i in items if isinstance(i, dict)]
            if len(items) < self.QTY:
                break
        return out

    def list_documents(self, since: date, until: date, direction: str) -> list[AccountingDocument]:
        endpoints = ("documents/getAll",) if direction == SALES else tuple(f"{e}/getAll"
                                                                           for e in self.PURCHASE_ENDPOINTS)
        out: list[AccountingDocument] = []
        for endpoint in endpoints:
            for year in range(since.year, until.year + 1):
                for item in self._all(endpoint, year):
                    doc = self._doc(item, direction)
                    if doc is None or (doc.issue_date is not None and not since <= doc.issue_date <= until):
                        continue
                    if direction == SALES and doc.doc_type not in _SALES_TYPES:
                        continue  # estimates, guides, internal documents: not accounting documents
                    out.append(doc)
        return out

    def _doc(self, item: dict[str, Any], direction: str) -> AccountingDocument | None:
        if item.get("document_id") is None:
            return None
        kind_info = item.get("document_type") if isinstance(item.get("document_type"), dict) else {}
        saft = str(kind_info.get("saft_code") or "").upper() or ("FT" if direction == PURCHASES else "")
        series = item.get("document_set") if isinstance(item.get("document_set"), dict) else {}
        number = str(item.get("number") or "")
        label = f"{saft} {series.get('name')}/{number}" if series.get("name") and number else number or None
        issued = str(item.get("date") or "")[:10]
        currency = item.get("exchange_currency") if isinstance(item.get("exchange_currency"), dict) else {}
        try:
            day = date.fromisoformat(issued) if issued else None
        except ValueError:
            day = None
        return AccountingDocument(
            provider=self.provider, provider_id=str(item["document_id"]), direction=direction, doc_type=saft,
            number=label, issue_date=day, counterparty=str(item.get("entity_name") or ""),
            counterparty_tax_id=str(item.get("entity_vat") or "") or None, net=_money(item.get("net_value")),
            vat=_money(item.get("taxes_value")), gross=_money(item.get("gross_value")),
            currency=_currency(currency.get("iso4217")) if item.get("exchange_currency_id") else "EUR",
            final=str(item.get("status")) == "1")

    def pdf(self, document: AccountingDocument) -> bytes | None:
        """``documents/getPDFLink``: a download address for a finalised document (never a draft)."""
        if not document.final:
            return None
        payload = self._post("documents/getPDFLink", {"company_id": self.company_id(),
                                                       "document_id": document.provider_id})
        url = payload.get("url") if isinstance(payload, dict) else None
        if not isinstance(url, str) or not url:
            return None
        return self._download(self._client, url, "moloni")


# --------------------------------------------------------------------------- TOConline


TOCONLINE_DEFAULT_REDIRECT = "https://oauth.pstmn.io/v1/callback"  # TOConline's fixed default (Dados API)


class TOConlineTokens:
    """A TOConline access token (4 hours), renewed with the refresh token (8 hours) and, when that one has lapsed,
    with a new authorization code from the company's own API data (no owner interaction: the company granted the
    integration when it issued that data). A refused client means the owner gives new API data."""

    def __init__(self, credentials: TOConlineCredentials, *, refresh_token: str | None = None,
                 client: httpx.Client | None = None, redirect_uri: str = TOCONLINE_DEFAULT_REDIRECT,
                 on_rotate: Callable[[OAuthToken], None] | None = None, clock: Callable[[], datetime] = utcnow,
                 skew: timedelta = timedelta(seconds=60)) -> None:
        self.credentials = credentials
        self._refresh = refresh_token
        self._client = client or httpx.Client(timeout=httpx.Timeout(20.0))
        self._redirect = redirect_uri
        self._on_rotate = on_rotate
        self._clock = clock
        self._skew = skew
        self._token: OAuthToken | None = None
        self._lock = threading.Lock()

    def access_token(self) -> str:
        with self._lock:
            if self._token is None or self._token.expires_at - self._skew <= self._clock():
                token: OAuthToken | None = None
                if self._refresh:
                    try:
                        token = self._exchange({"grant_type": "refresh_token", "refresh_token": self._refresh})
                    except ReconnectRequired:
                        token = None  # lapsed after 8 hours: a new code below
                if token is None:
                    token = self._exchange({"grant_type": "authorization_code", "code": self._code()})
                if token.refresh_token and token.refresh_token != self._refresh:
                    self._refresh = token.refresh_token
                    if self._on_rotate is not None:
                        self._on_rotate(token)
                self._token = token
            return self._token.access_token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def _code(self) -> str:
        params = {"client_id": self.credentials.client_id, "redirect_uri": self._redirect, "response_type": "code",
                  "scope": "commercial"}
        try:
            response = self._client.get(f"{self.credentials.oauth_url}/auth", params=params,
                                        headers={"Content-Type": "application/json"}, follow_redirects=False)
        except httpx.TimeoutException:
            raise TransientError("toconline_auth_timeout") from None
        except httpx.HTTPError:
            raise TransientError("toconline_auth_network") from None
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientError(f"toconline_auth_http_{response.status_code}")
        location = response.headers.get("location") or ""
        code = (parse_qs(urlsplit(location).query).get("code") or [""])[0]
        if response.status_code not in (301, 302, 303) or not code:
            raise ReconnectRequired("toconline_authorization_refused")
        return code

    def _exchange(self, form: dict[str, str]) -> OAuthToken:
        basic = base64.b64encode(f"{self.credentials.client_id}:{self.credentials.client_secret}".encode()).decode()
        try:
            response = self._client.post(f"{self.credentials.oauth_url}/token", data={**form, "scope": "commercial"},
                                         headers={"Accept": "application/json", "Authorization": f"Basic {basic}"})
        except httpx.TimeoutException:
            raise TransientError("toconline_token_timeout") from None
        except httpx.HTTPError:
            raise TransientError("toconline_token_network") from None
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientError(f"toconline_token_http_{response.status_code}")
        payload = json_body(response, "toconline") if response.content else None
        if response.status_code != 200 or not isinstance(payload, dict) or not payload.get("access_token"):
            error = str(payload.get("error", "")) if isinstance(payload, dict) else ""
            raise ReconnectRequired(f"toconline_{error or 'token_refused'}")
        now = self._clock()
        try:
            lifetime = int(payload.get("expires_in", 14400))
        except (TypeError, ValueError):
            lifetime = 14400
        return OAuthToken(access_token=str(payload["access_token"]), expires_at=now + timedelta(seconds=lifetime),
                          refresh_token=str(payload.get("refresh_token") or "") or None,
                          refresh_expires_at=now + timedelta(hours=TOCONLINE_REFRESH_HOURS))


class TOConlineConnector(AccountingConnector):
    """TOConline through its JSON:API (module docstring)."""

    provider = "toconline"
    display_name = "TOConline"
    PAGE_SIZE = 100
    MAX_PAGES = 200
    _RESOURCES = {SALES: ("commercial_sales_documents", "Document", "customer"),
                  PURCHASES: ("commercial_purchases_documents", "PurchasesDocument", "supplier")}

    def __init__(self, tokens: TokenProvider, api_url: str, *, client: httpx.Client | None = None,
                 clock: Callable[[], datetime] = utcnow) -> None:
        super().__init__(clock=clock)
        api = _toc_url(api_url, "API address")
        self.api_url = api[:-4] if api.endswith("/api") else api
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0))
        self._http = AuthorizedHttp(self._client, tokens, "toconline")

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self._http.request("GET", f"{self.api_url}{path}", params=params,
                                      headers={"Content-Type": "application/vnd.api+json",
                                               "Accept": "application/json"})
        payload = json_body(response, "toconline")
        if not isinstance(payload, dict):
            raise ProviderError("toconline_unexpected_json")
        return payload

    def list_documents(self, since: date, until: date, direction: str) -> list[AccountingDocument]:
        resource, _, party = self._RESOURCES[direction]
        out: list[AccountingDocument] = []
        for number in range(1, self.MAX_PAGES + 1):
            payload = self._get(f"/api/v1/{resource}", {"page[size]": self.PAGE_SIZE, "page[number]": number,
                                                        "sort": "-date"})
            data = payload.get("data")
            if not isinstance(data, list):
                raise ProviderError("toconline_unexpected_data")
            oldest: date | None = None
            for item in data:
                doc = self._doc(item, direction, party) if isinstance(item, dict) else None
                if doc is None:
                    continue
                if doc.issue_date is not None:
                    oldest = doc.issue_date if oldest is None else min(oldest, doc.issue_date)
                    if not since <= doc.issue_date <= until:
                        continue
                out.append(doc)
            if len(data) < self.PAGE_SIZE or (oldest is not None and oldest < since):
                break
        return out

    def _doc(self, item: dict[str, Any], direction: str, party: str) -> AccountingDocument | None:
        attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else None
        if attributes is None or item.get("id") is None:
            return None
        issued = str(attributes.get("date") or "")[:10]
        try:
            day = date.fromisoformat(issued) if issued else None
        except ValueError:
            day = None
        return AccountingDocument(
            provider=self.provider, provider_id=str(item["id"]), direction=direction,
            doc_type=str(attributes.get("document_type") or "").upper(),
            number=str(attributes.get("document_no") or "") or None, issue_date=day,
            counterparty=str(attributes.get(f"{party}_business_name") or ""),
            counterparty_tax_id=str(attributes.get(f"{party}_tax_registration_number") or "") or None,
            net=_money(attributes.get("net_total")), vat=_money(attributes.get("tax_payable")),
            gross=_money(attributes.get("gross_total")), currency=_currency(attributes.get("currency_iso_code")),
            final=str(attributes.get("status")) == "1")

    def pdf(self, document: AccountingDocument) -> bytes | None:
        """``/api/url_for_print/{id}``: the file's address (scheme, host, port, path), then the file itself."""
        if not document.final:
            return None  # only a finalised document can be printed
        _, kind, _ = self._RESOURCES[document.direction]
        payload = self._get(f"/api/url_for_print/{quote(document.provider_id, safe='')}",
                            {"filter[type]": kind, "filter[copies]": 1})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        attributes = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
        url = attributes.get("url") if isinstance(attributes.get("url"), dict) else None
        if not url or not url.get("host") or not url.get("path"):
            return None
        port = url.get("port")
        netloc = str(url["host"]) + (f":{port}" if port not in (None, 443, "443") else "")
        return self._download(self._client, f"{url.get('scheme') or 'https'}://{netloc}{url['path']}", "toconline")
