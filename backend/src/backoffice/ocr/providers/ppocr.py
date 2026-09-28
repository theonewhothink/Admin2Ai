"""PP-OCRv6, the primary self-hosted OCR engine (§14).

Variants: ``TINY`` (edge/mobile, quick preview, quality validation) and
``MEDIUM`` (primary high-volume server OCR). The variant decides the engine
name (``pp-ocrv6-tiny`` / ``pp-ocrv6-medium``); which detection and
recognition models answer is the deployment's choice.

Two backends, exactly one per provider:

* **HTTP** to a PaddleX OCR-pipeline server (``POST /ocr``, see
  :mod:`._paddlex` for the envelope). Each ``ocrResults[i].prunedResult``
  holds ``rec_texts``, ``rec_scores``, ``rec_polys`` (4-point polygons,
  clockwise from top-left) or ``rec_boxes``, and optionally
  ``doc_preprocessor_res.angle`` (orientation correction applied). The older
  PaddleOCR hub-serving shape (``{"status": "000", "results": [[{"text",
  "confidence", "text_region"}]]}``) is accepted too.
* **In-process** with the optional ``paddleocr`` package
  (:class:`InProcessPaddleOCR`), imported on first use and run in a worker
  thread. Its ``predict()`` results expose the same JSON via ``.json``.

Assumption to confirm per deployment: PP-OCRv6 is served through the same
PaddleX OCR pipeline contract as PP-OCRv5.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from functools import partial
from typing import Any

import httpx

from backoffice.domain.models import ExtractionMethod
from backoffice.extraction._optional import import_optional
from backoffice.extraction.media import (
    MIME_BMP,
    MIME_GIF,
    MIME_JPEG,
    MIME_PDF,
    MIME_PNG,
    MIME_TIFF,
    MIME_WEBP,
)

from ..base import (
    LATIN_LANGUAGES,
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
from ..layout import Box, geometry, infer_layout, to_bbox
from ._common import DEFAULT_CLOCK, Clock, build_result, confidence, gather_ordered
from ._http import EndpointConfig, JSONEndpoint, b64
from ._paddlex import first_key, page_sizes, positive_int, pruned, rotation, unwrap

__all__ = [
    "InProcessPaddleOCR",
    "PPOCRConfig",
    "PPOCRVariant",
    "PPOCRv6Provider",
    "page_from_pruned",
    "parse_ocr_response",
]


class PPOCRVariant(str, Enum):
    TINY = "tiny"
    MEDIUM = "medium"


@dataclass(frozen=True)
class PPOCRConfig:
    """``endpoint`` for HTTP serving; leave it None when using an in-process engine."""

    variant: PPOCRVariant = PPOCRVariant.MEDIUM
    endpoint: EndpointConfig | None = None
    path: str = "/ocr"
    model_version: str | None = None
    cost_per_page: Decimal = Decimal("0")  # amortised compute cost, not a vendor price
    languages: frozenset[str] = LATIN_LANGUAGES
    max_pages: int | None = None
    max_concurrency: int = 4
    request_options: Mapping[str, Any] = field(default_factory=lambda: {"visualize": False})

    def __post_init__(self) -> None:
        if self.cost_per_page < 0:
            raise ValueError("cost_per_page cannot be negative")


# --------------------------------------------------------------------------- parsing


def page_from_pruned(
    result: Mapping[str, Any], *, number: int, width: int | None = None, height: int | None = None
) -> OCRPage:
    """One OCRPage from a PaddleX OCR ``prunedResult``; malformed arrays read as empty."""
    texts = _array(result.get("rec_texts"))
    scores = _array(result.get("rec_scores"))
    polys = _array(result.get("rec_polys")) or _array(result.get("dt_polys"))
    flat_boxes = _array(result.get("rec_boxes"))
    lines: list[OCRLine] = []
    boxes: list[Box | None] = []
    angles: list[float | None] = []
    for i, text in enumerate(texts):
        if not isinstance(text, str) or not text.strip():
            continue
        box, angle = geometry(polys[i]) if i < len(polys) else (None, None)
        if box is None and i < len(flat_boxes):
            box, _ = geometry(flat_boxes[i])
        score = confidence(scores[i]) if i < len(scores) else None
        lines.append(OCRLine(text=text.strip(), bbox=to_bbox(box, number), confidence=score, angle=angle))
        boxes.append(box)
        angles.append(angle)
    page_width = width or positive_int(result.get("width"))
    return OCRPage(
        number=number,
        width=page_width,
        height=height or positive_int(result.get("height")),
        lines=tuple(lines),
        layout=infer_layout(
            boxes, angles, width=page_width, rotation=rotation(result)
        ),
    )


def _array(value: Any) -> list[Any]:
    """A JSON array, or empty for anything else (tolerant parsing)."""
    return value if isinstance(value, list) else []


def parse_ocr_response(body: Any, *, first_page: int, engine: str) -> list[OCRPage]:
    """Pages from a PaddleX OCR (or legacy hub-serving) response."""
    if isinstance(body, Mapping) and "status" in body and "results" in body:
        return _parse_hubserving(body, first_page=first_page, engine=engine)
    result = unwrap(body, engine=engine)
    items = first_key(result, "ocrResults", "ocr_results")
    if items is None and ("rec_texts" in result or "res" in result):
        items = [result]
    if not isinstance(items, list):
        raise OCRResponseError(engine, "no OCR results")
    sizes = page_sizes(result)
    pages = []
    for i, item in enumerate(items):
        width, height = sizes[i] if i < len(sizes) else (None, None)
        pages.append(page_from_pruned(pruned(item, engine=engine), number=first_page + i, width=width, height=height))
    return pages or [OCRPage(number=first_page)]


def _parse_hubserving(body: Mapping[str, Any], *, first_page: int, engine: str) -> list[OCRPage]:
    if str(body.get("status")) != "000":
        raise OCRResponseError(engine, f"status {body.get('status')}")
    results = body.get("results")
    if not isinstance(results, list):
        raise OCRResponseError(engine, "results is not a list")
    pages = []
    for i, image_lines in enumerate(results):
        items = [x for x in image_lines if isinstance(x, Mapping)] if isinstance(image_lines, list) else []
        converted = {
            "rec_texts": [x.get("text") for x in items],
            "rec_scores": [x.get("confidence") for x in items],
            "rec_polys": [x.get("text_region") for x in items],
        }
        pages.append(page_from_pruned(converted, number=first_page + i))
    return pages or [OCRPage(number=first_page)]


# --------------------------------------------------------------------------- in-process engine

_SUFFIX = {
    MIME_PNG: ".png", MIME_JPEG: ".jpg", MIME_TIFF: ".tif", MIME_BMP: ".bmp",
    MIME_GIF: ".gif", MIME_WEBP: ".webp", MIME_PDF: ".pdf",
}  # fmt: skip


class InProcessPaddleOCR:
    """PaddleOCR inside this process (optional ``paddleocr`` package).

    ``init_kwargs`` are passed to ``paddleocr.PaddleOCR(...)``; name the
    variant's detection and recognition models there (for example
    ``text_detection_model_name`` / ``text_recognition_model_name``).
    The page is written to a private temporary directory that is removed as
    soon as prediction ends.

    Paddle inference predictors are not thread-safe, so loading and every
    prediction hold one thread lock: pages of concurrent requests queue for
    the single model instead of running inside it at the same time. (A
    thread lock, unlike an asyncio lock, is not tied to one event loop.)
    """

    def __init__(
        self,
        *,
        init_kwargs: Mapping[str, Any] | None = None,
        predict_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        self._init_kwargs = dict(init_kwargs or {})
        self._predict_kwargs = dict(predict_kwargs or {})
        self._engine: Any = None
        self._lock = threading.Lock()

    def _loaded(self) -> Any:
        """The model, created on first use. Call with the lock held."""
        if self._engine is None:
            module = import_optional("paddleocr", feature="In-process PP-OCR")
            self._engine = module.PaddleOCR(**self._init_kwargs)
        return self._engine

    async def predict(self, page: PageImage) -> list[Mapping[str, Any]]:
        """One JSON result per page of ``page`` (several for a PDF)."""
        suffix = _SUFFIX.get(page.mime_type, "")

        def run() -> list[Mapping[str, Any]]:
            with self._lock, tempfile.TemporaryDirectory(prefix="backoffice-ocr-") as tmp:
                engine = self._loaded()
                path = os.path.join(tmp, "page" + suffix)
                with open(path, "wb") as fh:
                    fh.write(page.data)
                return [_result_json(r) for r in engine.predict(path, **self._predict_kwargs)]

        return await asyncio.to_thread(run)


def _result_json(result: Any) -> Mapping[str, Any]:
    payload = getattr(result, "json", result)
    if callable(payload):
        payload = payload()
    if isinstance(payload, Mapping) and isinstance(payload.get("res"), Mapping):
        payload = payload["res"]
    if not isinstance(payload, Mapping):
        raise OCRResponseError("paddleocr", "unexpected result object")
    return payload


# --------------------------------------------------------------------------- provider


class PPOCRv6Provider:
    """PP-OCRv6 behind :class:`~backoffice.ocr.base.OCRProviderInterface`."""

    def __init__(
        self,
        config: PPOCRConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        local: InProcessPaddleOCR | None = None,
        clock: Clock = DEFAULT_CLOCK,
    ) -> None:
        config = config or PPOCRConfig()
        if (config.endpoint is None) == (local is None):
            raise ValueError("configure exactly one backend: an HTTP endpoint or an in-process engine")
        self._config = config
        self._name = f"pp-ocrv6-{config.variant.value}"
        self._http = JSONEndpoint(config.endpoint, engine=self._name, client=client) if config.endpoint else None
        self._local = local
        self._clock = clock

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return self._config.model_version or f"PP-OCRv6-{self._config.variant.value}"

    @property
    def cost_per_page(self) -> Decimal:
        return self._config.cost_per_page

    @property
    def method(self) -> ExtractionMethod:
        return ExtractionMethod.OCR

    @property
    def capabilities(self) -> OCRCapabilities:
        return OCRCapabilities(
            languages=self._config.languages,
            max_pages=self._config.max_pages,
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
        if self._local is not None:
            results = await self._local.predict(page)
            return [page_from_pruned(r, number=page.number + i) for i, r in enumerate(results)] or [
                OCRPage(number=page.number)
            ]
        assert self._http is not None
        payload = {**self._config.request_options, "file": b64(page.data), "fileType": 0 if page.is_pdf else 1}
        body = await self._http.post(self._config.path, payload)
        return parse_ocr_response(body, first_page=page.number, engine=self.name)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
