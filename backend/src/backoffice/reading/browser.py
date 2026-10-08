"""Reading photos and scanned PDFs in the visitor's browser (the static demo, §13-19).

The static website runs this engine on Pyodide with pydantic only: no
Pillow, no pypdf, no OCR model. The page itself reads the file before it is
sent to the engine (web/lib/ocr.ts: tesseract.js with Portuguese and English
models for the text, a JavaScript QR decoder for the fiscal QR code, pdf.js
for a PDF's text layer and pages), all self-hosted, nothing sent anywhere.
What it read travels with the upload as a *device reading*
(``POST /api/evidence`` with ``reading``, wire format in
:meth:`DeviceReading.from_json`).

:class:`BrowserReader` is a :class:`~backoffice.reading.reader.DocumentReader`
whose engines answer from those readings, looked up by the file's SHA-256:

* the OCR lines are the primary engine's reading (``browser-ocr``, method
  OCR, every line with its box and score), so the normal router, field
  extraction and verification apply: one reading of pixels is never GREEN
  on its own (§57);
* the QR codes decoded on the page are Stage 0's QR decoder (``browser-qr``),
  for photos and for a PDF's rendered pages alike;
* a PDF's own text layer and metadata, as pdf.js read them, are Stage 0's
  PDF text (labelled OCR when a scanner's OCR tool made the layer, as on the
  server);
* the edge sharpness measured on the page is the photo's pixel measurement
  (a blurred photo escalates and becomes the retake task, §11).

Only a reader that says so (``accepts_device_readings``) takes them: the
production server reads every file itself and ignores any reading a client
sends. Replaying the demo's journal sends the same reading again, so a reload
gives the same result without reading the file a second time.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from backoffice.domain.models import BoundingBox, ExtractionMethod
from backoffice.extraction.quality import ImageMetrics

from .reader import DocumentReader, ReadOutcome, ReadRequest
from .stage0 import ReadStep, Stage0, StepState, read_pdf_stage0

__all__ = [
    "BROWSER_OCR",
    "BROWSER_QR",
    "BrowserOCRProvider",
    "BrowserReader",
    "DeviceReading",
    "DeviceReadingError",
    "SuppliedQRDecoder",
]

BROWSER_OCR = "browser-ocr"
BROWSER_QR = "browser-qr"
WIRE_METHOD = "ocr_browser"

MAX_PAGES = 20
MAX_LINES = 600
MAX_LINE_CHARS = 400
MAX_QR = 8
MAX_QR_CHARS = 2000
MAX_TEXT_LAYER_CHARS = 300_000
_METADATA_KEYS = ("Producer", "Creator", "Title", "Subject", "Keywords")


class DeviceReadingError(ValueError):
    """A device reading that is not well formed: it is ignored, never half used."""


@dataclass(frozen=True)
class DeviceReading:
    """What the visitor's browser read from one file."""

    engine: str
    version: str
    pages: tuple[Any, ...]  # OCRPage
    qr: tuple[str, ...] = ()
    text_layer: tuple[str, ...] | None = None  # a PDF's own text, one entry per page (None: not a PDF / unread)
    metadata: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    sharpness: float | None = None
    ms: int = 0

    @classmethod
    def from_json(cls, payload: Any) -> DeviceReading:
        """Parse the wire format::

            {"method": "ocr_browser", "engine": "tesseract.js", "version": "7.0.0 por+eng",
             "pages": [{"number": 1, "width": 1200, "height": 1600,
                        "lines": [{"text": "Total: 8,40 EUR", "confidence": 0.91, "box": [x0, y0, x1, y1]}]}],
             "qr": ["A:509882412*B:..."], "textLayer": ["..."], "metadata": {"Producer": "..."},
             "sharpness": 0.83, "ms": 2400}

        Boxes are pixels of the page image as read; confidence is 0-1. Anything malformed raises
        :class:`DeviceReadingError`.
        """
        from backoffice.ocr import OCRLine, OCRPage
        from backoffice.ocr.layout import infer_layout
        from backoffice.ocr.rows import TextBox, join_rows

        if not isinstance(payload, Mapping):
            raise DeviceReadingError("a reading is an object")
        if payload.get("method") != WIRE_METHOD:
            raise DeviceReadingError(f"method must be {WIRE_METHOD!r}")
        engine = _text(payload.get("engine"), 40) or "browser"
        version = _text(payload.get("version"), 80) or "?"
        raw_pages = payload.get("pages") or []
        if not isinstance(raw_pages, list) or len(raw_pages) > MAX_PAGES:
            raise DeviceReadingError("pages must be a list of at most %d" % MAX_PAGES)
        pages = []
        for index, raw in enumerate(raw_pages, start=1):
            if not isinstance(raw, Mapping):
                raise DeviceReadingError("a page is an object")
            number = _int(raw.get("number"), index)
            if number != index:
                raise DeviceReadingError("pages are numbered 1, 2, 3, ...")
            width, height = _int(raw.get("width"), None), _int(raw.get("height"), None)
            raw_lines = raw.get("lines") or []
            if not isinstance(raw_lines, list) or len(raw_lines) > MAX_LINES:
                raise DeviceReadingError("lines must be a list of at most %d" % MAX_LINES)
            found = []
            for line in raw_lines:
                if not isinstance(line, Mapping):
                    raise DeviceReadingError("a line is an object")
                text = _text(line.get("text"), MAX_LINE_CHARS)
                box = _box(line.get("box"))
                if not text or box is None:
                    continue
                found.append(TextBox(text=text, confidence=_confidence(line.get("confidence")), polygon=box))
            rows = join_rows(found)
            boxes = [r.box for r in rows]
            pages.append(OCRPage(
                number=number, width=width, height=height,
                lines=tuple(OCRLine(text=r.text, confidence=r.confidence, angle=r.angle,
                                    bbox=BoundingBox(page=number, x0=r.box[0], y0=r.box[1], x1=r.box[2],
                                                     y1=r.box[3]) if r.box else None) for r in rows),
                layout=infer_layout(boxes, [r.angle for r in rows], width=width),
            ))
        qr = payload.get("qr") or []
        if not isinstance(qr, list) or len(qr) > MAX_QR:
            raise DeviceReadingError("qr must be a list of at most %d" % MAX_QR)
        codes = tuple(dict.fromkeys(c for c in (_text(q, MAX_QR_CHARS, keep_spaces=True) for q in qr) if c))
        text_layer = payload.get("textLayer")
        layer: tuple[str, ...] | None = None
        if text_layer is not None:
            if not isinstance(text_layer, list) or len(text_layer) > MAX_PAGES * 25 or not all(
                    isinstance(t, str) for t in text_layer):
                raise DeviceReadingError("textLayer is a list of page texts")
            if sum(len(t) for t in text_layer) > MAX_TEXT_LAYER_CHARS:
                raise DeviceReadingError("textLayer is too long")
            layer = tuple(text_layer)
        raw_meta = payload.get("metadata") or {}
        if not isinstance(raw_meta, Mapping):
            raise DeviceReadingError("metadata is an object")
        metadata = {k: v for k in _METADATA_KEYS if (v := _text(raw_meta.get(k), 2000, keep_spaces=True))}
        sharpness = payload.get("sharpness")
        if sharpness is not None and not (isinstance(sharpness, (int, float)) and not isinstance(sharpness, bool)
                                          and math.isfinite(sharpness) and 0 <= sharpness <= 1):
            raise DeviceReadingError("sharpness is a number from 0 to 1")
        if not pages and not codes and layer is None:
            raise DeviceReadingError("the reading is empty")
        return cls(engine=engine, version=version, pages=tuple(pages), qr=codes, text_layer=layer,
                   metadata=MappingProxyType(metadata), sharpness=float(sharpness) if sharpness is not None else None,
                   ms=max(0, _int(payload.get("ms"), 0) or 0))


def _text(value: Any, limit: int, *, keep_spaces: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    value = "".join(ch for ch in value if ch >= " " or ch == "\t").strip()
    return value[:limit] if keep_spaces else " ".join(value.split())[:limit]


def _int(value: Any, default: int | None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return default
    return int(value)


def _confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return min(max(float(value), 0.0), 1.0)


def _box(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in value):
        return None
    x0, y0, x1, y1 = (float(v) for v in value)
    return [x0, y0, x1, y1] if x1 > x0 and y1 > y0 else None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- the engines


class _Readings:
    """Device readings by the SHA-256 of the file they were made from."""

    def __init__(self) -> None:
        self._by_hash: dict[str, DeviceReading] = {}

    def put(self, data: bytes, reading: DeviceReading) -> None:
        self._by_hash[_sha(data)] = reading

    def get(self, data: bytes) -> DeviceReading | None:
        return self._by_hash.get(_sha(data))


class SuppliedQRDecoder:
    """Stage 0's QR decoder: the codes the browser decoded on the photo, or on a PDF's rendered pages."""

    name = BROWSER_QR
    reads_whole_files = True  # given the PDF itself, not page images (stage0._qr_from_pages)

    def __init__(self, readings: _Readings) -> None:
        self._readings = readings

    def decode(self, image: bytes) -> tuple[str, ...]:
        reading = self._readings.get(image)
        return reading.qr if reading is not None else ()


class BrowserOCRProvider:
    """The browser's OCR lines behind :class:`~backoffice.ocr.base.OCRProviderInterface` (local, free)."""

    def __init__(self, readings: _Readings) -> None:
        self._readings = readings
        self._version = "made in the browser"

    @property
    def name(self) -> str:
        return BROWSER_OCR

    @property
    def version(self) -> str:
        return self._version

    @property
    def cost_per_page(self) -> Decimal:
        return Decimal("0")

    @property
    def method(self) -> ExtractionMethod:
        return ExtractionMethod.OCR

    @property
    def capabilities(self) -> Any:
        from backoffice.ocr import LATIN_LANGUAGES, OCRCapabilities

        return OCRCapabilities(languages=LATIN_LANGUAGES, accepts_pdf=True, returns_boxes=True)

    async def recognize(self, pages: Sequence[Any], hints: Any = None) -> Any:
        from backoffice.ocr import OCRInputError, OCRResult, as_pages
        from backoffice.ocr.base import LayoutSignals

        out = []
        versions: list[str] = []
        ms = 0
        for page in as_pages(pages):
            reading = self._readings.get(page.data)
            if reading is None or not reading.pages:
                raise OCRInputError(self.name, "not read in this browser")
            versions.append(f"{reading.engine} {reading.version}")
            ms += reading.ms
            for i, read in enumerate(reading.pages):
                number = page.number + i
                lines = tuple(ln.model_copy(update={"bbox": ln.bbox.model_copy(update={"page": number})})
                              if ln.bbox is not None else ln for ln in read.lines)
                out.append(read.model_copy(update={"number": number, "lines": lines}))
        return OCRResult(engine=self.name, model_version=", ".join(dict.fromkeys(versions)) or self._version,
                         method=self.method, pages=tuple(out), layout=LayoutSignals.combine(p.layout for p in out),
                         duration_ms=ms, pages_billed=len(out))


# --------------------------------------------------------------------------- the reader


class BrowserReader(DocumentReader):
    """The document reader of the static demo: the browser's readings, then the normal chain (module docstring)."""

    accepts_device_readings = True

    def __init__(self) -> None:
        from backoffice.ocr import EngineRegistry, EngineRoles, RouterConfig

        self._readings = _Readings()
        super().__init__(
            registry=EngineRegistry([BrowserOCRProvider(self._readings)]),
            router_config=RouterConfig(roles=EngineRoles(primary=BROWSER_OCR, complex_layout=None,
                                                         long_document=None, commercial=None),
                                       corroborate_single_source=True),
            qr_decoder=SuppliedQRDecoder(self._readings),
            external_ai=False,
        )

    def supply(self, data: bytes, payload: Any) -> DeviceReading:
        """Keep what the browser read from ``data`` (raises :class:`DeviceReadingError` when malformed)."""
        reading = DeviceReading.from_json(payload)
        self._readings.put(data, reading)
        return reading

    def read(self, request: ReadRequest) -> ReadOutcome:
        if self._readings.get(request.data) is None:
            return ReadOutcome(steps=(ReadStep("ocr_browser", StepState.NOT_AVAILABLE,
                                               "this browser did not read the file"),))
        return super().read(request)

    def measure(self, data: bytes) -> ImageMetrics | None:
        reading = self._readings.get(data)
        if reading is None or reading.sharpness is None:
            return None
        return ImageMetrics(edge_sharpness=reading.sharpness)

    def stage0_pdf(self, data: bytes) -> Stage0:
        """The PDF's own text layer and metadata as pdf.js read them in the browser (no pypdf here)."""
        from backoffice.extraction.pdf import PdfContent

        reading = self._readings.get(data)
        if reading is None or reading.text_layer is None:
            return super().stage0_pdf(data)
        content = PdfContent(page_texts=reading.text_layer, metadata=reading.metadata)
        stage0 = read_pdf_stage0(data, self.qr_decoder, content=content)
        steps = tuple(replace(s, detail=f"{s.detail} (read in the browser)") if s.step == "pdf_text" else s
                      for s in stage0.steps)
        return replace(stage0, steps=steps)
