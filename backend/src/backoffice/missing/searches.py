"""The places the missing-document autopilot looks, connected (§22 searches 1-6).

:class:`~backoffice.missing.autopilot.MissingEvidenceAutopilot` decides the order and what follows; this module
gives it real places to look:

**Live** (the production sync worker, before anything is recorded; server/search.py). Each
:class:`SourceSearch` asks one connected place for the document a :class:`SearchRequest` describes and returns
what it found as :class:`FoundFile` s (the bytes and where they came from):

1. :class:`MailboxSearch` (current email): the mailbox, around the payment, for the supplier's address, the
   invoice number, the amount as it is printed or the supplier's name (Gmail ``q``; Graph ``$search`` over every
   folder, the archive included; IMAP ``SEARCH``);
2. the same, ``historical=True`` (historical email): the months before that window, for the invoice number or
   the amount from that supplier only;
3. :class:`CloudStorageSearch`: Google Drive or OneDrive / SharePoint;
4. :class:`PortalSearch`: the supplier's website, through its adapter and the owner's sign-in there (the
   saved session first; a new sign-in when the website signed it out);
5. :class:`AccountingSearch`: TOConline, Moloni or InvoiceXpress;
6. :class:`RecurringMailSearch` (recurring history): the way this supplier's invoices usually arrive, learned
   from the earlier ones (the usual sender, the words its subjects share, an attachment), in the window.

:func:`run_searches` runs them in that order and notes each attempt: which place, when it started and finished,
and what it gave (files found, nothing, failed, timed out). Nothing is judged here: the files go into the event.
A place learned to hold this supplier's invoices (its website, backoffice.supplier_websites) is marked ``first``:
it is asked before the others, live and when the record is applied (the attempt carries ``first``).

**Applied** (live and on replay, inside the engine; backoffice.evidence_search). :class:`RecordedSearch` hands one
place's recorded result to the autopilot as an ``EvidenceSearch``: its files are read into the pipeline when the
autopilot asks that place, so the first place that gives a verified, matching document ends the search (§22),
and a place that failed is a failed attempt again. :func:`run_recorded` runs the plan without an event loop:
recorded searches never wait.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from .autopilot import SEARCH_ORDER, EvidenceCandidate, EvidenceQuery, SearchSource

__all__ = [
    "MAX_FILES_PER_PLACE",
    "AccountingSearch",
    "CloudStorageSearch",
    "FoundFile",
    "MailboxSearch",
    "PortalSearch",
    "RecordedFailure",
    "RecordedSearch",
    "RecurringMailSearch",
    "SearchPattern",
    "SearchRequest",
    "SearchRun",
    "SourceSearch",
    "amount_words",
    "run_recorded",
    "run_searches",
    "search_order",
]

MAX_FILES_PER_PLACE = 5  # what one place may hand in for one missing document
HISTORY_DAYS = 400  # how far back historical email reaches (an annual invoice, a renewal)


def amount_words(amount: Decimal | None) -> tuple[str, ...]:
    """The amount as invoices print it: ``64,10`` and ``64.10``; ``1.234,56``, ``1,234.56`` and ``1234,56``."""
    if amount is None:
        return ()
    value = abs(amount).quantize(Decimal("0.01"))
    plain = f"{value:.2f}"
    whole, cents = plain.split(".")
    grouped = f"{int(whole):,}"
    out = [f"{whole},{cents}", plain]
    if "," in grouped:
        out += [f"{grouped.replace(',', '.')},{cents}", f"{grouped}.{cents}"]
    return tuple(dict.fromkeys(out))


def _domain(value: str) -> str:
    value = value.strip().lower()
    return value.split("@", 1)[1] if "@" in value else value


@dataclass(frozen=True)
class SearchPattern:
    """How a supplier's invoices usually arrive, learned from the earlier ones (§22 search 6)."""

    senders: tuple[str, ...] = ()  # the addresses they came from
    subject_words: tuple[str, ...] = ()  # words every subject shared
    filename_words: tuple[str, ...] = ()  # words every attachment's name shared

    def to_json(self) -> dict[str, Any]:
        return {"senders": list(self.senders), "subjectWords": list(self.subject_words),
                "filenameWords": list(self.filename_words)}

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> SearchPattern | None:
        if not isinstance(data, Mapping):
            return None
        return cls(tuple(str(s) for s in data.get("senders") or ()),
                   tuple(str(s) for s in data.get("subjectWords") or ()),
                   tuple(str(s) for s in data.get("filenameWords") or ()))


@dataclass(frozen=True)
class SearchRequest:
    """One missing document to look for, as the engine describes it (``SearchAgent.requests``)."""

    subject_id: str
    subject_kind: str  # "payment" | "expected_invoice"
    round: int
    company_id: str | None
    amount: Decimal | None  # absolute; None when it is not known (a usual invoice of a varying amount)
    currency: str
    paid_on: date  # the payment's day, or the day the usual invoice was expected
    window_start: date
    window_end: date
    counterparty: str
    supplier_name: str | None = None
    supplier_tax_id: str | None = None
    supplier_domains: tuple[str, ...] = ()
    invoice_number: str | None = None
    direction: str = "purchases"  # a supplier's document ("purchases") or the company's own ("sales")
    pattern: SearchPattern | None = None
    connections: tuple[str, ...] = ()  # the connected places to look in (connection ids)
    entity_id: str | None = None
    first: tuple[str, ...] = ()  # connections learned to hold this supplier's invoices: asked before the others

    @property
    def name(self) -> str:
        return self.supplier_name or self.counterparty

    def terms(self) -> tuple[str, ...]:
        """What a file or message about it says: the invoice number, the amount as printed, the supplier."""
        out = [t for t in (self.invoice_number,) if t]
        out += list(amount_words(self.amount)[:2])
        if self.supplier_name:
            out.append(self.supplier_name)
        return tuple(dict.fromkeys(out))

    def evidence_query(self, tenant_id: str) -> EvidenceQuery:
        from backoffice.learning.keys import counterparty_key

        return EvidenceQuery(tenant_id=tenant_id, transaction_id=self.subject_id, amount=self.amount or Decimal("0"),
                             currency=self.currency, paid_on=self.paid_on, counterparty=self.counterparty,
                             window_start=self.window_start, window_end=self.window_end,
                             counterparty_key=counterparty_key(self.counterparty), entity_id=self.entity_id,
                             supplier_tax_id=self.supplier_tax_id, invoice_number=self.invoice_number)

    def to_json(self) -> dict[str, Any]:
        return {"subjectId": self.subject_id, "kind": self.subject_kind, "round": self.round,
                "companyId": self.company_id, "amount": str(self.amount) if self.amount is not None else None,
                "currency": self.currency, "date": self.paid_on.isoformat(),
                "windowStart": self.window_start.isoformat(), "windowEnd": self.window_end.isoformat(),
                "counterparty": self.counterparty, "supplierName": self.supplier_name,
                "supplierTaxId": self.supplier_tax_id, "supplierDomains": list(self.supplier_domains),
                "invoiceNumber": self.invoice_number, "direction": self.direction,
                "pattern": self.pattern.to_json() if self.pattern else None, "connections": list(self.connections),
                "entityId": self.entity_id, "first": list(self.first)}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SearchRequest:
        amount = data.get("amount")
        return cls(subject_id=str(data["subjectId"]), subject_kind=str(data.get("kind") or "payment"),
                   round=int(data.get("round") or 1), company_id=data.get("companyId"),
                   amount=Decimal(str(amount)) if amount not in (None, "") else None,
                   currency=str(data.get("currency") or "EUR"), paid_on=date.fromisoformat(str(data["date"])),
                   window_start=date.fromisoformat(str(data["windowStart"])),
                   window_end=date.fromisoformat(str(data["windowEnd"])),
                   counterparty=str(data.get("counterparty") or ""), supplier_name=data.get("supplierName"),
                   supplier_tax_id=data.get("supplierTaxId"),
                   supplier_domains=tuple(str(d) for d in data.get("supplierDomains") or ()),
                   invoice_number=data.get("invoiceNumber"), direction=str(data.get("direction") or "purchases"),
                   pattern=SearchPattern.from_json(data.get("pattern")),
                   connections=tuple(str(c) for c in data.get("connections") or ()), entity_id=data.get("entityId"),
                   first=tuple(str(c) for c in data.get("first") or ()))


@dataclass(frozen=True)
class FoundFile:
    """A file or message one place gave for a missing document, with where it came from (§55)."""

    source: SearchSource
    place: str  # plain words: "your email", "your Google Drive", "Moloni"
    data: bytes = field(repr=False)
    filename: str | None
    content_type: str | None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_email(self) -> bool:
        return (self.content_type or "").lower().startswith("message/rfc822")


@runtime_checkable
class SourceSearch(Protocol):
    """One connected place to look (live: it calls the provider)."""

    source: SearchSource
    place: str

    def find(self, request: SearchRequest) -> list[FoundFile]: ...


# --------------------------------------------------------------------------- mailboxes


def _mail_file(source: SearchSource, place: str, item: Any, provider: str) -> FoundFile:
    provenance: dict[str, Any] = {"source": "email", "provider": provider, "messageId": item.provider_id}
    if getattr(item, "thread_id", None):
        provenance["threadId"] = item.thread_id
    if getattr(item, "folder", None):
        provenance["folder"] = item.folder
    if getattr(item, "received_at", None):
        provenance["receivedAt"] = item.received_at.isoformat()
    return FoundFile(source, place, item.raw, "message.eml", "message/rfc822", provenance)


class MailboxSearch:
    """Search 1 (current email) or 2 (historical email, ``historical=True``) in one connected mailbox."""

    def __init__(self, connector: Any, *, place: str = "your email", provider: str = "email",
                 historical: bool = False, limit: int = MAX_FILES_PER_PLACE, history_days: int = HISTORY_DAYS) -> None:
        self.connector = connector
        self.place = place
        self.provider = provider
        self.historical = historical
        self.source = SearchSource.HISTORICAL_EMAIL if historical else SearchSource.CURRENT_EMAIL
        self.limit = limit
        self.history_days = history_days

    def query(self, request: SearchRequest) -> Any:
        from backoffice.connectors.mail_search import MailQuery, MailTerm, TermKind

        senders = [MailTerm(TermKind.FROM, d) for d in request.supplier_domains if d]
        strong = [MailTerm(TermKind.TEXT, t) for t in (request.invoice_number, *amount_words(request.amount)[:2]) if t]
        if not self.historical:
            named = [MailTerm(TermKind.TEXT, request.supplier_name)] if request.supplier_name else []
            if not senders and not strong and not named:
                return None
            return MailQuery(request.window_start, request.window_end, any_of=(*senders, *strong, *named),
                             limit=self.limit)
        # The months before: only what names this document (its number or amount), from this supplier.
        if not strong or not (senders or request.supplier_name):
            return None
        until = request.window_start - timedelta(days=1)
        since = until - timedelta(days=self.history_days)
        supplier = senders[0] if senders else MailTerm(TermKind.TEXT, request.supplier_name or "")
        return MailQuery(since, until, any_of=tuple(strong), all_of=(supplier,), limit=self.limit)

    def find(self, request: SearchRequest) -> list[FoundFile]:
        query = self.query(request)
        if query is None:
            return []
        return [_mail_file(self.source, self.place, item, self.provider)
                for item in self.connector.search_messages(query)]


class RecurringMailSearch:
    """Search 6 (recurring history): the supplier's usual way of sending its invoices, in the window."""

    source = SearchSource.RECURRING_SEQUENCE

    def __init__(self, connector: Any, *, place: str, provider: str = "email",
                 limit: int = MAX_FILES_PER_PLACE) -> None:
        self.connector = connector
        self.place = place
        self.provider = provider
        self.limit = limit

    def find(self, request: SearchRequest) -> list[FoundFile]:
        from backoffice.connectors.mail_search import MailQuery, MailTerm, TermKind

        pattern = request.pattern
        if pattern is None or not pattern.senders:
            return []
        senders = tuple(MailTerm(TermKind.FROM, s) for s in pattern.senders[:3])
        words = tuple(MailTerm(TermKind.SUBJECT, w) for w in pattern.subject_words[:3])
        files = tuple(MailTerm(TermKind.FILENAME, w) for w in pattern.filename_words[:1])
        query = MailQuery(request.window_start, request.window_end, any_of=senders, all_of=(*words, *files),
                          attachment=True, limit=self.limit)
        return [_mail_file(self.source, self.place, item, self.provider)
                for item in self.connector.search_messages(query)]


# --------------------------------------------------------------------------- cloud storage


class CloudStorageSearch:
    """Search 3: Google Drive or OneDrive / SharePoint (connectors.cloud_storage)."""

    source = SearchSource.FILES

    def __init__(self, connector: Any, *, place: str, limit: int = MAX_FILES_PER_PLACE) -> None:
        self.connector = connector
        self.place = place
        self.limit = limit

    def find(self, request: SearchRequest) -> list[FoundFile]:
        from backoffice.connectors.base import ProviderError
        from backoffice.connectors.cloud_storage import FileQuery

        terms = request.terms()
        if not terms:
            return []
        since = datetime.combine(request.window_start, time.min, tzinfo=timezone.utc)
        out: list[FoundFile] = []
        for file in self.connector.search(FileQuery(terms, since=since, limit=self.limit)):
            try:
                download = self.connector.download(file)
            except ProviderError:
                continue  # that one file cannot be read (no download right): the others still count
            if download is not None:
                out.append(FoundFile(self.source, self.place, download.data, download.filename, download.content_type,
                                     download.provenance()))
        return out


# --------------------------------------------------------------------------- accounting software


class AccountingSearch:
    """Search 5: what the accounting software (TOConline, Moloni, InvoiceXpress) has recorded."""

    source = SearchSource.ACCOUNTING_PLATFORM

    def __init__(self, connector: Any, *, place: str, limit: int = MAX_FILES_PER_PLACE) -> None:
        self.connector = connector
        self.place = place
        self.limit = limit

    def find(self, request: SearchRequest) -> list[FoundFile]:
        from backoffice.connectors.accounting import DocumentLookup

        if request.amount is None and not request.invoice_number:
            return []
        direction = request.direction if request.direction in self.connector.directions else None
        if direction is None:
            return []
        lookup = DocumentLookup(since=request.window_start, until=request.window_end, amount=request.amount,
                                currency=request.currency, number=request.invoice_number,
                                tax_id=request.supplier_tax_id if direction == "purchases" else None,
                                direction=direction, limit=self.limit)
        out: list[FoundFile] = []
        for doc in self.connector.search(lookup):
            got = self.connector.fetch(doc)
            if got is not None:
                out.append(FoundFile(self.source, self.place, got.data, got.filename, got.content_type,
                                     got.provenance()))
        return out


# --------------------------------------------------------------------------- supplier portals


class PortalSearch:
    """Search 4: the supplier's website through its adapter (connectors.portals), with the owner's sign-in there.

    The saved session is used first; when there is none, it expired, or the website signed it out, the stored
    password signs in again (once) and :attr:`session` holds the new session for the vault. ``sign_in=False``
    (a website that sends a one-time code at sign-in): only a saved session is used, so a search never sends the
    owner a code. A website that asks for a code is not searched this time; :attr:`challenge` keeps its request so
    the owner is asked for the code once (server/portals.py), as the daily sync would. A refused password needs
    the owner. Only the invoices that name this payment (its number or its amount) are downloaded; for a usual
    invoice of a varying amount, the ones issued in the window.
    """

    source = SearchSource.SUPPLIER_PORTAL

    def __init__(self, connector: Any, *, credentials: Any = None, session: Any = None, place: str,
                 limit: int = MAX_FILES_PER_PLACE, first: bool = False, connection_id: str | None = None,
                 clock: Callable[[], datetime] | None = None, sign_in: bool = True) -> None:
        self.connector = connector
        self.credentials = credentials
        self.session = session
        self.place = place
        self.limit = limit
        self.first = first  # learned to hold this supplier's invoices: asked before the other places
        self.connection_id = connection_id
        self.signed_in = False  # True once this search made a new session (to keep in the vault)
        self.retrieved: list[str] = []  # the website's ids of the invoices it downloaded
        self.challenge: Any = None  # the website asked for a one-time code (the owner is asked for it once)
        self.may_sign_in = sign_in
        self._clock = clock

    def _sign_in(self) -> Any:
        from backoffice.connectors.base import ReconnectRequired, TransientError
        from backoffice.connectors.portals import AuthStatus

        if not self.may_sign_in:
            raise TransientError("portal_waiting_for_code")  # signing in would send the owner another code
        if self.credentials is None:
            raise ReconnectRequired("portal_no_credentials")
        result = self.connector.authenticate(self.credentials)
        if result.status is AuthStatus.AUTHENTICATED and result.session is not None:
            self.session, self.signed_in = result.session, True
            return result.session
        if result.status is AuthStatus.LOGIN_REQUIRED:
            raise ReconnectRequired("portal_login_required")
        if result.status is AuthStatus.MFA_REQUIRED:
            self.challenge = result.challenge
        raise TransientError(f"portal_{result.status.value}")  # a code to enter, or the website is down

    def _listed(self, request: SearchRequest) -> tuple[Any, list[Any]]:
        from backoffice.connectors.portals import SessionExpired

        session = self.session
        now = self._clock() if self._clock is not None else None
        if session is not None and now is not None and session.expires_at is not None and session.expires_at <= now:
            session = None
        if session is None:
            session = self._sign_in()
            return session, self.connector.list_invoices(session, request.window_start, request.window_end)
        try:
            return session, self.connector.list_invoices(session, request.window_start, request.window_end)
        except SessionExpired:
            session = self._sign_in()
            return session, self.connector.list_invoices(session, request.window_start, request.window_end)

    def find(self, request: SearchRequest) -> list[FoundFile]:
        session, refs = self._listed(request)
        number = re.sub(r"[^0-9A-Z]", "", (request.invoice_number or "").upper())
        out: list[FoundFile] = []
        for ref in refs:
            same_number = bool(number) and re.sub(r"[^0-9A-Z]", "", (ref.invoice_number or "").upper()) == number
            same_amount = request.amount is not None and ref.gross_amount is not None and \
                abs(ref.gross_amount) == abs(request.amount)
            usual = request.amount is None and not number  # a usual invoice of a varying amount: any in the window
            if not (same_number or same_amount or usual):
                continue
            doc = self.connector.retrieve_invoice(session, ref)
            self.retrieved.append(ref.portal_id)
            provenance = {"source": "portal", "provider": self.connector.supplier_key, "portalId": ref.portal_id,
                          "number": ref.invoice_number, "date": ref.issue_date.isoformat() if ref.issue_date else None}
            if self.connection_id:
                provenance["connectionId"] = self.connection_id
            if getattr(doc, "source_url", None):
                provenance["webUrl"] = doc.source_url
            out.append(FoundFile(self.source, self.place, doc.data, doc.filename or "invoice.pdf", doc.content_type,
                                 provenance))
            if len(out) >= self.limit:
                break
        return out


# --------------------------------------------------------------------------- running them (live)


@dataclass
class SearchRun:
    """What one round of searching for one document did: each attempt, and every file found."""

    request: SearchRequest
    attempts: list[dict[str, Any]] = field(default_factory=list)
    files: list[FoundFile] = field(default_factory=list)


def _outcome(exc: BaseException) -> tuple[str, str]:
    code = getattr(exc, "code", "") or ""
    timed_out = isinstance(exc, TimeoutError) or str(code).endswith("_timeout")
    return ("timed_out" if timed_out else "failed"), f"{type(exc).__name__}:{code}" if code else type(exc).__name__


def search_order(searches: Sequence[Any]) -> list[int]:
    """The order places are asked in: the ones learned to hold the supplier's invoices (``first``), then the
    spec's order (§22), stable. Indexes into ``searches``."""
    rank = {source: i for i, source in enumerate(SEARCH_ORDER)}
    return [i for i, _ in sorted(enumerate(searches), key=lambda pair: (
        not getattr(pair[1], "first", False), rank[pair[1].source], pair[0]))]


def run_searches(request: SearchRequest, searches: Sequence[SourceSearch], *,
                 clock: Callable[[], datetime], max_files: int = MAX_FILES_PER_PLACE) -> SearchRun:
    """Ask every place, in order (:func:`search_order`), and note each attempt. A place that fails is noted and
    the others are still asked: the engine decides later (a failed place means "look again later")."""
    ordered = [searches[i] for i in search_order(searches)]
    run = SearchRun(request)
    for search in ordered:
        started = clock()
        failure: str | None = None
        try:
            files = list(search.find(request))[:max_files]
            outcome = "found" if files else "nothing"
        except Exception as exc:  # noqa: BLE001 - one broken place never stops the others; recorded, never shown
            files, (outcome, failure) = [], _outcome(exc)
        attempt: dict[str, Any] = {"source": search.source.value, "place": search.place,
                                   "startedAt": started.isoformat(), "finishedAt": clock().isoformat(),
                                   "outcome": outcome, "found": len(files)}
        if failure:
            attempt["failure"] = failure
        if getattr(search, "first", False):
            attempt["first"] = True
        run.attempts.append(attempt)
        run.files += files
    return run


# --------------------------------------------------------------------------- applying what was recorded


class RecordedFailure(Exception):
    """A place that could not be searched when the search ran (its internal reason is in the record)."""


class RecordedSearch:
    """One place's recorded result, as the autopilot's ``EvidenceSearch``. ``look`` reads its files into the
    pipeline and returns the candidates they gave for the missing document (graded by the engine)."""

    def __init__(self, source: SearchSource, attempts: Sequence[Mapping[str, Any]], files: Sequence[Any],
                 look: Callable[[SearchSource, Sequence[Any]], Sequence[EvidenceCandidate]]) -> None:
        self.source = source
        self.attempts = tuple(attempts)
        self.files = tuple(files)
        self._look = look

    async def search(self, query: EvidenceQuery) -> Sequence[EvidenceCandidate]:
        worked = [a for a in self.attempts if a.get("outcome") in ("found", "nothing")]
        if not worked:  # every connection of this kind failed: the autopilot records it and looks again later
            if any(a.get("outcome") == "timed_out" for a in self.attempts):
                raise TimeoutError(self.source.value)
            raise RecordedFailure(self.source.value)
        return list(self._look(self.source, self.files)) if self.files else []


def run_recorded(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a coroutine that never waits (recorded searches, a synchronous authorisation) without an event loop:
    the engine applies events where none runs (and in the browser). Anything that would wait is a bug: refused."""
    if not inspect.iscoroutine(coro):
        raise TypeError("expected a coroutine")
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise RuntimeError("a recorded search must not wait")
