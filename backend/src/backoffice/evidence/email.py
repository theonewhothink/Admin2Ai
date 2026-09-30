"""Email evidence: RFC 822 / .eml / Gmail raw messages (§7, §8, §9 step 1).

Reads everything §8 lists: plain and HTML bodies, inline images (``cid:``),
attachments, attached emails (``message/rfc822``, parsed recursively), ZIP
attachments (expanded safely), thread headers and every link, including
"View invoice" / "Ver fatura" buttons and Outlook VML buttons. Links are ranked
by how likely they lead to an invoice; nothing is fetched here (see
:mod:`backoffice.evidence.links`).

:class:`EmailIngestor` stores the original message and every file inside it
as immutable evidence (§55). Anything we cannot read is reported as skipped;
it is still preserved inside the original message. A malformed part never
stops the message, and a malformed message never raises anything but
:class:`EmailParseError`: in a mailbox sync one poison message must not block
every message after it.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.parser import BytesParser
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from backoffice.domain.models import Evidence, EvidenceFormat, SourceKind

from .archive import ArchiveExpansion, ZipLimits, expand_zip
from .domains import registrable_domain, to_ascii_host
from .html_signals import analyze_html, normalize_space
from .sniff import sniff
from .store import EvidenceRegistry, Registration

__all__ = [
    "AttachedEmail",
    "EmailAddress",
    "EmailIngestResult",
    "EmailIngestor",
    "EmailLimits",
    "EmailParseError",
    "IngestedFile",
    "LinkCandidate",
    "MailPart",
    "ParsedEmail",
    "SkippedPart",
    "ThreadInfo",
    "decode_gmail_raw",
    "expand_zip",
    "extract_links",
    "extract_text_links",
    "is_invoice_context",
    "parse_eml",
    "parse_gmail_raw",
    "score_link",
]

_REFOLD_NONE = policy.default.clone(refold_source="none")
# Ways to write an attached message back to bytes, most faithful first.
_SERIALISE_POLICIES = (_REFOLD_NONE, _REFOLD_NONE.clone(utf8=True), policy.compat32)


class EmailParseError(ValueError):
    """The input is not a message we can read (empty, oversize, bad encoding)."""


@dataclass(frozen=True)
class EmailLimits:
    max_bytes: int = 64 * 1024 * 1024
    max_depth: int = 3  # attached email inside attached email inside the original
    max_parts: int = 500
    max_nesting: int = 64  # multipart containers inside one message


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class EmailAddress:
    name: str
    address: str

    @property
    def domain(self) -> str | None:
        if "@" not in self.address:
            return None
        raw = self.address.rsplit("@", 1)[1].strip().strip(">").lower()
        return to_ascii_host(raw) or raw or None


@dataclass(frozen=True)
class ThreadInfo:
    message_id: str | None
    in_reply_to: tuple[str, ...] = ()
    references: tuple[str, ...] = ()

    @property
    def root(self) -> str | None:
        """First message of the thread, as far as the headers tell."""
        if self.references:
            return self.references[0]
        if self.in_reply_to:
            return self.in_reply_to[0]
        return self.message_id


@dataclass(frozen=True)
class MailPart:
    """A non-body leaf: attachment or inline image."""

    path: str  # MIME position, e.g. "2.1"
    filename: str | None
    declared_type: str
    disposition: str | None
    content_id: str | None
    inline: bool
    data: bytes = field(repr=False)

    @property
    def sha256(self) -> str:
        return Evidence.hash_bytes(self.data)

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class LinkCandidate:
    url: str
    text: str
    button_like: bool
    score: int
    invoice_likely: bool
    reasons: tuple[str, ...]  # internal scoring codes, e.g. "text:invoice"
    source: str  # "html", "vml", "area" or "text"
    order: int


@dataclass(frozen=True)
class AttachedEmail:
    path: str
    raw: bytes = field(repr=False)
    parsed: ParsedEmail | None  # None when nested deeper than the limit


@dataclass(frozen=True)
class ParsedEmail:
    raw_sha256: str
    size: int
    subject: str
    sender: EmailAddress | None
    reply_to: tuple[EmailAddress, ...]
    to: tuple[EmailAddress, ...]
    cc: tuple[EmailAddress, ...]
    date: datetime | None
    thread: ThreadInfo
    text_body: str
    html_body: str
    attachments: tuple[MailPart, ...]
    attached_emails: tuple[AttachedEmail, ...]
    links: tuple[LinkCandidate, ...]
    authentication_results: tuple[str, ...] = ()
    bulk: bool = False  # List-Unsubscribe / Precedence: bulk
    truncated: bool = False  # part or nesting limit reached
    defects: int = 0
    unreadable: bool = False  # MIME structure could not be read; headers only (original still kept)
    unreadable_parts: tuple[tuple[str, str], ...] = ()  # (MIME path, internal reason) we could not read

    @property
    def sender_domain(self) -> str | None:
        return self.sender.domain if self.sender else None

    @property
    def inline_images(self) -> tuple[MailPart, ...]:
        return tuple(p for p in self.attachments if p.inline and p.declared_type.startswith("image/"))

    @property
    def invoice_links(self) -> tuple[LinkCandidate, ...]:
        return tuple(link for link in self.links if link.invoice_likely)


# --------------------------------------------------------------------------- link ranking

_INVOICE_WORDS = re.compile(
    r"\b(invoices?|bills?|billing|receipts?|statements?|credit[ -]?notes?|faturas?|facturas?"
    r"|fatura[s]?-recibo|recibos?|notas? de cr[ée]dito|extratos?|fattur[ae]|ricevut[ae]|factures?"
    r"|re[çc]us?|rechnung(en)?|quittung|beleg|documento fiscal|segunda via|2[ªa] via)\b",
    re.IGNORECASE,
)
_ACTION_WORDS = re.compile(
    r"\b(view|see|download|open|get|access|ver|veja|visualizar|visualize|descarregar|descarregue"
    r"|baixar|baixe|transferir|consultar|consulte|aceder|aceda|acessar|acesse|obter|abrir"
    r"|descargar|descargue|voir|t[ée]l[ée]charger|scarica(re)?|visualizza(re)?|herunterladen"
    r"|ansehen|pdf)\b",
    re.IGNORECASE,
)
_NEGATIVE_WORDS = re.compile(
    r"unsubscribe|opt[- ]?out|(cancelar|anular|remover) (a )?(sua )?subscri[çc][ãa]o|deixar de receber"
    r"|darse de baja|d[ée]sabonner|abbestellen|disiscriv|preferen|privac"
    r"|\bterms (of|and|&)|termos (de|e) |t[ée]rminos (de|y) |condi[çc][õo]es gerais"
    r"|in (your |the )?browser|view online|web version|vers[ãa]o (web|online)"
    r"|(no|em|en el) (browser|navegador)|\bhelp\b|ajuda|suporte|support|contact|contacto|contato"
    r"|\bfaq\b|forgot|password|palavra-passe"
    r"|\bapps?\b|aplica[çc][ãa]o|aplicaci[oó]n",  # "Download our app": a store, not a document
    re.IGNORECASE,
)
_INVOICE_PATH = re.compile(
    r"invoice|fatura|factura|fattura|billing|/bills?\b|receipt|recibo|document|download"
    r"|statement|extrato|/pdf|\.pdf\b",
    re.IGNORECASE,
)
_NEGATIVE_PATH = re.compile(r"unsubscribe|optout|opt-out|preferences|privacy|/terms", re.IGNORECASE)
_SOCIAL_HOSTS = (
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com",
    "tiktok.com", "pinterest.com", "apps.apple.com", "itunes.apple.com", "play.google.com",
    "wa.me", "t.me",
)  # fmt: skip
INVOICE_LIKELY_SCORE = 40
MAX_LINKS = 2_000  # link candidates read per message; more is hostile or a newsletter
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'` ]+", re.IGNORECASE)
_TRAILING = ".,;:!?'\"»›>"


def _host(url: str) -> str | None:
    try:
        return (urlsplit(url).hostname or "").lower() or None
    except ValueError:
        return None


def is_invoice_context(text: str | None) -> bool:
    """Does a subject or page title talk about an invoice (§9 "Your invoice is ready")?"""
    return bool(text and _INVOICE_WORDS.search(text[:1000]))


def score_link(
    url: str,
    text: str,
    *,
    button_like: bool = False,
    sender_domain: str | None = None,
    invoice_context: bool = False,
) -> tuple[int, tuple[str, ...]]:
    """Deterministic invoice-likelihood score and the reasons behind it.

    ``invoice_context`` (the subject is about an invoice) lifts a bare
    "Download" / "Descarregar" button or action link; it never rescues a
    negative (unsubscribe, app, help) or social link.
    """
    score, reasons = 0, []
    host = _host(url) or ""
    path = url.split("?", 1)[0]
    if _INVOICE_WORDS.search(text):
        score, reasons = score + 40, [*reasons, "text:invoice"]
    if _ACTION_WORDS.search(text):
        score, reasons = score + 15, [*reasons, "text:action"]
    if invoice_context and "text:invoice" not in reasons and (button_like or "text:action" in reasons):
        score, reasons = score + 20, [*reasons, "context:invoice"]
    if _INVOICE_PATH.search(path):
        score, reasons = score + 20, [*reasons, "url:invoice"]
    if path.lower().endswith(".pdf"):
        score, reasons = score + 30, [*reasons, "url:pdf"]
    if button_like:
        score, reasons = score + 10, [*reasons, "button"]
    if sender_domain and host and registrable_domain(host) == registrable_domain(sender_domain):
        score, reasons = score + 10, [*reasons, "sender_domain"]
    if _NEGATIVE_WORDS.search(text) or _NEGATIVE_PATH.search(path):
        score, reasons = score - 60, [*reasons, "negative"]
    if any(host == h or host.endswith("." + h) for h in _SOCIAL_HOSTS):
        score, reasons = score - 100, [*reasons, "social"]
    return score, tuple(reasons)


def _clean_url(url: str) -> str | None:
    url = url.strip()
    while url and url[-1] in _TRAILING + ")]}":
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    if not re.match(r"^https?://", url, re.IGNORECASE) or len(url) > 4096:
        return None
    return url if _host(url) else None


def extract_text_links(text: str, *, limit: int = MAX_LINKS) -> list[tuple[str, str]]:
    """``(url, context)`` pairs for URLs in plain text; context is the words before it.

    Linear in ``len(text)``: the context lookup never scans back more than 80
    characters, and at most ``limit`` URLs are returned.
    """
    found: list[tuple[str, str]] = []
    for m in _URL_IN_TEXT.finditer(text):
        if len(found) >= limit:
            break
        url = _clean_url(m.group())
        if url:
            window_start = max(0, m.start() - 80)
            line_start = text.rfind("\n", window_start, m.start()) + 1
            context = normalize_space(text[max(line_start, window_start) : m.start()])
            found.append((url, context))
    return found


def extract_links(
    html_body: str,
    text_body: str,
    *,
    sender_domain: str | None = None,
    context: str | None = None,
    max_links: int = MAX_LINKS,
) -> tuple[LinkCandidate, ...]:
    """Every http(s) link in the message, ranked most invoice-likely first.

    ``context`` is the subject: an invoice subject lifts bare "Download"
    buttons (§9). At most ``max_links`` candidates are read, HTML first.
    """
    raw: list[tuple[str, str, bool, str]] = []
    if html_body:
        signals = analyze_html(html_body)
        raw += [(a.href, a.text, a.button_like, "html" if a.source == "a" else a.source) for a in signals.anchors]
        raw += [(u, ctx, False, "text") for u, ctx in extract_text_links(signals.text, limit=max_links)]
    if text_body and len(raw) < max_links:
        raw += [(u, ctx, False, "text") for u, ctx in extract_text_links(text_body, limit=max_links)]
    invoice_context = is_invoice_context(context)
    best: dict[str, LinkCandidate] = {}
    for order, (href, text, button, source) in enumerate(raw[:max_links]):
        url = _clean_url(href)
        if url is None:
            continue
        score, reasons = score_link(url, text, button_like=button, sender_domain=sender_domain,
                                    invoice_context=invoice_context)
        candidate = LinkCandidate(url, text, button, score, score >= INVOICE_LIKELY_SCORE, reasons, source, order)
        previous = best.get(url)
        if previous is None or candidate.score > previous.score:
            best[url] = candidate if previous is None else _keep_order(candidate, previous.order)
    return tuple(sorted(best.values(), key=lambda c: (-c.score, c.order)))


def _keep_order(candidate: LinkCandidate, order: int) -> LinkCandidate:
    return LinkCandidate(candidate.url, candidate.text, candidate.button_like, candidate.score,
                         candidate.invoice_likely, candidate.reasons, candidate.source, order)


# --------------------------------------------------------------------------- parsing helpers


def _header(msg: Message, name: str) -> str | None:
    try:
        value = msg.get(name)
    except Exception:  # noqa: BLE001 - malformed headers must not stop ingestion
        return None
    return None if value is None else str(value)


def _addresses(msg: Message, name: str) -> tuple[EmailAddress, ...]:
    try:
        header = msg.get(name)
        parsed = getattr(header, "addresses", None) if header is not None else ()
        if parsed:
            return tuple(EmailAddress(a.display_name or "", a.addr_spec or "") for a in parsed if a.addr_spec)
    except Exception:  # noqa: BLE001
        pass
    raw = _header(msg, name)
    if not raw:
        return ()
    return tuple(EmailAddress(n, a) for n, a in getaddresses([raw]) if a)


def _date(msg: Message) -> datetime | None:
    raw = _header(msg, "Date")
    if not raw:
        return None
    try:
        value = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _message_ids(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    ids = re.findall(r"<([^<>\s]+)>", value)
    if ids:
        return tuple(ids)
    stripped = value.strip()
    return (stripped,) if stripped and " " not in stripped else ()


def _text_of(part: Message) -> str:
    """Decoded text; an unknown or hostile charset (``"utf\x00-8"``) falls back to UTF-8."""
    try:
        content = part.get_content()  # type: ignore[attr-defined]
        if isinstance(content, str):
            return content
    except (LookupError, ValueError, TypeError, KeyError, AttributeError, AssertionError):
        pass
    payload = part.get_payload(decode=True) or b""
    return payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)


def _clean_filename(name: str | None) -> str | None:
    if not name:
        return None
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c.isprintable()).strip()
    return name[:255] or None


def _serialise(message: Message) -> bytes | None:
    """Bytes of an attached message, or ``None`` when no generator can write it."""
    for candidate in _SERIALISE_POLICIES:
        try:
            return message.as_bytes(policy=candidate)
        except Exception:  # noqa: BLE001 - malformed headers/bodies make the generators raise
            continue
    return None


class _Walker:
    """Collects bodies and leaves of one message level (nested emails recurse)."""

    def __init__(self, limits: EmailLimits, depth: int) -> None:
        self.limits = limits
        self.depth = depth
        self.texts: list[str] = []
        self.htmls: list[str] = []
        self.parts: list[MailPart] = []
        self.emails: list[AttachedEmail] = []
        self.unreadable: list[tuple[str, str]] = []
        self.leaves = 0
        self.truncated = False

    def visit(self, part: Message, path: str) -> None:
        if self.leaves >= self.limits.max_parts or path.count(".") >= self.limits.max_nesting:
            self.truncated = True
            return
        ctype = part.get_content_type()
        if ctype in ("message/rfc822", "message/global"):
            self._guarded(self._nested, part, path, "unreadable_email")
        elif part.is_multipart():
            for i, sub in enumerate(part.get_payload() or [], start=1):
                if isinstance(sub, Message):
                    self.visit(sub, f"{path}.{i}" if path else str(i))
        else:
            self.leaves += 1
            self._guarded(lambda p, at: self._leaf(p, at, ctype), part, path or "1", "unreadable_part")

    def _guarded(self, read: Any, part: Message, path: str, reason: str) -> None:
        """One malformed part is reported and skipped; the rest of the message is still read."""
        try:
            read(part, path)
        except RecursionError:
            raise
        except Exception:  # noqa: BLE001 - hostile MIME must not stop ingestion
            self.unreadable.append((path, reason))

    def _nested(self, part: Message, path: str) -> None:
        self.leaves += 1
        payload = part.get_payload()
        inner = payload[0] if isinstance(payload, list) and payload else None
        if isinstance(inner, Message):
            raw = _serialise(inner)
        else:  # base64-encoded message/rfc822 (not allowed by RFC 2046, seen in the wild)
            raw = part.get_payload(decode=True) or b""
        if raw is None:
            self.unreadable.append((path, "unreadable_email"))
        elif raw:
            self._attach_email(raw, path)

    def _attach_email(self, raw: bytes, path: str) -> None:
        nested = None
        if self.depth < self.limits.max_depth:
            nested = _parse(raw, self.limits, self.depth + 1)
        self.emails.append(AttachedEmail(path, raw, nested))

    def _leaf(self, part: Message, path: str, ctype: str) -> None:
        disposition = part.get_content_disposition()
        filename = _clean_filename(part.get_filename())
        is_body = disposition != "attachment" and filename is None
        if is_body and ctype == "text/plain":
            self.texts.append(_text_of(part))
            return
        if is_body and ctype == "text/html":
            self.htmls.append(_text_of(part))
            return
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes) or not data:
            return
        if ctype == "application/octet-stream" and (filename or "").lower().endswith(".eml"):
            self._attach_email(data, path)
            return
        cid = _header(part, "Content-ID")
        cid = cid.strip().strip("<>").strip() if cid else None
        inline = disposition == "inline" or (bool(cid) and disposition != "attachment")
        self.parts.append(MailPart(path, filename, ctype, disposition, cid or None, inline, data))


def _parse(raw: bytes, limits: EmailLimits, depth: int) -> ParsedEmail:
    """Full parse; hostile nesting or structure degrades to headers only, then to nothing."""
    try:
        return _parse_full(raw, limits, depth)
    except Exception:  # noqa: BLE001 - includes RecursionError from hostile nesting
        pass
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
        return _build(raw, msg, _Walker(limits, depth), defects=0, unreadable=True)
    except Exception:  # noqa: BLE001
        return _build(raw, Message(), _Walker(limits, depth), defects=0, unreadable=True)


def _parse_full(raw: bytes, limits: EmailLimits, depth: int) -> ParsedEmail:
    msg = message_from_bytes(raw, policy=policy.default)
    walker = _Walker(limits, depth)
    walker.visit(msg, "")
    defects = sum(len(getattr(p, "defects", ())) for p in msg.walk())
    return _build(raw, msg, walker, defects=defects, unreadable=False)


def _build(raw: bytes, msg: Message, walker: _Walker, *, defects: int, unreadable: bool) -> ParsedEmail:
    sender = next(iter(_addresses(msg, "From")), None)
    text_body = "\n\n".join(t for t in walker.texts if t.strip())
    html_body = "\n".join(walker.htmls)
    sender_domain = sender.domain if sender else None
    subject = normalize_space(_header(msg, "Subject") or "")
    referenced = html_body.lower()
    parts = tuple(
        replace(p, inline=True) if p.content_id and f"cid:{p.content_id.lower()}" in referenced else p
        for p in walker.parts
    )
    return ParsedEmail(
        raw_sha256=Evidence.hash_bytes(raw),
        size=len(raw),
        subject=subject,
        sender=sender,
        reply_to=_addresses(msg, "Reply-To"),
        to=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        date=_date(msg),
        thread=ThreadInfo(
            message_id=next(iter(_message_ids(_header(msg, "Message-ID"))), None),
            in_reply_to=_message_ids(_header(msg, "In-Reply-To")),
            references=_message_ids(_header(msg, "References")),
        ),
        text_body=text_body,
        html_body=html_body,
        attachments=parts,
        attached_emails=tuple(walker.emails),
        links=extract_links(html_body, text_body, sender_domain=sender_domain, context=subject),
        authentication_results=_all_headers(msg, "Authentication-Results"),
        bulk=_header(msg, "List-Unsubscribe") is not None
        or (_header(msg, "Precedence") or "").strip().lower() in ("bulk", "list"),
        truncated=walker.truncated or unreadable,
        defects=defects,
        unreadable=unreadable,
        unreadable_parts=tuple(walker.unreadable),
    )


def _all_headers(msg: Message, name: str) -> tuple[str, ...]:
    try:
        return tuple(str(v) for v in (msg.get_all(name) or ()))
    except Exception:  # noqa: BLE001 - a malformed header is left out, never fatal
        return ()


def parse_eml(raw: bytes, limits: EmailLimits | None = None) -> ParsedEmail:
    """Parse an RFC 822 message (a .eml file or a mailbox download)."""
    limits = limits or EmailLimits()
    if not isinstance(raw, (bytes, bytearray)) or not raw.strip():
        raise EmailParseError("empty message")
    if len(raw) > limits.max_bytes:
        raise EmailParseError("message exceeds the size limit")
    return _parse(bytes(raw), limits, 1)


def decode_gmail_raw(raw: str | bytes) -> bytes:
    """Decode the ``raw`` field of Gmail ``messages.get?format=raw`` (base64url)."""
    text = raw.decode("ascii") if isinstance(raw, (bytes, bytearray)) else raw
    text = "".join(text.split())
    if not text or not re.fullmatch(r"[A-Za-z0-9_\-]+=*", text):
        raise EmailParseError("not base64url message data")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise EmailParseError("not base64url message data") from exc


def parse_gmail_raw(raw: str | bytes, limits: EmailLimits | None = None) -> ParsedEmail:
    return parse_eml(decode_gmail_raw(raw), limits)


# --------------------------------------------------------------------------- ingestion


@dataclass(frozen=True)
class IngestedFile:
    registration: Registration
    path: str  # MIME path, plus "/member" for files inside a ZIP
    filename: str | None
    inline: bool
    type_mismatch: bool  # declared type disagreed with the bytes (§26 signal)

    @property
    def evidence(self) -> Evidence:
        return self.registration.evidence


@dataclass(frozen=True)
class SkippedPart:
    path: str
    filename: str | None
    reason: str  # internal code: "unsupported", "archive:encrypted", ...


@dataclass(frozen=True)
class EmailIngestResult:
    message: Registration
    parsed: ParsedEmail
    files: tuple[IngestedFile, ...]
    attached_emails: tuple[EmailIngestResult, ...]
    skipped: tuple[SkippedPart, ...]

    @property
    def links(self) -> tuple[LinkCandidate, ...]:
        return self.parsed.links

    def all_evidence(self) -> list[Evidence]:
        """Message, files and attached emails (depth first), without duplicates."""
        seen: dict[str, Evidence] = {self.message.evidence.id: self.message.evidence}
        for f in self.files:
            seen.setdefault(f.evidence.id, f.evidence)
        for nested in self.attached_emails:
            for ev in nested.all_evidence():
                seen.setdefault(ev.id, ev)
        return list(seen.values())


def _message_metadata(parsed: ParsedEmail) -> dict[str, Any]:
    return {
        "subject": parsed.subject[:500],
        "from": parsed.sender.address if parsed.sender else None,
        "sender_domain": parsed.sender_domain,
        "date": parsed.date.isoformat() if parsed.date else None,
        "message_id": parsed.thread.message_id,
        "in_reply_to": list(parsed.thread.in_reply_to),
        "references": list(parsed.thread.references[:50]),
        "thread_root": parsed.thread.root,
        "attachments": len(parsed.attachments),
        "attached_emails": len(parsed.attached_emails),
        "invoice_links": [link.url for link in parsed.invoice_links[:10]],
        "bulk": parsed.bulk,
        "unreadable": parsed.unreadable,
    }


@dataclass(frozen=True)
class _Arrival:
    """Where one message level came from; shared by everything inside it."""

    tenant_id: str
    source_kind: SourceKind
    received_at: datetime | None
    parent: Mapping[str, Any]
    depth: int


@dataclass
class _Collected:
    files: list[IngestedFile] = field(default_factory=list)
    skipped: list[SkippedPart] = field(default_factory=list)
    nested: list[EmailIngestResult] = field(default_factory=list)


class EmailIngestor:
    """Stores a message and everything inside it as evidence (§8, §55)."""

    def __init__(
        self,
        registry: EvidenceRegistry,
        *,
        limits: EmailLimits | None = None,
        zip_limits: ZipLimits | None = None,
        expand_archives: bool = True,
    ) -> None:
        self.registry = registry
        self.limits = limits or EmailLimits()
        self.zip_limits = zip_limits or ZipLimits()
        self.expand_archives = expand_archives

    def ingest_gmail_raw(self, raw: str | bytes, **kwargs: Any) -> EmailIngestResult:
        return self.ingest(decode_gmail_raw(raw), **kwargs)

    def ingest(
        self,
        raw: bytes,
        *,
        tenant_id: str,
        source_kind: SourceKind = SourceKind.EMAIL,
        message_format: EvidenceFormat = EvidenceFormat.EMAIL,
        received_at: datetime | None = None,
        filename: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> EmailIngestResult:
        """Parse and store ``raw``; returns every evidence record created or found."""
        parsed = parse_eml(raw, self.limits)
        arrival = _Arrival(tenant_id, source_kind, received_at, {}, 1)
        return self._ingest_parsed(bytes(raw), parsed, arrival, message_format, filename, dict(context or {}))

    def _ingest_parsed(
        self,
        raw: bytes,
        parsed: ParsedEmail,
        arrival: _Arrival,
        message_format: EvidenceFormat,
        filename: str | None,
        context: dict[str, Any],
    ) -> EmailIngestResult:
        message = self.registry.register(
            raw, tenant_id=arrival.tenant_id, source_kind=arrival.source_kind, format=message_format,
            mime_type="message/rfc822", filename=filename, retrieved_at=arrival.received_at,
            metadata=_message_metadata(parsed), context=context,
        )
        inner = _Arrival(arrival.tenant_id, arrival.source_kind, arrival.received_at,
                         {"parent_evidence_id": message.evidence.id}, arrival.depth)
        out = _Collected()
        for part in parsed.attachments:
            self._ingest_part(part, inner, out)
        for attached in parsed.attached_emails:
            if attached.parsed is None:
                out.skipped.append(SkippedPart(attached.path, None, "nested_too_deep"))
                continue
            child = _Arrival(inner.tenant_id, inner.source_kind, inner.received_at, inner.parent, inner.depth + 1)
            out.nested.append(self._ingest_parsed(attached.raw, attached.parsed, child, EvidenceFormat.EML,
                                                  None, {**inner.parent, "part": attached.path}))
        for part_path, reason in parsed.unreadable_parts:
            out.skipped.append(SkippedPart(part_path, None, reason))
        if parsed.unreadable:
            out.skipped.append(SkippedPart("*", None, "unreadable_structure"))
        elif parsed.truncated:
            out.skipped.append(SkippedPart("*", None, "too_many_parts"))
        return EmailIngestResult(message, parsed, tuple(out.files), tuple(out.nested), tuple(out.skipped))

    def _ingest_part(self, part: MailPart, arrival: _Arrival, out: _Collected) -> None:
        found = sniff(part.data, declared_type=part.declared_type, filename=part.filename)
        if found.format is None:
            out.skipped.append(SkippedPart(part.path, part.filename, "unsupported"))
            return
        if found.format is EvidenceFormat.EML:
            self._nested_file(part.data, part.path, arrival, out)
            return
        reg = self.registry.register(
            part.data, tenant_id=arrival.tenant_id, source_kind=arrival.source_kind, format=found.format,
            mime_type=found.mime_type, filename=part.filename, retrieved_at=arrival.received_at,
            metadata={"declared_mime_type": found.declared_mime_type, "type_mismatch": found.mismatch},
            context={**arrival.parent, "part": part.path, "inline": part.inline, "content_id": part.content_id},
        )
        out.files.append(IngestedFile(reg, part.path, part.filename, part.inline, found.mismatch))
        if found.format is EvidenceFormat.ZIP and self.expand_archives:
            self._ingest_archive(expand_zip(part.data, self.zip_limits), part.path, reg, arrival, out)

    def _nested_file(self, data: bytes, path: str, arrival: _Arrival, out: _Collected) -> None:
        """An attachment whose bytes are an email (e.g. a forwarded .eml file)."""
        if arrival.depth >= self.limits.max_depth:
            out.skipped.append(SkippedPart(path, None, "nested_too_deep"))
            return
        if not data.strip() or len(data) > self.limits.max_bytes:
            out.skipped.append(SkippedPart(path, None, "unreadable_email"))
            return
        child = _Arrival(arrival.tenant_id, arrival.source_kind, arrival.received_at, arrival.parent,
                         arrival.depth + 1)
        parsed = _parse(data, self.limits, child.depth)
        out.nested.append(self._ingest_parsed(data, parsed, child, EvidenceFormat.EML, None,
                                              {**arrival.parent, "part": path}))

    def _ingest_archive(
        self,
        expansion: ArchiveExpansion,
        path: str,
        archive: Registration,
        arrival: _Arrival,
        out: _Collected,
    ) -> None:
        for skip in expansion.skipped:
            out.skipped.append(SkippedPart(f"{path}/{skip.name}", None, f"archive:{skip.reason.value}"))
        for member in expansion.members:
            member_path = f"{path}/{member.path}"
            found = sniff(member.data, filename=member.filename)
            if found.format is None:
                out.skipped.append(SkippedPart(member_path, member.filename, "unsupported"))
                continue
            if found.format is EvidenceFormat.EML:
                self._nested_file(member.data, member_path, arrival, out)
                continue
            reg = self.registry.register(
                member.data, tenant_id=arrival.tenant_id, source_kind=arrival.source_kind,
                format=found.format, mime_type=found.mime_type, filename=member.filename,
                retrieved_at=arrival.received_at,
                context={**arrival.parent, "part": path, "archive_evidence_id": archive.evidence.id,
                         "archive_member": member.path},
            )
            out.files.append(IngestedFile(reg, member_path, member.filename, False, False))
