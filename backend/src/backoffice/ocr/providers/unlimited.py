"""Baidu Unlimited-OCR, experimental long-document engine (§16).

For very long PDFs, multi-page statements and long-horizon parsing. It sits
behind the common interface precisely so it can be replaced instantly; the
router only knows it by name.

Assumed deployment: vLLM's OpenAI-compatible server
(``vllm serve <model> --served-model-name <name>``), so the request and
response shapes are those in :mod:`._chat`. Pages are sent as image parts,
``pages_per_request`` at a time, with their page numbers in the prompt, and
the transcription is split back per page on ``=== PAGE n ===`` markers.

PDFs: with a ``rasterizer`` (PDF -> page images, e.g. a pypdfium2 worker)
pages are sent as images; without one, the PDF is sent whole as an
OpenAI-style ``file`` content part. Whether the served model accepts file
parts depends on the deployment: configure a rasterizer if it does not.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING

from backoffice.domain.models import ExtractionMethod

from ..base import (
    LATIN_LANGUAGES,
    OCRCapabilities,
    OCRHints,
    OCRPage,
    OCRResult,
    PageImage,
    as_pages,
    ensure_supported,
)
from ._chat import DEFAULT_TRANSCRIBE_PROMPT, chat_payload, chat_text, file_part, image_part, split_pages
from ._common import DEFAULT_CLOCK, Clock, build_result, gather_ordered, text_lines
from ._http import EndpointConfig, JSONEndpoint

if TYPE_CHECKING:  # only for annotations: httpx is imported where a request is made
    import httpx

__all__ = [
    "DEFAULT_TRANSCRIBE_PROMPT",
    "Rasterizer",
    "UnlimitedOCRConfig",
    "UnlimitedOCRProvider",
]

# Upper bound on page numbers accepted from a PDF of unknown length.
_UNKNOWN_PDF_PAGE_LIMIT = 5000

Rasterizer = Callable[[PageImage], Sequence[PageImage]]


@dataclass(frozen=True)
class UnlimitedOCRConfig:
    endpoint: EndpointConfig
    model: str  # the served model name
    path: str = "/v1/chat/completions"
    name: str = "unlimited-ocr"
    model_version: str | None = None
    cost_per_page: Decimal = Decimal("0")  # amortised compute cost, not a vendor price
    pages_per_request: int = 8
    max_tokens: int = 8192
    max_concurrency: int = 2
    max_pages: int | None = None
    prompt: str = DEFAULT_TRANSCRIBE_PROMPT
    languages: frozenset[str] = LATIN_LANGUAGES

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model is required")
        if self.pages_per_request < 1 or self.max_tokens < 1:
            raise ValueError("pages_per_request and max_tokens must be positive")
        if self.cost_per_page < 0:
            raise ValueError("cost_per_page cannot be negative")


class UnlimitedOCRProvider:
    """Unlimited-OCR behind :class:`~backoffice.ocr.base.OCRProviderInterface`."""

    def __init__(
        self,
        config: UnlimitedOCRConfig,
        *,
        client: httpx.AsyncClient | None = None,
        rasterizer: Rasterizer | None = None,
        clock: Clock = DEFAULT_CLOCK,
    ) -> None:
        self._config = config
        self._http = JSONEndpoint(config.endpoint, engine=config.name, client=client)
        self._rasterizer = rasterizer
        self._clock = clock

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def version(self) -> str:
        return self._config.model_version or self._config.model

    @property
    def cost_per_page(self) -> Decimal:
        return self._config.cost_per_page

    @property
    def method(self) -> ExtractionMethod:
        return ExtractionMethod.VLM

    @property
    def capabilities(self) -> OCRCapabilities:
        return OCRCapabilities(
            languages=self._config.languages,
            max_pages=self._config.max_pages,
            handles_tables=True,
            handles_long_docs=True,
            accepts_pdf=True,
            returns_boxes=False,
        )

    async def recognize(self, pages: Sequence[PageImage | bytes], hints: OCRHints | None = None) -> OCRResult:
        started = self._clock()
        items = as_pages(pages)
        ensure_supported(items, engine=self.name, max_pages=self._config.max_pages)
        units = self._expand(items)
        outputs = await gather_ordered(
            [partial(self._request, chunk) for chunk in self._chunks(units)],
            limit=self._config.max_concurrency,
        )
        result_pages = [page for chunk_pages, _ in outputs for page in chunk_pages]
        billed = max(sum(u.pages or 1 for u in units), len(result_pages))
        return build_result(
            engine=self.name,
            version=self.version,
            method=self.method,
            pages=result_pages,
            cost_per_page=self.cost_per_page,
            pages_billed=billed,
            started=started,
            clock=self._clock,
            warnings=[w for _, warnings in outputs for w in warnings],
        )

    def _expand(self, items: Sequence[PageImage]) -> list[PageImage]:
        if self._rasterizer is None:
            return list(items)
        expanded: list[PageImage] = []
        for item in items:
            expanded += list(self._rasterizer(item)) if item.is_pdf else [item]
        return expanded

    def _chunks(self, units: Sequence[PageImage]) -> list[list[PageImage]]:
        """Images in groups of ``pages_per_request``; every PDF alone."""
        chunks: list[list[PageImage]] = []
        for unit in units:
            if unit.is_pdf or not chunks or chunks[-1][0].is_pdf or len(chunks[-1]) >= self._config.pages_per_request:
                chunks.append([unit])
            else:
                chunks[-1].append(unit)
        return chunks

    async def _request(self, chunk: list[PageImage]) -> tuple[list[OCRPage], list[str]]:
        numbers, known = _page_numbers(chunk)
        parts = [file_part(p) if p.is_pdf else image_part(p) for p in chunk]
        if known:
            prompt = f"{self._config.prompt}\nPage numbers: {', '.join(map(str, numbers))}."
        else:
            prompt = f"{self._config.prompt}\nNumber the pages starting at {numbers[0]}."
        body = await self._http.post(
            self._config.path, chat_payload(self._config.model, parts, prompt, max_tokens=self._config.max_tokens)
        )
        text, finish = chat_text(body, engine=self.name)
        by_page, marked = split_pages(text, numbers)
        warnings = []
        if finish == "length":
            warnings.append("truncated")
        if not marked and (len(numbers) > 1 or not known):
            warnings.append("page_boundaries_unknown")
        wanted = numbers if known else sorted(by_page)
        pages = [
            OCRPage(number=n, lines=text_lines(by_page.get(n, "")), markdown=by_page.get(n) or None)
            for n in wanted
        ]
        return pages, warnings


def _page_numbers(chunk: Sequence[PageImage]) -> tuple[list[int], bool]:
    """Expected page numbers of a request, and whether the count is known."""
    numbers: list[int] = []
    known = True
    for page in chunk:
        count = page.pages
        if count is None:
            known = False
            count = _UNKNOWN_PDF_PAGE_LIMIT
        numbers += range(page.number, page.number + count)
    return numbers, known
