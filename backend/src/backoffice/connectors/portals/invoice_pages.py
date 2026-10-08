"""One deterministic adapter for every supplier website whose invoice area is a list of invoices (§10, C5).

A customer area is the same everywhere: a sign-in form (often with a hidden anti-forgery field), sometimes a
one-time code sent by SMS, then a page listing the invoices, one row each, with the number, the date, the total
and a link to the original PDF. :class:`InvoicePagePortal` reads that, and :func:`backoffice.invoice_sites.sites`
says where each supplier keeps it (addresses and CSS selectors). :func:`site_adapter` makes the registered adapter
class of one site (``EdpPortal`` for EDP), so the server's portal worker builds it by its key like any adapter.

Deterministic and contained:

* only ``https`` pages on the site's own hosts are ever requested; redirects are followed hop by hop and each hop
  is checked, so a password or a session cookie never leaves the supplier's website;
* the session (its cookies) is returned as plain data for the vault and reused until it expires; a page that is
  the sign-in form again means the website signed it out (:class:`SessionExpired`: sign in again);
* a page the configuration does not recognise (no sign-in form, no invoice list and no "no invoices" notice, a
  row without its number or date, a download that is not a PDF) raises :class:`PortalChanged`: the website
  changed, the AI-browser fallback may take over (``plan_retrieval``), nothing is guessed;
* an outage (5xx, 429, a timeout) is a :class:`PortalError` the sync retries later; a refused password is
  ``LOGIN_REQUIRED`` (the owner reconnects); a code is ``MFA_REQUIRED`` and resumes with the owner's code.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urljoin, urlsplit

from backoffice.domain.models import utcnow
from backoffice.invoice_sites import InvoicePageSite, on_host, sites

from .base import (
    AuthResult,
    AuthStatus,
    MfaChallenge,
    PortalChanged,
    PortalCredentials,
    PortalDocument,
    PortalError,
    PortalInvoiceRef,
    PortalSession,
    SessionExpired,
    SupplierPortalConnector,
    register_portal,
)
from .pages import Node, parse_html

if TYPE_CHECKING:
    import httpx

__all__ = ["ADAPTERS", "EdpPortal", "InvoicePagePortal", "parse_amount", "site_adapter"]

MAX_REDIRECTS = 5
MAX_LIST_PAGES = 12  # pages of one year's list
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_FILE_BYTES = 25 * 1024 * 1024
CODE_VALID_FOR = timedelta(minutes=10)
_MONEY = re.compile(r"-?\d[\d.,  ]*")


def parse_amount(text: str) -> Decimal | None:
    """A total as printed: ``64,10 €``, ``€ 1.234,56``, ``1,234.56 EUR``, ``-12,00``. None when there is none."""
    m = _MONEY.search(text.replace("−", "-"))
    if m is None:
        return None
    raw = re.sub(r"[\s ]", "", m.group(0)).rstrip(".,")
    negative = raw.startswith("-")
    raw = raw.lstrip("-")
    last_comma, last_dot = raw.rfind(","), raw.rfind(".")
    decimal_mark = "," if last_comma > last_dot else "."
    whole, _, cents = raw.rpartition(decimal_mark)
    if not whole or len(cents) not in (1, 2):  # "1.234" (thousands only) or "64": a whole amount
        whole, cents = raw, ""
    digits = re.sub(r"[.,]", "", whole) + ("." + cents if cents else "")
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None
    return -value if negative else value


def _filename(disposition: str | None, fallback: str) -> str:
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition or "", re.IGNORECASE)
    name = (m.group(1) if m else fallback).strip()
    name = re.sub(r"[^\w.\- ]", "_", name.replace("/", "_").replace("\\", "_"))[:120].strip(" .") or fallback
    return name if name.lower().endswith(".pdf") else f"{name}.pdf"


class InvoicePagePortal(SupplierPortalConnector):
    """A supplier website read through its invoice list (module docstring). Subclasses set ``site``."""

    site: ClassVar[InvoicePageSite]

    def __init__(self, *, client: httpx.Client | None = None, clock: Any = utcnow, timeout: float = 30.0) -> None:
        if client is None:
            import httpx

            client = httpx.Client(timeout=timeout, follow_redirects=False,
                                  headers={"User-Agent": "BackOffice/1.0 (invoice retrieval for the account holder)",
                                           "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8"})
        self._client = client
        self._clock = clock

    # ----------------------------------------------------------------- HTTP, contained to the site

    def _url(self, target: str | None, base: str) -> str:
        """An absolute https address on the site's own hosts, or :class:`PortalChanged` (never followed)."""
        if not target:
            raise PortalChanged("link_missing")
        url = urljoin(base, target.strip())
        parts = urlsplit(url)
        if parts.scheme != "https" or not on_host(parts.hostname, self.site.hosts) or parts.username or parts.password:
            raise PortalChanged("link_off_site")
        return url

    def _send(self, method: str, url: str, jar: dict[str, str], *, form: Mapping[str, str] | None = None,
              limit: int = MAX_PAGE_BYTES) -> tuple[str, Any]:
        """One request and its redirects, each hop on the site; the jar gains every cookie the site sets."""
        import httpx

        for _ in range(MAX_REDIRECTS + 1):
            url = self._url(url, url)
            self._client.cookies.clear()  # the session's cookies are ours, sent explicitly
            headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in jar.items())} if jar else {}
            try:
                if method == "POST":
                    response = self._client.post(url, data=dict(form or {}), headers=headers)
                else:
                    response = self._client.get(url, headers=headers)
            except httpx.TimeoutException:
                raise PortalError("portal_timeout") from None
            except httpx.HTTPError:
                raise PortalError("unreachable") from None
            for name, value in response.cookies.items():
                if value:
                    jar[name] = value
                else:
                    jar.pop(name, None)
            if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                url = self._url(response.headers["location"], url)
                if response.status_code in (301, 302, 303):
                    method, form = "GET", None
                continue
            if response.status_code == 429 or response.status_code >= 500:
                raise PortalError(f"http_{response.status_code}")
            if len(response.content) > limit:
                raise PortalChanged("response_too_large")
            return str(response.url), response
        raise PortalError("too_many_redirects")

    @staticmethod
    def _page(response: Any) -> Node:
        return parse_html(response.content.decode(response.encoding or "utf-8", errors="replace"))

    def _signed_out(self, response: Any, page: Node) -> bool:
        return response.status_code in (401, 403) or (
            page.select_one(self.site.signed_in) is None and page.select_one(self.site.sign_in_form) is not None)

    # ----------------------------------------------------------------- signing in

    def authenticate(self, credentials: PortalCredentials) -> AuthResult:
        site = self.site
        jar: dict[str, str] = {}
        url, response = self._send("GET", site.sign_in_url, jar)
        page = self._page(response)
        form = page.select_one(site.sign_in_form)
        if form is None:
            raise PortalChanged("sign_in_form_missing")
        fields = form.form_fields()
        fields[site.username_field] = credentials.username
        fields[site.password_field] = credentials.password.get_secret_value()
        url, response = self._send("POST", self._url(form.attr("action") or url, url), jar, form=fields)
        return self._signed_in(credentials.username, url, response, jar)

    def _signed_in(self, account: str, url: str, response: Any, jar: dict[str, str]) -> AuthResult:
        site = self.site
        page = self._page(response)
        now = self._clock()
        if response.status_code < 400 and page.select_one(site.signed_in) is not None:
            return AuthResult(AuthStatus.AUTHENTICATED, PortalSession(
                self.supplier_key, account, now, expires_at=now + timedelta(minutes=site.session_minutes),
                state={"cookies": dict(jar)}))
        code_form = page.select_one(site.code_form) if site.code_form else None
        if code_form is not None:
            resume = {"cookies": dict(jar), "action": self._url(code_form.attr("action") or url, url),
                      "fields": code_form.form_fields()}
            return self.mfa_required(account, channel=site.code_channel, resume_state=resume, issued_at=now,
                                     expires_at=now + CODE_VALID_FOR)
        if page.select_one(site.sign_in_form) is not None:
            return AuthResult(AuthStatus.LOGIN_REQUIRED)  # the form again: the website refused the password
        raise PortalChanged("sign_in_answer_unknown")

    def complete_mfa(self, challenge: MfaChallenge, code: str) -> AuthResult:
        site = self.site
        resume = challenge.resume_state
        if not site.code_form or not isinstance(resume.get("action"), str):
            return AuthResult(AuthStatus.FAILED)
        jar = {str(k): str(v) for k, v in dict(resume.get("cookies") or {}).items()}
        fields = {str(k): str(v) for k, v in dict(resume.get("fields") or {}).items()}
        fields[site.code_field] = code
        url, response = self._send("POST", str(resume["action"]), jar, form=fields)
        page = self._page(response)
        if response.status_code < 400 and page.select_one(site.signed_in) is not None:
            return self._signed_in(challenge.account, url, response, jar)
        if site.code_expired and page.select_one(site.code_expired) is not None:
            return AuthResult(AuthStatus.CODE_EXPIRED)
        if page.select_one(site.code_form) is not None:
            return AuthResult(AuthStatus.CODE_REJECTED)
        if page.select_one(site.sign_in_form) is not None:
            return AuthResult(AuthStatus.CODE_EXPIRED)  # back at the sign-in: that code's sign-in is over
        raise PortalChanged("code_answer_unknown")

    # ----------------------------------------------------------------- the invoice list

    def _jar(self, session: PortalSession) -> dict[str, str]:
        return {str(k): str(v) for k, v in dict(session.state.get("cookies") or {}).items()}

    def list_invoices(self, session: PortalSession, since: date, until: date) -> list[PortalInvoiceRef]:
        site = self.site
        jar = self._jar(session)
        years = range(since.year, until.year + 1) if "{year}" in site.invoices_url else (None,)
        found: dict[str, PortalInvoiceRef] = {}
        for year in years:
            url: str | None = site.invoices_url.format(year=year) if year is not None else site.invoices_url
            for _ in range(MAX_LIST_PAGES):
                if url is None:
                    break
                url, response = self._send("GET", url, jar)
                page = self._page(response)
                if self._signed_out(response, page):
                    raise SessionExpired("signed_out")
                if response.status_code >= 400:
                    raise PortalChanged(f"invoice_list_http_{response.status_code}")
                rows = page.select(site.invoice_row)
                if not rows:
                    if site.no_invoices and page.select_one(site.no_invoices) is not None:
                        break
                    raise PortalChanged("invoice_list_missing")
                for row in rows:
                    ref = self._ref(row, url)
                    if ref.issue_date is not None and since <= ref.issue_date <= until:
                        found.setdefault(ref.portal_id, ref)
                link = page.select_one(site.next_page) if site.next_page else None
                url = self._url(link.attr("href"), url) if link is not None and link.attr("href") else None
        return sorted(found.values(), key=lambda r: (r.issue_date or date.min, r.portal_id))

    def _ref(self, row: Node, page_url: str) -> PortalInvoiceRef:
        site = self.site

        def cell(selector: str | None) -> str:
            node = row.select_one(selector) if selector else None
            return node.text() if node is not None else ""

        number = cell(site.number)
        issued = self._date(cell(site.issue_date))
        link = row.select_one(site.download)
        if not number or issued is None or link is None:
            raise PortalChanged("invoice_row_unreadable")
        url = self._url(link.attr("href"), page_url)
        own_id = (row.attr(site.row_id) if site.row_id else None) or number
        start = end = None
        period = cell(site.period)
        if period:
            days = [d for d in (self._date(p) for p in re.findall(r"\d{1,4}[/-]\d{1,2}[/-]\d{1,4}", period)) if d]
            if len(days) == 2:
                start, end = days
        return PortalInvoiceRef(self.supplier_key, own_id, number, issued, parse_amount(cell(site.amount)), "EUR",
                                start, end, url)

    def _date(self, text: str) -> date | None:
        """The first date in ``text``, in one of the site's formats ("18/09/2026", "18.09.2026", "2026-09-18")."""
        m = re.search(r"\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}", text)
        if m is None:
            return None
        for fmt in self.site.date_formats:
            sep = re.search(r"[/.-]", fmt)
            try:
                return datetime.strptime(re.sub(r"[/.-]", sep.group(0) if sep else "/", m.group(0)), fmt).date()
            except ValueError:
                continue
        return None

    def retrieve_invoice(self, session: PortalSession, ref: PortalInvoiceRef) -> PortalDocument:
        if not ref.url:
            raise PortalChanged("invoice_without_link")
        jar = self._jar(session)
        url, response = self._send("GET", ref.url, jar, limit=MAX_FILE_BYTES)
        data = response.content
        ctype = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if not data.startswith(b"%PDF"):
            if "html" in ctype and self._signed_out(response, self._page(response)):
                raise SessionExpired("signed_out")
            if response.status_code >= 400:
                raise PortalError(f"http_{response.status_code}")
            raise PortalChanged("download_not_a_pdf")
        name = _filename(response.headers.get("content-disposition"),
                         re.sub(r"[^\w-]+", "_", ref.invoice_number or ref.portal_id).strip("_") or "invoice")
        return PortalDocument(ref, data, "application/pdf", name, retrieved_at=self._clock(), source_url=url)

    def retrieve_statement(self, session: PortalSession, period_start: date, period_end: date) -> None:
        return None  # an invoice list has no account statement


def site_adapter(site: InvoicePageSite) -> type[InvoicePagePortal]:
    """The adapter class of one configured website (registered under its key)."""
    name = re.sub(r"[^A-Za-z0-9]", "", site.name.title()) or "Site"
    return type(f"{name}Portal", (InvoicePagePortal,), {
        "site": site, "supplier_key": site.key, "display_name": site.name, "domains": site.hosts,
        "__doc__": f"{site.name}'s customer area (backoffice.invoice_sites).", "__module__": __name__})


ADAPTERS: dict[str, type[InvoicePagePortal]] = {s.key: register_portal(site_adapter(s)) for s in sites()}
EdpPortal = ADAPTERS["edp_pt"]
