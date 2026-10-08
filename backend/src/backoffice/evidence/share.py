"""Mobile share-extension intake (§12).

WhatsApp, Gmail, Outlook, Safari, Chrome, Photos, Files → Share → Back Office.
The native extensions (iOS Share Extension, Android Share Intent) send one of
three payload kinds; this module decides what each one is and routes it:

* a URL → stored as URL evidence, then followed with link intelligence (§9);
* a file → sniffed: .eml goes to email ingestion, ZIP is expanded, PDFs,
  images, screenshots and structured files are stored as they are;
* text → stored as text evidence; any links inside are followed.

:meth:`ShareIntake.ingest_file` is the same file routing for other callers
(the §43 offline upload queue hands over email exports and archives).
The owner never picks a category (§11). Replies are short (§69).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from backoffice.countries import LazyPattern, pack_words
from backoffice.domain.models import EvidenceFormat, SourceKind, utcnow

from .archive import ZipLimits, expand_zip, register_members
from .email import EmailIngestor, EmailIngestResult, EmailParseError, extract_text_links
from .links import FetchResult, LinkFetcher, LinkOutcome, register_fetch
from .sniff import sniff
from .store import EvidenceRegistry, Registration

__all__ = [
    "GOT_IT",
    "ShareIntake",
    "ShareKind",
    "ShareOutcome",
    "SharePayload",
    "ShareRoute",
    "route_for",
]

GOT_IT = "Got it."
_CANT_READ = "I can't read this kind of file yet."
_TOO_LARGE = "This file is too large to send."
_NOTHING = "There was nothing to save."

_SCREENSHOT_NAME = LazyPattern(lambda: (  # a pack's own words in "share.screenshot" (Portugal's "captura de ecrã")
    r"screenshot|screen shot|captura de pantalla|bildschirmfoto|capture d.[ée]cran|schermata"
    + "".join(f"|{w}" for w in pack_words("share.screenshot"))),
    re.IGNORECASE,
)
_URL_ONLY = re.compile(r"^\s*(https?://\S+)\s*$", re.IGNORECASE)


class ShareKind(str, Enum):
    URL = "url"
    FILE = "file"
    TEXT = "text"


class ShareRoute(str, Enum):
    LINK = "link"  # follow the link (§9)
    EMAIL = "email"  # .eml export (§8)
    DOCUMENT = "document"  # PDF, XML/UBL/SAF-T, CSV, XLSX, JSON, HTML
    IMAGE = "image"  # photo of a receipt
    SCREENSHOT = "screenshot"
    ARCHIVE = "archive"  # ZIP (e.g. a WhatsApp chat export)
    TEXT = "text"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class SharePayload:
    """What the native share extension sends."""

    kind: ShareKind
    url: str | None = None
    text: str | None = None
    data: bytes | None = field(default=None, repr=False)
    filename: str | None = None
    mime_type: str | None = None
    source_app: str | None = None  # e.g. "net.whatsapp.WhatsApp", "com.google.android.gm"
    is_screenshot: bool = False  # set natively (iOS photo subtype, Android Screenshots album)
    shared_at: datetime | None = None

    @classmethod
    def for_url(cls, url: str, **kw: Any) -> SharePayload:
        return cls(ShareKind.URL, url=url, **kw)

    @classmethod
    def for_file(cls, data: bytes, filename: str | None = None, mime_type: str | None = None,
                 **kw: Any) -> SharePayload:
        return cls(ShareKind.FILE, data=data, filename=filename, mime_type=mime_type, **kw)

    @classmethod
    def for_text(cls, text: str, **kw: Any) -> SharePayload:
        return cls(ShareKind.TEXT, text=text, **kw)


@dataclass(frozen=True)
class ShareOutcome:
    route: ShareRoute
    owner_message: str
    registrations: tuple[Registration, ...] = ()
    email: EmailIngestResult | None = None
    fetches: tuple[FetchResult, ...] = ()
    pending_links: tuple[str, ...] = ()  # links to follow later (no fetcher configured)
    skipped: tuple[str, ...] = ()  # internal codes for archive members we could not read

    @property
    def accepted(self) -> bool:
        return self.route is not ShareRoute.UNSUPPORTED and bool(self.registrations or self.email)


def _share_url(payload: SharePayload) -> str | None:
    if payload.kind is ShareKind.URL:
        return (payload.url or "").strip() or None
    if payload.kind is ShareKind.TEXT and payload.text:
        m = _URL_ONLY.match(payload.text)
        return m.group(1) if m else None
    return None


def route_for(payload: SharePayload) -> ShareRoute:
    """Pure classification of a payload; no storage, no network."""
    if _share_url(payload):
        return ShareRoute.LINK
    if payload.kind is ShareKind.TEXT:
        return ShareRoute.TEXT if (payload.text or "").strip() else ShareRoute.UNSUPPORTED
    if not payload.data:
        return ShareRoute.UNSUPPORTED
    found = sniff(payload.data, declared_type=payload.mime_type, filename=payload.filename)
    return _route_for_format(found.format, payload.filename, payload.is_screenshot)


def _route_for_format(fmt: EvidenceFormat | None, filename: str | None, is_screenshot: bool) -> ShareRoute:
    if fmt is None:
        return ShareRoute.UNSUPPORTED
    if fmt is EvidenceFormat.EML:
        return ShareRoute.EMAIL
    if fmt is EvidenceFormat.ZIP:
        return ShareRoute.ARCHIVE
    if fmt is EvidenceFormat.IMAGE:
        named = bool(filename and _SCREENSHOT_NAME.search(filename))
        return ShareRoute.SCREENSHOT if is_screenshot or named else ShareRoute.IMAGE
    if fmt is EvidenceFormat.TEXT:
        return ShareRoute.TEXT
    return ShareRoute.DOCUMENT


class ShareIntake:
    """Accepts one shared item for a tenant and stores it as evidence (§12)."""

    def __init__(
        self,
        registry: EvidenceRegistry,
        *,
        fetcher: LinkFetcher | None = None,
        email_ingestor: EmailIngestor | None = None,
        max_bytes: int = 50 * 1024 * 1024,
        zip_limits: ZipLimits | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.registry = registry
        self.fetcher = fetcher
        self.emails = email_ingestor or EmailIngestor(registry)
        self.max_bytes = max_bytes
        self.zip_limits = zip_limits or ZipLimits()
        self._clock = clock

    def accept(self, tenant_id: str, payload: SharePayload) -> ShareOutcome:
        at = payload.shared_at or self._clock()
        context = {"source_app": payload.source_app, "share_kind": payload.kind.value}
        url = _share_url(payload)
        if url:
            return self._link(tenant_id, url, at, context)
        if payload.kind is ShareKind.TEXT:
            return self._text(tenant_id, payload.text or "", at, context)
        if not payload.data:
            return ShareOutcome(ShareRoute.UNSUPPORTED, _NOTHING)
        if len(payload.data) > self.max_bytes:
            return ShareOutcome(ShareRoute.UNSUPPORTED, _TOO_LARGE)
        return self._file(tenant_id, payload, at, context)

    # ----------------------------------------------------------------- routes

    def _link(self, tenant_id: str, url: str, at: datetime, context: dict[str, Any]) -> ShareOutcome:
        reg = self._register_url(tenant_id, url, at, context)
        fetches, pending, regs = self._follow([url], tenant_id, {**context, "shared_url_evidence_id": reg.evidence.id})
        message = _link_message(fetches)
        return ShareOutcome(ShareRoute.LINK, message, (reg, *regs), fetches=fetches, pending_links=pending)

    def _text(self, tenant_id: str, text: str, at: datetime, context: dict[str, Any]) -> ShareOutcome:
        if not text.strip():
            return ShareOutcome(ShareRoute.UNSUPPORTED, _NOTHING)
        data = text.encode("utf-8")
        if len(data) > self.max_bytes:
            return ShareOutcome(ShareRoute.UNSUPPORTED, _TOO_LARGE)
        reg = self.registry.register(
            data, tenant_id=tenant_id, source_kind=SourceKind.MOBILE_SHARE, format=EvidenceFormat.TEXT,
            mime_type="text/plain; charset=utf-8", retrieved_at=at, context=context,
        )
        urls = list(dict.fromkeys(u for u, _ in extract_text_links(text)))
        fetches, pending, regs = self._follow(urls, tenant_id, {**context, "text_evidence_id": reg.evidence.id})
        message = _link_message(fetches) if fetches else GOT_IT
        return ShareOutcome(ShareRoute.TEXT, message, (reg, *regs), fetches=fetches, pending_links=pending)

    def _file(self, tenant_id: str, payload: SharePayload, at: datetime, context: dict[str, Any]) -> ShareOutcome:
        assert payload.data is not None
        return self.ingest_file(tenant_id, payload.data, filename=payload.filename, mime_type=payload.mime_type,
                                at=at, context=context, is_screenshot=payload.is_screenshot)

    def ingest_file(
        self,
        tenant_id: str,
        data: bytes,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        source_kind: SourceKind = SourceKind.MOBILE_SHARE,
        at: datetime | None = None,
        context: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        is_screenshot: bool = False,
    ) -> ShareOutcome:
        """Store one file by what its bytes are: email export, archive, image or document.

        The first registration of the outcome is the file itself (for an email
        export, the message); archive members and attachments follow.
        """
        at = at or self._clock()
        ctx = dict(context or {})
        found = sniff(data, declared_type=mime_type, filename=filename)
        route = _route_for_format(found.format, filename, is_screenshot)
        if route is ShareRoute.UNSUPPORTED or found.format is None:
            return ShareOutcome(ShareRoute.UNSUPPORTED, _CANT_READ)
        if route is ShareRoute.EMAIL:
            return self._email(tenant_id, data, filename, source_kind, at, ctx)
        fmt = EvidenceFormat.SCREENSHOT if route is ShareRoute.SCREENSHOT else found.format
        reg = self.registry.register(
            data, tenant_id=tenant_id, source_kind=source_kind, format=fmt,
            mime_type=found.mime_type, filename=filename, retrieved_at=at,
            metadata={**(metadata or {}), "declared_mime_type": found.declared_mime_type,
                      "type_mismatch": found.mismatch},
            context=ctx,
        )
        if route is ShareRoute.ARCHIVE:
            return self._archive(tenant_id, data, reg, source_kind, at, ctx)
        return ShareOutcome(route, GOT_IT, (reg,))

    def _email(self, tenant_id: str, data: bytes, filename: str | None, source_kind: SourceKind, at: datetime,
               context: dict[str, Any]) -> ShareOutcome:
        try:
            result = self.emails.ingest(
                data, tenant_id=tenant_id, source_kind=source_kind,
                message_format=EvidenceFormat.EML, received_at=at, filename=filename, context=context,
            )
        except EmailParseError:
            return ShareOutcome(ShareRoute.UNSUPPORTED, _CANT_READ)
        return ShareOutcome(ShareRoute.EMAIL, GOT_IT, (result.message,), email=result)

    def _archive(self, tenant_id: str, data: bytes, archive: Registration, source_kind: SourceKind, at: datetime,
                 context: dict[str, Any]) -> ShareOutcome:
        members, skipped = register_members(self.registry, expand_zip(data, self.zip_limits), archive,
                                            source_kind=source_kind, at=at, context=context)
        return ShareOutcome(ShareRoute.ARCHIVE, GOT_IT, (archive, *members), skipped=tuple(skipped))

    # ----------------------------------------------------------------- links

    def _register_url(self, tenant_id: str, url: str, at: datetime, context: Mapping[str, Any]) -> Registration:
        """The shared link itself is evidence of what the owner sent (§7: URL)."""
        return self.registry.register(
            url.encode("utf-8"), tenant_id=tenant_id, source_kind=SourceKind.MOBILE_SHARE,
            format=EvidenceFormat.URL, mime_type="text/uri-list", original_url=url, retrieved_at=at,
            metadata={"host": urlsplit(url).hostname if _parsable(url) else None}, context=dict(context),
        )

    def _follow(
        self, urls: list[str], tenant_id: str, context: dict[str, Any]
    ) -> tuple[tuple[FetchResult, ...], tuple[str, ...], list[Registration]]:
        if self.fetcher is None:
            return (), tuple(urls), []
        fetches, regs = [], []
        for url in urls[:10]:
            result = self.fetcher.fetch(url)
            fetches.append(result)
            regs += register_fetch(result, self.registry, tenant_id=tenant_id,
                                   source_kind=SourceKind.MOBILE_SHARE, context=context)
        return tuple(fetches), tuple(urls[10:]), regs


def _parsable(url: str) -> bool:
    try:
        _ = urlsplit(url).hostname  # raises on malformed ports and brackets
    except ValueError:
        return False
    return True


def _link_message(fetches: tuple[FetchResult, ...]) -> str:
    """The one thing the owner needs to hear about the shared link, if anything."""
    for outcome in (LinkOutcome.MFA_REQUIRED, LinkOutcome.LOGIN_REQUIRED, LinkOutcome.BLOCKED_UNSAFE):
        for result in fetches:
            if result.outcome is outcome and result.owner_message:
                return result.owner_message
    return GOT_IT
