"""Reading uploaded PDFs and photos: Stage 0, then the OCR chain (§13-19, §53).

:class:`DocumentReader` turns the bytes of one piece of evidence into what
the orchestrator's Document agent needs:

1. **Stage 0** (:mod:`.stage0`): the PDF text layer, Portuguese invoice QR
   payloads, embedded e-invoice XML. The caller's country pack turns them
   into field observations (``ReadRequest.stage0_fields``).
2. **The OCR chain**: the existing :class:`~backoffice.ocr.router.OCRRouter`
   with the engines registered by name (a PP-OCRv6 / PaddleOCR-VL sidecar,
   the Claude vision fallback). The router skips OCR entirely when Stage 0
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

The orchestrator is synchronous (and runs in a browser without any of
this); engines are asynchronous. :func:`run_sync` runs the chain on a fresh
event loop, in a helper thread when the caller is already inside one (the
FastAPI handlers are ``async``). Engines' HTTP clients are closed inside that
loop after every document.
"""

from __future__ import annotations

import asyncio
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
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_as_coroutine(factory))
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="document-reader") as pool:
        return pool.submit(lambda: asyncio.run(_as_coroutine(factory))).result()


async def _as_coroutine(factory: Callable[[], Awaitable[T]]) -> T:
    return await factory()


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
            stage0 = read_pdf_stage0(request.data, self.qr_decoder)
        elif mime in IMAGE_MIME_TYPES:
            stage0 = read_image_stage0(request.data, self.qr_decoder)
        else:
            return ReadOutcome(steps=(ReadStep("read", StepState.SKIPPED, "not a PDF or an image"),))
        structured = self._structured(stage0, request)
        steps = list(stage0.steps)
        outcome = ReadOutcome(text=stage0.text, text_method=stage0.method, embedded_xml=stage0.embedded_xml,
                              page_count=stage0.page_count)
        if any(s.step == "pdf_text" and s.state is StepState.FAILED for s in stage0.steps):
            return replace(outcome, steps=tuple(steps))  # encrypted or damaged: no engine will do better
        ocr = self._ocr(request, mime, stage0, structured)
        if ocr is None:
            steps.append(self._no_ocr_step(structured, request))
            return replace(outcome, steps=tuple(steps))
        return replace(
            outcome,
            readings=ocr["readings"], reading_text=ocr["text"], supplier_name=ocr["supplier"],
            doc_type=ocr["doc_type"], steps=(*steps, *ocr["steps"]), cost=ocr["cost"],
        )

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
             structured: Mapping[CriticalField, Sequence[FieldObservation]]) -> dict[str, Any] | None:
        registry = self._registry
        if registry is None or not len(registry) or request.extractor is None:
            return None
        from backoffice.ocr import COMMERCIAL, OCRHints, OCRRouter, PageImage, RouterConfig, RoutingRequest

        config = self._router_config or RouterConfig(corroborate_single_source=True)
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
        if s.stage is RouteStage.HUMAN:
            continue
        detail = ", ".join(r.code.value + (f":{r.detail}" if r.detail else "") for r in s.reasons)
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
