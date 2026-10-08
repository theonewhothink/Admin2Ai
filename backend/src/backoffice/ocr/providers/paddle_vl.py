"""PaddleOCR-VL-1.6, the complex-document engine (§15).

Used for tables, multi-column pages, forms, skewed or poor scans and complex
statements. Talks to a PaddleX PaddleOCR-VL pipeline server.

Assumed contract (envelope in :mod:`._paddlex`)::

    POST <base>/layout-parsing
    {"file": "<base64>", "fileType": 0 | 1, "visualize": false, ...}

    result.layoutParsingResults[i] = {
        "prunedResult": {"parsing_res_list": [
            {"block_label": "table" | "text" | "paragraph_title" | ...,
             "block_content": "<text, or HTML for tables>",
             "block_bbox": [x0, y0, x1, y1], "block_order": n?}, ...],
            "width": W?, "height": H?, "doc_preprocessor_res": {"angle": a}?},
        "markdown": {"text": "..."}}

Blocks become lines in reading order (table rows as "cell | cell"), the
page markdown is kept, tables are counted from block labels and columns
estimated from block boxes. The model gives no confidence scores, so lines
carry none.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Any

from backoffice.domain.models import ExtractionMethod

from ..base import (
    LATIN_LANGUAGES,
    LayoutSignals,
    OCRCapabilities,
    OCRHints,
    OCRLine,
    OCRPage,
    OCRResponseError,
    OCRResult,
    PageImage,
    as_pages,
    ensure_supported,
)
from ..layout import Box, estimate_columns, geometry, to_bbox
from ._common import DEFAULT_CLOCK, Clock, build_result, gather_ordered
from ._http import EndpointConfig, JSONEndpoint, b64
from ._markup import html_table_rows, strip_tags
from ._paddlex import first_key, page_sizes, positive_int, pruned, rotation, unwrap

if TYPE_CHECKING:  # only for annotations: httpx is imported where a request is made
    import httpx

__all__ = ["PaddleOCRVLConfig", "PaddleOCRVLProvider", "page_from_layout", "parse_layout_response"]


@dataclass(frozen=True)
class PaddleOCRVLConfig:
    endpoint: EndpointConfig
    path: str = "/layout-parsing"
    name: str = "paddleocr-vl"
    model_version: str = "PaddleOCR-VL-1.6"
    cost_per_page: Decimal = Decimal("0")  # amortised compute cost, not a vendor price
    languages: frozenset[str] = LATIN_LANGUAGES
    max_pages: int | None = None
    max_concurrency: int = 2
    request_options: Mapping[str, Any] = field(default_factory=lambda: {"visualize": False})

    def __post_init__(self) -> None:
        if self.cost_per_page < 0:
            raise ValueError("cost_per_page cannot be negative")


def _ordered(blocks: list[Any]) -> list[Mapping[str, Any]]:
    items = [b for b in blocks if isinstance(b, Mapping)]
    orders = [b.get("block_order") for b in items]
    if items and all(isinstance(o, (int, float)) and not isinstance(o, bool) for o in orders):
        return sorted(items, key=lambda b: b["block_order"])
    return items


def page_from_layout(
    item: Mapping[str, Any], *, number: int, width: int | None, height: int | None, engine: str
) -> OCRPage:
    """One OCRPage from a ``layoutParsingResults`` item."""
    result = pruned(item, engine=engine)
    blocks = first_key(result, "parsing_res_list", "blocks") or []
    lines: list[OCRLine] = []
    text_boxes: list[Box] = []
    tables = 0
    for block in _ordered(blocks if isinstance(blocks, list) else []):
        label = str(block.get("block_label") or block.get("label") or "").lower()
        content = str(first_key(block, "block_content", "content", "text") or "")
        box, _ = geometry(first_key(block, "block_bbox", "bbox"))
        bbox = to_bbox(box, number)
        if label == "table" or "<table" in content.lower():
            tables += 1
            lines += [OCRLine(text=" | ".join(row), bbox=bbox) for row in html_table_rows(content)]
            continue
        if box is not None:
            text_boxes.append(box)
        lines += [OCRLine(text=t.strip(), bbox=bbox) for t in strip_tags(content).splitlines() if t.strip()]
    markdown = item.get("markdown")
    md_text = markdown.get("text") if isinstance(markdown, Mapping) else markdown
    return OCRPage(
        number=number,
        width=width or positive_int(result.get("width")),
        height=height or positive_int(result.get("height")),
        lines=tuple(lines),
        markdown=md_text if isinstance(md_text, str) else None,
        layout=LayoutSignals(
            tables=tables,
            columns=estimate_columns(text_boxes),
            rotation=rotation(result),
        ),
    )


def parse_layout_response(body: Any, *, first_page: int, engine: str) -> list[OCRPage]:
    result = unwrap(body, engine=engine)
    items = first_key(result, "layoutParsingResults", "layout_parsing_results")
    if not isinstance(items, list):
        raise OCRResponseError(engine, "no layout results")
    sizes = page_sizes(result)
    pages = []
    for i, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise OCRResponseError(engine, "page result is not an object")
        width, height = sizes[i] if i < len(sizes) else (None, None)
        pages.append(page_from_layout(item, number=first_page + i, width=width, height=height, engine=engine))
    return pages or [OCRPage(number=first_page)]


class PaddleOCRVLProvider:
    """PaddleOCR-VL behind :class:`~backoffice.ocr.base.OCRProviderInterface`."""

    def __init__(
        self,
        config: PaddleOCRVLConfig,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Clock = DEFAULT_CLOCK,
    ) -> None:
        self._config = config
        self._http = JSONEndpoint(config.endpoint, engine=config.name, client=client)
        self._clock = clock

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def version(self) -> str:
        return self._config.model_version

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
            accepts_pdf=True,
            returns_boxes=True,
        )

    async def recognize(self, pages: Sequence[PageImage | bytes], hints: OCRHints | None = None) -> OCRResult:
        started = self._clock()
        items = as_pages(pages)
        ensure_supported(items, engine=self.name, max_pages=self._config.max_pages)
        groups = await gather_ordered([partial(self._one, p) for p in items], limit=self._config.max_concurrency)
        return build_result(
            engine=self.name,
            version=self.version,
            method=self.method,
            pages=[page for group in groups for page in group],
            cost_per_page=self.cost_per_page,
            pages_billed=sum(max(1, len(group)) for group in groups),
            started=started,
            clock=self._clock,
        )

    async def _one(self, page: PageImage) -> list[OCRPage]:
        payload = {**self._config.request_options, "file": b64(page.data), "fileType": 0 if page.is_pdf else 1}
        body = await self._http.post(self._config.path, payload)
        return parse_layout_response(body, first_page=page.number, engine=self.name)

    async def aclose(self) -> None:
        await self._http.aclose()
