"""PP-OCRv6 on this server's own CPU, no sidecar, no account, no network (§14, §17, §53).

:class:`LocalOCRProvider` runs the PaddleOCR PP-OCRv6 models through
RapidOCR (the optional ``rapidocr`` package with ``onnxruntime``: the
``ocr-local`` extra). The wheel ships the PP-OCRv6 *small* detection and
multilingual recognition models as ONNX files, so nothing is downloaded at
run time and nothing leaves the machine: Portuguese, Spanish, English and the
other Latin-script languages are read by one engine.

It is the primary engine whenever no PP-OCRv6 sidecar is configured
(``backoffice.reading.config``). Photos are read as they are (EXIF rotation
applied, very large photos scaled down); a PDF's pages are rendered first
(``pypdfium2``, at most ``max_pages``). Text boxes on one printed line are
joined (:mod:`backoffice.ocr.rows`) and every line keeps its box and score,
so each field read from it keeps its location (§18). Layout signals (tables,
columns, skew) come from the raw boxes, as for the sidecar.

The model is loaded once per process, on first use, and every call holds one
thread lock: RapidOCR keeps per-call settings on the shared object.
"""

from __future__ import annotations

import asyncio
import io
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from backoffice.domain.models import ExtractionMethod
from backoffice.extraction._optional import MissingDependencyError, import_optional

from ..base import (
    LATIN_LANGUAGES,
    OCRCapabilities,
    OCRHints,
    OCRInputError,
    OCRLine,
    OCRPage,
    OCRResponseError,
    OCRResult,
    OCRWord,
    PageImage,
    as_pages,
    ensure_supported,
)
from ..layout import geometry, infer_layout, to_bbox
from ..registry import LOCAL_OCR
from ..rows import TextBox, join_rows
from ._common import DEFAULT_CLOCK, Clock, build_result

__all__ = ["LocalOCRConfig", "LocalOCRProvider", "local_ocr_available"]

_FEATURE = "Reading photos and scanned PDFs on this server"


def local_ocr_available() -> bool:
    """True when the local engine can run here (``rapidocr`` and ``onnxruntime`` are installed)."""
    try:
        import_optional("onnxruntime", feature=_FEATURE)
        import_optional("cv2", feature=_FEATURE, package="opencv-python-headless")  # fails without libGL
        import_optional("rapidocr", feature=_FEATURE)
        import_optional("PIL.Image", feature=_FEATURE, package="Pillow")
    except (MissingDependencyError, OSError):  # OSError: a native library the package needs is missing
        return False
    return True


@dataclass(frozen=True)
class LocalOCRConfig:
    """``max_side`` caps a photo's longer side before reading (phones take 12-megapixel photos; text that
    small is not readable anyway). ``pdf_dpi`` and ``max_pages`` decide how a PDF is rendered."""

    name: str = LOCAL_OCR
    max_pages: int = 10
    pdf_dpi: int = 200
    max_side: int = 2400
    languages: frozenset[str] = LATIN_LANGUAGES
    params: Mapping[str, Any] = field(default_factory=dict)  # extra RapidOCR settings ("Det.box_thresh": 0.6)


# One model per process, whatever the number of readers (tests, workers): loading takes a second or two.
_ENGINES: dict[tuple[tuple[str, Any], ...], Any] = {}
_ENGINES_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()


def _engine(params: Mapping[str, Any]) -> Any:
    key = tuple(sorted(params.items()))
    with _ENGINES_LOCK:
        engine = _ENGINES.get(key)
        if engine is None:
            import_optional("onnxruntime", feature=_FEATURE)
            rapidocr = import_optional("rapidocr", feature=_FEATURE)
            engine = rapidocr.RapidOCR(params={"Global.log_level": "warning", **params})
            _ENGINES[key] = engine
        return engine


def _model_version() -> str:
    try:
        from importlib.metadata import version

        runtime = version("rapidocr")
    except Exception:  # metadata missing (vendored install): the model is what matters
        runtime = "?"
    return f"PP-OCRv6 small (ONNX) via RapidOCR {runtime}"


class LocalOCRProvider:
    """PP-OCRv6 in this process behind :class:`~backoffice.ocr.base.OCRProviderInterface` (module docstring)."""

    def __init__(self, config: LocalOCRConfig | None = None, *, clock: Clock = DEFAULT_CLOCK) -> None:
        self._config = config or LocalOCRConfig()
        self._clock = clock
        self._version: str | None = None

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def version(self) -> str:
        if self._version is None:
            self._version = _model_version()
        return self._version

    @property
    def cost_per_page(self) -> Decimal:
        return Decimal("0")

    @property
    def method(self) -> ExtractionMethod:
        return ExtractionMethod.OCR

    @property
    def capabilities(self) -> OCRCapabilities:
        return OCRCapabilities(languages=self._config.languages, max_pages=self._config.max_pages,
                               accepts_pdf=True, returns_boxes=True)

    async def recognize(self, pages: Sequence[PageImage | bytes], hints: OCRHints | None = None) -> OCRResult:
        started = self._clock()
        items = as_pages(pages)
        ensure_supported(items, engine=self.name)
        out: list[OCRPage] = []
        for page in items:
            out += await asyncio.to_thread(self._read, page)
        if len(out) > self._config.max_pages:
            raise OCRInputError(self.name, f"more than {self._config.max_pages} pages")
        return build_result(engine=self.name, version=self.version, method=self.method, pages=out,
                            cost_per_page=self.cost_per_page, pages_billed=len(out), started=started,
                            clock=self._clock)

    # ----------------------------------------------------------------- one input

    def _read(self, page: PageImage) -> list[OCRPage]:
        """Every page of one input (a photo, or a PDF's rendered pages), numbered from ``page.number``."""
        if page.is_pdf:
            from backoffice.extraction.fields import StructuredDataError
            from backoffice.extraction.pdf import render_pdf_pages

            try:
                rendered = render_pdf_pages(page.data, max_pages=self._config.max_pages, dpi=self._config.pdf_dpi)
            except StructuredDataError as exc:
                raise OCRInputError(self.name, exc.code) from None
            images = [png for png, _, _ in rendered]
        else:
            images = [page.data]
        return [self._read_image(data, page.number + i) for i, data in enumerate(images)]

    def _read_image(self, data: bytes, number: int) -> OCRPage:
        pil = import_optional("PIL.Image", feature=_FEATURE, package="Pillow")
        ops = import_optional("PIL.ImageOps", feature=_FEATURE, package="Pillow")
        try:
            with pil.open(io.BytesIO(data)) as opened:
                image = ops.exif_transpose(opened).convert("RGB")
        except Exception as exc:  # not an image Pillow can open: nothing to read
            raise OCRInputError(self.name, type(exc).__name__) from None
        longest = max(image.size)
        if longest > self._config.max_side:
            scale = self._config.max_side / longest
            image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                                 pil.Resampling.LANCZOS)
        engine = _engine(self._config.params)
        with _RUN_LOCK:
            try:
                result = engine(image)
            except Exception as exc:  # the model failed on this image: recorded by the router, chain continues
                raise OCRResponseError(self.name, type(exc).__name__) from None
        return _page(result, number, image.width, image.height)


def _page(result: Any, number: int, width: int, height: int) -> OCRPage:
    texts = list(getattr(result, "txts", None) or ())
    scores = list(getattr(result, "scores", None) or ())
    raw_boxes = getattr(result, "boxes", None)
    polygons = [b.tolist() if hasattr(b, "tolist") else list(b) for b in raw_boxes] if raw_boxes is not None else []
    found = [TextBox(text=str(t), confidence=_score(scores[i] if i < len(scores) else None), polygon=polygons[i])
             for i, t in enumerate(texts) if i < len(polygons)]
    shapes = [geometry(list(b.polygon)) for b in found]
    layout = infer_layout([s[0] for s in shapes], [s[1] for s in shapes], width=width)
    lines = tuple(
        OCRLine(text=row.text, bbox=to_bbox(row.box, number), confidence=row.confidence, angle=row.angle,
                words=tuple(OCRWord(text=m.text, bbox=to_bbox(m.box, number), confidence=m.confidence)
                            for m in row.members))  # each box the engine found: a table's cells (§13)
        for row in join_rows(found)
    )
    return OCRPage(number=number, width=width, height=height, lines=lines, layout=layout)


def _score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return min(max(score, 0.0), 1.0) if score == score else None
