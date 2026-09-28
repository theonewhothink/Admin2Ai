"""The OCR fallback chain as a deterministic router (§13, §16, §17).

=========  ==================  ====================================================
Stage      Engine role         Runs when
=========  ==================  ====================================================
0          structured          always evaluated; OCR is skipped entirely when the
                               structured evidence settles every required field
1          primary             PP-OCRv6 medium: whenever OCR is needed
2          complex_layout      PaddleOCR-VL: tables, columns, skew, low confidence
                               or a poor image; the primary failed; or (config)
                               fields are still unsettled
3          long_document       Unlimited-OCR: page count at or above the threshold
                               (and fields unsettled, by default); or as the
                               complex-layout fallback when PaddleOCR-VL gave nothing
4          commercial          only while critical fields are missing or disagree
                               and every conflict could still be settled; within
                               the per-document and per-tenant budget (a paid
                               external engine never runs without a tenant
                               ledger unless explicitly allowed; a PDF of unknown
                               length is reserved at the engine's page limit, or
                               refused without one), redacted (§53). Paid OCR is
                               the exception.
5          human               anything still unsettled: HUMAN_EXCEPTION
=========  ==================  ====================================================

"Settled" follows :mod:`backoffice.ocr.consensus`: a conflict between the
document's own structured statements can never be settled by more reading,
so the chain stops early instead of paying for it (§19).

Provenance is enforced here, not trusted to the extractor: every
observation read from an engine's text is re-labelled with that engine as
source and its method (OCR or VLM), so text can never pose as structured
evidence. A reading made from text another engine already produced
(``OCRResult.from_prior_text``) is attributed to that engine, so an echo
never counts as a second, independent vote (§17, §19).

An engine whose capabilities say ``external`` only runs in the commercial
role: everything else stays on our infrastructure (§53).

Every stage is recorded as a :class:`RouteStep` (engine, version, why it ran
or did not, cost, duration). Engines are resolved by name in an
:class:`~backoffice.ocr.registry.EngineRegistry` at each call, so swapping
one is instant (§16). Field values are read from each engine's text by a
pluggable :class:`~backoffice.extraction.fields.FieldExtractor`, e.g. a
country pack's text extractor adapted with ``from_named_observations``.

Engine and extractor failures never escape: they are recorded and the chain
continues. Nothing here produces GREEN quality (§57).
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from backoffice.domain.models import CriticalField, ExtractionMethod, FieldObservation
from backoffice.extraction._optional import MissingDependencyError
from backoffice.extraction.fields import FieldExtractor, FieldMap
from backoffice.extraction.quality import QualityReport

from .base import (
    OCRError,
    OCRHints,
    OCRInputError,
    OCRProviderInterface,
    OCRResult,
    OCRUnavailable,
    PageImage,
    RedactionError,
    as_pages,
    count_pages,
)
from .budget import BudgetHold, BudgetLedger
from .consensus import Consensus, ConsensusPolicy, FieldConsensus, build_consensus
from .locate import locate
from .registry import COMMERCIAL, PADDLEOCR_VL, PP_OCR_V6_MEDIUM, UNLIMITED_OCR, EngineRegistry

__all__ = [
    "INVOICE_FIELDS",
    "RECEIPT_FIELDS",
    "EngineRoles",
    "OCRRouter",
    "OutcomeStatus",
    "Reason",
    "ReasonCode",
    "RouteStage",
    "RouteStep",
    "RouterConfig",
    "RoutingOutcome",
    "RoutingRequest",
    "StepStatus",
    "owner_message",
]

logger = logging.getLogger(__name__)

F = CriticalField

# Fields that must be settled before the chain stops, per document kind.
INVOICE_FIELDS: frozenset[CriticalField] = frozenset(
    {F.INVOICE_NUMBER, F.SUPPLIER_TAX_ID, F.GROSS_AMOUNT, F.NET_AMOUNT, F.VAT_AMOUNT, F.CURRENCY, F.ISSUE_DATE}
)
RECEIPT_FIELDS: frozenset[CriticalField] = frozenset({F.SUPPLIER_TAX_ID, F.GROSS_AMOUNT, F.CURRENCY, F.ISSUE_DATE})


# --------------------------------------------------------------------------- vocabulary


class RouteStage(str, Enum):
    STRUCTURED = "structured"
    PRIMARY = "primary"
    COMPLEX_LAYOUT = "complex_layout"
    LONG_DOCUMENT = "long_document"
    COMMERCIAL = "commercial"
    HUMAN = "human"


_OCR_STAGES = (RouteStage.PRIMARY, RouteStage.COMPLEX_LAYOUT, RouteStage.LONG_DOCUMENT, RouteStage.COMMERCIAL)


class StepStatus(str, Enum):
    RAN = "ran"
    SKIPPED = "skipped"
    FAILED = "failed"


class ReasonCode(str, Enum):
    # Stage 0
    STRUCTURED_SETTLED = "structured_settled"
    STRUCTURED_INCOMPLETE = "structured_incomplete"
    NO_STRUCTURED_DATA = "no_structured_data"
    # why a stage ran
    FIRST_PASS = "first_pass"
    TABLES = "tables"
    MULTI_COLUMN = "multi_column"
    SKEWED = "skewed"
    LOW_CONFIDENCE = "low_confidence"
    POOR_IMAGE = "poor_image"
    PRIMARY_FAILED = "primary_failed"
    LONG_DOCUMENT = "long_document"
    COMPLEX_FALLBACK = "complex_fallback"
    MISSING_FIELD = "missing_field"
    CONFLICTING_FIELD = "conflicting_field"
    # why a stage did not run, or failed
    NOT_NEEDED = "not_needed"
    UNRESOLVABLE_CONFLICT = "unresolvable_conflict"
    NO_PAGES = "no_pages"
    NOT_CONFIGURED = "not_configured"
    ENGINE_NOT_REGISTERED = "engine_not_registered"
    UNSUPPORTED_INPUT = "unsupported_input"
    DOCUMENT_COST_CEILING = "document_cost_ceiling"
    BUDGET_EXCEEDED = "budget_exceeded"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    ENGINE_ERROR = "engine_error"
    REDACTION_REFUSED = "redaction_refused"
    EXTRACTOR_FAILED = "extractor_failed"
    EXTERNAL_NOT_ALLOWED = "external_not_allowed"
    NO_BUDGET = "no_budget"
    # how a step's readings were counted
    NOT_INDEPENDENT = "not_independent"


_FROZEN = ConfigDict(frozen=True, protected_namespaces=())


class Reason(BaseModel):
    """Why a stage ran, did not run or failed. ``detail`` names a field or metric."""

    model_config = _FROZEN

    code: ReasonCode
    detail: str = ""


class RouteStep(BaseModel):
    model_config = _FROZEN

    stage: RouteStage
    status: StepStatus
    engine: str | None = None
    model_version: str | None = None
    reasons: tuple[Reason, ...] = ()
    cost: Decimal = Field(default=Decimal("0"), ge=0)
    duration_ms: int = Field(default=0, ge=0)
    pages: int = Field(default=0, ge=0)

    def has(self, code: ReasonCode) -> bool:
        return any(r.code is code for r in self.reasons)


class OutcomeStatus(str, Enum):
    RESOLVED = "resolved"
    HUMAN_EXCEPTION = "human_exception"


class RoutingOutcome(BaseModel):
    """The full, auditable result of routing one piece of evidence (§54-55)."""

    model_config = _FROZEN

    tenant_id: str
    evidence_id: str
    status: OutcomeStatus
    steps: tuple[RouteStep, ...]
    fields: dict[CriticalField, FieldConsensus]
    results: tuple[OCRResult, ...] = ()
    total_cost: Decimal = Decimal("0")
    missing: tuple[CriticalField, ...] = ()
    conflicts: tuple[CriticalField, ...] = ()
    owner_message: str = ""

    @property
    def needs_human(self) -> bool:
        return self.status is OutcomeStatus.HUMAN_EXCEPTION

    @property
    def ocr_used(self) -> bool:
        return any(s.stage in _OCR_STAGES and s.status is StepStatus.RAN for s in self.steps)

    @property
    def engines_used(self) -> tuple[str, ...]:
        return tuple(s.engine for s in self.steps if s.status is StepStatus.RAN and s.engine)

    def step(self, stage: RouteStage) -> RouteStep | None:
        return next((s for s in self.steps if s.stage is stage), None)


# --------------------------------------------------------------------------- configuration


@dataclass(frozen=True)
class EngineRoles:
    """Registry names per chain role; None disables a role."""

    primary: str | None = PP_OCR_V6_MEDIUM
    complex_layout: str | None = PADDLEOCR_VL
    long_document: str | None = UNLIMITED_OCR
    commercial: str | None = COMMERCIAL


@dataclass(frozen=True)
class RouterConfig:
    """Routing thresholds. Defaults are starting points to tune on the golden set (§56)."""

    roles: EngineRoles = field(default_factory=EngineRoles)
    required_fields: frozenset[CriticalField] = INVOICE_FIELDS
    long_document_pages: int = 10
    min_mean_confidence: float = 0.85
    low_line_confidence: float = 0.60
    max_low_confidence_share: float = 0.15
    max_skew_degrees: float = 2.0
    complex_on_tables: bool = True
    complex_on_columns: bool = True
    vl_when_unsettled: bool = True
    long_requires_unsettled: bool = True
    max_cost_per_document: Decimal | None = None
    allow_paid_without_budget: bool = False
    consensus: ConsensusPolicy = field(default_factory=ConsensusPolicy)

    def __post_init__(self) -> None:
        if not self.required_fields:
            raise ValueError("required_fields cannot be empty: nothing is settled without evidence (§3)")
        if self.long_document_pages < 2:
            raise ValueError("long_document_pages must be at least 2")
        for name in ("min_mean_confidence", "low_line_confidence", "max_low_confidence_share"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.max_cost_per_document is not None and self.max_cost_per_document < 0:
            raise ValueError("max_cost_per_document cannot be negative")


@dataclass(frozen=True)
class RoutingRequest:
    """One piece of evidence to read.

    ``structured`` holds the Stage 0 observations (see
    ``backoffice.extraction.combine``); ``required_fields`` overrides the
    configured set (a receipt needs fewer fields than an invoice).
    """

    tenant_id: str
    evidence_id: str
    pages: Sequence[PageImage | bytes] = ()
    structured: FieldMap = field(default_factory=dict)
    hints: OCRHints = field(default_factory=OCRHints)
    image_quality: QualityReport | None = None
    required_fields: Collection[CriticalField] | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.evidence_id.strip():
            raise ValueError("tenant_id and evidence_id are required")
        if self.required_fields is not None and not self.required_fields:
            raise ValueError("required_fields cannot be empty: nothing is settled without evidence (§3)")


# --------------------------------------------------------------------------- owner language

_FIELD_WORDS: Mapping[CriticalField, str] = {
    F.INVOICE_NUMBER: "the invoice number",
    F.SUPPLIER_TAX_ID: "the supplier's tax number",
    F.CUSTOMER_TAX_ID: "your tax number",
    F.GROSS_AMOUNT: "the total",
    F.NET_AMOUNT: "the amount before tax",
    F.VAT_AMOUNT: "the tax amount",
    F.CURRENCY: "the currency",
    F.ISSUE_DATE: "the date",
    F.DUE_DATE: "the due date",
    F.IBAN: "the bank account",
    F.PAYMENT_REFERENCE: "the payment reference",
}


def _join(items: Sequence[CriticalField]) -> str:
    words = [_FIELD_WORDS[f] for f in items]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def owner_message(
    missing: Sequence[CriticalField],
    conflicts: Sequence[CriticalField],
    several: Sequence[CriticalField] = (),
) -> str:
    """Plain, calm words for the owner (§36, §69-70): no engines, no codes.

    Fields in ``several`` (also listed in ``conflicts``) are those where the
    document itself shows more than one value, which is not a misreading.
    """
    read_differently = [f for f in conflicts if f not in several]
    shown_twice = [f for f in conflicts if f in several]
    if not missing and not conflicts:
        return "Done."
    parts = ["I still need one thing."]
    if read_differently:
        parts.append(f"I read different values for {_join(read_differently)}.")
    if shown_twice:
        parts.append(f"I found more than one value for {_join(shown_twice)}.")
    if missing:
        parts.append(f"I couldn't find {_join(missing)}.")
    parts.append("Could you take a look at this document?")
    return " ".join(parts)


# --------------------------------------------------------------------------- the run


@dataclass(frozen=True)
class _Voter:
    """Whom observations read from a text are attributed to (§17, §19)."""

    source: str
    method: ExtractionMethod
    engine: str


class _Run:
    """Mutable state of one routing pass."""

    def __init__(self, request: RoutingRequest, config: RouterConfig, pages: tuple[PageImage, ...]) -> None:
        self.request = request
        self.config = config
        self.pages = pages
        self.required = frozenset(
            request.required_fields if request.required_fields is not None else config.required_fields
        )
        self.page_count = max(count_pages(pages), request.hints.page_count or 0) if pages else 0
        # A PDF of unknown length counts as one page above; paid engines need the real count.
        self.pages_known = bool(pages) and (
            request.hints.page_count is not None or all(p.pages is not None for p in pages)
        )
        self.observations: dict[CriticalField, list[FieldObservation]] = {
            CriticalField(f): list(obs) for f, obs in request.structured.items()
        }
        self.steps: list[RouteStep] = []
        self.results: list[OCRResult] = []
        self.readings: list[tuple[_Voter, OCRResult]] = []
        self.cost = Decimal("0")

    @property
    def has_pdf(self) -> bool:
        return any(p.is_pdf for p in self.pages)

    def assess(self) -> Consensus:
        return build_consensus(self.observations, required=self.required, policy=self.config.consensus)

    def record(
        self,
        stage: RouteStage,
        status: StepStatus,
        reasons: Sequence[Reason],
        *,
        engine: str | None = None,
        version: str | None = None,
        cost: Decimal = Decimal("0"),
        duration_ms: int = 0,
        pages: int = 0,
    ) -> None:
        self.cost += cost
        self.steps.append(
            RouteStep(
                stage=stage,
                status=status,
                engine=engine,
                model_version=version,
                reasons=tuple(reasons),
                cost=cost,
                duration_ms=duration_ms,
                pages=pages,
            )
        )

    def best_reading(self) -> tuple[_Voter, str] | None:
        """Text of the latest engine that read anything, with its voter (context for the paid step)."""
        for voter, result in reversed(self.readings):
            if result.full_text.strip():
                return voter, result.full_text
        return None

    def finish(self) -> RoutingOutcome:
        consensus = self.assess()
        status = OutcomeStatus.RESOLVED if consensus.settled else OutcomeStatus.HUMAN_EXCEPTION
        if status is OutcomeStatus.HUMAN_EXCEPTION:
            self.record(RouteStage.HUMAN, StepStatus.RAN, _unsettled(consensus))
        return RoutingOutcome(
            tenant_id=self.request.tenant_id,
            evidence_id=self.request.evidence_id,
            status=status,
            steps=tuple(self.steps),
            fields=dict(consensus.fields),
            results=tuple(self.results),
            total_cost=self.cost,
            missing=consensus.missing,
            conflicts=consensus.conflicts,
            owner_message=owner_message(consensus.missing, consensus.conflicts, consensus.several),
        )


def _unsettled(consensus: Consensus) -> list[Reason]:
    return [Reason(code=ReasonCode.MISSING_FIELD, detail=f.value) for f in consensus.missing] + [
        Reason(code=ReasonCode.CONFLICTING_FIELD, detail=f.value) for f in consensus.conflicts
    ]


def _failure_code(exc: BaseException) -> ReasonCode:
    if isinstance(exc, RedactionError):
        return ReasonCode.REDACTION_REFUSED
    if isinstance(exc, OCRInputError):
        return ReasonCode.UNSUPPORTED_INPUT
    if isinstance(exc, (OCRUnavailable, MissingDependencyError)):
        return ReasonCode.ENGINE_UNAVAILABLE
    return ReasonCode.ENGINE_ERROR


def _nothing_sent(exc: BaseException) -> bool:
    """Failures raised before any request left (so nothing can have been billed)."""
    return isinstance(exc, (RedactionError, OCRInputError, MissingDependencyError))


def _is_paid(provider: OCRProviderInterface) -> bool:
    """A vendor bills us per page: an external engine with a price (§17).

    Self-hosted engines may carry an amortised compute cost; they are never
    blocked for want of a ledger or a known page count.
    """
    return provider.capabilities.external and provider.cost_per_page > 0


def _estimate(run: _Run, provider: OCRProviderInterface) -> Decimal:
    """What a call may cost. A paid engine given a PDF of unknown length is
    assumed to bill up to its page limit (it refuses anything longer)."""
    pages = run.page_count
    limit = provider.capabilities.max_pages
    if _is_paid(provider) and not run.pages_known and limit is not None:
        pages = max(pages, limit)
    return provider.cost_per_page * pages


def _as_reading(observation: FieldObservation, voter: _Voter) -> FieldObservation:
    """The observation labelled as what it is: ``voter``'s reading of text (§13, §18)."""
    if observation.source == voter.source and observation.method is voter.method:
        return observation
    return observation.model_copy(update={"source": voter.source, "method": voter.method})


# --------------------------------------------------------------------------- router


class OCRRouter:
    """Runs the §17 chain for one piece of evidence at a time."""

    def __init__(
        self,
        registry: EngineRegistry,
        extractor: FieldExtractor,
        *,
        config: RouterConfig | None = None,
        budget: BudgetLedger | None = None,
    ) -> None:
        self._registry = registry
        self._extractor = extractor
        self._config = config or RouterConfig()
        self._budget = budget

    @property
    def config(self) -> RouterConfig:
        return self._config

    async def route(self, request: RoutingRequest) -> RoutingOutcome:
        try:
            pages = as_pages(request.pages)
            input_problem = None
        except OCRInputError:
            pages, input_problem = (), Reason(code=ReasonCode.UNSUPPORTED_INPUT, detail="pages")
        run = _Run(request, self._config, pages)

        consensus = self._stage0(run)
        if consensus.settled:
            return run.finish()
        if not consensus.improvable:
            self._skip_ocr(run, Reason(code=ReasonCode.UNRESOLVABLE_CONFLICT))
            return run.finish()
        if not run.pages:
            self._skip_ocr(run, input_problem or Reason(code=ReasonCode.NO_PAGES))
            return run.finish()

        roles = self._config.roles
        primary = await self._attempt(run, RouteStage.PRIMARY, roles.primary, [Reason(code=ReasonCode.FIRST_PASS)])
        complexity = self._complexity(primary, request.image_quality)

        vl_reasons = list(complexity)
        if primary is None:
            vl_reasons.append(Reason(code=ReasonCode.PRIMARY_FAILED))
        consensus = run.assess()
        if self._config.vl_when_unsettled and consensus.improvable:
            vl_reasons += _unsettled(consensus)
        vl = await self._maybe(run, RouteStage.COMPLEX_LAYOUT, roles.complex_layout, vl_reasons)

        consensus = run.assess()
        long_reasons: list[Reason] = []
        if run.page_count >= self._config.long_document_pages and (
            consensus.improvable or not self._config.long_requires_unsettled
        ):
            long_reasons.append(Reason(code=ReasonCode.LONG_DOCUMENT, detail=str(run.page_count)))
        elif vl is None and (complexity or primary is None) and consensus.improvable:
            long_reasons.append(Reason(code=ReasonCode.COMPLEX_FALLBACK))
        await self._maybe(run, RouteStage.LONG_DOCUMENT, roles.long_document, long_reasons)

        consensus = run.assess()
        if consensus.improvable and not consensus.unresolvable:
            await self._attempt(
                run, RouteStage.COMMERCIAL, roles.commercial, _unsettled(consensus), prior=run.best_reading()
            )
        else:
            # Settled, or a human is needed whatever a paid engine says: never pay for nothing.
            skip = [Reason(code=ReasonCode.NOT_NEEDED)] if consensus.settled else [
                Reason(code=ReasonCode.UNRESOLVABLE_CONFLICT, detail=f.value) for f in consensus.unresolvable
            ]
            run.record(RouteStage.COMMERCIAL, StepStatus.SKIPPED, skip, engine=roles.commercial)
        return run.finish()

    # ----------------------------------------------------------------- stages

    def _stage0(self, run: _Run) -> Consensus:
        consensus = run.assess()
        if not any(run.request.structured.values()):
            run.record(RouteStage.STRUCTURED, StepStatus.SKIPPED, [Reason(code=ReasonCode.NO_STRUCTURED_DATA)])
        elif consensus.settled:
            run.record(RouteStage.STRUCTURED, StepStatus.RAN, [Reason(code=ReasonCode.STRUCTURED_SETTLED)])
        else:
            reasons = [Reason(code=ReasonCode.STRUCTURED_INCOMPLETE), *_unsettled(consensus)]
            run.record(RouteStage.STRUCTURED, StepStatus.RAN, reasons)
        return consensus

    def _skip_ocr(self, run: _Run, reason: Reason) -> None:
        roles = self._config.roles
        for stage, engine in (
            (RouteStage.PRIMARY, roles.primary),
            (RouteStage.COMPLEX_LAYOUT, roles.complex_layout),
            (RouteStage.LONG_DOCUMENT, roles.long_document),
            (RouteStage.COMMERCIAL, roles.commercial),
        ):
            run.record(stage, StepStatus.SKIPPED, [reason], engine=engine)

    def _complexity(self, primary: OCRResult | None, quality: QualityReport | None) -> list[Reason]:
        """Why the primary reading cannot be trusted on its own (§15)."""
        cfg = self._config
        reasons: list[Reason] = []
        if primary is not None:
            layout = primary.layout
            if cfg.complex_on_tables and layout.tables > 0:
                reasons.append(Reason(code=ReasonCode.TABLES, detail=str(layout.tables)))
            if cfg.complex_on_columns and layout.columns > 1:
                reasons.append(Reason(code=ReasonCode.MULTI_COLUMN, detail=str(layout.columns)))
            if abs(layout.skew_degrees) > cfg.max_skew_degrees:
                reasons.append(Reason(code=ReasonCode.SKEWED, detail=f"{abs(layout.skew_degrees):.1f}"))
            mean = primary.mean_confidence
            share = primary.low_confidence_share(cfg.low_line_confidence)
            if mean is not None and mean < cfg.min_mean_confidence:
                reasons.append(Reason(code=ReasonCode.LOW_CONFIDENCE, detail=f"mean {mean:.2f}"))
            elif share is not None and share > cfg.max_low_confidence_share:
                reasons.append(Reason(code=ReasonCode.LOW_CONFIDENCE, detail=f"low share {share:.2f}"))
        if quality is not None and quality.flags:
            detail = ",".join(sorted(flag.value for flag in quality.flags))
            reasons.append(Reason(code=ReasonCode.POOR_IMAGE, detail=detail))
        return reasons

    async def _maybe(
        self, run: _Run, stage: RouteStage, engine: str | None, reasons: list[Reason]
    ) -> OCRResult | None:
        if not reasons:
            run.record(stage, StepStatus.SKIPPED, [Reason(code=ReasonCode.NOT_NEEDED)], engine=engine)
            return None
        return await self._attempt(run, stage, engine, reasons)

    # ----------------------------------------------------------------- one engine

    async def _attempt(
        self,
        run: _Run,
        stage: RouteStage,
        engine_name: str | None,
        reasons: list[Reason],
        *,
        prior: tuple[_Voter, str] | None = None,
    ) -> OCRResult | None:
        """Run one engine if it is configured, allowed, able and affordable; record the step."""
        if engine_name is None:
            run.record(stage, StepStatus.SKIPPED, [*reasons, Reason(code=ReasonCode.NOT_CONFIGURED)])
            return None
        provider = self._registry.find(engine_name)
        if provider is None:
            run.record(
                stage, StepStatus.SKIPPED, [*reasons, Reason(code=ReasonCode.ENGINE_NOT_REGISTERED)], engine=engine_name
            )
            return None
        estimate = _estimate(run, provider)
        blocked = self._blocked(run, stage, provider, estimate)
        hold: BudgetHold | None = None
        if blocked is None and self._budget is not None and estimate > 0:
            hold = self._budget.reserve(run.request.tenant_id, estimate)
            if hold is None:
                blocked = Reason(code=ReasonCode.BUDGET_EXCEEDED, detail=str(estimate))
        if blocked is not None:
            run.record(stage, StepStatus.SKIPPED, [*reasons, blocked], engine=provider.name, version=provider.version)
            return None

        hints = replace(run.request.hints, page_count=run.page_count, prior_text=prior[1] if prior else None)
        try:
            result = await provider.recognize(run.pages, hints)
        except Exception as exc:  # the chain must continue whatever an engine does (§17)
            # Once a request may have left, assume it was billed, ledger or not.
            charged = Decimal("0") if _nothing_sent(exc) else estimate
            self._settle(run, hold, charged)
            code = _failure_code(exc)
            logger.warning("OCR engine %s failed at %s: %s", provider.name, stage.value, type(exc).__name__)
            detail = exc.code if isinstance(exc, OCRError) else type(exc).__name__
            run.record(
                stage,
                StepStatus.FAILED,
                [*reasons, Reason(code=code, detail=detail)],
                engine=provider.name,
                version=provider.version,
                cost=charged,
                pages=run.page_count,
            )
            return None

        self._settle(run, hold, result.cost)
        voter = self._voter(run, provider, result, prior)
        extra = self._read_fields(run, voter, result)
        if voter.engine != provider.name:
            extra.insert(0, Reason(code=ReasonCode.NOT_INDEPENDENT, detail=voter.engine))
        run.results.append(result)
        run.readings.append((voter, result))
        run.record(
            stage,
            StepStatus.RAN,
            [*reasons, *extra],
            engine=provider.name,
            version=result.model_version,
            cost=result.cost,
            duration_ms=result.duration_ms,
            pages=result.pages_billed,
        )
        return result

    def _blocked(
        self, run: _Run, stage: RouteStage, provider: OCRProviderInterface, estimate: Decimal
    ) -> Reason | None:
        cfg = self._config
        caps = provider.capabilities
        if caps.external and stage is not RouteStage.COMMERCIAL:
            return Reason(code=ReasonCode.EXTERNAL_NOT_ALLOWED)  # §53: local engines first
        if caps.max_pages is not None and run.page_count > caps.max_pages:
            return Reason(code=ReasonCode.UNSUPPORTED_INPUT, detail="too_many_pages")
        if run.has_pdf and not caps.accepts_pdf:
            return Reason(code=ReasonCode.UNSUPPORTED_INPUT, detail="pdf")
        if not caps.supports_any(run.request.hints.languages):
            return Reason(code=ReasonCode.UNSUPPORTED_INPUT, detail="language")
        paid = _is_paid(provider)
        if paid and not run.pages_known and caps.max_pages is None:
            return Reason(code=ReasonCode.UNSUPPORTED_INPUT, detail="unknown_page_count")
        ceiling = cfg.max_cost_per_document
        if ceiling is not None and run.cost + estimate > ceiling:
            return Reason(code=ReasonCode.DOCUMENT_COST_CEILING, detail=str(estimate))
        if paid and self._budget is None and not cfg.allow_paid_without_budget:
            return Reason(code=ReasonCode.NO_BUDGET)
        return None

    def _settle(self, run: _Run, hold: BudgetHold | None, actual: Decimal) -> None:
        """Book what a call cost against the tenant ledger, if there is one."""
        if self._budget is None:
            return
        if hold is None:
            if actual <= 0:
                return
            hold = self._budget.reserve(run.request.tenant_id, Decimal("0"))  # zero holds always succeed
            if hold is None:
                return
        self._budget.settle(hold, actual)

    @staticmethod
    def _voter(
        run: _Run, provider: OCRProviderInterface, result: OCRResult, prior: tuple[_Voter, str] | None
    ) -> _Voter:
        """An echo of an earlier engine's text votes as that engine, never as a new one."""
        if result.from_prior_text and prior is not None:
            return prior[0]
        return _Voter(f"{run.request.evidence_id}@{provider.name}", provider.method, provider.name)

    def _read_fields(self, run: _Run, voter: _Voter, result: OCRResult) -> list[Reason]:
        """Extract fields from the engine's text and add them to the run as readings."""
        try:
            found = self._extractor(result.full_text, voter.source, voter.method)
            additions = [
                (CriticalField(name), _as_reading(locate(obs, CriticalField(name), result), voter))
                for name, observations in found.items()
                for obs in observations
                if isinstance(obs, FieldObservation)
            ]
        except Exception as exc:  # a broken extractor must not stop the chain
            logger.warning("field extractor failed on %s: %s", result.engine, type(exc).__name__)
            return [Reason(code=ReasonCode.EXTRACTOR_FAILED, detail=type(exc).__name__)]
        for name, observation in additions:
            run.observations.setdefault(name, []).append(observation)
        return []
