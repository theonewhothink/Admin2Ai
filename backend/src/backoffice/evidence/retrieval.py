"""One way to follow an invoice link, wherever the product runs (§9, §10).

The engine's Retrieval agent asks a :class:`LinkSource` what is behind a link
and gets a :class:`~backoffice.evidence.links.FetchResult` back (a file, a
rendered page, a sign-in wall, a blocked or expired link), or ``None`` when the
link has not been opened yet and must wait. Three implementations share that
one code path:

* :class:`PortalLinks`: the demo's simulated supplier portal adapters (a fixed
  dictionary of documents), no network at all (the browser build);
* the production server's recorded results (``server/links.py``): links are
  opened with :class:`~backoffice.evidence.links.LinkFetcher` *before* an event
  is recorded, the event keeps what came back, and every apply (live and on
  replay) reads through the recording, so a replay never opens a link;
* :class:`NoLinks`: nothing is ever opened (every link waits).

This module also says which links in an email the engine follows
(:func:`email_invoice_links`, so the server fetches exactly those in advance)
and whether a reply points at an invoice sent earlier in its thread
(:func:`refers_to_earlier_invoice`), whose message the server then fetches
through the mailbox connector.

Standard library only (the browser build has no network libraries).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from backoffice.domain.models import EvidenceFormat, utcnow

from .email import EmailIngestResult, ParsedEmail
from .links import FetchRecord, FetchResult, LinkOutcome
from .store import sha256_hex

__all__ = [
    "EXPIRED_REASONS",
    "MAX_LINKS_PER_MESSAGE",
    "LinkSource",
    "NoLinks",
    "PortalLinks",
    "all_invoice_links",
    "email_invoice_links",
    "refers_to_earlier_invoice",
]

MAX_LINKS_PER_MESSAGE = 5  # invoice links followed per email (the best ranked first)
EXPIRED_REASONS = frozenset({"http_404", "http_410"})


@runtime_checkable
class LinkSource(Protocol):
    """What is behind a link: a :class:`FetchResult`, or ``None`` when it has not been opened (it waits)."""

    def fetch(self, url: str, *, supplier_name: str | None = None) -> FetchResult | None: ...


class NoLinks:
    """Nothing is ever opened: every link waits."""

    def fetch(self, url: str, *, supplier_name: str | None = None) -> FetchResult | None:
        return None


class PortalLinks:
    """The demo's supplier portal adapters (§10): documents behind known links, no network.

    ``pages`` maps a link to an object with ``data``, ``filename``,
    ``content_type`` and ``portal`` (the orchestrator's ``PortalDocument``).
    """

    def __init__(self, pages: Mapping[str, Any], clock: Callable[[], datetime] = utcnow) -> None:
        self.pages = pages
        self._clock = clock

    def fetch(self, url: str, *, supplier_name: str | None = None) -> FetchResult | None:
        page = self.pages.get(url)
        if page is None:
            return None
        fmt = EvidenceFormat.UBL if page.content_type.endswith("xml") else EvidenceFormat.TEXT
        record = FetchRecord(original_url=url, final_url=url, redirect_chain=(), retrieved_at=self._clock(),
                             sha256=sha256_hex(page.data), content_type=page.content_type,
                             declared_content_type=page.content_type, status_code=None, size=len(page.data))
        return FetchResult(LinkOutcome.DOWNLOADED, record, page.data, fmt, page.filename, supplier=supplier_name,
                           portal=page.portal)


# --------------------------------------------------------------------------- which links an email gives


def email_invoice_links(parsed: ParsedEmail) -> list[str]:
    """The invoice links of one message the engine follows, best first (at most ``MAX_LINKS_PER_MESSAGE``)."""
    return [link.url for link in parsed.invoice_links[:MAX_LINKS_PER_MESSAGE]]


def all_invoice_links(result: EmailIngestResult) -> Iterator[str]:
    """Every link the engine follows in an ingested email and the emails attached to it."""
    yield from email_invoice_links(result.parsed)
    for nested in result.attached_emails:
        yield from all_invoice_links(nested)


# --------------------------------------------------------------------------- "see the invoice I sent on the 3rd"

_DOCUMENT = r"(?:invoices?|receipts?|bills?|faturas?|facturas?|recibos?|fatura-recibo)"
_SENT = (r"(?:sent|send|resent|forwarded|attached|enviei|envi[aá]mos|enviad[ao]s?|mandei|mand[aá]mos|reenviei"
         r"|anexei|em anexo|seguiu|segue)")
_EARLIER_WORDS = r"(?:earlier|before|previous(?:ly)?|last (?:week|month)|anterior(?:mente)?|antes|below|abaixo)"
_EARLIER = re.compile(
    rf"\b{_DOCUMENT}\b[^.\n]{{0,80}}?\b{_SENT}\b"  # "the invoice I sent on the 3rd", "a fatura que enviei"
    rf"|\b{_SENT}\b[^.\n]{{0,60}}?\b{_DOCUMENT}\b[^.\n]{{0,60}}?\b{_EARLIER_WORDS}\b"  # "sent you the invoice earlier"
    rf"|\b(?:see|ver|veja|consulte|check)\b[^.\n]{{0,40}}?\b{_DOCUMENT}\b[^.\n]{{0,60}}?\b{_EARLIER_WORDS}\b",
    re.IGNORECASE,
)
_REPLY_SUBJECT = re.compile(r"^\s*(?:re|res|aw|sv|fw|fwd|enc|rv|tr)\s*:", re.IGNORECASE)
_QUOTE_START = re.compile(
    r"^\s*(?:on .{0,200} wrote:|em .{0,200} escreveu:|-{2,}\s*original message|de:\s|from:\s)", re.IGNORECASE)


def _own_words(text: str) -> str:
    """The reply's own lines: quoted history ("> ...", "On ... wrote:") is cut off."""
    lines: list[str] = []
    for line in text.splitlines():
        if _QUOTE_START.match(line):
            break
        if not line.lstrip().startswith(">"):
            lines.append(line)
    return "\n".join(lines)


def refers_to_earlier_invoice(parsed: ParsedEmail) -> bool:
    """A reply without a document that points at one sent earlier in its thread (§8: "previous attachments").

    It is a reply (thread headers or a "Re:" subject), carries no attachment
    of its own, and its own words (not the quoted history) talk about an
    invoice or receipt that was sent before.
    """
    thread = parsed.thread
    if not (thread.in_reply_to or thread.references or _REPLY_SUBJECT.match(parsed.subject or "")):
        return False
    if parsed.attached_emails or any(not part.inline for part in parsed.attachments):
        return False
    body = parsed.text_body
    if not body.strip() and parsed.html_body:
        from .html_signals import html_to_text

        body = html_to_text(parsed.html_body, max_chars=20_000)
    return bool(_EARLIER.search(_own_words(body)[:20_000]))
