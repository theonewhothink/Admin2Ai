"""The §17 fallback chain: Stage 0 -> PP-OCRv6 -> PaddleOCR-VL -> Unlimited -> commercial -> human."""

import asyncio
import re
from decimal import Decimal

import pytest

from backoffice.domain.models import (
    BoundingBox,
    ExtractionMethod,
    FieldObservation,
    Quality,
)
from backoffice.domain.models import CriticalField as F
from backoffice.extraction import (
    ImageMetrics,
    LabelledFieldExtractor,
    assess,
    combine,
    parse_einvoice,
)
from backoffice.ocr import (
    COMMERCIAL,
    PADDLEOCR_VL,
    PP_OCR_V6_MEDIUM,
    RECEIPT_FIELDS,
    UNLIMITED_OCR,
    EngineRegistry,
    EngineRoles,
    FakeOCRProvider,
    FieldState,
    InMemoryBudgetLedger,
    LayoutSignals,
    OCRCapabilities,
    OCRHints,
    OCRRouter,
    OCRUnavailable,
    OutcomeStatus,
    PageImage,
    ReasonCode,
    RedactionError,
    RouterConfig,
    RouteStage,
    RoutingRequest,
    StepStatus,
    owner_message,
)

GOOD = """Invoice number: FT 2026/183
Invoice date: 2026-09-18
Supplier VAT: PT509123457
Subtotal: 393.17
VAT: 90.43
Total: 483.60 EUR"""

UBL = b"""<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
 <cbc:ID>FT 2026/183</cbc:ID><cbc:IssueDate>2026-09-18</cbc:IssueDate>
 <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
 <cac:AccountingSupplierParty><cac:Party><cac:PartyTaxScheme><cbc:CompanyID>PT509123457</cbc:CompanyID>
 <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme></cac:Party></cac:AccountingSupplierParty>
 <cac:TaxTotal><cbc:TaxAmount currencyID="EUR">90.43</cbc:TaxAmount></cac:TaxTotal>
 <cac:LegalMonetaryTotal><cbc:TaxExclusiveAmount currencyID="EUR">393.17</cbc:TaxExclusiveAmount>
 <cbc:TaxInclusiveAmount currencyID="EUR">483.60</cbc:TaxInclusiveAmount></cac:LegalMonetaryTotal>
</Invoice>"""

VLM = ExtractionMethod.VLM
EXTERNAL = OCRCapabilities(external=True, accepts_pdf=True)


def run(coro):
    return asyncio.run(coro)


def page(text: str = GOOD) -> PageImage:
    return PageImage(data=text.encode(), mime_type="text/plain")


def primary(**kwargs) -> FakeOCRProvider:
    return FakeOCRProvider(PP_OCR_V6_MEDIUM, **kwargs)


def vl(**kwargs) -> FakeOCRProvider:
    return FakeOCRProvider(PADDLEOCR_VL, method=VLM, confidence=None, **kwargs)


def long_engine(**kwargs) -> FakeOCRProvider:
    return FakeOCRProvider(UNLIMITED_OCR, method=VLM, confidence=None, **kwargs)


def paid(**kwargs) -> FakeOCRProvider:
    kwargs.setdefault("cost_per_page", Decimal("0.02"))
    return FakeOCRProvider(COMMERCIAL, method=VLM, capabilities=EXTERNAL, **kwargs)


_LEDGER = object()


def router(*providers, config: RouterConfig | None = None, budget=_LEDGER, extractor=None) -> OCRRouter:
    """A router with an uncapped tenant ledger unless ``budget`` says otherwise."""
    ledger = InMemoryBudgetLedger() if budget is _LEDGER else budget
    return OCRRouter(
        EngineRegistry(providers), extractor or LabelledFieldExtractor(), config=config, budget=ledger
    )


def request(*pages, **kwargs) -> RoutingRequest:
    kwargs.setdefault("pages", pages or (page(),))
    return RoutingRequest(tenant_id="t1", evidence_id="ev_1", **kwargs)


def stages(outcome):
    return [(s.stage, s.status) for s in outcome.steps]


def codes(outcome, stage):
    return [r.code for r in outcome.step(stage).reasons]


# --------------------------------------------------------------------------- Stage 0


def test_structured_evidence_settles_everything_and_ocr_is_skipped():
    engines = [primary(), vl(), long_engine(), paid()]
    structured = combine([parse_einvoice(UBL, source="ev_1")])
    outcome = run(router(*engines).route(request(structured=structured)))
    assert outcome.status is OutcomeStatus.RESOLVED
    assert stages(outcome) == [(RouteStage.STRUCTURED, StepStatus.RAN)]
    assert codes(outcome, RouteStage.STRUCTURED) == [ReasonCode.STRUCTURED_SETTLED]
    assert all(e.calls == [] for e in engines)
    assert not outcome.ocr_used and outcome.total_cost == 0
    assert outcome.fields[F.GROSS_AMOUNT].value == Decimal("483.60")
    assert outcome.owner_message == "Done."


def test_partial_structured_evidence_goes_to_ocr_and_is_corroborated():
    qr_like = {
        F.GROSS_AMOUNT: [
            FieldObservation(value="483.60", source="ev_1", method=ExtractionMethod.QR, confidence=0.95)
        ]
    }
    pp = primary()
    outcome = run(router(pp, vl(), paid()).route(request(structured=qr_like)))
    assert outcome.status is OutcomeStatus.RESOLVED
    assert codes(outcome, RouteStage.STRUCTURED)[0] is ReasonCode.STRUCTURED_INCOMPLETE
    assert ReasonCode.MISSING_FIELD in codes(outcome, RouteStage.STRUCTURED)
    assert len(pp.calls) == 1
    gross = outcome.fields[F.GROSS_AMOUNT]
    assert gross.state is FieldState.AGREED  # QR + PP-OCRv6
    assert {o.method for o in gross.observations} == {ExtractionMethod.QR, ExtractionMethod.OCR}


def test_hard_source_conflict_stops_early_without_paying():
    conflicting = {
        F.GROSS_AMOUNT: [
            FieldObservation(
                value="483.60", source="ev_1", method=ExtractionMethod.STRUCTURED_XML, confidence=0.98
            ),
            FieldObservation(value="438.60", source="ev_1", method=ExtractionMethod.QR, confidence=0.95),
        ]
    }
    engines = [primary(), paid()]
    outcome = run(router(*engines).route(request(structured=conflicting, required_fields=[F.GROSS_AMOUNT])))
    assert outcome.status is OutcomeStatus.HUMAN_EXCEPTION
    assert all(e.calls == [] for e in engines)
    assert codes(outcome, RouteStage.PRIMARY) == [ReasonCode.UNRESOLVABLE_CONFLICT]
    assert outcome.conflicts == (F.GROSS_AMOUNT,)
    assert outcome.fields[F.GROSS_AMOUNT].quality is Quality.RED
    assert outcome.owner_message == (
        "I still need one thing. I read different values for the total. Could you take a look at this document?"
    )


def test_no_pages_or_unreadable_pages_go_to_a_human():
    outcome = run(router(primary()).route(RoutingRequest(tenant_id="t1", evidence_id="ev_1")))
    assert outcome.needs_human and codes(outcome, RouteStage.PRIMARY) == [ReasonCode.NO_PAGES]
    assert outcome.step(RouteStage.HUMAN).has(ReasonCode.MISSING_FIELD)
    garbage = run(router(primary()).route(request(b"not an image")))
    assert codes(garbage, RouteStage.PRIMARY) == [ReasonCode.UNSUPPORTED_INPUT]


# --------------------------------------------------------------------------- primary and escalation


def test_simple_document_stops_after_the_primary_engine():
    engines = [primary(cost_per_page=Decimal("0.0004")), vl(), long_engine(), paid()]
    outcome = run(router(*engines).route(request()))
    assert outcome.status is OutcomeStatus.RESOLVED
    assert stages(outcome) == [
        (RouteStage.STRUCTURED, StepStatus.SKIPPED),
        (RouteStage.PRIMARY, StepStatus.RAN),
        (RouteStage.COMPLEX_LAYOUT, StepStatus.SKIPPED),
        (RouteStage.LONG_DOCUMENT, StepStatus.SKIPPED),
        (RouteStage.COMMERCIAL, StepStatus.SKIPPED),
    ]
    step = outcome.step(RouteStage.PRIMARY)
    assert (step.engine, step.model_version, step.cost, step.pages) == (
        PP_OCR_V6_MEDIUM,
        "fake-1",
        Decimal("0.0004"),
        1,
    )
    assert codes(outcome, RouteStage.PRIMARY) == [ReasonCode.FIRST_PASS]
    assert codes(outcome, RouteStage.COMMERCIAL) == [ReasonCode.NOT_NEEDED]
    assert outcome.total_cost == Decimal("0.0004")
    gross = outcome.fields[F.GROSS_AMOUNT]
    assert gross.state is FieldState.SINGLE and gross.quality is Quality.AMBER
    assert gross.observations[0].source == "ev_1@pp-ocrv6-medium"
    # The value was found on exactly one boxed line, so its box is attached (§18).
    assert isinstance(gross.observations[0].location, BoundingBox)
    assert gross.observations[0].location.y0 == 100.0


@pytest.mark.parametrize(
    ("layout", "confidence", "code"),
    [
        (LayoutSignals(tables=1), 0.98, ReasonCode.TABLES),
        (LayoutSignals(columns=2), 0.98, ReasonCode.MULTI_COLUMN),
        (LayoutSignals(skew_degrees=-3.5), 0.98, ReasonCode.SKEWED),
        (LayoutSignals(), 0.60, ReasonCode.LOW_CONFIDENCE),
    ],
)
def test_complex_layouts_escalate_to_paddleocr_vl(layout, confidence, code):
    layout_engine = vl()
    outcome = run(router(primary(layout=layout, confidence=confidence), layout_engine).route(request()))
    assert codes(outcome, RouteStage.COMPLEX_LAYOUT) == [code]
    assert outcome.step(RouteStage.COMPLEX_LAYOUT).status is StepStatus.RAN
    assert len(layout_engine.calls) == 1
    assert outcome.fields[F.GROSS_AMOUNT].state is FieldState.AGREED  # two engines, one value


def test_poor_image_quality_escalates():
    quality = assess(ImageMetrics(sharpness=20.0, glare_ratio=0.0))
    outcome = run(router(primary(), vl()).route(request(image_quality=quality)))
    step = outcome.step(RouteStage.COMPLEX_LAYOUT)
    assert [(r.code, r.detail) for r in step.reasons] == [(ReasonCode.POOR_IMAGE, "blurry")]


def test_primary_failure_falls_back_to_the_layout_engine():
    outcome = run(router(primary(error=OCRUnavailable(PP_OCR_V6_MEDIUM, "HTTP 503")), vl()).route(request()))
    failed = outcome.step(RouteStage.PRIMARY)
    assert failed.status is StepStatus.FAILED
    assert [(r.code, r.detail) for r in failed.reasons][-1] == (
        ReasonCode.ENGINE_UNAVAILABLE,
        "engine_unavailable",
    )
    assert codes(outcome, RouteStage.COMPLEX_LAYOUT)[0] is ReasonCode.PRIMARY_FAILED
    assert outcome.status is OutcomeStatus.RESOLVED
    assert outcome.engines_used == (PADDLEOCR_VL,)


def test_unsettled_fields_try_the_free_layout_engine_before_paying():
    partial = "Invoice number: FT 2026/183\nTotal: 483.60 EUR"
    layout_engine = vl(texts=[GOOD])
    outcome = run(router(primary(texts=[partial]), layout_engine, paid()).route(request()))
    assert ReasonCode.MISSING_FIELD in codes(outcome, RouteStage.COMPLEX_LAYOUT)
    assert outcome.status is OutcomeStatus.RESOLVED
    assert outcome.step(RouteStage.COMMERCIAL).status is StepStatus.SKIPPED
    no_vl_on_unsettled = RouterConfig(vl_when_unsettled=False)
    outcome = run(
        router(primary(texts=[partial]), vl(texts=[GOOD]), config=no_vl_on_unsettled).route(request())
    )
    assert codes(outcome, RouteStage.COMPLEX_LAYOUT) == [ReasonCode.NOT_NEEDED]


def test_long_documents_go_to_unlimited_ocr_when_fields_are_unsettled():
    pages = [page("Invoice number: FT 2026/183")] + [page("line items") for _ in range(10)] + [page(GOOD)]
    long_doc = long_engine(texts=[GOOD])
    engines = [primary(texts=["Invoice number: FT 2026/183"]), vl(texts=[""]), long_doc]
    outcome = run(router(*engines).route(request(*pages)))
    step = outcome.step(RouteStage.LONG_DOCUMENT)
    assert step.status is StepStatus.RAN
    assert [(r.code, r.detail) for r in step.reasons] == [(ReasonCode.LONG_DOCUMENT, "12")]
    assert long_doc.calls[0][1].page_count == 12
    assert outcome.status is OutcomeStatus.RESOLVED


def test_long_but_settled_documents_skip_unlimited_unless_configured():
    pages = [page(GOOD)] + [page("more") for _ in range(11)]
    outcome = run(router(primary(), long_engine()).route(request(*pages)))
    assert codes(outcome, RouteStage.LONG_DOCUMENT) == [ReasonCode.NOT_NEEDED]
    always = RouterConfig(long_requires_unsettled=False)
    outcome = run(router(primary(), long_engine(), config=always).route(request(*pages)))
    assert outcome.step(RouteStage.LONG_DOCUMENT).status is StepStatus.RAN
    hinted = run(
        router(primary(), long_engine(), config=always).route(request(hints=OCRHints(page_count=30)))
    )
    assert outcome.step(RouteStage.LONG_DOCUMENT).has(ReasonCode.LONG_DOCUMENT)
    assert hinted.step(RouteStage.LONG_DOCUMENT).reasons[0].detail == "30"


def test_unlimited_is_the_complex_fallback_when_the_layout_engine_is_down():
    engines = [
        primary(texts=["Total: 483.60 EUR"], layout=LayoutSignals(tables=2)),
        vl(error=OCRUnavailable(PADDLEOCR_VL)),
        long_engine(texts=[GOOD]),
    ]
    outcome = run(router(*engines).route(request()))
    assert outcome.step(RouteStage.COMPLEX_LAYOUT).status is StepStatus.FAILED
    assert codes(outcome, RouteStage.LONG_DOCUMENT) == [ReasonCode.COMPLEX_FALLBACK]
    assert outcome.status is OutcomeStatus.RESOLVED


# --------------------------------------------------------------------------- disagreement and the paid step


def misread(text: str) -> str:
    return text.replace("483.60", "488.60")


def test_disagreement_goes_to_the_commercial_engine_which_can_break_the_tie():
    commercial = paid()
    engines = [primary(texts=[misread(GOOD)], layout=LayoutSignals(tables=1)), vl(), commercial]
    budget = InMemoryBudgetLedger(default_ceiling=Decimal("1.00"))
    outcome = run(router(*engines, budget=budget).route(request()))
    step = outcome.step(RouteStage.COMMERCIAL)
    assert step.status is StepStatus.RAN
    assert [(r.code, r.detail) for r in step.reasons] == [(ReasonCode.CONFLICTING_FIELD, "gross_amount")]
    # The paid engine receives the best local text as context (to be redacted by the provider).
    assert "483.60" in commercial.calls[0][1].prior_text
    gross = outcome.fields[F.GROSS_AMOUNT]
    assert gross.state is FieldState.MAJORITY and gross.value == Decimal("483.60")
    assert gross.quality is Quality.AMBER  # a vote is never promoted to GREEN
    assert outcome.status is OutcomeStatus.RESOLVED
    assert outcome.total_cost == Decimal("0.02") and budget.spent("t1") == Decimal("0.02")


def test_still_ambiguous_after_the_paid_engine_is_a_human_exception():
    engines = [
        primary(texts=[misread(GOOD)], layout=LayoutSignals(tables=1)),
        vl(),
        paid(texts=[GOOD.replace("483.60", "433.60")]),
    ]
    outcome = run(router(*engines).route(request()))
    assert outcome.status is OutcomeStatus.HUMAN_EXCEPTION
    assert outcome.conflicts == (F.GROSS_AMOUNT,) and outcome.missing == ()
    human = outcome.step(RouteStage.HUMAN)
    assert [(r.code, r.detail) for r in human.reasons] == [(ReasonCode.CONFLICTING_FIELD, "gross_amount")]
    assert len(outcome.fields[F.GROSS_AMOUNT].candidates) == 3


def test_missing_fields_reach_the_paid_engine_then_a_human():
    commercial = paid(texts=["Total: 483.60 EUR"])
    config = RouterConfig(required_fields=frozenset({F.GROSS_AMOUNT, F.DUE_DATE}))
    outcome = run(
        router(primary(texts=["Total: 483.60 EUR"]), vl(texts=[""]), commercial, config=config).route(
            request()
        )
    )
    assert [(r.code, r.detail) for r in outcome.step(RouteStage.COMMERCIAL).reasons] == [
        (ReasonCode.MISSING_FIELD, "due_date")
    ]
    assert outcome.missing == (F.DUE_DATE,)
    assert outcome.owner_message == (
        "I still need one thing. I couldn't find the due date. Could you take a look at this document?"
    )


def test_tenant_budget_blocks_the_paid_step():
    budget = InMemoryBudgetLedger({"t1": Decimal("0.01")})
    commercial = paid()
    engines = [primary(texts=[misread(GOOD)], layout=LayoutSignals(tables=1)), vl(), commercial]
    outcome = run(router(*engines, budget=budget).route(request()))
    step = outcome.step(RouteStage.COMMERCIAL)
    assert step.status is StepStatus.SKIPPED and step.has(ReasonCode.BUDGET_EXCEEDED)
    assert commercial.calls == [] and budget.spent("t1") == 0
    assert outcome.needs_human


def test_per_document_cost_ceiling():
    config = RouterConfig(max_cost_per_document=Decimal("0.01"))
    engines = [primary(texts=[misread(GOOD)], layout=LayoutSignals(tables=1)), vl(), paid()]
    outcome = run(router(*engines, config=config).route(request()))
    assert outcome.step(RouteStage.COMMERCIAL).has(ReasonCode.DOCUMENT_COST_CEILING)


@pytest.mark.parametrize(
    ("error", "code", "charged"),
    [
        (RedactionError(COMMERCIAL, "nothing left to send"), ReasonCode.REDACTION_REFUSED, Decimal("0")),
        (OCRUnavailable(COMMERCIAL, "timeout"), ReasonCode.ENGINE_UNAVAILABLE, Decimal("0.02")),
        (RuntimeError("vendor SDK bug"), ReasonCode.ENGINE_ERROR, Decimal("0.02")),
    ],
)
def test_paid_step_failures_are_recorded_and_billed_conservatively(error, code, charged):
    budget = InMemoryBudgetLedger(default_ceiling=Decimal("1"))
    engines = [primary(texts=[misread(GOOD)], layout=LayoutSignals(tables=1)), vl(), paid(error=error)]
    outcome = run(router(*engines, budget=budget).route(request()))
    step = outcome.step(RouteStage.COMMERCIAL)
    assert step.status is StepStatus.FAILED and step.reasons[-1].code is code
    assert step.cost == charged and budget.spent("t1") == charged
    assert outcome.needs_human
    assert "SDK" not in outcome.owner_message and "vendor" not in outcome.owner_message


# --------------------------------------------------------------------------- configuration and robustness


def test_engines_are_swapped_by_name():
    registry = EngineRegistry([primary(texts=["Total: 1.00 EUR"])])
    cfg = RouterConfig(required_fields=frozenset({F.GROSS_AMOUNT}))
    ocr_router = OCRRouter(registry, LabelledFieldExtractor(), config=cfg)
    first = run(ocr_router.route(request()))
    registry.replace(
        PP_OCR_V6_MEDIUM, FakeOCRProvider(PP_OCR_V6_MEDIUM, version="v2", texts=["Total: 2.00 EUR"])
    )
    second = run(ocr_router.route(request()))
    assert first.fields[F.GROSS_AMOUNT].value == Decimal("1.00")
    assert second.fields[F.GROSS_AMOUNT].value == Decimal("2.00")
    assert second.step(RouteStage.PRIMARY).model_version == "v2"


def test_unregistered_and_disabled_roles_are_recorded():
    config = RouterConfig(roles=EngineRoles(commercial=None))
    outcome = run(router(primary(texts=["Total: 1.00 EUR"]), config=config).route(request()))
    assert outcome.step(RouteStage.COMPLEX_LAYOUT).has(ReasonCode.ENGINE_NOT_REGISTERED)
    assert outcome.step(RouteStage.COMMERCIAL).has(ReasonCode.NOT_CONFIGURED)
    assert outcome.needs_human


def test_capability_limits_skip_an_engine():
    small = FakeOCRProvider(PP_OCR_V6_MEDIUM, capabilities=OCRCapabilities(max_pages=1))
    outcome = run(router(small, vl()).route(request(page(), page())))
    assert [(r.code, r.detail) for r in outcome.step(RouteStage.PRIMARY).reasons][-1] == (
        ReasonCode.UNSUPPORTED_INPUT,
        "too_many_pages",
    )
    latin = FakeOCRProvider(PP_OCR_V6_MEDIUM, capabilities=OCRCapabilities(languages=frozenset({"pt"})))
    outcome = run(router(latin, vl()).route(request(hints=OCRHints(languages=("he",)))))
    assert outcome.step(RouteStage.PRIMARY).reasons[-1].detail == "language"
    no_pdf = FakeOCRProvider(PP_OCR_V6_MEDIUM, capabilities=OCRCapabilities(accepts_pdf=False))
    pdf = PageImage(data=b"%PDF-1.7 << /Type /Page >>", mime_type="application/pdf")
    outcome = run(router(no_pdf, vl()).route(request(pdf)))
    assert outcome.step(RouteStage.PRIMARY).reasons[-1].detail == "pdf"


def test_a_broken_extractor_does_not_stop_the_chain():
    def explode(text, source, method):
        raise KeyError("bug")

    outcome = run(router(primary(), vl(), extractor=explode).route(request()))
    assert outcome.step(RouteStage.PRIMARY).has(ReasonCode.EXTRACTOR_FAILED)
    assert outcome.step(RouteStage.PRIMARY).status is StepStatus.RAN
    assert outcome.needs_human


def test_receipts_need_fewer_fields():
    receipt = "Supplier VAT: PT987654322\nDate: 12/09/2026\nTotal: 7,80 EUR"
    outcome = run(router(primary(texts=[receipt])).route(request(required_fields=RECEIPT_FIELDS)))
    assert outcome.status is OutcomeStatus.RESOLVED


def test_routing_is_deterministic():
    def once():
        engines = [primary(texts=[misread(GOOD)], layout=LayoutSignals(tables=1)), vl(), paid()]
        return run(router(*engines).route(request())).model_dump(mode="json")

    assert once() == once()


def test_owner_messages_stay_plain():
    banned = re.compile(
        r"ocr|engine|vlm|vat|iban|error|exception|conflict|api|null|http|\bid\b", re.IGNORECASE
    )
    fields = list(F)
    for missing, conflicts in [(fields[:1], []), ([], fields[3:5]), (fields[5:8], fields[8:])]:
        message = owner_message(missing, conflicts)
        assert not banned.search(message), message
        assert message.startswith("I still need one thing.")
    assert owner_message([], []) == "Done."


def test_request_validation():
    with pytest.raises(ValueError):
        RoutingRequest(tenant_id=" ", evidence_id="ev")
    with pytest.raises(ValueError):
        RouterConfig(long_document_pages=1)
    with pytest.raises(ValueError):
        RouterConfig(min_mean_confidence=2)
    with pytest.raises(ValueError):
        RouterConfig(max_cost_per_document=Decimal("-1"))
