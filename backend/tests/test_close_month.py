"""Month status and the §2 closed-month summary (§2, §3, §19, §24, §35, §47–48, §57)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from backoffice.closure import (
    Activity,
    ActivityKind,
    Actor,
    BlockerKind,
    ConnectorCoverage,
    EvidenceDecision,
    InteractionKind,
    ItemState,
    Month,
    MonthNotClosed,
    MonthState,
    OwnerInteraction,
    classify_item,
    closed_summary,
    compute_month_status,
    render_closed_summary,
)
from backoffice.domain.lifecycle import ORDER, Stage, TrackedItem
from backoffice.domain.models import Obligation, ObligationKind, Quality

LISBON = ZoneInfo("Europe/Lisbon")
SEPT = Month(2026, 9)
NOW = datetime(2026, 10, 3, 10, 0, tzinfo=LISBON)
ENT = "ent_hazel"

# --------------------------------------------------------------------------- fakes and builders


@dataclass(frozen=True)
class FakeConnector:
    name: str
    healthy: bool = True
    covered_from: datetime | None = datetime(2026, 6, 1, tzinfo=timezone.utc)
    covered_until: datetime | None = datetime(2026, 10, 3, 9, 0, tzinfo=LISBON)
    last_synced_at: datetime | None = None


@dataclass(frozen=True)
class FakeDecision:
    transaction_id: str
    requires_document: bool = True
    quality: Quality = Quality.GREEN


GMAIL = FakeConnector("Gmail")
BANK = FakeConnector("Millennium")


def walk(subject_type: str, subject_id: str, to: Stage, quality: Quality = Quality.GREEN) -> TrackedItem:
    item = TrackedItem(tenant_id="t1", subject_type=subject_type, subject_id=subject_id)
    for stage in ORDER[1 : ORDER.index(to) + 1]:
        item.advance(stage, actor="system", evidence_ids=[f"ev_{stage.value}"], quality=quality)
    return item


def closed(subject_type: str = "transaction", subject_id: str = "tx_1") -> TrackedItem:
    return walk(subject_type, subject_id, Stage.CLOSED)


def not_required(subject_id: str, quality: Quality) -> TrackedItem:
    item = walk("transaction", subject_id, Stage.UNDERSTOOD, Quality.AMBER)
    item.advance(Stage.NOT_REQUIRED, actor="system", evidence_ids=["ev_rule"], quality=quality)
    return item


def status(items=(), **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("connectors", [GMAIL, BANK])
    kw.setdefault("tz", LISBON)
    return compute_month_status(ENT, SEPT, list(items), **kw)


def obligation(kind=ObligationKind.TAX_DEADLINE, due=date(2026, 9, 20), satisfied=False, entity=ENT, **kw):
    return Obligation(
        tenant_id="t1",
        entity_id=entity,
        kind=kind,
        title=kw.pop("title", "Tax payment"),
        due_on=due,
        satisfied_by_evidence_ids=["ev_paid"] if satisfied else [],
        **kw,
    )


def at(day: int, month: int = 9) -> datetime:
    return datetime(2026, month, day, 12, 0, tzinfo=LISBON)


_BANNED = re.compile(
    r"reconcil|\bentit(y|ies)\b|exception|workflow|queue|\bAPI\b|error|traceback|null|"
    r"[a-z]{2,8}_[0-9a-f]{16}|!|great job|ledger|\bOCR\b",
    re.IGNORECASE,
)


def assert_plain(text: str) -> None:
    assert not _BANNED.search(text), text


# --------------------------------------------------------------------------- protocols


def test_fakes_satisfy_the_public_protocols():
    assert isinstance(GMAIL, ConnectorCoverage)
    assert isinstance(FakeDecision("tx_1"), EvidenceDecision)


# --------------------------------------------------------------------------- closing


def test_month_closes_when_everything_is_green_and_covered():
    s = status([closed(subject_id="tx_1"), closed("document", "doc_1")])
    assert s.state is MonthState.CLOSED and s.closed
    assert s.percent_closed == 100
    assert s.blockers == ()
    assert s.headline == "September is closed."
    assert s.company_status == "Closed"
    assert s.connectors_ok


def test_an_empty_month_with_full_coverage_is_closed():
    s = status([])
    assert s.closed and s.percent_closed == 100


def test_north_star_summary_uses_exactly_the_section_2_phrasing():
    items = [closed("transaction", f"tx_{i}") for i in range(218)]
    items += [closed("document", f"doc_{i}") for i in range(186)]
    activities = [
        Activity(ActivityKind.MISSING_DOCUMENT_RETRIEVED, at(5 + i % 20), ENT, subject_id=f"tx_{i}")
        for i in range(14)
    ]
    # Retrieved by the owner: not "automatically".
    activities.append(
        Activity(ActivityKind.MISSING_DOCUMENT_RETRIEVED, at(9), ENT, Actor.OWNER, subject_id="tx_99")
    )
    activities += [
        Activity(ActivityKind.SUPPLIER_CHASED, at(10 + i), ENT, subject_id=f"sup_{i % 7}") for i in range(9)
    ]
    activities += [
        Activity(ActivityKind.ACCOUNTANT_QUESTION_RESOLVED, at(2, 10), ENT, subject_id=f"q_{i}",
                 period=SEPT)
        for i in range(3)
    ]  # fmt: skip
    # Another company's and another month's activity never count.
    activities += [
        Activity(ActivityKind.SUPPLIER_CHASED, at(12), "ent_other", subject_id="sup_x"),
        Activity(ActivityKind.SUPPLIER_CHASED, at(12, 8), ENT, subject_id="sup_y"),
    ]
    obligations = [
        obligation(ObligationKind.TAX_DEADLINE, date(2026, 9, 20), satisfied=True),
        obligation(ObligationKind.FILING, date(2026, 9, 15), satisfied=True, title="Tax return"),
        obligation(ObligationKind.RENT, date(2026, 9, 1), satisfied=True, title="Rent payment"),
        obligation(ObligationKind.TAX_DEADLINE, date(2026, 10, 20), satisfied=False),  # due later
    ]
    interactions = [
        OwnerInteraction(at(14), 170, InteractionKind.ANSWER, ENT),
        OwnerInteraction(at(21), 40, InteractionKind.APPROVAL, None),  # across companies: counts
        OwnerInteraction(at(22), 20, InteractionKind.CAPTURE, ENT),
        OwnerInteraction(at(2), 600, InteractionKind.ONBOARDING, ENT),  # set-up: excluded
        OwnerInteraction(at(22), 900, InteractionKind.ANSWER, "ent_other"),
    ]
    s = status(items, activities=activities, obligations=obligations, interactions=interactions)
    assert s.closed
    assert render_closed_summary(s) == (
        "September is closed. 218 transactions checked · 186 documents collected · "
        "14 missing documents retrieved automatically · 7 suppliers chased · "
        "3 accountant questions resolved · 2 tax obligations verified · 0 unresolved issues. "
        "You spent 4 minutes."
    )
    parts = closed_summary(s)
    assert parts.headline == "September is closed."
    assert parts.footer == "You spent 4 minutes."
    assert len(parts.facts) == 7


def test_summary_uses_singular_forms():
    items = [closed("transaction", "tx_1"), closed("document", "doc_1")]
    activities = [
        Activity(ActivityKind.MISSING_DOCUMENT_RETRIEVED, at(3), ENT, subject_id="tx_1"),
        Activity(ActivityKind.SUPPLIER_CHASED, at(3), ENT, subject_id="sup_1"),
        Activity(ActivityKind.ACCOUNTANT_QUESTION_RESOLVED, at(3), ENT, subject_id="q_1"),
    ]
    s = status(
        items,
        activities=activities,
        obligations=[obligation(satisfied=True)],
        interactions=[OwnerInteraction(at(4), 1, InteractionKind.ANSWER, ENT)],
    )
    assert render_closed_summary(s) == (
        "September is closed. 1 transaction checked · 1 document collected · "
        "1 missing document retrieved automatically · 1 supplier chased · "
        "1 accountant question resolved · 1 tax obligation verified · 0 unresolved issues. "
        "You spent 1 minute."
    )


def test_summary_of_an_earlier_year_names_the_year():
    s = status([closed()], now=datetime(2027, 1, 5, 9, 0, tzinfo=LISBON))
    assert s.label == "September 2026"
    assert render_closed_summary(s).startswith("September 2026 is closed. ")


def test_closed_summary_refuses_an_open_month():
    s = status([walk("transaction", "tx_1", Stage.VERIFIED)])
    with pytest.raises(MonthNotClosed):
        render_closed_summary(s)


# --------------------------------------------------------------------------- connectors (§47–48)


def test_a_connector_that_stopped_syncing_keeps_the_month_from_ever_being_green():
    broken = FakeConnector(
        "Gmail",
        healthy=False,
        covered_until=datetime(2026, 10, 2, 14, 42, tzinfo=LISBON),
        last_synced_at=datetime(2026, 10, 2, 14, 42, tzinfo=LISBON),
    )
    s = status([closed()], connectors=[broken, BANK])
    assert not s.closed
    assert s.state is MonthState.NEEDS_OWNER
    assert s.percent_closed == 99  # every item is done, but the month is not
    (blocker,) = s.blockers
    assert blocker.kind is BlockerKind.CONNECTOR
    assert blocker.message == "Gmail has not synced since 14:42 yesterday."
    assert blocker.action == "Reconnect" and blocker.needs_owner
    assert s.needs_you == 1
    assert s.company_status == "Needs reconnecting"
    assert s.summary.unresolved_issues == 1


def test_unhealthy_connector_blocks_even_when_its_data_covers_the_month():
    broken = FakeConnector("Gmail", healthy=False)
    s = status([closed()], connectors=[broken])
    assert not s.closed
    assert s.blockers[0].message.startswith("Gmail has not synced since ")


def test_unhealthy_connector_that_never_synced():
    s = status([], connectors=[FakeConnector("Outlook", healthy=False, covered_from=None, covered_until=None)])
    assert s.reasons() == ["Outlook has not synced yet."]
    assert s.blockers[0].action == "Reconnect"


def test_history_not_imported_back_to_the_start_of_the_month():
    late_start = FakeConnector("Gmail", covered_from=datetime(2026, 9, 3, tzinfo=LISBON))
    s = status([closed()], connectors=[late_start])
    assert s.reasons() == ["Gmail is still syncing back to 1 September."]
    assert s.state is MonthState.ON_TRACK
    assert s.company_status == "On track"


def test_healthy_connector_that_has_not_caught_up_to_month_end():
    lagging = FakeConnector("Millennium", covered_until=datetime(2026, 9, 30, 23, 0, tzinfo=LISBON))
    s = status([closed()], connectors=[GMAIL, lagging])
    assert s.reasons() == ["Waiting for Millennium to catch up to the end of September."]
    assert s.needs_you == 0


def test_month_boundaries_are_read_in_the_business_time_zone():
    # 23:30 UTC on 30 September is 00:30 on 1 October in Lisbon: September is fully covered there.
    edge = FakeConnector("Gmail", covered_until=datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc))
    assert status([closed()], connectors=[edge]).closed
    in_utc = status([closed()], connectors=[edge], tz=timezone.utc)
    assert not in_utc.closed


def test_no_connected_accounts_means_no_proof_the_month_is_complete():
    s = status([closed()], connectors=[])
    assert [b.kind for b in s.blockers] == [BlockerKind.NO_SOURCES]
    assert s.reasons() == ["No accounts are connected yet."]
    assert s.needs_you == 1
    assert s.company_status == "Needs connecting"


def test_a_month_that_is_not_over_cannot_close():
    now = datetime(2026, 9, 20, 10, 0, tzinfo=LISBON)
    live = FakeConnector("Gmail", covered_until=now - timedelta(minutes=5))
    s = status([closed()], now=now, connectors=[live])
    assert s.reasons() == ["September isn't over yet."]
    assert s.state is MonthState.ON_TRACK
    assert s.percent_closed == 99


def test_connector_times_must_be_timezone_aware():
    naive = FakeConnector("Gmail", covered_until=datetime(2026, 10, 3, 9, 0))
    with pytest.raises(ValueError):
        status([], connectors=[naive])


# --------------------------------------------------------------------------- items (§3, §19, §57)


def test_classify_item_only_calls_green_terminal_items_done():
    assert classify_item(closed()) is ItemState.DONE
    assert classify_item(not_required("tx_2", Quality.GREEN)) is ItemState.DONE
    assert classify_item(not_required("tx_3", Quality.AMBER)) is ItemState.UNPROVEN
    assert classify_item(walk("transaction", "tx_4", Stage.MATCHED)) is ItemState.IN_PROGRESS


def test_amber_is_never_counted_as_done():
    s = status([closed(subject_id="tx_1"), not_required("tx_2", Quality.AMBER)])
    assert not s.closed
    assert s.counts.done == 1 and s.counts.unproven == 1
    assert s.percent_closed == 50
    assert s.reasons() == ["1 payment still needs proof."]


def test_a_later_contradiction_reopens_a_closed_item_as_a_conflict():
    item = closed(subject_id="tx_1")
    item.advance(Stage.CONFLICT, actor="system", evidence_ids=["ev_qr"])
    s = status([item, closed(subject_id="tx_2")])
    assert classify_item(item) is ItemState.CONFLICT
    assert s.state is MonthState.NEEDS_OWNER
    assert s.reasons() == ["1 payment has details that don't agree."]
    assert s.needs_you == 1


def test_owner_questions_drive_the_company_status():
    one = walk("transaction", "tx_1", Stage.VERIFIED)
    one.advance(Stage.NEEDS_OWNER, actor="system", evidence_ids=["ev_q"])
    s = status([one])
    assert s.reasons()[0] == "I need one answer from you."
    assert s.company_status == "Needs one answer"
    two = walk("document", "doc_2", Stage.UNDERSTOOD)
    two.advance(Stage.NEEDS_OWNER, actor="system", evidence_ids=["ev_q"])
    s2 = status([one, two])
    assert s2.reasons()[0] == "I need 2 answers from you."
    assert s2.company_status == "Needs 2 answers"


def test_missing_documents_follow_the_expected_evidence_decisions():
    waiting_for_invoice = walk("transaction", "tx_voda", Stage.VERIFIED)
    internal_transfer = walk("transaction", "tx_own", Stage.VERIFIED)
    matched = walk("transaction", "tx_uber", Stage.MATCHED)
    asked = walk("transaction", "tx_ikea", Stage.UNDERSTOOD)
    asked.advance(Stage.NEEDS_OWNER, actor="system", evidence_ids=["ev_q"])
    decisions = [
        FakeDecision("tx_voda"),
        FakeDecision("tx_own", requires_document=False),
        FakeDecision("tx_uber"),
        FakeDecision("tx_ikea"),
    ]
    s = status([waiting_for_invoice, internal_transfer, matched, asked], decisions=decisions)
    assert s.missing_documents == 2  # voda and ikea; uber already has its document matched
    kinds = [b.kind for b in s.blockers]
    assert kinds == [BlockerKind.NEEDS_OWNER, BlockerKind.MISSING_DOCUMENTS, BlockerKind.IN_PROGRESS]
    assert "I'm still looking for 1 document." in s.reasons()
    assert "I'm still checking 2 payments." in s.reasons()


def test_stricter_decision_wins_when_two_disagree():
    item = walk("transaction", "tx_1", Stage.VERIFIED)
    decisions = [FakeDecision("tx_1", requires_document=False), FakeDecision("tx_1", requires_document=True)]
    assert status([item], decisions=decisions).missing_documents == 1


def test_decided_transactions_without_a_tracked_item_block_the_month():
    s = status([closed(subject_id="tx_1")], decisions=[FakeDecision("tx_1"), FakeDecision("tx_2")])
    assert not s.closed
    assert s.counts.untracked == 1 and s.items_total == 2
    assert s.reasons() == ["I haven't started on 1 payment yet."]
    assert s.percent_closed == 50


def test_mixed_subject_types_are_called_things():
    items = [walk("transaction", "tx_1", Stage.VERIFIED), walk("document", "doc_1", Stage.ACQUIRED)]
    assert status(items).reasons()[0] == "I'm still checking 2 things."


def test_duplicate_items_are_refused():
    item = closed()
    with pytest.raises(ValueError):
        status([item, item])


def test_documents_collected_counts_acquired_documents_that_matter():
    acquired = walk("document", "doc_1", Stage.ACQUIRED)
    only_found = walk("document", "doc_2", Stage.DISCOVERED)
    duplicate = walk("document", "doc_3", Stage.ACQUIRED)
    duplicate.advance(Stage.NOT_REQUIRED, actor="system", evidence_ids=["ev_dup"], quality=Quality.GREEN)
    asked = walk("document", "doc_4", Stage.UNDERSTOOD)
    asked.advance(Stage.NEEDS_OWNER, actor="system", evidence_ids=["ev_q"])
    s = status([acquired, only_found, duplicate, asked])
    assert s.summary.documents_collected == 2  # doc_1 and doc_4


# --------------------------------------------------------------------------- obligations (§24)


def test_open_obligations_block_and_explain_themselves():
    overdue = obligation(due=date(2026, 9, 20))
    s = status([closed()], obligations=[overdue])
    assert not s.closed
    assert s.open_obligations == (overdue.id,)
    assert s.reasons() == ["Tax payment was due on 20 September. I haven't seen proof yet."]
    assert s.needs_you == 1  # the owner is responsible and it is late
    assert s.summary.unresolved_issues == 1


def test_obligations_that_do_not_block():
    s = status(
        [closed()],
        obligations=[
            obligation(satisfied=True),
            obligation(due=date(2026, 10, 20)),  # due after the month
            obligation(entity="ent_other"),  # another company
        ],
    )
    assert s.closed


def test_upcoming_obligation_in_an_unfinished_month():
    now = datetime(2026, 9, 10, 9, 0, tzinfo=LISBON)
    live = FakeConnector("Gmail", covered_until=now)
    ob = obligation(due=date(2026, 9, 25), responsible="accountant", title="Tax return")
    s = status([], now=now, connectors=[live], obligations=[ob])
    assert "Tax return is due on 25 September." in s.reasons()
    assert s.needs_you == 0


def test_obligation_tracked_as_an_item_is_not_counted_twice():
    ob = obligation(due=date(2026, 9, 20))
    item = walk("obligation", ob.id, Stage.UNDERSTOOD)
    s = status([item], obligations=[ob])
    assert s.summary.unresolved_issues == 1
    assert s.needs_you == 0


# --------------------------------------------------------------------------- percent


def test_percent_rounds_down_and_never_claims_100_early():
    items = [closed(subject_id=f"tx_{i}") for i in range(2)] + [walk("transaction", "tx_x", Stage.VERIFIED)]
    assert status(items).percent_closed == 66
    many = [closed(subject_id=f"tx_{i}") for i in range(999)] + [walk("transaction", "tx_x", Stage.VERIFIED)]
    assert status(many).percent_closed == 99


def test_weighted_percent_uses_subject_weights():
    items = [closed(subject_id="tx_big"), walk("transaction", "tx_small", Stage.VERIFIED)]
    weights = {"tx_big": Decimal("900.00"), "tx_small": Decimal("100.00")}
    assert status(items).percent_closed == 50
    assert status(items, weights=weights).percent_closed == 90


def test_weights_default_for_unlisted_subjects_and_zero_total():
    items = [closed(subject_id="tx_a"), walk("transaction", "tx_b", Stage.VERIFIED)]
    assert status(items, weights={"tx_a": Decimal(3)}).percent_closed == 75
    zero = {"tx_a": Decimal(0), "tx_b": Decimal(0)}
    assert status(items, weights=zero).percent_closed == 0


def test_weights_must_be_non_negative_decimals():
    items = [closed(subject_id="tx_a"), walk("transaction", "tx_b", Stage.VERIFIED)]
    with pytest.raises(ValueError):
        status(items, weights={"tx_a": Decimal(-1)})
    with pytest.raises(TypeError):
        status(items, weights={"tx_a": 1.5})


def test_empty_open_month_is_zero_percent():
    s = status([], connectors=[])
    assert s.percent_closed == 0


# --------------------------------------------------------------------------- inputs and wording


def test_now_must_be_timezone_aware():
    with pytest.raises(ValueError):
        status([], now=datetime(2026, 10, 3, 10, 0))


def test_entity_is_required():
    with pytest.raises(ValueError):
        compute_month_status("", SEPT, [], now=NOW, connectors=[GMAIL])


def test_every_owner_facing_reason_is_plain_language():
    asked = walk("transaction", "tx_q", Stage.UNDERSTOOD)
    asked.advance(Stage.NEEDS_OWNER, actor="system", evidence_ids=["ev_q"])
    conflict = walk("document", "doc_c", Stage.VERIFIED)
    conflict.advance(Stage.CONFLICT, actor="system", evidence_ids=["ev_qr"])
    items = [
        asked,
        conflict,
        walk("transaction", "tx_m", Stage.VERIFIED),
        not_required("tx_nr", Quality.AMBER),
        walk("transaction", "tx_w", Stage.MATCHED),
    ]
    connectors = [
        FakeConnector("Gmail", healthy=False),
        FakeConnector("Outlook", covered_from=None, covered_until=None),
        FakeConnector("Drive", covered_from=datetime(2026, 9, 5, tzinfo=LISBON)),
        FakeConnector("Millennium", covered_until=datetime(2026, 9, 29, tzinfo=LISBON)),
    ]
    s = status(
        items,
        connectors=connectors,
        decisions=[FakeDecision("tx_m"), FakeDecision("tx_untracked")],
        obligations=[obligation()],
    )
    kinds = {b.kind for b in s.blockers}
    assert kinds >= {
        BlockerKind.CONNECTOR, BlockerKind.NEEDS_OWNER, BlockerKind.CONFLICT, BlockerKind.OBLIGATION,
        BlockerKind.MISSING_DOCUMENTS, BlockerKind.UNPROVEN, BlockerKind.UNTRACKED, BlockerKind.IN_PROGRESS,
    }  # fmt: skip
    for reason in s.reasons():
        assert_plain(reason)
        assert reason.endswith(".")
    assert_plain(s.headline)
    assert s.headline == f"September is {s.percent_closed}% closed."
