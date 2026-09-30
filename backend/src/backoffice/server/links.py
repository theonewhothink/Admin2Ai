"""Opening invoice links on the server, replay-safe (§9, §10, §12).

:class:`~backoffice.evidence.links.LinkFetcher` opens links on the internet:
external and never the same twice. A replay must rebuild exactly what the
owner saw, so a link is never opened while an event is applied. The order is
the one used for reading files (server/reads.py):

1. **Before** an event is recorded, the links the engine will follow are found
   with the engine's own intake on a scratch registry (:func:`links_in_files`:
   the invoice links of every email in an upload or a mailbox sync, the same
   ones and in the same order as the Retrieval agent; :func:`links_in_share`:
   a link or text shared from the phone), and :func:`pre_fetch` opens each one
   with the live fetcher. Unsafe links are refused before any connection
   (``UrlSafety``); a sign-in wall, an expired link or an outage is an outcome
   like any other.
2. The event keeps what came back (:func:`encode_fetch`): the outcome, its plain
   message and the full provenance (original URL, final URL, every redirect,
   retrieval time, status, content types). File bytes and a page's screenshot
   go to the content-addressed object store; the event keeps their key and
   SHA-256.
3. Live **and** on replay the engine follows links through
   :class:`RecordedLinks`, which returns exactly the recording. A link without
   one waits (``None``): a replay never opens a link.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from backoffice.domain.models import EvidenceFormat
from backoffice.evidence.links import FetchRecord, FetchResult, LinkOutcome, RedirectHop

from .events import OBJECT

__all__ = [
    "INGEST_PATHS",
    "MAX_LINKS_PER_EVENT",
    "RecordedLinks",
    "decode_fetch",
    "encode_fetch",
    "link_fetcher_from_env",
    "links_in_files",
    "links_in_share",
    "pre_fetch",
]

log = logging.getLogger("backoffice.server.links")

# Routes whose body is evidence the engine ingests (a file, an email, a shared link or text).
INGEST_PATHS = frozenset({"/api/evidence", "/api/evidence/upload", "/api/receipts", "/api/share"})
MAX_LINKS_PER_EVENT = 20

PutFile = Callable[[bytes], dict[str, Any]]
GetFile = Callable[[Mapping[str, Any]], bytes]


# --------------------------------------------------------------------------- recording


def encode_fetch(result: FetchResult, put_file: PutFile) -> dict[str, Any]:
    """What an event keeps of one retrieval: never the bytes themselves (they go to the object store)."""
    return {
        "outcome": result.outcome.value,
        "record": result.record.as_metadata(),
        "content": {OBJECT: put_file(result.content)} if result.content else None,
        "format": result.format.value if result.format is not None else None,
        "filename": result.filename,
        "reason": result.reason,
        "ownerMessage": result.owner_message,
        "retryable": bool(result.retryable),
        "viaBrowser": bool(result.via_browser),
        "snapshot": {OBJECT: put_file(result.snapshot_png)} if result.snapshot_png else None,
        "supplier": result.supplier,
    }


def decode_fetch(data: Mapping[str, Any], get_file: GetFile) -> FetchResult:
    """The retrieval exactly as it was recorded (the bytes come back from the object store, hash-checked)."""
    raw = data.get("record") or {}
    record = FetchRecord(
        original_url=str(raw.get("original_url") or ""),
        final_url=str(raw.get("final_url") or ""),
        redirect_chain=tuple(RedirectHop(str(h.get("url") or ""), h.get("status"), str(h.get("kind") or ""))
                             for h in raw.get("redirect_chain") or () if isinstance(h, Mapping)),
        retrieved_at=datetime.fromisoformat(str(raw["retrieved_at"])),
        sha256=raw.get("sha256"),
        content_type=raw.get("content_type"),
        declared_content_type=raw.get("declared_content_type"),
        status_code=raw.get("status_code"),
        size=int(raw.get("size") or 0),
    )
    content = data.get("content")
    snapshot = data.get("snapshot")
    return FetchResult(
        outcome=LinkOutcome(str(data["outcome"])),
        record=record,
        content=get_file(content[OBJECT]) if isinstance(content, Mapping) else None,
        format=EvidenceFormat(str(data["format"])) if data.get("format") else None,
        filename=data.get("filename"),
        reason=data.get("reason"),
        owner_message=data.get("ownerMessage"),
        retryable=bool(data.get("retryable")),
        via_browser=bool(data.get("viaBrowser")),
        snapshot_png=get_file(snapshot[OBJECT]) if isinstance(snapshot, Mapping) else None,
        supplier=data.get("supplier"),
    )


class RecordedLinks:
    """The engine's link source during an apply: what was opened before the event was recorded.

    A link with no recording is not opened: it waits (the same live and on every replay).
    """

    def __init__(self, recorded: Mapping[str, Any] | None, get_file: GetFile) -> None:
        self._recorded = dict(recorded or {})
        self._get_file = get_file

    def __contains__(self, url: object) -> bool:
        return url in self._recorded

    def fetch(self, url: str, *, supplier_name: str | None = None) -> FetchResult | None:
        data = self._recorded.get(url)
        if not isinstance(data, Mapping):
            return None
        return decode_fetch(data, self._get_file)


# --------------------------------------------------------------------------- before recording


def links_in_files(tenant_id: str, files: Sequence[tuple[Any, ...]]) -> list[str]:
    """The links the engine will follow in these files (bytes, file name, declared type[, hints]): the invoice
    links of every email among them (and of the emails attached to those), found with the engine's own intake
    on a scratch registry, so nothing is stored for the tenant."""
    from backoffice.evidence import EvidenceRegistry, ShareIntake
    from backoffice.evidence.retrieval import all_invoice_links
    from backoffice.orchestrator import MemoryObjectStore

    found: dict[str, None] = {}
    for upload in files:
        data, filename, mime_type = upload[:3]
        registry = EvidenceRegistry(MemoryObjectStore())
        try:
            outcome = ShareIntake(registry).ingest_file(tenant_id, bytes(data), filename=filename,
                                                         mime_type=mime_type)
        except Exception:  # whatever the intake refuses is refused again when the event applies
            continue
        if outcome.email is not None:
            for url in all_invoice_links(outcome.email):
                found.setdefault(url, None)
    return list(found)


def links_in_share(body: Mapping[str, Any]) -> list[str]:
    """The links a share from the phone asks to follow: the link itself, or the links in shared text."""
    from backoffice.evidence import SharePayload
    from backoffice.evidence.email import extract_text_links
    from backoffice.evidence.share import _share_url

    kind = str(body.get("kind") or ("url" if body.get("url") else "text" if body.get("text") else ""))
    if kind == "url":
        url = _share_url(SharePayload.for_url(str(body.get("url") or "")))
        return [url] if url else []
    if kind == "text":
        text = str(body.get("text") or "")
        url = _share_url(SharePayload.for_text(text))
        if url:
            return [url]
        return list(dict.fromkeys(u for u, _ in extract_text_links(text)))[:10]
    return []


Files = list[tuple[bytes, str | None, str | None]]


def pre_fetch(fetcher: Any, urls: Sequence[str], put_file: PutFile, *, limit: int = MAX_LINKS_PER_EVENT,
              skip: Callable[[str], bool] | None = None) -> tuple[dict[str, Any], Files]:
    """Open each link with the live fetcher. Returns ``({url: recording}, files to read before recording)``.

    A fetcher failure never loses the event: that link simply has no recording and waits.
    """
    if fetcher is None:
        return {}, []
    recorded: dict[str, Any] = {}
    files: Files = []
    for url in list(dict.fromkeys(urls)):
        if len(recorded) >= limit:
            break
        if skip is not None and skip(url):
            continue
        try:
            result = fetcher.fetch(url)
        except Exception as exc:  # noqa: BLE001 - a fetcher bug: the link waits, the rest goes on
            log.warning("link_fetch_failed", extra={"exc_type": type(exc).__name__})
            continue
        recorded[url] = encode_fetch(result, put_file)
        if result.content:
            files.append((result.content, result.filename, result.record.content_type))
    return recorded, files


def link_fetcher_from_env() -> Any:
    """The live link fetcher (§9): on unless ``BACKOFFICE_LINK_FETCHING=off``.

    Safety is not configurable here: http(s) only, no private or metadata
    addresses after DNS, every redirect re-checked, one total deadline.
    """
    if os.environ.get("BACKOFFICE_LINK_FETCHING", "on").strip().lower() in ("off", "0", "false", "no"):
        return None
    from backoffice.evidence.links import LinkFetcher, UrlSafety

    return LinkFetcher(UrlSafety())
