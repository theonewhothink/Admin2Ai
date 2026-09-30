"""Regression tests for defects found in review of the OCR / Stage 0 module.

Each test names the rule it protects. They failed against the original
implementation and pass with the fixes.
"""

import asyncio
import json
import re
import sys
import threading
import time
import types
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from backoffice.domain.models import CriticalField as F
from backoffice.domain.models import ExtractionMethod as M
from backoffice.domain.models import FieldObservation
from backoffice.extraction import (
    FieldPath,
    ImageMetrics,
    JsonFieldMapper,
    LabelledFieldExtractor,
    PdfContent,
    QRHook,
    StructuredDataError,
    assess,
    extract_from_pdf,
    extract_html_structured,
    extract_structured,
    parse_einvoice,
    parse_epc_qr,
)
from backoffice.ocr import (
    COMMERCIAL,
    PADDLEOCR_VL,
    PP_OCR_V6_MEDIUM,
    CommercialOCRConfig,
    CommercialOCRProvider,
    ConflictKind,
    EndpointConfig,
    EngineRegistry,
    FakeOCRProvider,
    FieldState,
    GoldenDataset,
    InMemoryBudgetLedger,
    InProcessPaddleOCR,
    LayoutSignals,
    OCRCapabilities,
    OCRHints,
    OCRInputError,
    OCRRouter,
    OCRUnavailable,
    OutcomeStatus,
    PageImage,
    PPOCRConfig,
    PPOCRv6Provider,
    ReasonCode,
    RedactedInput,
    RouterConfig,
    RouteStage,
    RoutingRequest,
    StepStatus,
    decide,
    masked_pages_redactor,
    owner_message,
    text_only_redactor,
)
from backoffice.ocr.budget import BudgetHold
from backoffice.ocr.providers.commercial import parse_generic_response
from backoffice.ocr.providers.paddle_vl import parse_layout_response
from backoffice.ocr.providers.ppocr import parse_ocr_response

GOOD = """Invoice number: FT 2026/183
Invoice date: 2026-09-18
Supplier VAT: PT509123457
Subtotal: 393.17
VAT: 90.43
Total: 483.60 EUR"""
MISREAD = GOOD.replace("483.60", "488.60")
EXTERNAL = OCRCapabilities(external=True, accepts_pdf=True)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 24
UNKNOWN_LENGTH_PDF = b"%PDF-1.7\n1 0 obj << /Length 9 /Filter /FlateDecode >> stream ... endstream"


def run(coro):
    return asyncio.run(coro)


def page(text: str = GOOD) -> PageImage:
    return PageImage(data=text.encode(), mime_type="text/plain")


def request(*pages, **kwargs) -> RoutingRequest:
    kwargs.setdefault("pages", pages or (page(),))
    return RoutingRequest(tenant_id="t1", evidence_id="ev_1", **kwargs)


def paid(**kwargs) -> FakeOCRProvider:
    kwargs.setdefault("cost_per_page", Decimal("0.02"))
    return FakeOCRProvider(COMMERCIAL, method=M.VLM, capabilities=EXTERNAL, **kwargs)


def chain(*providers, budget=None, config=None, extractor=None) -> OCRRouter:
    return OCRRouter(
        EngineRegistry(providers), extractor or LabelledFieldExtractor(), config=config, budget=budget
    )


def see(value, method=M.OCR, source="ev_1@pp-ocrv6-medium"):
    return FieldObservation(value=value, source=source, method=method, confidence=0.6)


# --------------------------------------------------------------------------- §17/§19 independence


def test_a_text_only_paid_reading_is_an_echo_and_cannot_break_a_tie():
    """The paid engine re-read PaddleOCR-VL's text; counting it as a second vote
    would have turned VL's misread 488.60 into a MAJORITY (a silent error)."""
    echo = paid(echo_prior_text=True)
    engines = [
        FakeOCRProvider(PP_OCR_V6_MEDIUM, texts=[GOOD], layout=LayoutSignals(tables=1)),
        FakeOCRProvider(PADDLEOCR_VL, method=M.VLM, confidence=None, texts=[MISREAD]),
        echo,
    ]
    outcome = run(chain(*engines, budget=InMemoryBudgetLedger()).route(request()))
    assert "488.60" in echo.calls[0][1].prior_text  # it was given VL's text
    gross = outcome.fields[F.GROSS_AMOUNT]
    assert gross.state is FieldState.CONFLICT and gross.value is None
    assert outcome.status is OutcomeStatus.HUMAN_EXCEPTION
    step = outcome.step(RouteStage.COMMERCIAL)
    assert step.status is StepStatus.RAN and step.cost == Decimal("0.02")
    assert (step.reasons[-1].code, step.reasons[-1].detail) == (ReasonCode.NOT_INDEPENDENT, PADDLEOCR_VL)
    voters = {c.key: c.voters for c in gross.candidates}
    assert voters["488.6"] == ("ev_1@paddleocr-vl|vlm",)  # the echo merged into VL's vote


def test_a_text_only_paid_reading_may_still_fill_a_gap_as_the_local_engines_reading():
    echo = paid(echo_prior_text=True)
    engines = [FakeOCRProvider(PP_OCR_V6_MEDIUM, texts=["Total: 483.60 EUR"]), echo]
    config = RouterConfig(required_fields=frozenset({F.GROSS_AMOUNT, F.DUE_DATE}))
    outcome = run(chain(*engines, budget=InMemoryBudgetLedger(), config=config).route(request()))
    gross = outcome.fields[F.GROSS_AMOUNT]
    assert gross.state is FieldState.SINGLE  # never AGREED with itself
    assert outcome.missing == (F.DUE_DATE,)


def test_extractors_cannot_label_ocr_text_as_structured_evidence():
    """An extractor returning STRUCTURED_XML for OCR text made it an anchor that
    readings could never overrule, turning a misread into a hard conflict."""

    def spoofing(text, source, method):
        forged = FieldObservation(value="488.60", source="ev_1", method=M.STRUCTURED_XML, confidence=1)
        return {F.GROSS_AMOUNT: (forged,)}

    qr = {F.GROSS_AMOUNT: [see("483.60", M.QR, "ev_1")]}
    config = RouterConfig(required_fields=frozenset({F.GROSS_AMOUNT, F.ISSUE_DATE}))
    outcome = run(
        chain(FakeOCRProvider(PP_OCR_V6_MEDIUM), extractor=spoofing, config=config).route(request(structured=qr))
    )
    gross = outcome.fields[F.GROSS_AMOUNT]
    read = [o for o in gross.observations if o.value == "488.60"]
    assert [(o.method, o.source) for o in read] == [(M.OCR, "ev_1@pp-ocrv6-medium")]
    assert gross.conflict is ConflictKind.READINGS and gross.resolvable


# --------------------------------------------------------------------------- §3 closure needs evidence


def test_nothing_is_settled_without_required_fields():
    """An empty required set made routing RESOLVED ("Done.") with zero evidence."""
    with pytest.raises(ValueError):
        RoutingRequest(tenant_id="t1", evidence_id="ev_1", required_fields=[])
    with pytest.raises(ValueError):
        RouterConfig(required_fields=frozenset())


# --------------------------------------------------------------------------- unusable values


def test_unreadable_values_never_settle_a_field():
    assert decide(F.ISSUE_DATE, [see("31/02/2026")]).state is FieldState.MISSING
    both_garbage = decide(F.GROSS_AMOUNT, [see("4B3,6O"), see("4B3,6O", M.VLM, "ev_1@paddleocr-vl")])
    assert both_garbage.state is FieldState.MISSING and both_garbage.value is None
    assert both_garbage.observations  # kept for the audit trail (§54)
    bad_checksum = decide(F.IBAN, [see("PT50 0002 0123 1234 5678 9015 5")])
    assert bad_checksum.state is FieldState.MISSING


def test_garbage_can_be_dissent_but_never_a_majority():
    readings = [
        see("4B3,6O"),
        see("4B3,6O", M.VLM, "ev_1@paddleocr-vl"),
        see("483.60", M.VLM, "ev_1@commercial"),
    ]
    result = decide(F.GROSS_AMOUNT, readings)
    assert result.state is FieldState.CONFLICT and result.value is None
    split = decide(F.GROSS_AMOUNT, readings[:1] + readings[2:])
    assert split.state is FieldState.CONFLICT and split.resolvable  # the real value could still win


def test_an_unreadable_date_goes_to_a_human_as_missing():
    def raw_date(text, source, method):
        return {F.ISSUE_DATE: (FieldObservation(value="31/02/2026", source=source, method=method, confidence=0.5),)}

    config = RouterConfig(required_fields=frozenset({F.ISSUE_DATE}))
    outcome = run(chain(FakeOCRProvider(PP_OCR_V6_MEDIUM), extractor=raw_date, config=config).route(request()))
    assert outcome.status is OutcomeStatus.HUMAN_EXCEPTION and outcome.missing == (F.ISSUE_DATE,)
    assert "I couldn't find the date." in outcome.owner_message


# --------------------------------------------------------------------------- the paid step


def test_the_paid_step_is_skipped_when_a_human_is_needed_anyway():
    """A self-contradicting document plus a missing field still paid for commercial OCR."""
    contradiction = {F.GROSS_AMOUNT: [see("483.60", M.STRUCTURED_XML, "ev_1"), see("438.60", M.QR, "ev_1")]}
    commercial = paid(texts=["Due date: 2026-10-01"])
    primary = FakeOCRProvider(PP_OCR_V6_MEDIUM, texts=["nothing useful"])
    ledger = InMemoryBudgetLedger()
    required = [F.GROSS_AMOUNT, F.DUE_DATE]
    outcome = run(
        chain(primary, commercial, budget=ledger).route(request(structured=contradiction, required_fields=required))
    )
    assert commercial.calls == [] and outcome.total_cost == 0 and ledger.spent("t1") == 0
    assert len(primary.calls) == 1  # free local reading still helps the person who looks at it
    step = outcome.step(RouteStage.COMMERCIAL)
    assert step.status is StepStatus.SKIPPED
    assert [(r.code, r.detail) for r in step.reasons] == [(ReasonCode.UNRESOLVABLE_CONFLICT, "gross_amount")]
    assert outcome.needs_human


def test_external_engines_only_run_in_the_commercial_role():
    """§53: an external engine registered as the primary would have received every document."""
    external_primary = FakeOCRProvider(PP_OCR_V6_MEDIUM, capabilities=EXTERNAL)
    outcome = run(chain(external_primary).route(request()))
    assert external_primary.calls == []
    assert outcome.step(RouteStage.PRIMARY).reasons[-1].code is ReasonCode.EXTERNAL_NOT_ALLOWED


def test_paid_engines_need_a_tenant_ledger_unless_explicitly_allowed():
    engines = [FakeOCRProvider(PP_OCR_V6_MEDIUM, texts=[MISREAD], layout=LayoutSignals(tables=1)),
               FakeOCRProvider(PADDLEOCR_VL, method=M.VLM, confidence=None)]  # fmt: skip
    commercial = paid()
    outcome = run(chain(*engines, commercial).route(request()))
    assert commercial.calls == [] and outcome.step(RouteStage.COMMERCIAL).has(ReasonCode.NO_BUDGET)
    allowed = RouterConfig(allow_paid_without_budget=True)
    outcome = run(chain(*engines, paid(), config=allowed).route(request()))
    assert outcome.step(RouteStage.COMMERCIAL).status is StepStatus.RAN


def test_self_hosted_engines_with_an_amortised_cost_need_no_ledger():
    primary = FakeOCRProvider(PP_OCR_V6_MEDIUM, cost_per_page=Decimal("0.0004"))
    outcome = run(chain(primary).route(request()))
    assert outcome.step(RouteStage.PRIMARY).status is StepStatus.RAN
    assert outcome.total_cost == Decimal("0.0004")


def test_a_failed_paid_call_costs_the_same_with_or_without_a_ledger():
    """Without a ledger a timed-out paid call was recorded as free."""
    engines = [
        FakeOCRProvider(PP_OCR_V6_MEDIUM, texts=[MISREAD], layout=LayoutSignals(tables=1)),
        FakeOCRProvider(PADDLEOCR_VL, method=M.VLM, confidence=None),
        paid(error=OCRUnavailable(COMMERCIAL, "timeout")),
    ]
    allowed = RouterConfig(allow_paid_without_budget=True)
    outcome = run(chain(*engines, config=allowed).route(request()))
    step = outcome.step(RouteStage.COMMERCIAL)
    assert step.status is StepStatus.FAILED and step.cost == Decimal("0.02")
    assert outcome.total_cost == Decimal("0.02")


def test_a_paid_engine_is_not_given_a_pdf_of_unknown_length():
    """A compressed PDF counted as one page: the budget check and the vendor
    page limit were both bypassed for what could be hundreds of pages."""
    pdf = PageImage(data=UNKNOWN_LENGTH_PDF, mime_type="application/pdf")
    assert pdf.pages is None
    primary = FakeOCRProvider(PP_OCR_V6_MEDIUM, texts=[MISREAD], layout=LayoutSignals(tables=1))
    vl = FakeOCRProvider(PADDLEOCR_VL, method=M.VLM, confidence=None, texts=[GOOD])
    commercial = paid(texts=[GOOD])
    ledger = InMemoryBudgetLedger(default_ceiling=Decimal("1"))
    outcome = run(chain(primary, vl, commercial, budget=ledger).route(request(pdf)))
    assert len(primary.calls) == 1  # free local engines still read it
    step = outcome.step(RouteStage.COMMERCIAL)
    assert commercial.calls == [] and step.reasons[-1].detail == "unknown_page_count"
    known = request(pdf, hints=OCRHints(page_count=3))
    outcome = run(chain(primary, vl, commercial, budget=ledger).route(known))
    assert outcome.step(RouteStage.COMMERCIAL).status is StepStatus.RAN
    assert commercial.calls[0][1].page_count == 3


# --------------------------------------------------------------------------- budget periods


def test_a_hold_is_settled_in_the_period_it_was_taken():
    """Settling after a month boundary made October's spend negative (-0.38)."""
    clock = {"period": "2026-09"}
    ledger = InMemoryBudgetLedger(default_ceiling=Decimal("1"), period=lambda: clock["period"])
    hold = ledger.reserve("t1", Decimal("0.40"))
    assert isinstance(hold, BudgetHold) and hold.period == "2026-09"
    clock["period"] = "2026-10"
    ledger.settle(hold, Decimal("0.02"))
    assert ledger.spent("t1") == 0 and ledger.remaining("t1") == Decimal("1")
    assert ledger.spent("t1", "2026-09") == Decimal("0.02")


def test_zero_holds_always_succeed_and_amounts_are_checked():
    ledger = InMemoryBudgetLedger(default_ceiling=Decimal("0.01"))
    assert ledger.reserve("t1", Decimal("0.02")) is None
    zero = ledger.reserve("t1", Decimal("0"))
    assert zero is not None
    ledger.settle(zero, Decimal("0.05"))  # an engine billed although it was expected to be free
    assert ledger.spent("t1") == Decimal("0.05") and ledger.remaining("t1") == 0
    with pytest.raises(ValueError):
        ledger.reserve("t1", Decimal("-1"))
    with pytest.raises(ValueError):
        ledger.settle(zero, Decimal("-1"))


# --------------------------------------------------------------------------- commercial transport and flags


def vendor(respond, redactor, url="https://api.vendor.example", **config):
    cfg = CommercialOCRConfig(
        endpoint=EndpointConfig(url, api_key="k"), model="v", cost_per_page=Decimal("0.015"), **config
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return CommercialOCRProvider(cfg, redactor=redactor, client=client)


def chat(text):
    return lambda request: httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


class Redaction:
    def __init__(self, text):
        self.text = text

    def restore(self, text):
        return text


def test_an_external_engine_needs_verified_https():
    """Documents and the API key would otherwise cross the internet in clear text (§52)."""
    with pytest.raises(ValueError):
        vendor(chat("x"), text_only_redactor(Redaction), url="http://api.vendor.example")
    with pytest.raises(ValueError):
        CommercialOCRConfig(
            endpoint=EndpointConfig("https://api.vendor.example", verify_tls=False),
            model="v",
            cost_per_page=Decimal("0.01"),
        )


def test_commercial_results_say_whether_they_re_read_local_text():
    text_only = vendor(chat("Total 1,00"), text_only_redactor(Redaction))
    result = run(text_only.recognize([PNG], OCRHints(prior_text="Total 1,00")))
    assert result.from_prior_text

    def mask(p):
        return PageImage(data=p.data + b"m", mime_type=p.mime_type, number=p.number)

    pixels_only = vendor(chat("Total 1,00"), masked_pages_redactor(mask))
    assert not run(pixels_only.recognize([PNG], OCRHints(prior_text="Total 1,00"))).from_prior_text
    primed = vendor(chat("Total 1,00"), masked_pages_redactor(mask, Redaction))
    assert run(primed.recognize([PNG], OCRHints(prior_text="Total 1,00"))).from_prior_text


def test_commercial_refuses_to_send_a_pdf_of_unknown_length():
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    def as_is_but_new_bytes(pages, hints):
        return RedactedInput(pages=tuple(PageImage(data=p.data + b" ", mime_type=p.mime_type) for p in pages))

    provider = vendor(respond, as_is_but_new_bytes)
    with pytest.raises(OCRInputError):
        run(provider.recognize([PageImage(data=UNKNOWN_LENGTH_PDF, mime_type="application/pdf")]))
    assert sent == []


# --------------------------------------------------------------------------- Stage 0 robustness (§52)

DEEP_JSON = "[" * 100_000 + "]" * 100_000


def test_deeply_nested_json_ld_is_noted_not_a_crash():
    """json.loads raised RecursionError straight out of Stage 0."""
    html = f'<script type="application/ld+json">{DEEP_JSON}</script>'
    [result] = extract_html_structured(html, source="ev_1")
    assert result.empty and result.notes == ("jsonld_unreadable",)


def test_deeply_nested_api_json_is_a_typed_error():
    mapper = JsonFieldMapper({F.GROSS_AMOUNT: "total"})
    with pytest.raises(StructuredDataError) as info:
        mapper.extract(DEEP_JSON, source="ev_1")
    assert info.value.code == "json_unreadable"


def test_unreadable_json_and_oversized_html_continue_as_noted_results():
    """PDFs were noted and passed on to OCR, but broken JSON/HTML raised instead."""
    mapper = JsonFieldMapper({F.GROSS_AMOUNT: "total"})
    [result] = extract_structured(b'{"total": ', source="ev_1", json_mapper=mapper)
    assert result.empty and result.notes == ("json_unreadable",)
    huge = b"<html>" + b" " * (10 * 1024 * 1024)
    [result] = extract_structured(huge, source="ev_1")
    assert result.empty and result.notes == ("html_too_large",)


def test_deeply_nested_microdata_is_linear_and_noted():
    """The microdata parser rescanned its whole element stack for every text
    chunk: 10 MB of nested tags took hours (quadratic)."""
    html = '<div itemscope itemtype="https://schema.org/Invoice">' + "<b>x" * 60_000
    started = time.perf_counter()
    results = extract_html_structured(html, source="ev_1")
    assert time.perf_counter() - started < 3.0
    assert any("microdata_too_deep" in r.notes for r in results)


def test_microdata_still_reads_values_after_the_fix():
    html = """<div itemscope itemtype="https://schema.org/Invoice">
      <span itemprop="identifier">A-1</span>
      <div itemprop="totalPaymentDue" itemscope itemtype="https://schema.org/PriceSpecification">
        <span itemprop="price"><b>12</b>.50</span><meta itemprop="priceCurrency" content="EUR"></div>
      <div itemprop="provider" itemscope itemtype="https://schema.org/Organization">
        <span itemprop="vatID">PT509123457</span></div></div>"""
    [result] = extract_html_structured(html, source="ev_1")
    values = {f: [o.value for o in obs] for f, obs in result.fields.items()}
    assert values[F.INVOICE_NUMBER] == ["A-1"] and values[F.GROSS_AMOUNT] == [Decimal("12.50")]
    assert values[F.CURRENCY] == ["EUR"] and values[F.SUPPLIER_TAX_ID] == ["PT509123457"]


def invoice_jsonld(status: str | None, price) -> str:
    status_part = f'"paymentStatus": "{status}",' if status else ""
    return (
        '<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Invoice",'
        f' "identifier": "A-1", {status_part} "totalPaymentDue": '
        f'{{"@type": "PriceSpecification", "price": {price}, "priceCurrency": "EUR"}}}}</script>'
    )


@pytest.mark.parametrize(
    ("status", "price"),
    [("https://schema.org/PaymentComplete", "70.00"), ("PaymentAutomaticallyApplied", "70.00"), (None, "0")],
)
def test_an_amount_due_on_a_paid_invoice_is_not_its_total(status, price):
    """totalPaymentDue 0 on a paid invoice became gross_amount 0: a settled, silent error."""
    [result] = extract_html_structured(invoice_jsonld(status, price), source="ev_1")
    assert F.GROSS_AMOUNT not in result.fields
    assert result.extras["amount_payable"] == str(Decimal(price))
    assert "total_due_not_invoice_total" in result.notes
    assert [o.value for o in result.fields[F.CURRENCY]] == ["EUR"]


def test_an_open_invoice_total_due_is_still_its_total():
    [result] = extract_html_structured(invoice_jsonld("PaymentDue", "70.00"), source="ev_1")
    assert [o.value for o in result.fields[F.GROSS_AMOUNT]] == [Decimal("70.00")]


# --------------------------------------------------------------------------- image quality (§11)


def test_non_finite_or_impossible_metrics_are_unknown_not_good():
    """NaN never compares below a threshold, so a NaN sharpness passed as sharp."""
    nan = float("nan")
    report = assess(ImageMetrics(sharpness=nan, glare_ratio=1.7, brightness=float("inf"), skew_degrees=nan))
    assert {"sharpness", "glare_ratio", "brightness", "skew_degrees"} <= report.unknown
    assert not report.flags
    phone = ImageMetrics.from_mapping({"sharpness": "NaN", "width": "wide", "glareRatio": "0.5", "height": 1200})
    assert phone.sharpness is None and phone.width is None
    assert phone.glare_ratio == 0.5 and phone.height == 1200


# --------------------------------------------------------------------------- in-process PaddleOCR


def test_in_process_paddleocr_never_predicts_concurrently(monkeypatch):
    """Paddle predictors are not thread-safe; four pages ran four predictions at once."""
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    class Result:
        json = {"res": {"rec_texts": ["x"], "rec_scores": [0.9]}}

    class PaddleOCR:
        def __init__(self, **kwargs):
            pass

        def predict(self, path, **kwargs):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.02)
            with lock:
                state["active"] -= 1
            return [Result()]

    module = types.ModuleType("paddleocr")
    module.PaddleOCR = PaddleOCR
    monkeypatch.setitem(sys.modules, "paddleocr", module)
    engine = InProcessPaddleOCR()
    provider = PPOCRv6Provider(PPOCRConfig(max_concurrency=4), local=engine)
    result = run(provider.recognize([PNG] * 4))
    assert state["peak"] == 1 and len(result.pages) == 4
    run(provider.recognize([PNG]))  # a second event loop must not break the engine's lock


# --------------------------------------------------------------------------- dates across time zones


def test_epoch_timestamps_can_be_read_in_the_issuers_time_zone():
    """23:30 UTC on 30 September is already 1 October in Lisbon: the wrong month to close."""
    stamp = int(datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc).timestamp())
    utc = JsonFieldMapper({F.ISSUE_DATE: FieldPath("created", date_format="unix")})
    lisbon = JsonFieldMapper({F.ISSUE_DATE: FieldPath("created", date_format="unix", timezone="Europe/Lisbon")})
    assert utc.extract({"created": stamp}, source="ev").fields[F.ISSUE_DATE][0].value == date(2026, 9, 30)
    assert lisbon.extract({"created": stamp}, source="ev").fields[F.ISSUE_DATE][0].value == date(2026, 10, 1)
    with pytest.raises(ValueError):
        FieldPath("created", date_format="unix", timezone="Mars/Olympus")


# --------------------------------------------------------------------------- §18 confidence


def test_a_located_reading_carries_the_ocr_line_confidence():
    """The stored confidence was the extractor's constant, whatever the engine's own score."""
    shaky = FakeOCRProvider(PP_OCR_V6_MEDIUM, confidence=0.31)
    config = RouterConfig(required_fields=frozenset({F.GROSS_AMOUNT}))
    outcome = run(chain(shaky, config=config).route(request()))
    [reading] = outcome.fields[F.GROSS_AMOUNT].observations
    assert reading.location is not None and reading.confidence == pytest.approx(0.31)
    sure = FakeOCRProvider(PP_OCR_V6_MEDIUM, confidence=0.99)
    outcome = run(chain(sure, config=config).route(request()))
    assert outcome.fields[F.GROSS_AMOUNT].observations[0].confidence == 0.5  # extractor's own doubt kept


# --------------------------------------------------------------------------- a page showing several values

TWO_ACCOUNTS = GOOD + "\nIBAN: PT50 0002 0123 1234 5678 9015 4\nIBAN: GB82 WEST 1234 5698 7654 32"


def test_a_page_listing_two_accounts_is_not_a_misreading_to_pay_for():
    """Two readers both see the same two accounts: more reading cannot pick one,
    yet the chain escalated to the paid engine and blamed a misreading."""
    commercial = paid()
    engines = [FakeOCRProvider(PP_OCR_V6_MEDIUM), FakeOCRProvider(PADDLEOCR_VL, method=M.VLM, confidence=None)]
    outcome = run(chain(*engines, commercial, budget=InMemoryBudgetLedger()).route(request(page(TWO_ACCOUNTS))))
    iban = outcome.fields[F.IBAN]
    assert iban.state is FieldState.CONFLICT and iban.conflict is ConflictKind.SEVERAL and not iban.resolvable
    assert commercial.calls == [] and outcome.total_cost == 0
    assert outcome.needs_human  # which account to pay stays a person's call (§19, §26)
    assert outcome.owner_message == (
        "I still need one thing. I found more than one value for the bank account. "
        "Could you take a look at this document?"
    )


def test_one_reader_seeing_two_values_may_still_be_a_misread():
    first = see("483.60"), see("488.60")  # one engine, two different totals
    assert decide(F.GROSS_AMOUNT, first).resolvable
    confirmed = decide(F.GROSS_AMOUNT, [*first, see("483.60", M.VLM, "ev_1@paddleocr-vl"),
                                        see("483.60", M.VLM, "ev_1@commercial")])  # fmt: skip
    assert confirmed.state is FieldState.MAJORITY and confirmed.value == Decimal("483.60")


def test_a_structured_source_listing_several_accounts_is_several_not_a_contradiction():
    a, b = "PT50000201231234567890154", "GB82WEST12345698765432"
    xml = [see(a, M.STRUCTURED_XML, "ev_1"), see(b, M.STRUCTURED_XML, "ev_1")]
    assert decide(F.IBAN, xml).conflict is ConflictKind.SEVERAL
    assert decide(F.IBAN, [*xml, see(a, M.QR, "ev_1")]).conflict is ConflictKind.SEVERAL
    other = "DE89370400440532013000"
    hijacked = decide(F.IBAN, [*xml, see(other, M.QR, "ev_1")])  # the payment code names a third account
    assert hijacked.conflict is ConflictKind.SOURCES


# --------------------------------------------------------------------------- tolerant parsing


@pytest.mark.parametrize("junk", [1, 1.5, True, float("nan"), {"a": 1}, "text"])
@pytest.mark.parametrize("key", ["rec_scores", "rec_polys", "rec_boxes", "rec_texts"])
def test_malformed_paddlex_arrays_never_escape_as_raw_errors(key, junk):
    """A scalar or object where PaddleX sends an array raised KeyError/TypeError."""
    pruned = {"rec_texts": ["Total: 1,00"], "rec_scores": [0.9], "rec_polys": [[[0, 0], [9, 0], [9, 9], [0, 9]]]}
    pruned[key] = junk
    body = {"errorCode": 0, "result": {"ocrResults": [{"prunedResult": pruned}]}}
    [result_page] = parse_ocr_response(body, first_page=1, engine="pp-ocrv6-medium")
    assert all(isinstance(line.text, str) for line in result_page.lines)


@pytest.mark.parametrize("junk", [1, 1.5, True, "text", {"a": 1}])
def test_malformed_generic_vendor_lines_never_escape_as_raw_errors(junk):
    [result_page] = parse_generic_response({"pages": [{"text": "Total 1,00", "lines": junk}]}, first_page=1, engine="c")
    assert result_page.text == "Total 1,00"


# --------------------------------------------------------------------------- e-invoices

UBL_TEMPLATE = """<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
 <cbc:ID>A-1</cbc:ID><cbc:IssueDate>2026-09-18</cbc:IssueDate><cbc:InvoiceTypeCode>{code}</cbc:InvoiceTypeCode>{currency}
 <cac:TaxTotal><cbc:TaxAmount currencyID="{unit}">{vat}</cbc:TaxAmount></cac:TaxTotal>
 <cac:LegalMonetaryTotal><cbc:TaxExclusiveAmount currencyID="{unit}">{net}</cbc:TaxExclusiveAmount>
 <cbc:TaxInclusiveAmount currencyID="{unit}">{gross}</cbc:TaxInclusiveAmount></cac:LegalMonetaryTotal>
</Invoice>"""


def ubl(code="380", currency="", unit="EUR", vat="90.43", net="393.17", gross="483.60"):
    return UBL_TEMPLATE.format(code=code, currency=currency, unit=unit, vat=vat, net=net, gross=gross)


def test_amount_currency_ids_give_the_currency_when_the_document_code_is_absent():
    """Every UBL amount carries a mandatory currencyID; it was ignored without a document currency."""
    result = parse_einvoice(ubl(), source="ev_1")
    assert {o.value for o in result.fields[F.CURRENCY]} == {"EUR"}
    mixed = parse_einvoice(ubl().replace('TaxAmount currencyID="EUR"', 'TaxAmount currencyID="USD"'), source="ev_1")
    assert {o.value for o in mixed.fields[F.CURRENCY]} == {"EUR", "USD"}  # surfaces as a conflict, never picked


def test_a_credit_note_with_negative_amounts_is_flagged():
    """Credit amounts are positive by convention (the type carries the sign); negative
    ones would be negated twice downstream, turning a refund into a charge."""
    result = parse_einvoice(ubl(code="381", vat="-90.43", net="-393.17", gross="-483.60"), source="ev_1")
    assert "credit_note_with_negative_amounts" in result.notes
    positive = parse_einvoice(ubl(code="381"), source="ev_1")
    assert "credit_note_with_negative_amounts" not in positive.notes


# --------------------------------------------------------------------------- golden dataset (§56)


def test_golden_expectations_must_be_usable_values(tmp_path):
    """An expected IBAN failing mod-97 was accepted: the benchmark could never score it correct."""
    (tmp_path / "a.txt").write_text("IBAN: PT50 0002 0123 1234 5678 9015 5")
    manifest = tmp_path / "golden.json"
    doc = {"id": "a", "pages": ["a.txt"], "expected": {"iban": "PT50000201231234567890155"}}
    manifest.write_text(json.dumps({"name": "t", "version": "1", "documents": [doc]}))
    with pytest.raises(ValidationError):
        GoldenDataset.load(manifest)


def test_pdf_text_observations_are_labelled_as_embedded_text():
    """The text-layer path trusted the extractor's method, so text could pose as XML or QR."""

    def spoofing(text, source, method):
        return {F.GROSS_AMOUNT: (FieldObservation(value="483.60", source="elsewhere", method=M.QR, confidence=0.9),)}

    content = PdfContent(page_texts=("Total: 483.60 EUR and enough text for a layer",))
    [result] = extract_from_pdf(content, source="ev_1", text_extractor=spoofing)
    [observation] = result.fields[F.GROSS_AMOUNT]
    assert (observation.method, observation.source) == (M.EMBEDDED_TEXT, "ev_1")
    scanned = PdfContent(page_texts=content.page_texts, metadata={"Producer": "OCRmyPDF 15"})
    [result] = extract_from_pdf(scanned, source="ev_1", text_extractor=spoofing)
    assert result.fields[F.GROSS_AMOUNT][0].method is M.OCR


def test_owner_messages_for_several_values_stay_plain():
    banned = re.compile(r"ocr|engine|vlm|vat|iban|error|exception|conflict|api|null|http|\bid\b", re.IGNORECASE)
    fields = list(F)
    message = owner_message(fields[:2], fields[2:6], several=fields[4:6])
    assert not banned.search(message), message
    assert "I read different values for" in message and "I found more than one value for" in message


def test_a_broken_qr_handler_is_noted_and_does_not_stop_stage0():
    def buggy(payload, source):
        raise IndexError("list index out of range")

    hook = QRHook([buggy, parse_epc_qr])
    [result] = hook.extract(["A:509123457*B:999999990"], source="ev_1")
    assert result.empty and result.notes == ("qr_handler_failed:IndexError",)


@pytest.mark.parametrize("junk", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_numbers_from_paddlex_are_ignored(junk):
    """json.loads accepts NaN/Infinity; int() of them raised ValueError/OverflowError."""
    pruned = {"rec_texts": ["a"], "doc_preprocessor_res": {"angle": junk}, "width": junk}
    pp = {"result": {"ocrResults": [{"prunedResult": pruned}], "dataInfo": {"width": junk, "height": junk}}}
    [pp_page] = parse_ocr_response(pp, first_page=1, engine="pp")
    assert pp_page.layout.rotation == 0 and pp_page.width is None
    vl = {"result": {"layoutParsingResults": [{"prunedResult": {
        "parsing_res_list": [{"block_content": "a", "block_bbox": [0, 0, 5, 5]}],
        "doc_preprocessor_res": {"angle": junk}, "width": junk}}]}}  # fmt: skip
    [vl_page] = parse_layout_response(vl, first_page=1, engine="vl")
    assert vl_page.layout.rotation == 0 and vl_page.width is None


def test_an_unknown_length_pdf_reserves_the_paid_engines_page_limit():
    """With a page limit the reservation has an upper bound, so text-only redaction
    (which sends no pages) can still run on compressed PDFs."""
    pdf = PageImage(data=UNKNOWN_LENGTH_PDF, mime_type="application/pdf")
    limited = OCRCapabilities(external=True, accepts_pdf=True, max_pages=20)
    commercial = FakeOCRProvider(
        COMMERCIAL, method=M.VLM, capabilities=limited, cost_per_page=Decimal("0.02"), echo_prior_text=True
    )
    primary = FakeOCRProvider(PP_OCR_V6_MEDIUM, texts=["Total: 483.60 EUR"])
    config = RouterConfig(required_fields=frozenset({F.GROSS_AMOUNT, F.DUE_DATE}))
    tight = InMemoryBudgetLedger(default_ceiling=Decimal("0.39"))
    outcome = run(chain(primary, commercial, budget=tight, config=config).route(request(pdf)))
    assert commercial.calls == [] and outcome.step(RouteStage.COMMERCIAL).has(ReasonCode.BUDGET_EXCEEDED)
    enough = InMemoryBudgetLedger(default_ceiling=Decimal("0.40"))
    outcome = run(chain(primary, commercial, budget=enough, config=config).route(request(pdf)))
    assert outcome.step(RouteStage.COMMERCIAL).status is StepStatus.RAN
    assert enough.spent("t1") == Decimal("0.02")  # the hold of 0.40 settled to the real bill
