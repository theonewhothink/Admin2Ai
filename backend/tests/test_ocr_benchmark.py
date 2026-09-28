"""Golden-dataset harness and release gate (§56-57, §59)."""

import asyncio
import json
import shutil
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from backoffice.domain.models import CriticalField as F
from backoffice.extraction import LabelledFieldExtractor
from backoffice.ocr import (
    PADDLEOCR_VL,
    PP_OCR_V6_MEDIUM,
    BenchmarkReport,
    EngineRegistry,
    FakeOCRProvider,
    FieldOutcome,
    GoldenDataset,
    OCRRouter,
    gate,
    provider_runner,
    router_runner,
    run_benchmark,
)
from backoffice.ocr.benchmark import main

FIXTURES = Path(__file__).parent / "fixtures" / "ocr"
MANIFEST = FIXTURES / "golden.json"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def dataset() -> GoldenDataset:
    return GoldenDataset.load(MANIFEST)


def bench(dataset, provider, label):
    return run(run_benchmark(dataset, provider_runner(provider, LabelledFieldExtractor()), label=label))


@pytest.fixture(scope="module")
def baseline(dataset) -> BenchmarkReport:
    return bench(dataset, FakeOCRProvider("fake", version="fake-1"), "fake-1")


# --------------------------------------------------------------------------- dataset


def test_golden_dataset_loads(dataset):
    assert (dataset.name, dataset.version, len(dataset.documents)) == ("synthetic-invoices", "2026-09-27", 5)
    multi = next(d for d in dataset.documents if d.id == "inv-003")
    pages = dataset.page_images(multi)
    assert [(p.number, p.mime_type) for p in pages] == [(1, "text/plain"), (2, "text/plain")]
    structured = next(d for d in dataset.documents if d.id == "inv-004")
    assert dataset.structured_bytes(structured).lstrip().startswith(b"<?xml")
    assert dataset.fingerprint() == GoldenDataset.load(MANIFEST).fingerprint()
    assert len(dataset.fingerprint()) == 64


def write_manifest(tmp_path, documents, pages=("a.txt",)):
    for name in pages:
        (tmp_path / name).write_text("Total: 1.00 EUR")
    path = tmp_path / "golden.json"
    path.write_text(json.dumps({"name": "t", "version": "1", "documents": documents}))
    return path


@pytest.mark.parametrize(
    ("documents", "error"),
    [
        ([{"id": "a", "pages": ["a.txt"], "expected": {"gross_amount": "1.00"}}] * 2, ValidationError),
        ([{"id": "a", "pages": ["a.txt"], "expected": {"gross_amount": 1.0}}], ValidationError),
        ([{"id": "a", "pages": ["a.txt"], "expected": {"gross_amount": "1.492"}}], ValidationError),
        ([{"id": "a", "pages": ["a.txt"], "expected": {"not_a_field": "1"}}], ValidationError),
        ([{"id": "a", "pages": ["a.txt"], "expected": {}}], ValidationError),
        ([{"id": "a", "expected": {"gross_amount": "1.00"}}], ValidationError),
        ([{"id": "a", "pages": ["missing.txt"], "expected": {"gross_amount": "1.00"}}], FileNotFoundError),
        ([{"id": "a", "pages": ["../outside.txt"], "expected": {"gross_amount": "1.00"}}], ValueError),
        ([{"id": "a", "pages": ["a.docx"], "expected": {"gross_amount": "1.00"}}], ValueError),
    ],
)
def test_invalid_manifests_are_rejected(tmp_path, documents, error):
    (tmp_path / "a.docx").write_bytes(b"PK")
    with pytest.raises(error):
        GoldenDataset.load(write_manifest(tmp_path, documents))


def test_fingerprint_changes_with_fixture_content(tmp_path):
    for item in FIXTURES.iterdir():
        target = tmp_path / item.name
        shutil.copytree(item, target) if item.is_dir() else shutil.copy(item, target)
    before = GoldenDataset.load(tmp_path / "golden.json").fingerprint()
    assert before == GoldenDataset.load(MANIFEST).fingerprint()  # location does not matter
    (tmp_path / "pages" / "inv-001.txt").write_text("changed")
    assert GoldenDataset.load(tmp_path / "golden.json").fingerprint() != before


# --------------------------------------------------------------------------- scoring


def test_a_faithful_engine_scores_perfectly(baseline):
    assert baseline.critical_accuracy == 1.0 and baseline.document_accuracy == 1.0
    assert baseline.silent_errors == 0 and baseline.human_exception_rate == 0.0
    assert baseline.total == 39
    assert baseline.field(F.GROSS_AMOUNT).correct == 5
    assert baseline.field(F.PAYMENT_REFERENCE).total == 1
    assert baseline.total_cost == Decimal("0")


def test_a_noisy_engine_is_scored_honestly(dataset, baseline):
    noisy = bench(dataset, FakeOCRProvider("fake", version="fake-2", substitutions={"3": "8"}), "fake-2")
    assert noisy.critical_accuracy < baseline.critical_accuracy
    assert noisy.silent_errors > 0  # e.g. 393.17 read as 898.17, settled and wrong
    inv1 = next(d for d in noisy.documents if d.id == "inv-001")
    outcomes = {r.field: r.outcome for r in inv1.fields}
    assert outcomes[F.NET_AMOUNT] is FieldOutcome.WRONG
    assert outcomes[F.IBAN] is FieldOutcome.MISSING  # the checksum refuses the misread IBAN
    assert outcomes[F.CURRENCY] is FieldOutcome.CORRECT


def test_gate_blocks_regressions_and_allows_equal_runs(dataset, baseline):
    noisy = bench(dataset, FakeOCRProvider("fake", version="fake-2", substitutions={"3": "8"}), "fake-2")
    blocked = gate(baseline, noisy)
    assert not blocked.allowed
    assert any(r.startswith("critical accuracy fell from 100.00%") for r in blocked.reasons)
    assert any(r.startswith("silent errors rose from 0") for r in blocked.reasons)
    assert any(r.startswith("net_amount accuracy fell") for r in blocked.reasons)
    still_blocked = gate(baseline, noisy, max_drop="1")
    assert not still_blocked.allowed and len(still_blocked.reasons) == 1  # silent errors never tolerated
    rerun = bench(dataset, FakeOCRProvider("fake", version="fake-1b"), "fake-1b")
    assert gate(baseline, rerun).allowed
    assert gate(noisy, baseline).allowed  # improvements pass


def test_gate_tolerance_is_exact(dataset, baseline):
    # "Currency: GPB" disagrees with the "£" sign: flagged for a human, never silently wrong.
    flagged = bench(dataset, FakeOCRProvider("fake", substitutions={"GBP": "GPB"}), "fake-3")
    assert flagged.flagged == 1 and flagged.silent_errors == 0
    assert flagged.field(F.CURRENCY).fraction() == Fraction(4, 5)
    assert not gate(baseline, flagged).allowed
    assert gate(baseline, flagged, max_drop="0.2").allowed  # exactly at the tolerance
    assert not gate(baseline, flagged, max_drop="0.19").allowed
    with pytest.raises(ValueError):
        gate(baseline, flagged, max_drop=-0.1)


def test_gate_refuses_to_compare_different_datasets(baseline):
    other = baseline.model_copy(update={"fingerprint": "0" * 64})
    decision = gate(baseline, other)
    assert not decision.allowed and "different golden datasets" in decision.reasons[0]
    empty = baseline.model_copy(update={"per_field": (), "documents": ()})
    reasons = gate(baseline, empty).reasons
    assert "the current run scored no fields" in reasons
    assert any(r.endswith("is no longer measured") for r in reasons)


def test_router_runner_uses_stage0_and_skips_ocr_for_structured_documents(dataset):
    registry = EngineRegistry([FakeOCRProvider(PP_OCR_V6_MEDIUM), FakeOCRProvider(PADDLEOCR_VL)])
    report = run(
        run_benchmark(dataset, router_runner(OCRRouter(registry, LabelledFieldExtractor())), label="chain")
    )
    assert report.critical_accuracy == 1.0 and report.silent_errors == 0
    engines = {d.id: d.engines for d in report.documents}
    assert engines["inv-004"] == ()  # UBL settled it: no OCR at all
    assert engines["inv-001"] == (PP_OCR_V6_MEDIUM,)
    assert all(d.status == "resolved" for d in report.documents)


def test_a_crashing_runner_scores_missing_not_correct(dataset):
    async def flaky(ds, doc):
        if doc.id == "inv-002":
            raise RuntimeError("engine crashed")
        return await provider_runner(FakeOCRProvider("fake"), LabelledFieldExtractor())(ds, doc)

    report = run(run_benchmark(dataset, flaky, label="flaky"))
    crashed = next(d for d in report.documents if d.id == "inv-002")
    assert crashed.status == "error" and crashed.error == "RuntimeError"
    assert all(r.outcome is FieldOutcome.MISSING for r in crashed.fields)
    assert report.silent_errors == 0 and report.missing == len(crashed.fields)


def test_reports_round_trip_and_cli_gate(tmp_path, dataset, baseline, capsys):
    noisy = bench(dataset, FakeOCRProvider("fake", substitutions={"3": "8"}), "fake-2")
    previous, current = tmp_path / "previous.json", tmp_path / "current.json"
    baseline.save(previous)
    noisy.save(current)
    loaded = BenchmarkReport.load(previous)
    assert loaded == baseline and loaded.critical_accuracy == 1.0
    assert main(["gate", str(previous), str(previous)]) == 0
    assert main(["gate", str(previous), str(current)]) == 1
    output = capsys.readouterr().out
    assert "ALLOWED" in output and "BLOCKED" in output
