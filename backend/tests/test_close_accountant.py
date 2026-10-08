"""Accountant workspace (§28): home table and client drill-down."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from backoffice.closure import (
    AccountantQuestion,
    Anomaly,
    ClientMonth,
    DeliveryChannel,
    ExportState,
    ExportStatus,
    Month,
    PackageDelivery,
    PackageEntry,
    QuestionDirection,
    QuestionStatus,
    TaxFlag,
    build_client_view,
    build_home_table,
    compute_month_status,
    needs_accountant,
)
from backoffice.domain.lifecycle import ORDER, Stage, TrackedItem
from backoffice.domain.models import (
    Document,
    Evidence,
    EvidenceFormat,
    LegalEntity,
    Quality,
    SourceKind,
    Transaction,
)

SEPT = Month(2026, 9)
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
HAZEL = LegalEntity(id="ent_hazel", tenant_id="t1", name="Hazel Tree", country="PT", tax_id="PT509123456")
OAK = LegalEntity(id="ent_oak", tenant_id="t1", name="oak studio", country="PT", tax_id="PT516000111")
BIRCH = LegalEntity(id="ent_birch", tenant_id="t1", name="Birch Café", country="PT", tax_id="PT500000000")


class Covered:
    name = "Gmail"
    healthy = True
    covered_from = datetime(2026, 1, 1, tzinfo=timezone.utc)
    covered_until = NOW


@dataclass(frozen=True)
class Decision:
    transaction_id: str
    requires_document: bool = True
    quality: Quality = Quality.GREEN


def item(subject_id: str, to: Stage = Stage.CLOSED) -> TrackedItem:
    it = TrackedItem(id=f"item_{subject_id}", tenant_id="t1", subject_type="transaction", subject_id=subject_id)
    for stage in ORDER[1 : ORDER.index(to) + 1]:
        it.advance(stage, actor="system", evidence_ids=["ev"], quality=Quality.GREEN)
    return it


def status_for(entity: LegalEntity, items, decisions=()):
    return compute_month_status(entity.id, SEPT, items, now=NOW, connectors=[Covered()], decisions=decisions)


def question(qid: str, direction=QuestionDirection.TO_ACCOUNTANT, status=QuestionStatus.OPEN, asked=None):
    return AccountantQuestion(qid, f"Question {qid}", direction, status, asked_at=asked)


def test_needs_accountant_counts_what_only_the_accountant_can_settle():
    questions = [
        question("q1"),
        question("q2", status=QuestionStatus.ANSWERED),
        question("q3", QuestionDirection.FROM_ACCOUNTANT),  # the accountant asked; we answer
    ]
    flags = [TaxFlag("f1", "item_a", "VAT on the restaurant bill"), TaxFlag("f2", "item_b", "x", resolved=True)]
    assert needs_accountant(questions, flags) == 2


def test_home_table_rows_and_order():
    hazel = ClientMonth(HAZEL, status_for(HAZEL, [item("tx_1"), item("tx_2", Stage.VERIFIED)],
                                         [Decision("tx_2")]))  # fmt: skip
    oak = ClientMonth(OAK, status_for(OAK, [item("tx_3")]), questions=[question("q1")])
    birch = ClientMonth(BIRCH, status_for(BIRCH, [item("tx_4"), item("tx_5", Stage.VERIFIED)]))
    rows = build_home_table([hazel, oak, birch])
    assert [r.client for r in rows] == ["oak studio", "Birch Café", "Hazel Tree"]
    oak_row, birch_row, hazel_row = rows
    assert (oak_row.complete_percent, oak_row.missing, oak_row.needs_accountant, oak_row.closed) == (100, 0, 1, True)
    assert (hazel_row.complete_percent, hazel_row.missing, hazel_row.needs_accountant) == (50, 1, 0)
    assert birch_row.missing == 0  # no decision says a document is expected
    assert hazel_row.month == SEPT


def test_client_month_must_match_the_status():
    with pytest.raises(ValueError):
        ClientMonth(HAZEL, status_for(OAK, []))


def ev(eid: str) -> Evidence:
    return Evidence(id=eid, tenant_id="t1", source_kind=SourceKind.EMAIL, format=EvidenceFormat.PDF,
                    sha256=Evidence.hash_bytes(eid.encode()), filename=f"{eid}.pdf",
                    retrieved_at=datetime(2026, 9, 20, tzinfo=timezone.utc))  # fmt: skip


def tx(tid: str, day: int, amount: str) -> Transaction:
    return Transaction(id=tid, tenant_id="t1", account_id="a", booked_on=date(2026, 9, day),
                       amount=Decimal(amount), counterparty=tid.upper())  # fmt: skip


def test_client_view_gathers_everything_open_first():
    matched = PackageEntry.from_domain(item("tx_1"), transaction=tx("tx_1", 5, "-20.00"),
                                       documents=[Document(id="doc_1", tenant_id="t1", evidence_ids=["ev_a"])],
                                       evidence=[ev("ev_a"), ev("ev_shared")], why=["Invoice total €20.00"])  # fmt: skip
    missing = PackageEntry.from_domain(item("tx_2", Stage.VERIFIED), transaction=tx("tx_2", 2, "-117.20"),
                                       evidence=[ev("ev_shared")])  # fmt: skip
    internal = PackageEntry.from_domain(item("tx_3", Stage.VERIFIED), transaction=tx("tx_3", 3, "-500.00"))
    document_only = PackageEntry.from_domain(
        TrackedItem(id="item_doc_9", tenant_id="t1", subject_type="document", subject_id="doc_9"),
        documents=[Document(id="doc_9", tenant_id="t1", evidence_ids=[], issue_date=date(2026, 9, 1))],
    )
    decisions = [Decision("tx_1"), Decision("tx_2"), Decision("tx_3", requires_document=False)]
    status = status_for(HAZEL, [item("tx_1"), item("tx_2", Stage.VERIFIED), item("tx_3", Stage.VERIFIED)], decisions)
    t = lambda h: datetime(2026, 10, 1, h, tzinfo=timezone.utc)  # noqa: E731
    delivery = PackageDelivery(entity_id=HAZEL.id, month=SEPT, package_sha256="b" * 64, prepared_at=t(8)).deliver(
        at=t(9), channel=DeliveryChannel.ACCOUNTING_SOFTWARE, recipient="TOConline", evidence_id="ev_upload"
    )
    view = build_client_view(
        HAZEL,
        status,
        entries=[matched, missing, internal, document_only],
        decisions=decisions,
        anomalies=[
            Anomaly("a1", "item_tx_1", "Amount higher than usual"),
            Anomaly("a2", "item_tx_2", "New bank details", Quality.RED),
            Anomaly("a0", "item_tx_3", "Old", Quality.RED, resolved=True),
        ],
        tax_flags=[TaxFlag("f2", "item_tx_1", "done", resolved=True), TaxFlag("f1", "item_tx_2", "VAT rate?")],
        questions=[
            question("q_answered", status=QuestionStatus.ANSWERED, asked=t(1)),
            question("q_late", asked=t(5)),
            question("q_early", QuestionDirection.FROM_ACCOUNTANT, asked=t(2)),
            question("q_undated"),
        ],
        delivery=delivery,
    )
    assert view.client == "Hazel Tree" and view.month == SEPT
    assert [e.subject_id for e in view.reconciliations] == ["tx_2", "tx_3", "tx_1"]
    assert [m.transaction_id for m in view.missing] == ["tx_2"]
    assert view.missing[0].amount == Decimal("-117.20")
    assert [(r.evidence_id, r.item_ids) for r in view.evidence] == [
        ("ev_a", ("item_tx_1",)),
        ("ev_shared", ("item_tx_1", "item_tx_2")),
    ]
    assert [a.anomaly_id for a in view.anomalies] == ["a2", "a1", "a0"]
    assert [f.flag_id for f in view.tax_flags] == ["f1", "f2"]
    assert [q.question_id for q in view.questions] == ["q_early", "q_late", "q_undated", "q_answered"]
    assert view.export.state is ExportState.DELIVERED and view.export.delivered_at == t(9)
    assert view.needs_accountant == 3  # q_late and q_undated (to the accountant) plus tax flag f1
    assert view.complete_percent == 33 and not view.closed
    assert view.open_reasons == ("I'm still looking for 1 document.", "I'm still checking 1 payment.")


def test_export_state_without_a_package():
    assert ExportStatus.of(None).state is ExportState.NOT_PREPARED


def test_client_view_refuses_mixed_up_inputs():
    status = status_for(HAZEL, [])
    with pytest.raises(ValueError):
        build_client_view(OAK, status)
    other_month = PackageDelivery(entity_id=HAZEL.id, month=Month(2026, 8), package_sha256="c" * 64,
                                  prepared_at=NOW)  # fmt: skip
    with pytest.raises(ValueError):
        build_client_view(HAZEL, status, delivery=other_month)


def test_questions_need_aware_times():
    with pytest.raises(ValueError):
        AccountantQuestion("q", "?", QuestionDirection.TO_ACCOUNTANT, asked_at=datetime(2026, 10, 1))
