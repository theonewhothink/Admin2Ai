"""Uploaded PDFs and photos, read end to end on the demo tenant (§13-19, §56).

Every test drives the real upload path (``POST /api/evidence`` through
``BackOfficeService.dispatch``) with a real file from the golden set in
``fixtures/documents`` (see ``make_documents.py``): Stage 0 reads the PDF text
layer and the fiscal QR, local OCR is a scripted engine behind the real
router, and the Claude fallback is the real provider over a mocked transport.
The document must then travel the normal pipeline exactly like an e-invoice:
understood -> checked -> matched -> closed, visible in Documents, the Diagram
and the month.
"""

from __future__ import annotations

import base64
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import BoundingBox, ExtractionMethod, Quality, TransactionKind
from backoffice.ocr import (
    COMMERCIAL,
    PP_OCR_V6_MEDIUM,
    ClaudeVisionConfig,
    ClaudeVisionProvider,
    EngineRegistry,
    FakeOCRProvider,
    GoldenDataset,
    InMemoryBudgetLedger,
    OCRCapabilities,
)
from backoffice.orchestrator import BankRow
from backoffice.reading import DocumentReader, StepState
from backoffice.service import BackOfficeService

pytest.importorskip("pypdf")

FIXTURES = Path(__file__).parent / "fixtures" / "documents"
DATASET = GoldenDataset.load(FIXTURES / "golden.json")
GOLDEN = {d.id: d for d in DATASET.documents}
TRANSCRIPTION = (FIXTURES / "central-fs-cc2026-3317.txt").read_text(encoding="utf-8")
CENTRAL_QR = ("A:516722344*B:516123459*C:PT*D:FS*E:N*F:20260929*G:FS CC2026/3317*H:CCQ7M2KP-3317*I1:PT*I5:21.15"
              "*I6:2.75*N:2.75*O:23.90*Q:Kx2e*R:0987")


class FixedQR:
    """A QR decoder that always sees the given payloads (keeps tests independent of zxing-cpp)."""

    name = "fixed"

    def __init__(self, *payloads: str) -> None:
        self.payloads = payloads
        self.calls = 0

    def decode(self, image: bytes) -> tuple[str, ...]:
        self.calls += 1
        return self.payloads


def service(reader: DocumentReader | None) -> BackOfficeService:
    svc = BackOfficeService.demo()
    svc.repo.reader = reader
    return svc


def upload(svc: BackOfficeService, name: str, content_type: str = "application/octet-stream") -> dict:
    body = {"filename": name, "contentType": content_type,
            "dataBase64": base64.b64encode((FIXTURES / name).read_bytes()).decode()}
    status, out = svc.dispatch("POST", "/api/evidence", json.dumps(body))
    assert status == 200, out
    return out


def only_document(svc: BackOfficeService, out: dict):  # type: ignore[no-untyped-def]
    assert len(out["documents"]) == 1, out
    return svc.repo.documents[out["documents"][0]["id"]]


def ocr_engine(text: str = TRANSCRIPTION) -> FakeOCRProvider:
    return FakeOCRProvider(PP_OCR_V6_MEDIUM, texts=[text], capabilities=OCRCapabilities(accepts_pdf=True))


def assert_expected(record, golden_id: str) -> None:  # type: ignore[no-untyped-def]
    """Every expected golden value is what verification settled, never a guess (§56)."""
    for name, expected in GOLDEN[golden_id].expected.items():
        check = record.checks[name.value]
        assert check.value is not None, (name, check.reasons)
        assert str(check.value).replace(" ", "") == expected.replace(" ", ""), (name, check.value, expected)


# --------------------------------------------------------------------------- the golden set itself


def test_golden_set_is_complete_and_loads() -> None:
    assert len(DATASET.documents) == 5
    for doc in DATASET.documents:
        for page in doc.pages:
            assert (FIXTURES / page).is_file(), page
        for tag in doc.tags:
            if tag.startswith("transcription:"):
                assert (FIXTURES / tag.split(":", 1)[1]).is_file()
    assert {t for d in DATASET.documents for t in d.tags} >= {"expect:verified", "expect:conflict", "qr-mismatch"}


# --------------------------------------------------------------------------- PDFs with a text layer


def test_pdf_with_text_layer_is_verified_and_closes_after_the_bank_match() -> None:
    svc = service(DocumentReader(qr_decoder=None))  # no OCR engine at all: Stage 0 is enough
    out = upload(svc, "edp-ft-558120.pdf", "application/pdf")
    record = only_document(svc, out)
    ev = out["evidenceIds"][0]
    assert out["message"] == "Got it. It matches the €64.10 payment to EDP on 19 September."
    assert record.document.quality is Quality.GREEN and record.matched_tx_ids
    assert_expected(record, "edp-ft-558120")
    # §18: every critical field keeps value, source, method, confidence and location
    gross = record.checks["gross_amount"]
    methods = {o.method for o in gross.observations}
    assert {ExtractionMethod.QR, ExtractionMethod.EMBEDDED_TEXT, ExtractionMethod.BANK} <= methods
    for obs in gross.observations:
        assert obs.source and obs.location and 0 < obs.confidence <= 1
    qr = next(o for o in gross.observations if o.method is ExtractionMethod.QR)
    assert qr.source == ev and qr.location == "qr:O"
    text = next(o for o in gross.observations if o.method is ExtractionMethod.EMBEDDED_TEXT)
    assert text.source == ev and str(text.location).startswith("text:line ")
    # the normal pipeline: understood -> checked -> matched -> closed, for both sides
    item = svc.repo.items[record.item_id]
    assert item.stage is Stage.CLOSED
    assert [t.to_stage for t in item.history][:2] == [Stage.ACQUIRED, Stage.UNDERSTOOD]
    tx = svc.repo.transactions[record.matched_tx_ids[0]]
    assert svc.repo.items[tx.item_id].stage is Stage.CLOSED and tx.document_ids == [record.id]
    # visible in Documents, the Diagram and the month
    docs = svc.dispatch("GET", "/api/documents", None)[1]["items"]
    listed = next(d for d in docs if d["id"] == record.id)
    assert listed["quality"] == "green" and listed["status"] == "matched" and listed["number"] == "FT EDP2026/558120"
    pipeline = svc.dispatch("GET", "/api/pipeline", None)[1]
    row = next(r for r in pipeline["items"] if r["id"] == record.item_id)
    assert row["stage"] == "closed" and row["source"] == "upload"
    month = svc.dispatch("GET", "/api/months/hazel-tree/2026-09", None)[1]
    assert not any("EDP" in r["text"] for r in month["remaining"])
    assert any(m["supplier"] == "EDP" for m in month["matched"])
    # the reading itself is audited, step by step
    read = svc.repo.reads[ev]
    assert [(s.step, s.state) for s in read.steps][:2] == [("pdf_text", StepState.DONE), ("pdf_qr", StepState.DONE)]
    assert svc.repo.audit.verify(svc.repo.tenant_id).ok


def test_qr_payload_in_pdf_metadata_counts_and_a_later_payment_closes_it() -> None:
    svc = service(DocumentReader(qr_decoder=None))
    out = upload(svc, "moderna-ft-pm2026-88.pdf", "application/pdf")
    record = only_document(svc, out)
    assert record.document.quality is Quality.GREEN and not record.matched_tx_ids
    assert_expected(record, "moderna-ft-pm2026-88")
    assert record.document.entity_id == "company-c"
    svc.orchestrator.ingest_bank([BankRow(
        bank_id="card-0922-moderna", account_id="card-2291", booked_on=date(2026, 9, 22), amount=Decimal("-49.20"),
        counterparty="PAPELARIA MODERNA PORTO", description="COMPRA CARTAO", kind=TransactionKind.CARD,
        card_last4="2291")])
    assert record.matched_tx_ids and svc.repo.items[record.item_id].stage is Stage.CLOSED


def test_mismatching_qr_total_is_a_conflict_with_a_plain_question() -> None:
    svc = service(DocumentReader(qr_decoder=None))
    svc.orchestrator.ingest_bank([BankRow(
        bank_id="mbcp-0925-alianca", account_id="mbcp-ht", booked_on=date(2026, 9, 25), amount=Decimal("-483.60"),
        counterparty="GRAFICA ALIANCA LDA", description="TRF GRAFICA ALIANCA", kind=TransactionKind.TRANSFER_OUT)])
    out = upload(svc, "alianca-ft-ga2026-412.pdf", "application/pdf")
    record = only_document(svc, out)
    assert record.document.quality is Quality.RED
    assert svc.repo.items[record.item_id].stage is Stage.CONFLICT
    assert record.document.gross_amount is None  # never guessed (§19)
    assert record.checks["gross_amount"].quality is Quality.RED
    assert not record.matched_tx_ids  # the €483.60 payment is not matched to a disputed invoice
    assert_expected(record, "alianca-ft-ga2026-412")  # what the sources agree on is still verified
    assert out["message"] == "Got it. I need one answer from you: Which is the right total on the Gráfica Aliança invoice?"
    items = svc.dispatch("GET", "/api/needs-you", None)[1]["items"]
    card = next(i for i in items if i["merchant"] == "Gráfica Aliança")
    assert card["kind"] == "choice" and card["tone"] == "attention"
    assert [o["label"] for o in card["options"]] == [
        "€438.00, as the QR code shows",
        "€483.60, as the document text shows",
        "Neither. I'll get a corrected invoice from Gráfica Aliança.",
    ]
    assert any("€438.00" in w and "€483.60" in w for w in card["why"])
    assert not any(word in " ".join(card["why"]).lower() for word in ("ocr", "reconcil", "embedded", "ev_"))
    # no guessing on "neither": it stays in conflict, nothing matched
    status, answer = svc.dispatch("POST", f"/api/needs-you/{card['id']}/answer", json.dumps({"option_id": "neither"}))
    assert status == 200 and answer["message"].startswith("Done. I set it aside.")
    assert svc.repo.items[record.item_id].stage is Stage.CONFLICT and record.document.quality is Quality.RED


def test_owner_answer_settles_the_conflict_with_evidence_and_the_payment_closes_it() -> None:
    svc = service(DocumentReader(qr_decoder=None))
    out = upload(svc, "alianca-ft-ga2026-412.pdf", "application/pdf")
    record = only_document(svc, out)
    card = next(i for i in svc.dispatch("GET", "/api/needs-you", None)[1]["items"] if i["merchant"] == "Gráfica Aliança")
    qr_option = next(o["id"] for o in card["options"] if "QR code" in o["label"])
    status, answer = svc.dispatch("POST", f"/api/needs-you/{card['id']}/answer", json.dumps({"option_id": qr_option}))
    assert status == 200 and answer["message"] == "Done. I will use €438.00 for the Gráfica Aliança invoice."
    assert record.document.quality is Quality.GREEN
    assert (record.document.gross_amount, record.document.net_amount, record.document.vat_amount) == (
        Decimal("438.00"), Decimal("356.10"), Decimal("81.90"))
    human = [o for o in record.checks["gross_amount"].observations if o.method is ExtractionMethod.HUMAN]
    assert len(human) == 1 and svc.repo.evidence(human[0].source)  # the owner's answer is stored evidence
    assert not any(o.value == Decimal("483.60") for o in record.checks["gross_amount"].observations
                   if o.method is not ExtractionMethod.ARITHMETIC)
    assert record.observations["gross_amount"]  # the disagreeing reading stays on the record (§55)
    svc.orchestrator.ingest_bank([BankRow(
        bank_id="mbcp-0926-alianca", account_id="mbcp-ht", booked_on=date(2026, 9, 26), amount=Decimal("-438.00"),
        counterparty="GRAFICA ALIANCA LDA", description="TRF GRAFICA ALIANCA", kind=TransactionKind.TRANSFER_OUT)])
    assert record.matched_tx_ids and svc.repo.items[record.item_id].stage is Stage.CLOSED


def _text_upload(svc: BackOfficeService, name: str, lines: list[str]) -> dict:
    body = {"filename": name, "contentType": "text/plain",
            "dataBase64": base64.b64encode("\n".join(lines).encode()).decode()}
    status, out = svc.dispatch("POST", "/api/evidence", json.dumps(body))
    assert status == 200, out
    return out


def test_an_invoice_that_does_not_add_up_asks_what_to_do() -> None:
    svc = service(None)
    out = _text_upload(svc, "fatura.txt", [
        "Gráfica Aliança, Lda.", "NIF: 509123457", "Fatura n.º FT GA2026/500", "Data de emissão: 20/09/2026",
        "Cliente: Hazel Tree Interiores, Lda.", "NIF: 516123459", "Base tributável (23%): 100,00", "IVA 23%: 23,00",
        "Total: 150,00 €"])
    record = only_document(svc, out)
    assert record.document.quality is Quality.RED and svc.repo.items[record.item_id].stage is Stage.CONFLICT
    card = next(i for i in svc.dispatch("GET", "/api/needs-you", None)[1]["items"] if i["merchant"] == "Gráfica Aliança")
    assert card["question"] == "The Gráfica Aliança invoice does not add up. What should I do?"
    assert [o["label"] for o in card["options"]] == ["Set it aside. I'll get a corrected invoice from Gráfica Aliança."]


def test_a_second_copy_that_disagrees_reopens_the_invoice_and_asks() -> None:
    svc = service(DocumentReader(qr_decoder=None))
    first = only_document(svc, upload(svc, "edp-ft-558120.pdf", "application/pdf"))
    assert svc.repo.items[first.item_id].stage is Stage.CLOSED
    out = _text_upload(svc, "copia.txt", [
        "EDP Comercial - Comercialização de Energia, S.A.", "NIF: 501000100", "Fatura n.º FT EDP2026/558120",
        "Data de emissão: 18/09/2026", "Cliente: Hazel Tree Interiores, Lda.", "NIF: 516123459",
        "Base tributável (23%): 52,11", "IVA 23%: 11,99", "Total: 46,10 €"])
    assert out["documents"][0]["id"] == first.id and first.document.quality is Quality.RED
    assert svc.repo.items[first.item_id].stage is Stage.CONFLICT
    assert out["message"] == "Got it. I need one answer from you: Which is the right total on the EDP invoice?"


# --------------------------------------------------------------------------- photos and scans


def test_photo_read_by_local_ocr_keeps_field_level_provenance() -> None:
    engine = ocr_engine()
    svc = service(DocumentReader(registry=EngineRegistry([engine]), qr_decoder=None))
    out = upload(svc, "central-fs-cc2026-3317.jpg", "image/jpeg")
    record = only_document(svc, out)
    ev = out["evidenceIds"][0]
    assert len(engine.calls) == 1
    assert_expected(record, "central-fs-cc2026-3317-photo")
    assert record.document.supplier_name == "Café Central da Baixa" and record.document.entity_id == "hazel-tree"
    for name in ("gross_amount", "invoice_number", "issue_date", "supplier_tax_id"):
        [obs] = [o for o in record.checks[name].observations if o.method is not ExtractionMethod.ARITHMETIC]
        assert obs.method is ExtractionMethod.OCR and obs.source == f"{ev}@{PP_OCR_V6_MEDIUM}"
        assert isinstance(obs.location, BoundingBox) and obs.location.page == 1
        assert 0 < obs.confidence <= 0.98
    # one reading of pixels is not verification: likely, never promoted (§57)
    assert record.document.quality is Quality.AMBER


def test_photo_qr_code_and_ocr_agree_so_the_receipt_is_verified() -> None:
    decoder = FixedQR(CENTRAL_QR)
    svc = service(DocumentReader(registry=EngineRegistry([ocr_engine()]), qr_decoder=decoder))
    out = upload(svc, "central-fs-cc2026-3317.jpg", "image/jpeg")
    record = only_document(svc, out)
    assert decoder.calls == 1 and record.document.quality is Quality.GREEN
    methods = {o.method for o in record.checks["gross_amount"].observations}
    assert {ExtractionMethod.QR, ExtractionMethod.OCR} <= methods
    steps = {s.step: s for s in svc.repo.reads[out["evidenceIds"][0]].steps}
    assert steps["image_qr"].state is StepState.DONE
    assert "single_source" in steps["ocr_primary"].detail  # the QR alone settled, OCR ran to corroborate it
    assert steps["ocr_commercial"].state is not StepState.DONE


def test_photo_qr_decoded_from_real_pixels_when_zxing_is_installed() -> None:
    pytest.importorskip("zxingcpp")
    svc = service(DocumentReader(registry=EngineRegistry([ocr_engine()])))
    out = upload(svc, "central-fs-cc2026-3317.jpg", "image/jpeg")
    record = only_document(svc, out)
    assert record.document.quality is Quality.GREEN
    assert any(o.method is ExtractionMethod.QR and o.location == "qr:O"
               for o in record.checks["gross_amount"].observations)


def test_scanned_pdf_is_read_by_ocr_and_its_qr_image() -> None:
    pytest.importorskip("pypdfium2")
    decoder = FixedQR(CENTRAL_QR)
    svc = service(DocumentReader(registry=EngineRegistry([ocr_engine()]), qr_decoder=decoder))
    out = upload(svc, "central-fs-cc2026-3317-scan.pdf", "application/pdf")
    record = only_document(svc, out)
    assert_expected(record, "central-fs-cc2026-3317-scan")
    steps = {s.step: s.state for s in svc.repo.reads[out["evidenceIds"][0]].steps}
    assert steps["pdf_text"] is StepState.NOTHING and steps["pdf_qr_image"] is StepState.DONE
    assert record.document.quality is Quality.GREEN


def test_scanned_pdf_without_an_ocr_engine_is_stored_and_says_why() -> None:
    svc = service(DocumentReader(qr_decoder=None))
    out = upload(svc, "central-fs-cc2026-3317-scan.pdf", "application/pdf")
    assert out["storedOnly"] is True and out["documents"] == []
    assert out["message"] == "Got it. I stored it. Reading scanned PDFs is not set up here yet."
    steps = {s.step: s.state for s in svc.repo.reads[out["evidenceIds"][0]].steps}
    assert steps["ocr"] is StepState.NOT_AVAILABLE


def test_no_reader_keeps_the_demo_behaviour() -> None:
    svc = BackOfficeService.demo()  # the browser demo: no reader configured
    for name in ("edp-ft-558120.pdf", "central-fs-cc2026-3317.jpg"):
        out = upload(svc, name)
        assert out["storedOnly"] is True and out["documents"] == [] and out["evidenceIds"]
        assert out["message"] == "Got it. I stored it. Reading photos and PDFs is switched off in this demo."
    assert svc.repo.reads == {}


def test_pdf_reader_not_installed_is_reported_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    from backoffice.extraction import pdf as pdf_module
    from backoffice.extraction._optional import MissingDependencyError

    def missing(module: str, **_: object) -> None:
        raise MissingDependencyError(module, "Reading PDF text")

    monkeypatch.setattr(pdf_module, "import_optional", missing)
    svc = service(DocumentReader(qr_decoder=None))
    out = upload(svc, "edp-ft-558120.pdf", "application/pdf")
    assert out["storedOnly"] is True
    assert out["message"] == "Got it. I stored it. Reading PDFs is not set up here yet."
    [step, *_] = svc.repo.reads[out["evidenceIds"][0]].steps
    assert (step.step, step.state) == ("pdf_text", StepState.NOT_AVAILABLE)


# --------------------------------------------------------------------------- the external fallback


# OCR that missed the tax lines: net and VAT are missing, so the chain would need the fallback.
PARTIAL = "\n".join(line for line in TRANSCRIPTION.splitlines() if "13%" not in line)


def claude_answer() -> dict:
    fields = {
        "invoice_number": {"value": "FS CC2026/3317", "page": 1, "printed": "Fatura simplificada n.º FS CC2026/3317"},
        "supplier_tax_id": {"value": "516722344", "page": 1},
        "gross_amount": {"value": "23.90", "page": 1, "printed": "Total: 23,90 €"},
        "net_amount": {"value": "21.15", "page": 1, "printed": "Base tributável (13%): 21,15"},
        "vat_amount": {"value": "2.75", "page": 1, "printed": "IVA 13%: 2,75"},
        "currency": {"value": "EUR", "page": 1},
        "issue_date": {"value": "2026-09-29", "page": 1},
    }
    return {"id": "msg_1", "type": "message", "role": "assistant", "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "record_document_fields",
                         "input": {"document_type": "simplified_invoice", "supplier_name": "Café Central da Baixa",
                                   "fields": fields}}],
            "usage": {"input_tokens": 1800, "output_tokens": 240}}


def claude(requests: list[httpx.Request]) -> ClaudeVisionProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=claude_answer())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ClaudeVisionProvider(ClaudeVisionConfig(api_key="sk-test"), client=client)


def test_external_ai_off_sends_nothing_anywhere() -> None:
    requests: list[httpx.Request] = []
    registry = EngineRegistry([ocr_engine(PARTIAL)])
    registry.register(claude(requests), name=COMMERCIAL)
    svc = service(DocumentReader(registry=registry, qr_decoder=None, external_ai=False,
                                 budget=InMemoryBudgetLedger(default_ceiling=Decimal("5.00"))))
    out = upload(svc, "central-fs-cc2026-3317.jpg", "image/jpeg")
    assert requests == []  # BACKOFFICE_EXTERNAL_AI=off: no file ever leaves (§53)
    steps = {s.step: s for s in svc.repo.reads[out["evidenceIds"][0]].steps}
    assert steps["external_ai"].state is StepState.OFF
    assert steps["ocr_commercial"].state is StepState.NOT_AVAILABLE
    record = only_document(svc, out)
    assert "net_amount" not in record.observations  # missing stays missing: nothing is invented


def test_external_ai_on_asks_claude_once_redacted_and_within_budget() -> None:
    requests: list[httpx.Request] = []
    registry = EngineRegistry([ocr_engine(PARTIAL)])
    registry.register(claude(requests), name=COMMERCIAL)
    budget = InMemoryBudgetLedger(default_ceiling=Decimal("5.00"))
    svc = service(DocumentReader(registry=registry, qr_decoder=None, external_ai=True, budget=budget))
    out = upload(svc, "central-fs-cc2026-3317.jpg", "image/jpeg")
    ev = out["evidenceIds"][0]
    assert len(requests) == 1
    request = requests[0]
    assert request.url == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "sk-test" and request.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-5-5"
    assert body["tool_choice"] == {"type": "tool", "name": "record_document_fields"}
    [image, prompt] = body["messages"][0]["content"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"
    sent = base64.b64decode(image["source"]["data"])
    original = (FIXTURES / "central-fs-cc2026-3317.jpg").read_bytes()
    assert b"Exif" in original[:2048] and b"Exif" not in sent[:4096]  # camera and GPS never leave
    assert b"Fictional Phone" not in sent
    assert prompt["type"] == "text" and "Rua Augusta" not in json.dumps(body)  # no local text is sent
    assert budget.spent(svc.repo.tenant_id) == Decimal("0.02")
    steps = {s.step: s for s in svc.repo.reads[ev].steps}
    commercial = steps["ocr_commercial"]
    assert commercial.state is StepState.DONE and commercial.engine == "claude-vision"
    assert "redaction:metadata_removed" in commercial.detail and "redaction:masked_lines:2" in commercial.detail
    record = only_document(svc, out)
    vlm = [o for o in record.checks["vat_amount"].observations if o.method is ExtractionMethod.VLM]
    assert len(vlm) == 1 and vlm[0].source == f"{ev}@claude-vision" and vlm[0].value == Decimal("2.75")
    assert vlm[0].location == "vlm:page 1 · printed “IVA 13%: 2,75”"
    # two readings of the same pixels (local OCR + Claude) are not enough for GREEN: likely, not verified
    assert record.document.quality is Quality.AMBER


def test_external_ai_respects_the_budget() -> None:
    requests: list[httpx.Request] = []
    registry = EngineRegistry([ocr_engine(PARTIAL)])
    registry.register(claude(requests), name=COMMERCIAL)
    budget = InMemoryBudgetLedger(default_ceiling=Decimal("0.01"))  # less than one page
    svc = service(DocumentReader(registry=registry, qr_decoder=None, external_ai=True, budget=budget))
    out = upload(svc, "central-fs-cc2026-3317.jpg", "image/jpeg")
    assert requests == []
    commercial = {s.step: s for s in svc.repo.reads[out["evidenceIds"][0]].steps}["ocr_commercial"]
    assert commercial.state is StepState.SKIPPED and "budget_exceeded" in commercial.detail


def test_external_ai_is_not_called_when_local_reading_settles_everything() -> None:
    requests: list[httpx.Request] = []
    registry = EngineRegistry([ocr_engine()])
    registry.register(claude(requests), name=COMMERCIAL)
    svc = service(DocumentReader(registry=registry, qr_decoder=FixedQR(CENTRAL_QR), external_ai=True,
                                 budget=InMemoryBudgetLedger(default_ceiling=Decimal("5.00"))))
    out = upload(svc, "central-fs-cc2026-3317.jpg", "image/jpeg")
    assert requests == [] and only_document(svc, out).document.quality is Quality.GREEN
