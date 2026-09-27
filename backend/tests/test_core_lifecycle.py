"""Golden-rule state machine (§3, §19, §57)."""

from __future__ import annotations

import pytest

from backoffice.domain.lifecycle import (
    ORDER,
    SKIPPABLE,
    IllegalTransition,
    Stage,
    TrackedItem,
)
from backoffice.domain.models import Quality

LINEAR = list(ORDER)


def new_item(**kw) -> TrackedItem:
    return TrackedItem(
        tenant_id="t1", subject_type="transaction", subject_id="tx_1", **kw
    )


def walk_to(stage: Stage, *, quality: Quality = Quality.GREEN) -> TrackedItem:
    """Item advanced step by step (no skips) to ``stage``."""
    item = new_item()
    for s in LINEAR[1 : LINEAR.index(stage) + 1]:
        item.advance(s, actor="system", evidence_ids=[f"ev_{s.value}"], quality=quality)
    return item


def snapshot(item: TrackedItem) -> tuple:
    return item.stage, item.quality, len(item.history)


# --------------------------------------------------------------------------- happy paths


def test_full_path_closes_with_green_evidence():
    item = walk_to(Stage.CLOSED)
    assert item.stage is Stage.CLOSED
    assert item.is_done
    assert [t.to_stage for t in item.history] == LINEAR[1:]


def test_acted_is_skippable_when_nothing_needs_doing():
    item = walk_to(Stage.MATCHED)
    item.advance(Stage.CONFIRMED, actor="system", evidence_ids=["ev_bank"])
    assert item.stage is Stage.CONFIRMED
    assert SKIPPABLE == {Stage.ACTED}


def test_quality_given_on_the_closing_transition_counts():
    item = walk_to(Stage.CONFIRMED, quality=Quality.AMBER)
    item.advance(
        Stage.CLOSED, actor="system", evidence_ids=["ev_qr"], quality=Quality.GREEN
    )
    assert item.stage is Stage.CLOSED and item.quality is Quality.GREEN


# --------------------------------------------------------------------------- exhaustive linear moves


def _linear_move_is_legal(src: Stage, dst: Stage) -> bool:
    i, j = LINEAR.index(src), LINEAR.index(dst)
    return j > i and all(s in SKIPPABLE for s in LINEAR[i + 1 : j])


@pytest.mark.parametrize("src", LINEAR[:-1], ids=lambda s: s.value)
@pytest.mark.parametrize("dst", LINEAR, ids=lambda s: s.value)
def test_every_linear_transition(src: Stage, dst: Stage):
    item = walk_to(src)
    before = snapshot(item)
    if _linear_move_is_legal(src, dst):
        item.advance(dst, actor="system", evidence_ids=["ev_x"], quality=Quality.GREEN)
        assert item.stage is dst
    else:
        with pytest.raises(IllegalTransition):
            item.advance(
                dst, actor="system", evidence_ids=["ev_x"], quality=Quality.GREEN
            )
        assert snapshot(item) == before


# --------------------------------------------------------------------------- evidence


@pytest.mark.parametrize("dst", list(Stage), ids=lambda s: s.value)
def test_every_transition_requires_evidence(dst: Stage):
    item = walk_to(Stage.CONFIRMED)
    before = snapshot(item)
    with pytest.raises(IllegalTransition, match="requires evidence"):
        item.advance(dst, actor="system", evidence_ids=[], quality=Quality.GREEN)
    assert snapshot(item) == before


@pytest.mark.parametrize("bad", [[""], ["   "], ["ev_1", ""]])
def test_blank_evidence_ids_are_not_evidence(bad):
    item = new_item()
    with pytest.raises(IllegalTransition, match="non-blank"):
        item.advance(Stage.ACQUIRED, actor="system", evidence_ids=bad)
    assert item.history == []


def test_history_records_the_transition_and_copies_evidence():
    item = new_item()
    ev = ["ev_pdf"]
    item.advance(Stage.ACQUIRED, actor="retrieval", evidence_ids=ev, note="from Gmail")
    ev.append("ev_other")
    t = item.history[0]
    assert (t.from_stage, t.to_stage, t.actor, t.note) == (
        Stage.DISCOVERED,
        Stage.ACQUIRED,
        "retrieval",
        "from Gmail",
    )
    assert t.evidence_ids == ["ev_pdf"]
    assert t.quality is Quality.AMBER
    assert t.at.tzinfo is not None


# --------------------------------------------------------------------------- closure needs GREEN


@pytest.mark.parametrize("quality", [Quality.AMBER, Quality.RED])
def test_closed_requires_green(quality: Quality):
    item = walk_to(Stage.CONFIRMED, quality=quality)
    with pytest.raises(IllegalTransition, match="GREEN"):
        item.advance(Stage.CLOSED, actor="system", evidence_ids=["ev_bank"])
    assert item.stage is Stage.CONFIRMED


def test_rejected_transition_does_not_leak_quality():
    """Regression: a failed advance used to keep the quality it was given."""
    item = walk_to(Stage.UNDERSTOOD, quality=Quality.AMBER)
    with pytest.raises(IllegalTransition):
        item.advance(
            Stage.CLOSED, actor="ai", evidence_ids=["ev_guess"], quality=Quality.GREEN
        )
    assert item.quality is Quality.AMBER
    walk = [Stage.VERIFIED, Stage.MATCHED, Stage.CONFIRMED]
    for s in walk:
        item.advance(s, actor="system", evidence_ids=["ev_1"])
    with pytest.raises(IllegalTransition, match="GREEN"):
        item.advance(Stage.CLOSED, actor="system", evidence_ids=["ev_1"])


def test_rejected_transition_without_evidence_does_not_leak_quality():
    item = walk_to(Stage.CONFIRMED, quality=Quality.AMBER)
    with pytest.raises(IllegalTransition):
        item.advance(Stage.CLOSED, actor="ai", evidence_ids=[], quality=Quality.GREEN)
    assert item.quality is Quality.AMBER


# --------------------------------------------------------------------------- conflict


@pytest.mark.parametrize("src", LINEAR, ids=lambda s: s.value)
def test_conflict_from_any_stage_sets_red(src: Stage):
    item = walk_to(src)
    item.advance(
        Stage.CONFLICT,
        actor="verification",
        evidence_ids=["ev_qr"],
        quality=Quality.GREEN,
    )
    assert item.stage is Stage.CONFLICT
    assert item.quality is Quality.RED
    assert item.history[-1].quality is Quality.RED
    assert not item.is_done


def test_conflict_cannot_close_without_new_green_evidence():
    item = walk_to(Stage.CONFIRMED)
    item.advance(Stage.CONFLICT, actor="fraud", evidence_ids=["ev_new_iban"])
    with pytest.raises(IllegalTransition, match="GREEN"):
        item.advance(Stage.CLOSED, actor="system", evidence_ids=["ev_owner"])
    item.advance(
        Stage.CLOSED, actor="owner", evidence_ids=["ev_owner"], quality=Quality.GREEN
    )
    assert item.stage is Stage.CLOSED


def test_conflict_allows_rewinding_to_redo_work():
    item = walk_to(Stage.MATCHED)
    item.advance(Stage.CONFLICT, actor="verification", evidence_ids=["ev_qr"])
    item.advance(
        Stage.VERIFIED, actor="owner", evidence_ids=["ev_answer"], quality=Quality.AMBER
    )
    assert item.stage is Stage.VERIFIED
    item.advance(Stage.MATCHED, actor="system", evidence_ids=["ev_bank"])
    assert item.stage is Stage.MATCHED


def test_resume_from_conflict_still_cannot_skip():
    item = walk_to(Stage.UNDERSTOOD)
    item.advance(Stage.CONFLICT, actor="verification", evidence_ids=["ev_qr"])
    with pytest.raises(IllegalTransition, match="cannot skip"):
        item.advance(Stage.CONFIRMED, actor="system", evidence_ids=["ev_1"])
    assert item.stage is Stage.CONFLICT


# --------------------------------------------------------------------------- needs owner


def test_resume_after_needs_owner_continues_from_last_linear_stage():
    item = walk_to(Stage.MATCHED, quality=Quality.AMBER)
    item.advance(Stage.NEEDS_OWNER, actor="system", evidence_ids=["ev_question"])
    assert item.quality is Quality.AMBER  # asking the owner does not change quality
    item.advance(Stage.ACTED, actor="owner", evidence_ids=["ev_answer"])
    assert item.stage is Stage.ACTED


def test_resume_after_needs_owner_may_skip_acted():
    item = walk_to(Stage.MATCHED)
    item.advance(Stage.NEEDS_OWNER, actor="system", evidence_ids=["ev_question"])
    item.advance(Stage.CONFIRMED, actor="owner", evidence_ids=["ev_answer"])
    assert item.stage is Stage.CONFIRMED


def test_resume_after_needs_owner_may_stay_at_same_stage():
    item = walk_to(Stage.VERIFIED)
    item.advance(Stage.NEEDS_OWNER, actor="system", evidence_ids=["ev_question"])
    item.advance(Stage.VERIFIED, actor="owner", evidence_ids=["ev_answer"])
    assert item.stage is Stage.VERIFIED


def test_resume_after_needs_owner_cannot_skip_stages():
    item = walk_to(Stage.UNDERSTOOD)
    item.advance(Stage.NEEDS_OWNER, actor="system", evidence_ids=["ev_question"])
    with pytest.raises(IllegalTransition, match="cannot skip"):
        item.advance(Stage.MATCHED, actor="owner", evidence_ids=["ev_answer"])
    assert item.stage is Stage.NEEDS_OWNER


def test_side_state_without_history_resumes_from_discovered():
    item = new_item(stage=Stage.NEEDS_OWNER)
    item.advance(Stage.ACQUIRED, actor="owner", evidence_ids=["ev_upload"])
    assert item.stage is Stage.ACQUIRED
    with pytest.raises(IllegalTransition):
        new_item(stage=Stage.NEEDS_OWNER).advance(
            Stage.UNDERSTOOD, actor="owner", evidence_ids=["ev_upload"]
        )


def test_needs_owner_can_escalate_to_conflict():
    item = walk_to(Stage.VERIFIED)
    item.advance(Stage.NEEDS_OWNER, actor="system", evidence_ids=["ev_q"])
    item.advance(Stage.CONFLICT, actor="owner", evidence_ids=["ev_a"])
    assert item.stage is Stage.CONFLICT and item.quality is Quality.RED


# --------------------------------------------------------------------------- terminal states


@pytest.mark.parametrize("dst", LINEAR + [Stage.NOT_REQUIRED], ids=lambda s: s.value)
def test_closed_is_final_for_progress(dst: Stage):
    item = walk_to(Stage.CLOSED)
    with pytest.raises(IllegalTransition, match="already closed"):
        item.advance(dst, actor="system", evidence_ids=["ev_x"], quality=Quality.GREEN)
    assert item.stage is Stage.CLOSED


def test_closed_item_can_be_reopened_by_a_conflict():
    item = walk_to(Stage.CLOSED)
    item.advance(
        Stage.CONFLICT, actor="fraud", evidence_ids=["ev_duplicate_other_iban"]
    )
    assert (item.stage, item.quality, item.is_done) == (
        Stage.CONFLICT,
        Quality.RED,
        False,
    )
    item.advance(
        Stage.CLOSED, actor="owner", evidence_ids=["ev_answer"], quality=Quality.GREEN
    )
    assert item.is_done
    assert [t.to_stage for t in item.history][-3:] == [
        Stage.CLOSED,
        Stage.CONFLICT,
        Stage.CLOSED,
    ]


@pytest.mark.parametrize(
    "src", LINEAR[:-1] + [Stage.NEEDS_OWNER, Stage.CONFLICT], ids=lambda s: s.value
)
def test_not_required_from_any_open_stage(src: Stage):
    item = new_item(stage=src) if src not in LINEAR else walk_to(src)
    item.advance(
        Stage.NOT_REQUIRED, actor="expected_evidence", evidence_ids=["ev_internal"]
    )
    assert item.stage is Stage.NOT_REQUIRED
    assert item.is_done


@pytest.mark.parametrize("dst", LINEAR + [Stage.NOT_REQUIRED], ids=lambda s: s.value)
def test_not_required_is_final_for_progress(dst: Stage):
    item = new_item()
    item.advance(Stage.NOT_REQUIRED, actor="system", evidence_ids=["ev_internal"])
    with pytest.raises(IllegalTransition, match="already not_required"):
        item.advance(dst, actor="system", evidence_ids=["ev_x"], quality=Quality.GREEN)


def test_not_required_can_be_reopened_and_resumed():
    item = walk_to(Stage.ACQUIRED)
    item.advance(Stage.NOT_REQUIRED, actor="system", evidence_ids=["ev_internal"])
    item.advance(Stage.NEEDS_OWNER, actor="accountant", evidence_ids=["ev_question"])
    item.advance(Stage.UNDERSTOOD, actor="system", evidence_ids=["ev_pdf"])
    assert item.stage is Stage.UNDERSTOOD


def test_is_done_only_for_terminal_states():
    done = {s for s in Stage if new_item(stage=s).is_done}
    assert done == {Stage.CLOSED, Stage.NOT_REQUIRED}


def test_legacy_transition_records_without_quality_still_load():
    from backoffice.domain.lifecycle import Transition

    t = Transition(
        from_stage=None,
        to_stage=Stage.DISCOVERED,
        actor="system",
        evidence_ids=["ev_1"],
    )
    assert t.quality is None
    item = TrackedItem.model_validate(
        {
            "tenant_id": "t1",
            "subject_type": "document",
            "subject_id": "doc_1",
            "history": [t.model_dump()],
        }
    )
    item.advance(Stage.ACQUIRED, actor="system", evidence_ids=["ev_2"])
    assert item.stage is Stage.ACQUIRED
