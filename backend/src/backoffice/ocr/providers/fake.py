"""Deterministic in-memory engine for tests and the golden-dataset harness (§56).

By default it "reads" synthetic pages whose bytes are UTF-8 text
(``text/plain``), which is how the golden fixtures are stored. ``texts``
fixes the output instead, ``substitutions`` simulates misreads
(e.g. ``{"3": "8"}``), and ``error`` makes every call fail.
``echo_prior_text`` simulates an external engine behind a text-only
redactor: it re-reads ``hints.prior_text`` (and marks the result
``from_prior_text``) instead of the pages. Lines get
synthetic boxes (one 20-pixel row per line) so location handling can be
tested. Every call is recorded in ``calls``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal

from backoffice.domain.models import BoundingBox, ExtractionMethod
from backoffice.extraction.media import MIME_TEXT, OCR_INPUT_MIME_TYPES

from ..base import (
    LayoutSignals,
    OCRCapabilities,
    OCRHints,
    OCRLine,
    OCRPage,
    OCRResult,
    PageImage,
    as_pages,
    ensure_supported,
)

__all__ = ["FakeOCRProvider"]

_LINE_HEIGHT = 20.0
_CHAR_WIDTH = 8.0


def _utf8(page: PageImage) -> str:
    return page.data.decode("utf-8", errors="replace") if page.mime_type == MIME_TEXT else ""


class FakeOCRProvider:
    """Scriptable :class:`~backoffice.ocr.base.OCRProviderInterface` implementation."""

    def __init__(
        self,
        name: str = "fake",
        *,
        version: str = "fake-1",
        method: ExtractionMethod = ExtractionMethod.OCR,
        cost_per_page: Decimal = Decimal("0"),
        capabilities: OCRCapabilities | None = None,
        texts: Sequence[str] | None = None,
        transcribe: Callable[[PageImage], str] = _utf8,
        substitutions: Mapping[str, str] | None = None,
        confidence: float | None = 0.98,
        layout: LayoutSignals | None = None,
        error: Exception | None = None,
        duration_ms: int = 0,
        echo_prior_text: bool = False,
    ) -> None:
        self._name = name
        self._version = version
        self._method = method
        self._cost = cost_per_page
        self._capabilities = capabilities or OCRCapabilities(accepts_pdf=True)
        self._texts = list(texts) if texts is not None else None
        self._transcribe = transcribe
        self._substitutions = dict(substitutions or {})
        self._confidence = confidence
        self._layout = layout or LayoutSignals()
        self._error = error
        self._duration_ms = duration_ms
        self._echo = echo_prior_text
        self.calls: list[tuple[tuple[PageImage, ...], OCRHints]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    @property
    def cost_per_page(self) -> Decimal:
        return self._cost

    @property
    def method(self) -> ExtractionMethod:
        return self._method

    @property
    def capabilities(self) -> OCRCapabilities:
        return self._capabilities

    async def recognize(self, pages: Sequence[PageImage | bytes], hints: OCRHints | None = None) -> OCRResult:
        items = as_pages(pages, default_mime=MIME_TEXT)
        self.calls.append((items, hints or OCRHints()))
        if self._error is not None:
            raise self._error
        ensure_supported(items, engine=self.name, accepted=OCR_INPUT_MIME_TYPES | {MIME_TEXT})
        if self._echo:
            prior = (hints or OCRHints()).prior_text or ""
            out = [self._lines(items[0].number, prior)]
        else:
            out = [self._page(i, page) for i, page in enumerate(items)]
        return OCRResult(
            engine=self._name,
            model_version=self._version,
            method=self._method,
            pages=tuple(out),
            layout=self._layout,
            duration_ms=self._duration_ms,
            cost=self._cost * len(out),
            pages_billed=len(out),
            from_prior_text=self._echo,
        )

    def _page(self, index: int, page: PageImage) -> OCRPage:
        if self._texts is not None:
            text = self._texts[index] if index < len(self._texts) else ""
        else:
            text = self._transcribe(page)
        return self._lines(page.number, text)

    def _lines(self, number: int, text: str) -> OCRPage:
        for old, new in self._substitutions.items():
            text = text.replace(old, new)
        lines = []
        for row, line in enumerate(t for t in text.splitlines() if t.strip()):
            box = BoundingBox(
                page=number, x0=0.0, y0=row * _LINE_HEIGHT, x1=len(line) * _CHAR_WIDTH, y1=(row + 1) * _LINE_HEIGHT
            )
            lines.append(OCRLine(text=line.strip(), bbox=box, confidence=self._confidence))
        return OCRPage(number=number, lines=tuple(lines), layout=self._layout)
