"""Searching connected places for missing documents, before anything is recorded (§22, sync worker step 5).

For each document the tenant says is missing (``BackOfficeService.search_requests``, a read), the worker builds
the searches its connections allow (``missing.searches``):

* each signed-in mailbox (Gmail, Microsoft 365, IMAP): the current window, the months before it, and the
  supplier's usual way of sending (learned from the earlier invoices);
* Google Drive or OneDrive / SharePoint;
* the accounting software (TOConline, Moloni, InvoiceXpress);
* the supplier's website the owner connected (server/portals.py: its adapter and the sign-in in the vault), asked
  first when it is learned to hold that supplier's invoices (``SearchRequest.first``, backoffice.supplier_websites).

The searches run live (``run_searches``: each attempt timed and its result noted; a place that fails is noted and
the others still searched). Then :meth:`TenantManager.record_search` reads the files found (OCR) and opens their
invoice links, leaves out the files that clearly are about something else (:func:`triage`: their text names
neither the amount nor the invoice number), and records one ``search.recorded`` event. Applying it (live and on
replay) is the engine's: ingestion, verification, reconciliation, and the supplier request only when nothing was
found (backoffice.evidence_search). A place whose sign-in was refused is marked as needing the owner, as its sync
would.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from backoffice.missing import (
    AccountingSearch,
    CloudStorageSearch,
    FoundFile,
    MailboxSearch,
    PortalSearch,
    RecurringMailSearch,
    SearchRequest,
    SearchSource,
    amount_words,
    run_searches,
    search_order,
)

__all__ = ["SEARCHES_PER_PASS", "MissingSearches", "triage"]

log = logging.getLogger("backoffice.server.search")

SEARCHES_PER_PASS = 5  # missing documents searched for per tenant and pass


# --------------------------------------------------------------------------- what is clearly something else


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def _texts(tenant_id: str, f: FoundFile, readings: Mapping[str, Any]) -> list[str]:
    """Every readable text of one found file: its own words (an email's subject and body, a text or XML file) and
    the readings of the PDFs and photos inside it (taken before the event is recorded)."""
    from backoffice.evidence.email import EmailParseError, parse_eml
    from backoffice.evidence.html_signals import html_to_text

    from .reads import decode_outcome, readable_files, sha

    out: list[str] = []
    ctype = (f.content_type or "").lower()
    if f.is_email:
        try:
            parsed = parse_eml(f.data)
            out.append(f"{parsed.subject}\n{parsed.text_body}\n{html_to_text(parsed.html_body or '')}")
        except (EmailParseError, ValueError):
            pass
    elif ctype.startswith("text/") or ctype.endswith("xml"):
        out.append(f.data.decode("utf-8", errors="replace"))
    for _, blob, _ in readable_files(tenant_id, f.data, filename=f.filename, mime_type=f.content_type):
        recorded = readings.get(sha(blob))
        if recorded is not None:
            outcome = decode_outcome(recorded)
            out.append(f"{outcome.text or ''}\n{outcome.reading_text or ''}")
    return [t for t in out if t.strip()]


def _about_it(request: SearchRequest, texts: Sequence[str]) -> bool:
    if not texts:
        return True  # nothing readable (a photo without OCR here): kept, the pipeline stores it
    if request.amount is None and not request.invoice_number:
        return True  # a usual invoice of a varying amount: anything the place gave for that supplier and period
    joined = "\n".join(texts)
    if request.invoice_number and _norm(request.invoice_number) and _norm(request.invoice_number) in _norm(joined):
        return True
    return any(word in joined for word in amount_words(request.amount))


def triage(tenant_id: str, request: SearchRequest, files: Sequence[FoundFile], attempts: Sequence[Mapping[str, Any]],
           readings: Mapping[str, Any]) -> tuple[list[FoundFile], list[dict[str, Any]]]:
    """The files that may be the missing document (the rest are clearly about something else and are not
    recorded), and the attempts with what each place gave after that."""
    kept = [f for f in files if _about_it(request, _texts(tenant_id, f, readings))]
    counts: dict[str, int] = {}
    for f in kept:
        counts[f.source.value] = counts.get(f.source.value, 0) + 1
    out: list[dict[str, Any]] = []
    for a in attempts:
        a = dict(a)
        if a.get("outcome") in ("found", "nothing"):
            n = counts.get(str(a.get("source")), 0) if a.get("outcome") == "found" else 0
            # Shared by the places of one kind (two mailboxes): each keeps at most what it gave.
            a["found"] = min(int(a.get("found") or 0), n)
            counts[str(a.get("source"))] = max(0, n - a["found"])
            a["outcome"] = "found" if a["found"] else "nothing"
        out.append(a)
    return kept, out


# --------------------------------------------------------------------------- running the searches


@dataclass(frozen=True)
class _Unavailable:
    """A place that could not even be opened (no sign-in, the vault unreachable): a failed attempt."""

    source: SearchSource
    place: str
    error: Exception
    connection_id: str
    first: bool = False  # the place learned to hold the supplier's invoices (still asked first, and noted)

    def find(self, request: SearchRequest) -> list[FoundFile]:
        raise self.error


class MissingSearches:
    """Runs the searches of one tenant's missing documents (the sync worker's step; module docstring)."""

    def __init__(self, worker: Any, *, portal_factory: Callable[[str, SearchRequest], Any] | None = None,
                 per_pass: int = SEARCHES_PER_PASS) -> None:
        self.worker = worker
        self.manager = worker.manager
        self.portal_factory = portal_factory  # (tenant, request) -> (adapter, credentials) | None
        self.per_pass = per_pass

    def run(self, tenant_id: str, report: Any) -> None:
        from .sync import _Gone, _searchable_connections

        requests = self.manager.read(tenant_id, lambda svc: svc.search_requests(), what="search plan")
        if not requests:
            return
        connections = {c.id: c for c in self.manager.read(
            tenant_id, lambda svc: _searchable_connections(tenant_id, svc), what="search places")}
        for raw in requests[: self.per_pass]:
            request = SearchRequest.from_json(raw)
            searches = self._searches(tenant_id, request, connections)
            run = run_searches(request, searches, clock=self.worker.now)
            self._keep_sessions(tenant_id, searches)
            status, body = self.manager.record_search(tenant_id, run)
            if status == 404:
                raise _Gone(tenant_id)
            report.searches += 1
            if body.get("status") == "found":
                report.found += 1
            self._lost_access(tenant_id, searches, run.attempts, connections)

    def _searches(self, tenant_id: str, request: SearchRequest, connections: Mapping[str, Any]) -> list[Any]:
        from backoffice.connectors.base import ConnectorError

        searches: list[Any] = []
        name = request.name
        for cid in request.connections:
            c = connections.get(cid)
            if c is None:
                continue
            if c.kind == "portal":
                website = self._website(tenant_id, c, first=cid in request.first)
                if website is not None:
                    searches.append(website)
                continue
            try:
                connector, provider = self.worker.search_connector(c)
            except Exception as exc:  # noqa: BLE001 - noted as a failed attempt; the other places still searched
                error = exc if isinstance(exc, ConnectorError) else RuntimeError(type(exc).__name__)
                source = {"email": SearchSource.CURRENT_EMAIL, "files": SearchSource.FILES,
                          "accounting": SearchSource.ACCOUNTING_PLATFORM}[c.kind]
                searches.append(_Unavailable(source, _place(c), error, c.id))
                continue
            if connector is None:
                continue
            if c.kind == "email":
                found = [MailboxSearch(connector, place="your email", provider=provider),
                         MailboxSearch(connector, place="your email", provider=provider, historical=True)]
                if request.pattern is not None and request.pattern.senders:
                    found.append(RecurringMailSearch(connector, place=f"{name}'s usual invoice emails",
                                                     provider=provider))
            elif c.kind == "files":
                found = [CloudStorageSearch(connector, place=_place(c))]
            else:
                found = [AccountingSearch(connector, place=_place(c))]
            for search in found:
                search.connection_id = c.id
            searches += found
        if self.portal_factory is not None:
            try:
                portal = self.portal_factory(tenant_id, request)
            except Exception:  # noqa: BLE001 - a broken portal set-up never stops the other places
                log.warning("portal_factory_failed", extra={"tenant": tenant_id})
                portal = None
            if portal is not None:
                adapter, credentials = portal
                searches.append(PortalSearch(adapter, credentials=credentials,
                                             place=f"your account on {name}'s website"))
        return searches

    def _website(self, tenant_id: str, c: Any, *, first: bool) -> Any:
        """The supplier's website as a place (server/portals.py), or a failed attempt when its sign-in cannot be
        opened; None when this worker reads no websites or has no adapter for this one."""
        portals = getattr(self.worker, "portals", None)
        if portals is None:
            return None
        try:
            website = portals.search_place(tenant_id, c.id, place=_place(c), first=first)
        except Exception as exc:  # noqa: BLE001 - the vault or the adapter: a failed attempt, the others searched
            from backoffice.connectors.base import ConnectorError

            error = exc if isinstance(exc, ConnectorError) else RuntimeError(type(exc).__name__)
            return _Unavailable(SearchSource.SUPPLIER_PORTAL, _place(c), error, c.id, first)
        return website

    def _keep_sessions(self, tenant_id: str, searches: Sequence[Any]) -> None:
        """A website session a search signed in with, and the invoices it fetched, are kept (server/portals.py)."""
        portals = getattr(self.worker, "portals", None)
        if portals is None:
            return
        for search in searches:
            if isinstance(search, PortalSearch) and search.connection_id:
                try:
                    portals.searched(tenant_id, search)
                except Exception:  # noqa: BLE001 - only a convenience for the next run: the search stands
                    log.warning("portal_session_not_kept", extra={"tenant": tenant_id})

    def _lost_access(self, tenant_id: str, searches: Sequence[Any], attempts: Sequence[Mapping[str, Any]],
                     connections: Mapping[str, Any]) -> None:
        """A cloud storage sign-in refused during a search: the connection needs the owner, as a sync would say."""
        refused = {getattr(s, "connection_id", None) for s, a in zip(searches, _ordered(searches, attempts))
                   if str(a.get("failure") or "").startswith("ReconnectRequired")}
        for cid in sorted(c for c in refused if c):
            c = connections.get(cid)
            if c is None or c.kind != "files":
                continue  # mailboxes and accounting software are told by their own sync
            state = self.worker.saved_state(c)
            self.manager.record_sync_failure(tenant_id, cid, state, reconnect=True)


def _place(c: Any) -> str:
    """The same words as the engine's ``evidence_search.place_of``."""
    if c.kind == "email":
        return "your email"
    if c.kind == "files":
        return f"your {c.name}"
    if c.kind == "portal":
        return f"your account on {c.name}'s website"
    return c.name


def _ordered(searches: Sequence[Any], attempts: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The attempts in the order of ``searches`` (run_searches asks them in ``search_order``)."""
    order = search_order(searches)
    out: list[Mapping[str, Any]] = [{} for _ in searches]
    for position, index in enumerate(order):
        if position < len(attempts):
            out[index] = attempts[position]
    return out
