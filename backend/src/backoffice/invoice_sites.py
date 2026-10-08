"""Supplier websites the back office can read: where each one keeps its invoices, page by page (§9, §10).

Many suppliers (utilities, telecoms) keep their invoices in a customer area: a sign-in form, sometimes a one-time
code, then a list of invoices with a download link on every row. One deterministic adapter reads every such site
(``connectors.portals.invoice_pages.InvoicePagePortal``); what differs per supplier is only *where* things are,
written here as data: the pages' addresses and the CSS selectors of the form, the rows and the cells.

This module is pure (no network, no HTTP library) so the engine, which also runs in the browser, can use it to
know which hosts are a supplier's invoice website (an email that only links to one teaches where that supplier's
invoices are, backoffice.supplier_websites). The adapter that signs in lives with the connectors, on the server.

Honesty: a configuration is ``verified`` only once it was checked against a real customer account. The ones here
were written against recorded pages that imitate the site's structure (tests/fixtures/portals); until a real
account confirms the addresses and selectors, the adapter reports a page it does not recognise as a changed
website (``PortalChanged``), which flags it for the AI-browser fallback, never as "no invoices".
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

__all__ = ["EDP", "SITES", "InvoicePageSite", "host_of", "on_host", "site_for_host", "site_for_key", "site_for_name"]


@dataclass(frozen=True)
class InvoicePageSite:
    """Where a supplier's customer area keeps its sign-in and its invoices (selectors are CSS)."""

    key: str  # the adapter's key in the portal registry ("edp_pt")
    name: str  # the supplier as the owner reads it ("EDP")
    hosts: tuple[str, ...]  # the website's domains; every page and file must be on one of them (https only)
    names: tuple[str, ...]  # supplier names this website is for (matched when the owner adds it by name)
    sign_in_url: str
    sign_in_form: str  # the sign-in form
    username_field: str
    password_field: str
    signed_in: str  # present only on a signed-in page
    invoices_url: str  # the invoice list; "{year}" when the site lists one year per page
    invoice_row: str  # one row per invoice
    number: str  # inside a row: the invoice number
    issue_date: str
    amount: str  # the total, as printed ("64,10 €")
    download: str  # the link to the original file (its href)
    row_id: str | None = None  # an attribute of the row with the site's own id for the invoice
    period: str | None = None  # the period it covers ("18/08/2026 a 17/09/2026"), when shown
    no_invoices: str | None = None  # shown instead of the list when there is nothing yet (not a changed page)
    next_page: str | None = None  # the link to the next page of the list
    code_form: str | None = None  # the one-time code form, when the site sends one after the password
    code_field: str = "code"
    code_channel: str | None = "sms"  # where the code goes, as the owner is told
    code_expired: str | None = None  # shown when the code is no longer valid
    date_formats: tuple[str, ...] = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d")
    session_minutes: int = 30  # how long a signed-in session is reused before signing in again
    verified: bool = False  # checked against a real customer account


# EDP (electricity and gas, Portugal): the customer area's sign-in, its SMS code and the invoice list per year.
# Written against recorded pages that imitate the area's structure; not yet checked with a real EDP account.
EDP = InvoicePageSite(
    key="edp_pt", name="EDP", hosts=("edp.pt",), names=("EDP", "EDP Comercial", "EDP Energia"),
    sign_in_url="https://www.edp.pt/area-cliente/entrar",
    sign_in_form="form#form-login", username_field="email", password_field="password",
    signed_in="main[data-area-cliente]",
    code_form="form#form-codigo", code_field="codigo", code_channel="sms", code_expired=".codigo-expirado",
    invoices_url="https://www.edp.pt/area-cliente/faturas?ano={year}",
    invoice_row="table.lista-faturas > tbody > tr", row_id="data-fatura-id",
    number="td.numero", issue_date="td.data-emissao", amount="td.valor", download="a.descarregar-pdf",
    period="td.periodo", no_invoices=".sem-faturas", next_page="nav.paginacao a[rel=next]",
)

SITES: tuple[InvoicePageSite, ...] = (EDP,)


def host_of(url: str) -> str | None:
    """The lower-case host of an address, or None."""
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return None
    return host.lower().rstrip(".") if host else None


def on_host(host: str | None, domains: tuple[str, ...] | list[str]) -> str | None:
    """The domain ``host`` belongs to (itself or a parent: ``www.edp.pt`` is on ``edp.pt``), or None."""
    if not host:
        return None
    host = host.lower().rstrip(".")
    for domain in domains:
        d = domain.lower().strip(".")
        if d and (host == d or host.endswith("." + d)):
            return d
    return None


def site_for_key(key: str | None) -> InvoicePageSite | None:
    return next((s for s in SITES if s.key == key), None) if key else None


def site_for_host(host: str | None) -> InvoicePageSite | None:
    """The known supplier website ``host`` is part of."""
    return next((s for s in SITES if on_host(host, s.hosts)), None)


def site_for_name(name: str | None) -> InvoicePageSite | None:
    """The known supplier website for a supplier the owner named ("EDP", "edp comercial")."""
    wanted = " ".join((name or "").casefold().split())
    if not wanted:
        return None
    return next((s for s in SITES if wanted in {" ".join(n.casefold().split()) for n in s.names}), None)
