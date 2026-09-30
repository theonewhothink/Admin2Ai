"""Golden-dataset harness and release gate (§56-57, §59).

Every model or engine update reruns the permanent golden dataset, and
rollout is blocked if critical accuracy falls.

Fixture format (one JSON manifest; paths are relative to it)::

    {
      "name": "synthetic-invoices",
      "version": "2026-09-27",
      "documents": [
        {
          "id": "inv-001",
          "pages": ["pages/inv-001.txt"],
          "structured": "einvoice/inv-004.xml",
          "expected": {"invoice_number": "FT 2026/183", "gross_amount": "483.60",
                       "currency": "EUR", "issue_date": "2026-09-18"},
          "tags": ["simple"],
          "description": "..."
        }
      ]
    }

Page files are images or PDFs (.png .jpg .jpeg .tif .tiff .pdf) or ``.txt``
synthetic pages, which :class:`~backoffice.ocr.providers.FakeOCRProvider`
reads as text. ``structured`` (optional) is Stage 0 input (UBL/CII XML,
HTML, PDF). Expected values are strings, so amounts never pass through float.

Scoring per expected field: CORRECT (settled and equal), WRONG (settled but
different: a *silent error*, the one §59 wants at zero), FLAGGED (conflict,
sent to a human), MISSING. Only CORRECT counts as accurate; critical
accuracy is the micro-average over all expected critical fields.

The gate compares two reports of the same dataset:

    python -m backoffice.ocr gate previous.json current.json --max-drop 0
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backoffice.domain.models import CriticalField
from backoffice.extraction.fields import FieldExtractor, FieldMap, combine
from backoffice.extraction.media import MIME_JPEG, MIME_PDF, MIME_PNG, MIME_TEXT, MIME_TIFF
from backoffice.extraction.structured import extract_structured
from backoffice.extraction.values import comparison_key, is_usable

from .base import OCRHints, OCRProviderInterface, PageImage
from .consensus import DEFAULT_POLICY, ConsensusPolicy, FieldConsensus, FieldState, build_consensus
from .router import OCRRouter, RoutingRequest

__all__ = [
    "BenchmarkReport",
    "DocumentResult",
    "DocumentRunner",
    "FieldAccuracy",
    "FieldOutcome",
    "FieldResult",
    "GateDecision",
    "GoldenDataset",
    "GoldenDocument",
    "RunOutput",
    "gate",
    "main",
    "provider_runner",
    "router_runner",
    "run_benchmark",
]

_MIME_BY_SUFFIX: Mapping[str, str] = {
    ".png": MIME_PNG, ".jpg": MIME_JPEG, ".jpeg": MIME_JPEG, ".tif": MIME_TIFF,
    ".tiff": MIME_TIFF, ".pdf": MIME_PDF, ".txt": MIME_TEXT,
}  # fmt: skip

_FROZEN = ConfigDict(frozen=True)


# --------------------------------------------------------------------------- dataset


class GoldenDocument(BaseModel):
    model_config = _FROZEN

    id: str = Field(min_length=1)
    pages: tuple[str, ...] = ()
    structured: str | None = None
    expected: dict[CriticalField, str]
    tags: tuple[str, ...] = ()
    description: str = ""

    @field_validator("expected")
    @classmethod
    def _parseable(cls, expected: dict[CriticalField, str]) -> dict[CriticalField, str]:
        if not expected:
            raise ValueError("a golden document needs at least one expected field")
        for name, value in expected.items():
            if not is_usable(name, value):
                raise ValueError(f"expected {name.value} is not a valid value: {value!r}")
        return expected

    @model_validator(mode="after")
    def _has_input(self) -> GoldenDocument:
        if not self.pages and not self.structured:
            raise ValueError(f"document {self.id!r} has neither pages nor structured input")
        return self


class GoldenDataset(BaseModel):
    model_config = _FROZEN

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    documents: tuple[GoldenDocument, ...]
    root: Path = Field(default=Path("."), exclude=True)

    @model_validator(mode="after")
    def _unique_ids(self) -> GoldenDataset:
        ids = [d.id for d in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("document ids must be unique")
        return self

    @classmethod
    def load(cls, path: str | Path) -> GoldenDataset:
        """Load a manifest and check that every referenced file exists."""
        manifest = Path(path)
        dataset = cls.model_validate({**json.loads(manifest.read_text("utf-8")), "root": manifest.parent})
        for doc in dataset.documents:
            for relative in doc.pages:
                dataset._page_file(relative)
            if doc.structured:
                dataset._file(doc.structured)
        return dataset

    def _file(self, relative: str) -> Path:
        root = self.root.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"fixture path escapes the dataset: {relative!r}")
        if not path.is_file():
            raise FileNotFoundError(relative)
        return path

    def _page_file(self, relative: str) -> tuple[Path, str]:
        path = self._file(relative)
        mime = _MIME_BY_SUFFIX.get(path.suffix.lower())
        if mime is None:
            raise ValueError(f"unsupported page file type: {relative!r}")
        return path, mime

    def page_images(self, doc: GoldenDocument) -> tuple[PageImage, ...]:
        pages = []
        for number, relative in enumerate(doc.pages, start=1):
            path, mime = self._page_file(relative)
            pages.append(PageImage(data=path.read_bytes(), mime_type=mime, number=number))
        return tuple(pages)

    def structured_bytes(self, doc: GoldenDocument) -> bytes | None:
        return self._file(doc.structured).read_bytes() if doc.structured else None

    def fingerprint(self) -> str:
        """Hash of the expectations and of every fixture file's content."""
        digest = hashlib.sha256()
        digest.update(json.dumps([self.name, self.version], sort_keys=True).encode())
        for doc in self.documents:
            expected = {f.value: v for f, v in sorted(doc.expected.items(), key=lambda kv: kv[0].value)}
            digest.update(json.dumps([doc.id, expected], sort_keys=True).encode())
            for relative in (*doc.pages, *([doc.structured] if doc.structured else [])):
                digest.update(hashlib.sha256(self._file(relative).read_bytes()).digest())
        return digest.hexdigest()


# --------------------------------------------------------------------------- scoring


class FieldOutcome(str, Enum):
    CORRECT = "correct"
    WRONG = "wrong"  # settled but wrong: a silent error (§59)
    FLAGGED = "flagged"  # conflict, sent to a human
    MISSING = "missing"


class FieldResult(BaseModel):
    model_config = _FROZEN

    field: CriticalField
    expected: str
    observed: str | None
    state: FieldState
    outcome: FieldOutcome


class DocumentResult(BaseModel):
    model_config = _FROZEN

    id: str
    status: str
    fields: tuple[FieldResult, ...]
    cost: Decimal = Decimal("0")
    engines: tuple[str, ...] = ()
    error: str | None = None

    @property
    def all_correct(self) -> bool:
        return all(f.outcome is FieldOutcome.CORRECT for f in self.fields)


class FieldAccuracy(BaseModel):
    model_config = _FROZEN

    field: CriticalField
    correct: int = 0
    wrong: int = 0
    flagged: int = 0
    missing: int = 0

    @property
    def total(self) -> int:
        return self.correct + self.wrong + self.flagged + self.missing

    def fraction(self) -> Fraction:
        return Fraction(self.correct, self.total) if self.total else Fraction(0)

    @property
    def accuracy(self) -> float:
        return float(self.fraction())


class BenchmarkReport(BaseModel):
    model_config = _FROZEN

    label: str
    dataset: str
    dataset_version: str
    fingerprint: str
    documents: tuple[DocumentResult, ...]
    per_field: tuple[FieldAccuracy, ...]

    @property
    def correct(self) -> int:
        return sum(f.correct for f in self.per_field)

    @property
    def total(self) -> int:
        return sum(f.total for f in self.per_field)

    @property
    def silent_errors(self) -> int:
        return sum(f.wrong for f in self.per_field)

    @property
    def flagged(self) -> int:
        return sum(f.flagged for f in self.per_field)

    @property
    def missing(self) -> int:
        return sum(f.missing for f in self.per_field)

    def critical_fraction(self) -> Fraction:
        return Fraction(self.correct, self.total) if self.total else Fraction(0)

    @property
    def critical_accuracy(self) -> float:
        return float(self.critical_fraction())

    @property
    def document_accuracy(self) -> float:
        return sum(d.all_correct for d in self.documents) / len(self.documents) if self.documents else 0.0

    @property
    def human_exception_rate(self) -> float:
        if not self.documents:
            return 0.0
        return sum(d.status != "resolved" for d in self.documents) / len(self.documents)

    @property
    def total_cost(self) -> Decimal:
        return sum((d.cost for d in self.documents), Decimal("0"))

    def field(self, name: CriticalField) -> FieldAccuracy | None:
        return next((f for f in self.per_field if f.field is name), None)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> BenchmarkReport:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class RunOutput:
    """What a runner produced for one document."""

    fields: Mapping[CriticalField, FieldConsensus]
    status: str
    cost: Decimal = Decimal("0")
    engines: tuple[str, ...] = ()


DocumentRunner = Callable[[GoldenDataset, GoldenDocument], Awaitable[RunOutput]]


def _score_field(name: CriticalField, expected: str, consensus: FieldConsensus | None) -> FieldResult:
    if consensus is None or consensus.state is FieldState.MISSING:
        return FieldResult(
            field=name, expected=expected, observed=None, state=FieldState.MISSING, outcome=FieldOutcome.MISSING
        )
    if consensus.state is FieldState.CONFLICT:
        observed = " / ".join(str(c.value) for c in consensus.candidates)
        return FieldResult(
            field=name, expected=expected, observed=observed, state=consensus.state, outcome=FieldOutcome.FLAGGED
        )
    same = comparison_key(name, consensus.value) == comparison_key(name, expected)
    return FieldResult(
        field=name,
        expected=expected,
        observed=str(consensus.value),
        state=consensus.state,
        outcome=FieldOutcome.CORRECT if same else FieldOutcome.WRONG,
    )


def _expected_items(doc: GoldenDocument) -> list[tuple[CriticalField, str]]:
    return [(f, doc.expected[f]) for f in CriticalField if f in doc.expected]


def _score(doc: GoldenDocument, output: RunOutput) -> DocumentResult:
    return DocumentResult(
        id=doc.id,
        status=output.status,
        fields=tuple(_score_field(f, v, output.fields.get(f)) for f, v in _expected_items(doc)),
        cost=output.cost,
        engines=output.engines,
    )


def _errored(doc: GoldenDocument, exc: Exception) -> DocumentResult:
    return DocumentResult(
        id=doc.id,
        status="error",
        fields=tuple(_score_field(f, v, None) for f, v in _expected_items(doc)),
        error=type(exc).__name__,
    )


def _aggregate(documents: Sequence[DocumentResult]) -> tuple[FieldAccuracy, ...]:
    counts: dict[CriticalField, dict[FieldOutcome, int]] = {}
    for doc in documents:
        for result in doc.fields:
            per = counts.setdefault(result.field, dict.fromkeys(FieldOutcome, 0))
            per[result.outcome] += 1
    return tuple(
        FieldAccuracy(
            field=f,
            correct=counts[f][FieldOutcome.CORRECT],
            wrong=counts[f][FieldOutcome.WRONG],
            flagged=counts[f][FieldOutcome.FLAGGED],
            missing=counts[f][FieldOutcome.MISSING],
        )
        for f in CriticalField
        if f in counts
    )


async def run_benchmark(dataset: GoldenDataset, runner: DocumentRunner, *, label: str) -> BenchmarkReport:
    """Run every golden document through ``runner`` in order and score it.

    A runner that raises on one document scores that document as missing
    everywhere (never as correct) and the run continues.
    """
    results = []
    for doc in dataset.documents:
        try:
            output = await runner(dataset, doc)
        except Exception as exc:  # one broken document must not hide the others
            results.append(_errored(doc, exc))
            continue
        results.append(_score(doc, output))
    return BenchmarkReport(
        label=label,
        dataset=dataset.name,
        dataset_version=dataset.version,
        fingerprint=dataset.fingerprint(),
        documents=tuple(results),
        per_field=_aggregate(results),
    )


def provider_runner(
    provider: OCRProviderInterface,
    extractor: FieldExtractor,
    *,
    policy: ConsensusPolicy = DEFAULT_POLICY,
) -> DocumentRunner:
    """Benchmark one engine on its own: its reading, nothing else."""

    async def run(dataset: GoldenDataset, doc: GoldenDocument) -> RunOutput:
        pages = dataset.page_images(doc)
        result = await provider.recognize(pages, OCRHints(page_count=len(pages)))
        found = extractor(result.full_text, f"{doc.id}@{provider.name}", provider.method)
        consensus = build_consensus(found, required=doc.expected.keys(), policy=policy)
        status = "resolved" if consensus.settled else "human_exception"
        return RunOutput(fields=consensus.fields, status=status, cost=result.cost, engines=(provider.name,))

    return run


def _default_stage0(dataset: GoldenDataset, doc: GoldenDocument) -> FieldMap:
    data = dataset.structured_bytes(doc)
    return combine(extract_structured(data, source=doc.id)) if data else {}


def router_runner(
    router: OCRRouter,
    *,
    tenant_id: str = "golden-dataset",
    stage0: Callable[[GoldenDataset, GoldenDocument], FieldMap] = _default_stage0,
) -> DocumentRunner:
    """Benchmark the whole chain, Stage 0 included."""

    async def run(dataset: GoldenDataset, doc: GoldenDocument) -> RunOutput:
        outcome = await router.route(
            RoutingRequest(
                tenant_id=tenant_id,
                evidence_id=doc.id,
                pages=dataset.page_images(doc),
                structured=stage0(dataset, doc),
                required_fields=frozenset(doc.expected),
            )
        )
        return RunOutput(
            fields=outcome.fields,
            status=outcome.status.value,
            cost=outcome.total_cost,
            engines=outcome.engines_used,
        )

    return run


# --------------------------------------------------------------------------- gate


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reasons: tuple[str, ...]
    previous_accuracy: float
    current_accuracy: float


def gate(
    previous: BenchmarkReport, current: BenchmarkReport, max_drop: float | str | Decimal = 0
) -> GateDecision:
    """Allow rollout only if nothing critical got worse (§56).

    Blocks when the reports cover different datasets, when critical accuracy
    or any field's accuracy falls by more than ``max_drop`` (0-1, exact
    arithmetic), or when silent errors increase.
    """
    drop = Fraction(str(max_drop))
    if not 0 <= drop <= 1:
        raise ValueError("max_drop must be between 0 and 1")
    reasons: list[str] = []
    if previous.fingerprint != current.fingerprint:
        reasons.append("the reports were produced on different golden datasets")
    if current.total == 0:
        reasons.append("the current run scored no fields")
    prev_acc, cur_acc = previous.critical_fraction(), current.critical_fraction()
    if cur_acc < prev_acc - drop:
        reasons.append(f"critical accuracy fell from {float(prev_acc):.2%} to {float(cur_acc):.2%}")
    for before in previous.per_field:
        if before.total == 0:
            continue
        after = current.field(before.field)
        if after is None or after.total == 0:
            reasons.append(f"{before.field.value} is no longer measured")
        elif after.fraction() < before.fraction() - drop:
            reasons.append(
                f"{before.field.value} accuracy fell from {before.accuracy:.2%} to {after.accuracy:.2%}"
            )
    if current.silent_errors > previous.silent_errors:
        reasons.append(f"silent errors rose from {previous.silent_errors} to {current.silent_errors}")
    return GateDecision(
        allowed=not reasons,
        reasons=tuple(reasons),
        previous_accuracy=float(prev_acc),
        current_accuracy=float(cur_acc),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: ``gate PREVIOUS CURRENT [--max-drop X]``; exit 1 blocks the rollout."""
    parser = argparse.ArgumentParser(prog="python -m backoffice.ocr")
    commands = parser.add_subparsers(dest="command", required=True)
    gate_cmd = commands.add_parser("gate", help="compare two benchmark reports")
    gate_cmd.add_argument("previous")
    gate_cmd.add_argument("current")
    gate_cmd.add_argument("--max-drop", default="0")
    args = parser.parse_args(argv)
    decision = gate(BenchmarkReport.load(args.previous), BenchmarkReport.load(args.current), args.max_drop)
    verdict = "ALLOWED" if decision.allowed else "BLOCKED"
    print(f"{verdict}: critical accuracy {decision.previous_accuracy:.2%} -> {decision.current_accuracy:.2%}")
    for reason in decision.reasons:
        print(f"  - {reason}")
    return 0 if decision.allowed else 1

