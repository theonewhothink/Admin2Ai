"""Reading uploaded PDFs and photos: Stage 0, then the OCR chain (§13-19, §53).

:class:`DocumentReader` turns the bytes of one piece of evidence into what
the orchestrator's Document agent needs:

1. **Stage 0** (:mod:`.stage0`): the PDF text layer, Portuguese invoice QR
   payloads, embedded e-invoice XML. The caller's country pack turns them
   into field observations (``ReadRequest.stage0_fields``).
2. **The OCR chain**: the existing :class:`~backoffice.ocr.router.OCRRouter`
   with the engines registered by name (a PP-OCRv6 / PaddleOCR-VL sidecar, or
   PP-OCRv6 in this process when no sidecar is configured, the Claude vision
   fallback). The router skips OCR entirely when Stage 0
   settles every required field, never pays when the document contradicts
   itself, and only calls the paid external engine while fields are missing
   or disputed, within the tenant's budget (§17).
3. **External AI** runs only when switched on (``external_ai``); otherwise
   the commercial role is removed before routing, so nothing can leave.
   Even when it is on, a sensitive document (``ReadRequest.sensitive``:
   medical, legal, pay or staff wording in its own text or what a local
   engine read from it) never leaves: every external engine is guarded and
   answers "kept on our servers" without sending anything (§52, §53).

Nothing here decides quality: readings are returned with their engine as
source and method OCR/VLM, and the Verification agent grades them (§18, §57).
A photo's quality is the phone's hints plus the reader's own estimate: its
header (size, resolution, orientation) and, when Pillow and numpy are
installed, the sharpness of its print (``measure_pixels``): a blurred photo
goes to the stronger engine and, if nothing can read it, becomes one task to
take it again (§11).

The orchestrator is synchronous (and runs in a browser without any of
this); engines are asynchronous. :func:`run_sync` runs the chain on a fresh
event loop, in a helper thread when the caller is already inside one (the
FastAPI handlers are ``async``). Engines' HTTP clients are closed inside that
loop after every document. In the browser (Pyodide) there is no blocking event
loop: the chain is stepped directly, which works because the browser's engines
answer from readings already made and never wait.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, TypeVar

from backoffice.domain.models import CriticalField, DocumentType, ExtractionMethod, FieldObservation
from backoffice.extraction.fields import FieldExtractor
from backoffice.extraction.media import IMAGE_MIME_TYPES, MIME_PDF, sniff_mime

from .stage0 import QRDecoder, ReadStep, Stage0, StepState, find_qr_decoder, read_image_stage0, read_pdf_stage0

__all__ = ["AUTO", "DocumentReader", "ReadOutcome", "ReadRequest", "run_sync"]

T = TypeVar("T")
AUTO = "auto"

Stage0Fields = Callable[[str, ExtractionMethod], Mapping[Any, Sequence[FieldObservation]]]

_READING_METHODS = frozenset({ExtractionMethod.OCR, ExtractionMethod.VLM})
_RETAKE_FLAGS = ("blurry", "glare", "too_dark", "overexposed", "low_resolution")
_VLM_TYPES = {
    "invoice": DocumentType.INVOICE,
    "invoice_receipt": DocumentType.INVOICE_RECEIPT,
    "simplified_invoice": DocumentType.SIMPLIFIED_INVOICE,
    "receipt": DocumentType.RECEIPT,
    "credit_note": DocumentType.CREDIT_NOTE,
    "debit_note": DocumentType.DEBIT_NOTE,
}


def run_sync(factory: Callable[[], Awaitable[T]]) -> T:
    """Run a coroutine to completion from synchronous code, inside or outside an event loop."""
    if sys.platform == "emscripten":  # Pyodide: no thread, no loop that can block
        return _step(factory())
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_as_coroutine(factory))
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="document-reader") as pool:
        return pool.submit(lambda: asyncio.run(_as_coroutine(factory))).result()


async def _as_coroutine(factory: Callable[[], Awaitable[T]]) -> T:
    return await factory()


def _step(awaitable: Awaitable[T]) -> T:
    """Run a coroutine that never waits on anything, without an event loop."""
    coroutine = _as_coroutine(lambda: awaitable)
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value  # type: ignore[no-any-return]
    coroutine.close()
    raise RuntimeError("an engine waited on input or output, which this environment cannot do")


@dataclass(frozen=True)
class ReadRequest:
    """One file to read.

    ``stage0_fields(text, method)`` reads fields from Stage 0 text (the
    country pack, e.g. Portuguese text fields and the fiscal QR);
    ``extractor`` reads fields from an OCR engine's text.
    """

    tenant_id: str
    evidence_id: str
    data: bytes = field(repr=False)
    mime_type: str | None = None
    stage0_fields: Stage0Fields | None = None
    extractor: FieldExtractor | None = None
    required_fields: Collection[CriticalField] | None = None
    # What the phone noticed when the photo was taken ("blurry", "glare", "too_dark"; §11). Advisory only: with
    # the reader's own estimate of the image they route a poor photo to the stronger engine (§15), never a value.
    quality_hints: tuple[str, ...] = ()
    # True when a text read from the file (its own text, or a local engine's reading) shows it is
    # sensitive (backoffice.sensitivity): then no external engine sees it, whatever the settings.
    sensitive: Callable[[str], bool] | None = None


@dataclass(frozen=True)
class ReadOutcome:
    """What reading one file produced. Stage 0 text is re-read by the caller's pack;
    ``readings`` are OCR/VLM observations (source ``<evidence>@<engine>``)."""

    text: str = ""
    text_method: ExtractionMethod = ExtractionMethod.EMBEDDED_TEXT
    embedded_xml: tuple[bytes, ...] = ()
    readings: Mapping[str, tuple[FieldObservation, ...]] = field(default_factory=dict)
    reading_text: str = ""
    supplier_name: str | None = None
    doc_type: DocumentType | None = None
    page_count: int | None = None
    steps: tuple[ReadStep, ...] = ()
    cost: Decimal = Decimal("0")
    # The image's problems (the phone's hints and the reader's own estimate: "blurry", "glare", "too_dark",
    # "overexposed", "low_resolution", "rotated", "skewed"), as the OCR chain was routed with them.
    image_quality: tuple[str, ...] = ()
    # Every engine that could run did, and required fields are still missing or disagree (§17: a person's turn).
    needs_person: bool = False

    @property
    def retake_worthy(self) -> tuple[str, ...]:
        """The problems a new photo would fix (§11): blur, glare, too dark or too bright, too small."""
        return tuple(f for f in self.image_quality if f in _RETAKE_FLAGS)

    @property
    def found_anything(self) -> bool:
        return bool(self.text.strip() or self.embedded_xml or self.readings)

    @property
    def engines(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(s.engine for s in self.steps if s.engine and s.state is StepState.DONE))

    def missing_readers(self) -> tuple[ReadStep, ...]:
        return tuple(s for s in self.steps if s.state is StepState.NOT_AVAILABLE)


class DocumentReader:
    """Stage 0 plus the OCR chain for PDFs and images (module docstring).

    ``registry`` holds the engines by their conventional names (see
    :mod:`backoffice.ocr.registry`); None or empty means no OCR engine.
    ``qr_decoder`` is a :class:`~.stage0.QRDecoder`, None for none, or
    ``AUTO`` to use whichever is installed.
    """

    def __init__(
        self,
        *,
        registry: Any = None,
        budget: Any = None,
        router_config: Any = None,
        qr_decoder: QRDecoder | str | None = AUTO,
        external_ai: bool = False,
    ) -> None:
        self._registry = registry
        self._budget = budget
        self._router_config = router_config
        self._qr_decoder = find_qr_decoder() if qr_decoder == AUTO else qr_decoder
        self.external_ai = bool(external_ai)

    @property
    def qr_decoder(self) -> QRDecoder | None:
        return self._qr_decoder if not isinstance(self._qr_decoder, str) else None

    def engines(self) -> tuple[str, ...]:
        return tuple(self._registry.names()) if self._registry is not None else ()

    # ----------------------------------------------------------------- reading

    def read(self, request: ReadRequest) -> ReadOutcome:
        mime = sniff_mime(request.data) or request.mime_type
        if mime == MIME_PDF:
            stage0 = self.stage0_pdf(request.data)
        elif mime in IMAGE_MIME_TYPES:
            stage0 = read_image_stage0(request.data, self.qr_decoder)
        else:
            return ReadOutcome(steps=(ReadStep("read", StepState.SKIPPED, "not a PDF or an image"),))
        structured = self._structured(stage0, request)
        steps = list(stage0.steps)
        quality = self._quality(request, mime)
        flags = tuple(sorted(f.value for f in quality.flags)) if quality is not None else ()
        outcome = ReadOutcome(text=stage0.text, text_method=stage0.method, embedded_xml=stage0.embedded_xml,
                              page_count=stage0.page_count, image_quality=flags)
        if any(s.step == "pdf_text" and s.state is StepState.FAILED for s in stage0.steps):
            return replace(outcome, steps=tuple(steps))  # encrypted or damaged: no engine will do better
        ocr = self._ocr(request, mime, stage0, structured, quality)
        if ocr is None:
            steps.append(self._no_ocr_step(structured, request))
            return replace(outcome, steps=tuple(steps))
        return replace(
            outcome,
            readings=ocr["readings"], reading_text=ocr["text"], supplier_name=ocr["supplier"],
            doc_type=ocr["doc_type"], steps=(*steps, *ocr["steps"]), cost=ocr["cost"],
            needs_person=ocr["needs_person"],
        )

    def stage0_pdf(self, data: bytes) -> Stage0:
        """Stage 0 of a PDF: its text layer, QR payloads and embedded XML."""
        return read_pdf_stage0(data, self.qr_decoder)

    def measure(self, data: bytes) -> Any:
        """Pixel measurements of a photo (edge sharpness), when Pillow and numpy are installed; else None."""
        from backoffice.extraction.quality import measure_pixels

        return measure_pixels(data)

    def _quality(self, request: ReadRequest, mime: str) -> Any:
        """The image's quality report (§11, §15): the reader's own estimate from the photo's header (size,
        resolution, orientation) and its pixels (sharpness), plus what the phone measured when it was
        taken. None for a PDF the phone said nothing about."""
        from backoffice.extraction.quality import ImageMetrics, QualityFlag, QualityReport, assess_image

        known = {f.value for f in QualityFlag}
        phone = frozenset(QualityFlag(h) for h in request.quality_hints if h in known)
        if mime in IMAGE_MIME_TYPES:
            report = assess_image(request.data, self.measure(request.data))
        elif phone:
            report = QualityReport(frozenset(), ImageMetrics(), frozenset())
        else:
            return None
        return replace(report, flags=report.flags | phone) if phone else report

    def _structured(self, stage0: Stage0, request: ReadRequest) -> dict[CriticalField, list[FieldObservation]]:
        """Stage 0 observations for routing: the pack's reading of text and QR, plus embedded XML."""
        from backoffice.extraction.einvoice import parse_einvoice

        found: dict[CriticalField, list[FieldObservation]] = {}
        if stage0.text and request.stage0_fields is not None:
            for name, observations in request.stage0_fields(stage0.text, stage0.method).items():
                found.setdefault(CriticalField(name), []).extend(observations)
        for blob in stage0.embedded_xml:
            try:
                result = parse_einvoice(blob, source=request.evidence_id)
            except Exception:  # an attachment that is not an e-invoice is simply not evidence
                continue
            for name, observations in result.fields.items():
                found.setdefault(CriticalField(name), []).extend(observations)
        return found

    def _required(self, request: ReadRequest) -> frozenset[CriticalField]:
        from backoffice.ocr.router import INVOICE_FIELDS

        return frozenset(request.required_fields) if request.required_fields else INVOICE_FIELDS

    def _no_ocr_step(self, structured: Mapping[CriticalField, Sequence[FieldObservation]],
                     request: ReadRequest) -> ReadStep:
        from backoffice.ocr.consensus import FieldState, build_consensus

        consensus = build_consensus(structured, required=self._required(request)) if structured else None
        if consensus is not None and consensus.settled and all(
                consensus.fields[f].state is not FieldState.SINGLE for f in consensus.required):
            return ReadStep("ocr", StepState.SKIPPED, "not needed: two of the file's own sources agree on every field")
        return ReadStep("ocr", StepState.NOT_AVAILABLE, "no OCR engine is configured")

    def _ocr(self, request: ReadRequest, mime: str, stage0: Stage0,
             structured: Mapping[CriticalField, Sequence[FieldObservation]], quality: Any = None
             ) -> dict[str, Any] | None:
        registry = self._registry
        if registry is None or not len(registry) or request.extractor is None:
            return None
        from backoffice.ocr import (
            COMMERCIAL,
            LOCAL_OCR,
            OCRHints,
            OCRRouter,
            PageImage,
            RouterConfig,
            RoutingRequest,
        )

        config = self._router_config or RouterConfig(corroborate_single_source=True)
        if config.roles.primary not in registry and LOCAL_OCR in registry:
            # No PP-OCRv6 sidecar: PP-OCRv6 in this process is the primary engine (§14).
            config = replace(config, roles=replace(config.roles, primary=LOCAL_OCR))
        extra: list[ReadStep] = []
        if not self.external_ai:
            config = replace(config, roles=replace(config.roles, commercial=None))
            if registry.find(COMMERCIAL) is not None or any(
                    registry.find(n).capabilities.external for n in registry.names()):
                extra.append(ReadStep("external_ai", StepState.OFF, "BACKOFFICE_EXTERNAL_AI is off"))
        elif request.sensitive is not None:
            if request.sensitive(stage0.text):
                # Sensitive by its own text: no external engine runs at all (§52, §53).
                config = replace(config, roles=replace(config.roles, **{
                    role: None for role in ("primary", "complex_layout", "long_document", "commercial")
                    if _is_external(registry, getattr(config.roles, role), role)}))
                extra.append(ReadStep("external_ai", StepState.OFF, "sensitive document: kept on our servers"))
            else:
                registry = _guarded(registry, request.sensitive, stage0.text)
        router = OCRRouter(registry, request.extractor, config=config, budget=self._budget)
        page = PageImage(data=request.data, mime_type=mime, page_count=stage0.page_count)
        routing = RoutingRequest(
            tenant_id=request.tenant_id, evidence_id=request.evidence_id, pages=[page],
            structured={f: tuple(o) for f, o in structured.items()},
            hints=OCRHints(page_count=stage0.page_count), required_fields=self._required(request),
            image_quality=quality,  # a poor photo goes to the complex-document engine too (§15)
        )

        async def route() -> Any:
            try:
                return await router.route(routing)
            finally:
                for name in registry.names():
                    closer = getattr(registry.find(name), "aclose", None)
                    if closer is not None:
                        try:
                            await closer()
                        except Exception:  # closing a client must never lose a finished reading
                            pass

        result = run_sync(route)
        return {
            "readings": _readings(result),
            "text": _reading_text(result),
            "supplier": next((r.supplier_name for r in result.results if r.supplier_name), None),
            "doc_type": next((_VLM_TYPES[r.document_type] for r in result.results
                              if r.document_type in _VLM_TYPES), None),
            "steps": [*extra, *_steps(result)],
            "cost": result.total_cost,
            "needs_person": result.needs_human,
        }


# --------------------------------------------------------------------------- sensitive documents stay here


def _is_external(registry: Any, name: str | None, role: str) -> bool:
    from backoffice.ocr import COMMERCIAL

    if name is None:
        return False
    engine = registry.find(name)
    return role == "commercial" or name == COMMERCIAL or (engine is not None and engine.capabilities.external)


class _KeptHere:
    """An external engine that first checks what our own engines read: a sensitive document is answered
    "kept on our servers" and nothing is sent (§52, §53)."""

    def __init__(self, inner: Any, sensitive: Callable[[str], bool], known_text: str) -> None:
        self._inner = inner
        self._sensitive = sensitive
        self._known = known_text

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def version(self) -> str:
        return self._inner.version

    @property
    def cost_per_page(self) -> Decimal:
        return self._inner.cost_per_page

    @property
    def capabilities(self) -> Any:
        return self._inner.capabilities

    @property
    def method(self) -> Any:
        return self._inner.method

    async def recognize(self, pages: Any, hints: Any = None) -> Any:
        from backoffice.ocr import OCRResult

        seen = [self._known, getattr(hints, "prior_text", None) or ""]
        seen += [p.text for p in getattr(hints, "prior_pages", ()) or ()]
        if self._sensitive("\n".join(t for t in seen if t)):
            return OCRResult(engine=self._inner.name, model_version=self._inner.version, method=self._inner.method,
                             pages=(), warnings=("sensitive document: kept on our servers",))
        return await self._inner.recognize(pages, hints)


def _guarded(registry: Any, sensitive: Callable[[str], bool], known_text: str) -> Any:
    """The registry with every external engine guarded by :class:`_KeptHere`."""
    from backoffice.ocr import COMMERCIAL, EngineRegistry

    out = EngineRegistry()
    for name in registry.names():
        engine = registry.find(name)
        external = name == COMMERCIAL or bool(getattr(engine.capabilities, "external", False))
        out.register(_KeptHere(engine, sensitive, known_text) if external else engine, name=name)
    return out


# --------------------------------------------------------------------------- outcome helpers


def _readings(outcome: Any) -> dict[str, tuple[FieldObservation, ...]]:
    """OCR/VLM observations only: Stage 0 observations are re-read by the caller's pack."""
    found: dict[str, list[FieldObservation]] = {}
    for name, consensus in outcome.fields.items():
        for observation in consensus.observations:
            if observation.method not in _READING_METHODS:
                continue
            bucket = found.setdefault(CriticalField(name).value, [])
            if observation not in bucket:
                bucket.append(observation)
    return {k: tuple(v) for k, v in found.items() if v}


def _reading_text(outcome: Any) -> str:
    """The first local engine's transcription (a VLM answer is a field list, not a transcription)."""
    for result in outcome.results:
        if not result.fields and result.full_text.strip():
            return result.full_text
    return ""


def _steps(outcome: Any) -> list[ReadStep]:
    from backoffice.ocr import ReasonCode, RouteStage, StepStatus

    unavailable = {ReasonCode.NOT_CONFIGURED, ReasonCode.ENGINE_NOT_REGISTERED}
    warnings = {r.engine: r.warnings for r in outcome.results}
    steps = []
    for s in outcome.steps:
        detail = ", ".join(r.code.value + (f":{r.detail}" if r.detail else "") for r in s.reasons)
        if s.stage is RouteStage.HUMAN:  # kept: the owner is asked (a retake, or the reading to check)
            steps.append(ReadStep("ocr_human", StepState.NEEDS_PERSON, detail))
            continue
        if s.status is StepStatus.RAN:
            state = StepState.DONE
            extra = warnings.get(s.engine or "", ())
            if extra:
                detail = ", ".join([detail, *extra]) if detail else ", ".join(extra)
        elif s.status is StepStatus.FAILED:
            state = StepState.FAILED
        elif any(r.code in unavailable for r in s.reasons):
            state = StepState.NOT_AVAILABLE
        else:
            state = StepState.SKIPPED
        steps.append(ReadStep(f"ocr_{s.stage.value}", state, detail, engine=s.engine, cost=s.cost))
    return steps
