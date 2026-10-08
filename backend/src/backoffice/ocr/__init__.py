"""Multi-engine, cost-optimized OCR (§13-18, §53, §56).

No single engine is trusted. Structured evidence comes first (see
``backoffice.extraction``); OCR engines sit behind one interface, are
registered by name, and are escalated deterministically by the router.

Wiring example (integration step)::

    from backoffice.extraction import combine, extract_structured, from_named_observations
    from backoffice.ocr import (EngineRegistry, OCRRouter, RoutingRequest, RouterConfig,
                                PPOCRv6Provider, PPOCRConfig, EndpointConfig, ...)

    registry = EngineRegistry([
        PPOCRv6Provider(PPOCRConfig(endpoint=EndpointConfig("http://ppocr:8080"))),
        PaddleOCRVLProvider(PaddleOCRVLConfig(endpoint=EndpointConfig("http://paddle-vl:8080"))),
        UnlimitedOCRProvider(UnlimitedOCRConfig(endpoint=..., model="unlimited-ocr")),
        CommercialOCRProvider(CommercialOCRConfig(endpoint=..., model=..., cost_per_page=...),
                              redactor=text_only_redactor(privacy.redact)),
    ])
    router = OCRRouter(registry, from_named_observations(pt_pack.extract_text_fields),
                       budget=InMemoryBudgetLedger(default_ceiling=Decimal("5.00")))
    # A paid external engine never runs without a tenant ledger (or
    # RouterConfig(allow_paid_without_budget=True)). For a PDF whose page
    # count is unknown it is reserved at its max_pages (refused without one):
    # pass OCRHints(page_count=...) when Stage 0 knows the count.
    outcome = await router.route(RoutingRequest(
        tenant_id=..., evidence_id=evidence.id, pages=[pdf_bytes],
        structured=combine(extract_structured(pdf_bytes, source=evidence.id))))

Public API
----------
Contract: OCRProviderInterface, OCRResult, OCRPage, OCRLine, OCRWord,
    LayoutSignals, OCRCapabilities, OCRHints, PageImage, errors (OCRError...)
Engines: PPOCRv6Provider (TINY/MEDIUM, HTTP or in-process), LocalOCRProvider (PP-OCRv6
    small on this server's CPU through RapidOCR, no sidecar), PaddleOCRVLProvider,
    UnlimitedOCRProvider, CommercialOCRProvider (+ redactors), ClaudeVisionProvider
    (+ backoffice.ocr.redact.vision_redactor), FakeOCRProvider
Registry: EngineRegistry and conventional names (PP_OCR_V6_MEDIUM, ...)
Routing: OCRRouter.route(RoutingRequest) -> RoutingOutcome, RouterConfig,
    EngineRoles, RouteStep, Reason/ReasonCode, INVOICE_FIELDS, RECEIPT_FIELDS
Consensus: build_consensus, decide, FieldConsensus, FieldState, ConflictKind
    (SOURCES / READINGS / SEVERAL), ConsensusPolicy
Budget: BudgetLedger (protocol: reserve -> BudgetHold | None, settle(hold, actual)),
    BudgetHold, InMemoryBudgetLedger

Trust rules enforced by the router (§17, §19, §53): readings are always
labelled with the engine that produced them (an extractor cannot pass OCR
text off as structured evidence); a reading of text another engine already
produced (``OCRResult.from_prior_text``) votes as that engine; external
engines only run in the commercial role; nothing settles with an empty set
of required fields.
Benchmark (§56): GoldenDataset.load, run_benchmark, provider_runner,
    router_runner, BenchmarkReport, gate
"""

from .base import (
    ANY_LANGUAGE,
    LATIN_LANGUAGES,
    LayoutSignals,
    OCRCapabilities,
    OCRError,
    OCRFieldReading,
    OCRHints,
    OCRInputError,
    OCRLine,
    OCRPage,
    OCRProviderInterface,
    OCRRejected,
    OCRResponseError,
    OCRResult,
    OCRUnavailable,
    OCRWord,
    PageImage,
    RedactionError,
    as_pages,
    count_pages,
    estimate_pdf_pages,
)
from .benchmark import (
    BenchmarkReport,
    DocumentResult,
    FieldAccuracy,
    FieldOutcome,
    GateDecision,
    GoldenDataset,
    GoldenDocument,
    RunOutput,
    gate,
    provider_runner,
    router_runner,
    run_benchmark,
)
from .budget import BudgetHold, BudgetLedger, InMemoryBudgetLedger
from .consensus import (
    Candidate,
    ConflictKind,
    Consensus,
    ConsensusPolicy,
    FieldConsensus,
    FieldState,
    build_consensus,
    decide,
)
from .locate import locate
from .providers import (
    CLAUDE_VISION,
    ClaudeVisionConfig,
    ClaudeVisionProvider,
    CommercialOCRConfig,
    CommercialOCRProvider,
    CommercialWireFormat,
    EndpointConfig,
    FakeOCRProvider,
    InProcessPaddleOCR,
    LocalOCRConfig,
    LocalOCRProvider,
    PaddleOCRVLConfig,
    PaddleOCRVLProvider,
    PPOCRConfig,
    PPOCRv6Provider,
    PPOCRVariant,
    RedactedInput,
    Redactor,
    TextRedaction,
    UnlimitedOCRConfig,
    UnlimitedOCRProvider,
    local_ocr_available,
    masked_pages_redactor,
    text_only_redactor,
)
from .registry import (
    COMMERCIAL,
    LOCAL_OCR,
    PADDLEOCR_VL,
    PP_OCR_V6_MEDIUM,
    PP_OCR_V6_TINY,
    UNLIMITED_OCR,
    EngineRegistry,
    UnknownEngineError,
)
from .router import (
    INVOICE_FIELDS,
    RECEIPT_FIELDS,
    EngineRoles,
    OCRRouter,
    OutcomeStatus,
    Reason,
    ReasonCode,
    RouterConfig,
    RouteStage,
    RouteStep,
    RoutingOutcome,
    RoutingRequest,
    StepStatus,
    owner_message,
)

__all__ = [
    "ANY_LANGUAGE",
    "CLAUDE_VISION",
    "COMMERCIAL",
    "INVOICE_FIELDS",
    "LATIN_LANGUAGES",
    "LOCAL_OCR",
    "PADDLEOCR_VL",
    "PP_OCR_V6_MEDIUM",
    "PP_OCR_V6_TINY",
    "RECEIPT_FIELDS",
    "UNLIMITED_OCR",
    "BenchmarkReport",
    "BudgetHold",
    "BudgetLedger",
    "Candidate",
    "ClaudeVisionConfig",
    "ClaudeVisionProvider",
    "CommercialOCRConfig",
    "CommercialOCRProvider",
    "CommercialWireFormat",
    "ConflictKind",
    "Consensus",
    "ConsensusPolicy",
    "DocumentResult",
    "EndpointConfig",
    "EngineRegistry",
    "EngineRoles",
    "FakeOCRProvider",
    "FieldAccuracy",
    "FieldConsensus",
    "FieldOutcome",
    "FieldState",
    "GateDecision",
    "GoldenDataset",
    "GoldenDocument",
    "InMemoryBudgetLedger",
    "InProcessPaddleOCR",
    "LayoutSignals",
    "LocalOCRConfig",
    "LocalOCRProvider",
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
    "OCRRouter",
    "OCRUnavailable",
    "OCRWord",
    "OutcomeStatus",
    "PPOCRConfig",
    "PPOCRVariant",
    "PPOCRv6Provider",
    "PageImage",
    "PaddleOCRVLConfig",
    "PaddleOCRVLProvider",
    "Reason",
    "ReasonCode",
    "RedactedInput",
    "RedactionError",
    "Redactor",
    "RouteStage",
    "RouteStep",
    "RouterConfig",
    "RoutingOutcome",
    "RoutingRequest",
    "RunOutput",
    "StepStatus",
    "TextRedaction",
    "UnknownEngineError",
    "UnlimitedOCRConfig",
    "UnlimitedOCRProvider",
    "as_pages",
    "build_consensus",
    "count_pages",
    "decide",
    "estimate_pdf_pages",
    "gate",
    "local_ocr_available",
    "locate",
    "masked_pages_redactor",
    "owner_message",
    "provider_runner",
    "router_runner",
    "run_benchmark",
    "text_only_redactor",
]
