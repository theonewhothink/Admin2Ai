"""Photographed receipts and scanned PDFs read by the real PP-OCRv6 model on this machine (§11, §14-19, §56).

No fakes: :class:`~backoffice.ocr.LocalOCRProvider` runs the PP-OCRv6 small ONNX models that ship with
RapidOCR, on the CPU, with no sidecar and no network, behind the normal router, field extraction and
verification. The photos in ``fixtures/photos`` (``make_photos.py``) are rendered with a common font and
photographed: perspective, uneven light, sensor noise, rotation, blur. They are uploaded through the real
``POST /api/evidence`` on the demo tenant, exactly as the server reads them (``reader_from_env``).

The golden rule holds with a real engine too: a reading of pixels is one source. A document becomes GREEN
only when its fiscal QR code (decoded from the same photo) and the reading agree, and it closes only with
the bank payment; a receipt without a QR code stays likely; a photo nobody could read becomes one plain
task to take it again.

These tests skip only when the engine is not installed (``pip install -e ".[ocr-local,qr]"``). CI installs
both and sets ``BACKOFFICE_REQUIRE_LOCAL_OCR=1``, so there a missing engine fails instead of skipping.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import BoundingBox, ExtractionMethod, Quality, TransactionKind
from backoffice.ocr import (
    LOCAL_OCR,
    PP_OCR_V6_MEDIUM,
    GoldenDataset,
    LocalOCRProvider,
    local_ocr_available,
    provider_runner,
    run_benchmark,
)
from backoffice.orchestrator import BankRow
from backoffice.reading import StepState, reader_from_env
from backoffice.service import BackOfficeService

if not local_ocr_available():
    if os.environ.get("BACKOFFICE_REQUIRE_LOCAL_OCR"):
        pytest.fail("the local OCR engine must run here (pip install -e '.[ocr-local,qr]')", pytrace=False)
    pytest.skip("the local OCR engine is not installed (the ocr-local extra)", allow_module_level=True)

FIXTURES = Path(__file__).parent / "fixtures"
PHOTOS = FIXTURES / "photos"
DOCUMENTS = FIXTURES / "documents"
GOLDEN = {d.id: d for d in GoldenDataset.load(PHOTOS / "golden.json").documents}


def service() -> BackOfficeService:
    svc = BackOfficeService.demo()
    svc.repo.reader = reader_from_env({})  # what the server does with no sidecar configured
    assert svc.repo.reader.engines() == (LOCAL_OCR,)
    return svc


def upload(svc: BackOfficeService, path: Path) -> dict:
    body = {"filename": path.name, "contentType": None, "dataBase64": base64.b64encode(path.read_bytes()).decode()}
    status, out = svc.dispatch("POST", "/api/evidence", json.dumps(body))
    assert status == 200, out
    return out


def only_document(svc: BackOfficeService, out: dict):  # type: ignore[no-untyped-def]
    assert len(out["documents"]) == 1, out
    return svc.repo.documents[out["documents"][0]["id"]]


def bank(svc: BackOfficeService, golden_id: str, kind: TransactionKind, card: str | None = None) -> None:
    """The payment named in the golden document's ``bank:`` tag."""
    [tag] = [t for t in GOLDEN[golden_id].tags if t.startswith("bank:")]
    _, account, day, amount, counterparty = tag.split(":", 4)
    svc.orchestrator.ingest_bank([BankRow(
        bank_id=f"{account}-{golden_id}", account_id=account, booked_on=date.fromisoformat(day),
        amount=Decimal(amount), counterparty=counterparty, description=counterparty, kind=kind, card_last4=card)])


def assert_read(record, golden_id: str, *, skip: tuple[str, ...] = ()) -> None:  # type: ignore[no-untyped-def]
    """Every expected value is what the checks hold: read correctly, never guessed."""
    for name, expected in GOLDEN[golden_id].expected.items():
        if name.value in skip:
            continue
        check = record.checks[name.value]
        assert check.value is not None, (name, check.reasons)
        assert str(check.value).replace(" ", "") == expected.replace(" ", ""), (name, check.value, expected)


def local_readings(record, field: str, evidence_id: str) -> list:  # type: ignore[no-untyped-def]
    return [o for o in record.checks[field].observations
            if o.method is ExtractionMethod.OCR and o.source == f"{evidence_id}@{LOCAL_OCR}"]


# --------------------------------------------------------------------------- photos


def test_photographed_invoice_with_fiscal_qr_is_verified_by_the_real_engine_and_closes_with_the_payment() -> None:
    pytest.importorskip("zxingcpp")
    svc = service()
    bank(svc, "ft-fba2026-1207-photo", TransactionKind.TRANSFER_OUT)
    out = upload(svc, PHOTOS / "ft-fba2026-1207.jpg")
    record = only_document(svc, out)
    ev = out["evidenceIds"][0]
    assert_read(record, "ft-fba2026-1207-photo")
    assert record.document.quality is Quality.GREEN  # the photo's QR code and the reading agree on every field
    assert record.matched_tx_ids and svc.repo.items[record.item_id].stage is Stage.CLOSED
    assert out["message"].startswith("Got it. It matches the €118.70 payment to")
    assert record.document.supplier_name == "Ferragens Boavista"  # its legal suffix dropped, as everywhere
    for field in ("invoice_number", "supplier_tax_id", "issue_date", "gross_amount", "net_amount", "vat_amount"):
        methods = {o.method for o in record.checks[field].observations}
        assert {ExtractionMethod.QR, ExtractionMethod.OCR} <= methods, field
        [ocr] = local_readings(record, field, ev)  # §18: value, source, method, confidence and location
        assert isinstance(ocr.location, BoundingBox) and ocr.location.page == 1
        assert 0 < ocr.confidence <= 1
    gross = local_readings(record, "gross_amount", ev)[0]
    assert gross.value == Decimal("118.70") and 900 < gross.location.y0 < 1300  # the "Total" line, mid-page
    steps = {s.step: s for s in svc.repo.reads[ev].steps}
    assert steps["image_qr"].state is StepState.DONE
    assert steps["ocr_primary"].state is StepState.DONE and steps["ocr_primary"].engine == LOCAL_OCR
    assert steps["ocr_commercial"].state is not StepState.DONE  # nothing paid, nothing sent anywhere
    assert svc.repo.audit.verify(svc.repo.tenant_id).ok


def test_cafe_till_receipt_is_read_verified_and_matched_to_the_card_payment() -> None:
    pytest.importorskip("zxingcpp")
    svc = service()
    bank(svc, "fs-pb2026-0441-photo", TransactionKind.CARD, card="5530")
    out = upload(svc, PHOTOS / "fs-pb2026-0441.jpg")
    record = only_document(svc, out)
    assert_read(record, "fs-pb2026-0441-photo")
    assert record.document.quality is Quality.GREEN
    assert record.matched_tx_ids and svc.repo.items[record.item_id].stage is Stage.CLOSED
    ev = out["evidenceIds"][0]
    for field in ("invoice_number", "supplier_tax_id", "issue_date", "gross_amount"):
        assert local_readings(record, field, ev), field  # read off the thermal print, not only the QR code
    assert local_readings(record, "gross_amount", ev)[0].value == Decimal("8.40")


def test_tilted_slightly_blurred_receipt_without_qr_is_read_correctly_but_stays_likely() -> None:
    svc = service()
    out = upload(svc, PHOTOS / "fs-mr2026-0088.jpg")
    record = only_document(svc, out)
    ev = out["evidenceIds"][0]
    assert_read(record, "fs-mr2026-0088-photo", skip=("vat_amount",))  # "IVA incluído" is not a VAT label (yet)
    for field in ("invoice_number", "supplier_tax_id", "issue_date", "gross_amount"):
        [ocr] = local_readings(record, field, ev)
        assert record.checks[field].quality is Quality.AMBER  # one reading of pixels: likely, never verified
    assert record.document.quality is Quality.AMBER
    read = svc.repo.reads[ev]
    assert read.image_quality == ()  # slightly soft is not "blurry": nobody is asked to retake it
    # The card payment of the same amount that day is only a likely match: nothing closes on one reading.
    report = svc.orchestrator.ingest_bank([BankRow(
        bank_id="card-0924-marinheiro", account_id="card-5530", booked_on=date(2026, 9, 24),
        amount=Decimal("-45.60"), counterparty="RESTAURANTE O MARINHEIRO", kind=TransactionKind.CARD,
        card_last4="5530")])
    tx = svc.repo.transactions[report.transaction_ids[0]]
    assert tx.likely_document_ids == [record.id] and not record.matched_tx_ids
    assert record.document.quality is Quality.AMBER and not svc.repo.items[record.item_id].is_done  # §57


def test_badly_blurred_photo_becomes_the_plain_retake_task() -> None:
    svc = service()
    out = upload(svc, PHOTOS / "fs-mr2026-0088-blurred.jpg")
    assert out["documents"] == [] and out["storedOnly"] is True
    assert out["message"] == ("Got it, but I can't read it. This photo of a receipt is too blurred to read. "
                              "Take it again?")
    read = svc.repo.reads[out["evidenceIds"][0]]
    assert read.image_quality == ("blurry",)  # measured on the pixels: nobody had to say it was blurred
    assert read.needs_person and not read.readings
    steps = {s.step: s for s in read.steps}
    assert steps["ocr_primary"].state is StepState.DONE and "poor_image:blurry" in steps["ocr_complex_layout"].detail
    [task] = [n for n in svc.dispatch("GET", "/api/needs-you", None)[1]["items"] if n["id"].startswith("nd_retake")]
    assert task["question"] == "This photo of a receipt is too blurred to read. Take it again?"
    assert [o["label"] for o in task["options"]] == ["I'll take it again", "It isn't a receipt. Leave it."]


def test_scanned_pdf_is_rendered_and_read_by_the_real_engine() -> None:
    pytest.importorskip("pypdfium2")
    pytest.importorskip("zxingcpp")
    svc = service()
    out = upload(svc, DOCUMENTS / "central-fs-cc2026-3317-scan.pdf")
    record = only_document(svc, out)
    ev = out["evidenceIds"][0]
    expected = {"invoice_number": "FS CC2026/3317", "supplier_tax_id": "516722344", "gross_amount": "23.90",
                "issue_date": "2026-09-29", "net_amount": "21.15", "vat_amount": "2.75"}
    for field, value in expected.items():
        [ocr] = local_readings(record, field, ev)
        assert str(ocr.value) == value and isinstance(ocr.location, BoundingBox), field
    assert record.document.quality is Quality.GREEN  # the QR code on the scanned page agrees
    steps = {s.step: s.state for s in svc.repo.reads[ev].steps}
    assert steps["pdf_text"] is StepState.NOTHING and steps["ocr_primary"] is StepState.DONE


def test_born_digital_pdf_still_needs_no_ocr() -> None:
    svc = service()
    out = upload(svc, DOCUMENTS / "edp-ft-558120.pdf")
    record = only_document(svc, out)
    assert record.document.quality is Quality.GREEN and record.matched_tx_ids
    steps = [s.step for s in svc.repo.reads[out["evidenceIds"][0]].steps]
    assert "ocr_primary" not in steps  # its own text and QR code settle every field (§13 Stage 0)


# --------------------------------------------------------------------------- the engine itself


def test_local_engine_joins_boxes_into_printed_lines_with_boxes_and_scores() -> None:
    provider = LocalOCRProvider()
    assert provider.name == LOCAL_OCR and provider.cost_per_page == 0 and not provider.capabilities.external
    assert "PP-OCRv6" in provider.version
    receipt = asyncio.run(provider.recognize([(PHOTOS / "fs-mr2026-0088.jpg").read_bytes()]))
    lines = {line.text: line for line in receipt.lines}
    total = next(line for text, line in lines.items() if text.startswith("Total:"))
    assert total.text.endswith("45,60 €")  # "Total:" and "45,60 €" are two boxes on a 4-degree tilted line
    assert total.bbox is not None and total.bbox.page == 1 and 0.8 < (total.confidence or 0) <= 1
    assert 2 < abs(receipt.layout.skew_degrees) < 6
    cafe = asyncio.run(provider.recognize([(PHOTOS / "fs-pb2026-0441.jpg").read_bytes()]))
    assert any(line.text.startswith("TOTAL") and line.text.endswith("8,40 EUR") for line in cafe.lines)
    assert any(line.text.replace(" ", "") == "ATCUD:KPB4M8XT-441" for line in cafe.lines)
    assert not any("\uff1a" in line.text for line in cafe.lines)  # a full-width colon is folded back to ":"


def test_reader_from_env_runs_the_local_engine_only_without_a_sidecar() -> None:
    assert reader_from_env({}).engines() == (LOCAL_OCR,)
    assert reader_from_env({"BACKOFFICE_OCR_LOCAL": "off"}).engines() == ()
    assert reader_from_env({"BACKOFFICE_OCR_URL": "http://ppocr:8080"}).engines() == (PP_OCR_V6_MEDIUM,)


def test_local_engine_accuracy_and_speed_on_the_golden_sets() -> None:
    """§56: the real engine on both golden sets (documents and photos): every expected field it reads must be
    right (zero silent errors) and nearly all of them read. Prints the numbers for the record."""
    pytest.importorskip("pypdfium2")
    extractor = BackOfficeService.demo().orchestrator.documents.text_extractor()
    provider = LocalOCRProvider()
    asyncio.run(provider.recognize([(PHOTOS / "fs-pb2026-0441.jpg").read_bytes()]))  # load the model first
    correct = total = silent = pages = 0
    started = time.perf_counter()
    for manifest in (DOCUMENTS / "golden.json", PHOTOS / "golden.json"):
        dataset = GoldenDataset.load(manifest)
        report = asyncio.run(run_benchmark(dataset, provider_runner(provider, extractor), label="local"))
        correct, total, silent = correct + report.correct, total + report.total, silent + report.silent_errors
        pages += sum(len(d.pages) for d in dataset.documents)
        print(f"{dataset.name}: {report.correct}/{report.total} critical fields "
              f"({report.critical_accuracy:.1%}), silent errors {report.silent_errors}")
    per_page = (time.perf_counter() - started) / pages
    print(f"all: {correct}/{total} ({correct / total:.1%}), {per_page:.2f} s per page")
    assert silent == 0
    assert correct / total >= 0.9
    assert per_page < 30  # CPU only; about 2 s on two cores
