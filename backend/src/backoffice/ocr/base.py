"""OCR engine contract and result model (§13-18).

No single engine is trusted and none is architected around (§16): every
engine sits behind :class:`OCRProviderInterface` and returns an
:class:`OCRResult` with the same shape, so the router can swap engines by
name, compare their readings field by field (§18) and account for their
cost.

Coordinates are pixels of the page image as the engine saw it; boxes use
the domain :class:`~backoffice.domain.models.BoundingBox` with 1-based pages.
Confidence is 0-1, or None when an engine gives no score (document VLMs):
an unknown score is never invented.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from backoffice.domain.models import BoundingBox, CriticalField, ExtractionMethod
from backoffice.extraction.media import MIME_PDF, OCR_INPUT_MIME_TYPES, sniff_mime

__all__ = [
    "ANY_LANGUAGE",
    "LATIN_LANGUAGES",
    "LayoutSignals",
    "OCRCapabilities",
    "OCRError",
    "OCRFieldReading",
    "OCRHints",
    "OCRInputError",
    "OCRLine",
    "OCRPage",
    "OCRProviderInterface",
    "OCRRejected",
    "OCRResponseError",
    "OCRResult",
    "OCRUnavailable",
    "OCRWord",
    "PageImage",
    "RedactionError",
    "as_pages",
    "count_pages",
    "ensure_supported",
    "estimate_pdf_pages",
]

ANY_LANGUAGE = "*"
# §14: Portuguese, Spanish, English and the major Latin-script languages.
LATIN_LANGUAGES: frozenset[str] = frozenset({"pt", "es", "en", "fr", "it", "de"})


# --------------------------------------------------------------------------- errors


class OCRError(Exception):
    """An engine could not produce a result. ``code`` is a stable machine code.

    Messages carry engine names and status codes only, never document content.
    """

    code = "ocr_failed"
    retryable = False

    def __init__(self, engine: str, detail: str = "") -> None:
        self.engine = engine
        self.detail = detail
        super().__init__(f"{engine}: {self.code}" + (f" ({detail})" if detail else ""))


class OCRUnavailable(OCRError):
    """Timeout, connection failure, 429 or 5xx: worth retrying later."""

    code = "engine_unavailable"
    retryable = True


class OCRRejected(OCRError):
    """The engine refused the request (4xx other than 429)."""

    code = "request_rejected"


class OCRResponseError(OCRError):
    """The engine answered, but not with anything we can read."""

    code = "bad_response"


class OCRInputError(OCRError):
    """The input is not something this engine accepts."""

    code = "unsupported_input"


class RedactionError(OCRError):
    """Refused to send content outside our infrastructure unredacted (§53)."""

    code = "redaction_required"


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class PageImage:
    """One page image, or a whole PDF for engines that accept documents.

    ``number`` is the 1-based page number of the (first) page within the
    evidence. ``page_count`` is the number of pages inside a PDF when known.
    """

    data: bytes = field(repr=False)
    mime_type: str
    number: int = 1
    width: int | None = None
    height: int | None = None
    page_count: int | None = None

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("page numbers start at 1")
        if not self.data:
            raise ValueError("empty page")

    @property
    def is_pdf(self) -> bool:
        return self.mime_type == MIME_PDF

    @property
    def pages(self) -> int | None:
        """Pages this input stands for; None for a PDF of unknown length."""
        if not self.is_pdf:
            return 1
        return self.page_count or estimate_pdf_pages(self.data)


_PDF_PAGE_OBJECT = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")


def estimate_pdf_pages(data: bytes) -> int | None:
    """Page count of an uncompressed-xref PDF, or None when it cannot be seen.

    Counts ``/Type /Page`` objects; pages hidden in compressed object
    streams are invisible to this cheap check, hence None rather than 0.
    """
    count = len(_PDF_PAGE_OBJECT.findall(data))
    return count or None


def as_pages(
    items: Sequence[PageImage | bytes], *, default_mime: str | None = None
) -> tuple[PageImage, ...]:
    """Normalize raw bytes into numbered :class:`PageImage` objects.

    Raw bytes are identified by content; unknown content raises
    :class:`OCRInputError` unless ``default_mime`` is given.
    """
    pages: list[PageImage] = []
    next_number = 1
    for item in items:
        if isinstance(item, PageImage):
            page = item
        else:
            if not isinstance(item, (bytes, bytearray, memoryview)) or not item:
                raise OCRInputError("input", "empty or non-binary page")
            data = bytes(item)
            mime = sniff_mime(data) or default_mime
            if mime is None:
                raise OCRInputError("input", "unrecognised page content")
            page = PageImage(data=data, mime_type=mime, number=next_number)
        pages.append(page)
        next_number = page.number + (page.pages or 1)
    return tuple(pages)


def count_pages(pages: Iterable[PageImage]) -> int:
    """Total pages; a PDF of unknown length counts as one."""
    return sum(p.pages or 1 for p in pages)


def ensure_supported(
    pages: Sequence[PageImage],
    *,
    engine: str,
    accepted: frozenset[str] = OCR_INPUT_MIME_TYPES,
    max_pages: int | None = None,
) -> None:
    """Raise :class:`OCRInputError` for inputs this engine cannot take."""
    if not pages:
        raise OCRInputError(engine, "no pages")
    for page in pages:
        if page.mime_type not in accepted:
            raise OCRInputError(engine, f"unsupported type {page.mime_type}")
    if max_pages is not None and count_pages(pages) > max_pages:
        raise OCRInputError(engine, f"more than {max_pages} pages")


@dataclass(frozen=True)
class OCRHints:
    """What the caller already knows about the document.

    ``prior_text`` is text read by an earlier engine; it is local data and
    only leaves our infrastructure through a redactor (§53). ``prior_pages``
    are that engine's boxed pages: a redactor may use the boxes to mask
    personal details on page images before they leave (§53); they are never
    sent themselves.
    """

    languages: tuple[str, ...] = ("pt", "es", "en")
    page_count: int | None = None
    document_kind: str | None = None
    fields: tuple[CriticalField, ...] = ()
    prior_text: str | None = field(default=None, repr=False)
    prior_pages: tuple[OCRPage, ...] = field(default=(), repr=False)


# --------------------------------------------------------------------------- results

_FROZEN = ConfigDict(frozen=True, protected_namespaces=())


class OCRWord(BaseModel):
    model_config = _FROZEN

    text: str
    bbox: BoundingBox | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class OCRLine(BaseModel):
    """A line (or text block) with its box, score and tilt in degrees."""

    model_config = _FROZEN

    text: str
    bbox: BoundingBox | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    words: tuple[OCRWord, ...] = ()
    angle: float | None = None


class LayoutSignals(BaseModel):
    """Layout facts that drive escalation to the complex-document engine (§15, §17).

    ``inferred`` is True when the numbers come from our own box geometry
    heuristics rather than from the engine's layout model.
    """

    model_config = _FROZEN

    tables: int = Field(default=0, ge=0)
    columns: int = Field(default=1, ge=1)
    skew_degrees: float = 0.0
    rotation: int = 0
    inferred: bool = False

    @classmethod
    def combine(cls, signals: Iterable[LayoutSignals]) -> LayoutSignals:
        """Document-level view: tables add up, the worst columns/skew win."""
        items = list(signals)
        if not items:
            return cls()
        return cls(
            tables=sum(s.tables for s in items),
            columns=max(s.columns for s in items),
            skew_degrees=max((s.skew_degrees for s in items), key=abs),
            rotation=next((s.rotation for s in items if s.rotation), 0),
            inferred=any(s.inferred for s in items),
        )


class OCRPage(BaseModel):
    model_config = _FROZEN

    number: int = Field(ge=1)
    width: int | None = None
    height: int | None = None
    lines: tuple[OCRLine, ...] = ()
    markdown: str | None = None
    layout: LayoutSignals = LayoutSignals()

    @property
    def text(self) -> str:
        if self.lines:
            return "\n".join(line.text for line in self.lines)
        return self.markdown or ""


class OCRFieldReading(BaseModel):
    """A critical field an engine stated directly (a structured answer), not found by parsing text.

    ``value`` is the engine's string ("483.60", "2026-09-18"); it is typed by
    the router and, when it does not parse, kept as an unreadable reading.
    ``printed`` is the text as it appears on the page, for review.
    """

    model_config = _FROZEN

    field: CriticalField
    value: str
    page: int | None = Field(default=None, ge=1)
    printed: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class OCRResult(BaseModel):
    """What one engine read, what it cost and how long it took.

    ``from_prior_text`` is True when the engine was given text that one of
    our engines had already read (``OCRHints.prior_text``, e.g. through a
    text-only redactor, §53). Such a reading is not independent of that
    engine, so it must never count as a second vote (§17, §19).

    ``fields`` holds critical fields an engine returned as structured data
    (a multimodal model answering with JSON); when present, the router uses
    them instead of parsing the engine's text. ``supplier_name`` and
    ``document_type`` are what such an engine said about the document.
    """

    model_config = _FROZEN

    engine: str
    model_version: str
    method: ExtractionMethod
    pages: tuple[OCRPage, ...]
    layout: LayoutSignals = LayoutSignals()
    duration_ms: int = Field(default=0, ge=0)
    cost: Decimal = Field(default=Decimal("0"), ge=0)
    pages_billed: int = Field(default=0, ge=0)
    warnings: tuple[str, ...] = ()
    from_prior_text: bool = False
    fields: tuple[OCRFieldReading, ...] = ()
    supplier_name: str | None = None
    document_type: str | None = None

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def full_text(self) -> str:
        return "\n\n".join(page.text for page in self.pages if page.text)

    @property
    def lines(self) -> tuple[OCRLine, ...]:
        return tuple(line for page in self.pages for line in page.lines)

    @property
    def mean_confidence(self) -> float | None:
        """Character-weighted mean line confidence; None when no line is scored."""
        scored = [(len(ln.text), ln.confidence) for ln in self.lines if ln.confidence is not None and ln.text]
        weight = sum(n for n, _ in scored)
        if not weight:
            return None
        return sum(n * c for n, c in scored) / weight

    def low_confidence_share(self, threshold: float) -> float | None:
        """Share of scored characters on lines below ``threshold``."""
        scored = [(len(ln.text), ln.confidence) for ln in self.lines if ln.confidence is not None and ln.text]
        weight = sum(n for n, _ in scored)
        if not weight:
            return None
        return sum(n for n, c in scored if c < threshold) / weight


# --------------------------------------------------------------------------- contract


class OCRCapabilities(BaseModel):
    """What an engine can take. ``external`` means data leaves our infrastructure."""

    model_config = _FROZEN

    languages: frozenset[str] = frozenset({ANY_LANGUAGE})
    max_pages: int | None = Field(default=None, ge=1)
    handles_tables: bool = False
    handles_long_docs: bool = False
    accepts_pdf: bool = False
    returns_boxes: bool = True
    external: bool = False

    def supports_language(self, language: str) -> bool:
        return ANY_LANGUAGE in self.languages or language.lower() in self.languages

    def supports_any(self, languages: Iterable[str]) -> bool:
        """True when no language is known or at least one is supported."""
        wanted = list(languages)
        return not wanted or any(self.supports_language(lang) for lang in wanted)


@runtime_checkable
class OCRProviderInterface(Protocol):
    """Every OCR engine, local or external, behind one contract (§16).

    ``method`` labels the observations read from this engine's text
    (OCR for text recognisers, VLM for document vision-language models).
    """

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def cost_per_page(self) -> Decimal: ...

    @property
    def capabilities(self) -> OCRCapabilities: ...

    @property
    def method(self) -> ExtractionMethod: ...

    async def recognize(
        self, pages: Sequence[PageImage | bytes], hints: OCRHints | None = None
    ) -> OCRResult: ...
