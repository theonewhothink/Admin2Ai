"""The Golden Rule as an enforced state machine (§3).

Discover -> Acquire -> Understand -> Verify -> Match -> Act -> Confirm -> Close

Nothing becomes CLOSED because an AI believes something probably happened.
Every transition must carry evidence; CLOSED additionally requires GREEN quality.

Rules enforced by :meth:`TrackedItem.advance`:

* Every transition carries at least one non-blank evidence id.
* Linear stages move forward one step at a time; only ``ACTED`` may be skipped
  (nothing needed doing).
* ``NEEDS_OWNER`` and ``CONFLICT`` can be entered from any stage, including a
  closed one (a later contradiction must never be hidden). ``CONFLICT`` always
  sets quality to RED (§19, §57).
* Leaving ``NEEDS_OWNER`` / ``CONFLICT`` resumes from the last linear stage; the
  item may resume there, move forward, or rewind to an earlier stage to redo work.
* ``CLOSED`` and ``NOT_REQUIRED`` are final for linear progress.
* A rejected transition leaves the item completely unchanged.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from .models import Quality, new_id, utcnow


class Stage(str, Enum):
    DISCOVERED = "discovered"
    ACQUIRED = "acquired"
    UNDERSTOOD = "understood"
    VERIFIED = "verified"
    MATCHED = "matched"
    ACTED = "acted"
    CONFIRMED = "confirmed"
    CLOSED = "closed"
    # Side states
    NEEDS_OWNER = "needs_owner"
    CONFLICT = "conflict"
    NOT_REQUIRED = "not_required"


ORDER = [
    Stage.DISCOVERED,
    Stage.ACQUIRED,
    Stage.UNDERSTOOD,
    Stage.VERIFIED,
    Stage.MATCHED,
    Stage.ACTED,
    Stage.CONFIRMED,
    Stage.CLOSED,
]

# Stages that may be skipped when nothing needs doing (e.g. no action required).
SKIPPABLE = {Stage.ACTED}

# Waiting states an item can enter from anywhere and later resume from.
SIDE_STATES = frozenset({Stage.NEEDS_OWNER, Stage.CONFLICT})

# Final states for linear progress.
TERMINAL = frozenset({Stage.CLOSED, Stage.NOT_REQUIRED})


class IllegalTransition(Exception):
    pass


class Transition(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    from_stage: Stage | None
    to_stage: Stage
    actor: str
    evidence_ids: list[str]
    note: str = ""
    # Quality of the item after this transition (None only for legacy records).
    quality: Quality | None = None


class TrackedItem(BaseModel):
    """Any administrative event moving toward closure."""

    id: str = Field(default_factory=lambda: new_id("item"))
    tenant_id: str
    subject_type: str  # "transaction" | "document" | "obligation"
    subject_id: str
    stage: Stage = Stage.DISCOVERED
    quality: Quality = Quality.AMBER
    history: list[Transition] = Field(default_factory=list)

    def advance(
        self,
        to: Stage,
        *,
        actor: str,
        evidence_ids: list[str],
        quality: Quality | None = None,
        note: str = "",
    ) -> None:
        """Move to ``to`` with evidence, or raise :class:`IllegalTransition`.

        Validation happens before any state changes, so a rejected call never
        leaves a half-applied quality or stage behind (§3, §57).
        """
        new_quality = self._validate(to, evidence_ids, quality)
        self.history.append(
            Transition(
                from_stage=self.stage,
                to_stage=to,
                actor=actor,
                evidence_ids=list(evidence_ids),
                note=note,
                quality=new_quality,
            )
        )
        self.quality = new_quality
        self.stage = to

    def _validate(
        self, to: Stage, evidence_ids: list[str], quality: Quality | None
    ) -> Quality:
        """Check the transition and return the item's quality after it."""
        if not evidence_ids:
            raise IllegalTransition(f"{to.value}: every transition requires evidence")
        if any(not isinstance(e, str) or not e.strip() for e in evidence_ids):
            raise IllegalTransition(f"{to.value}: evidence ids must be non-blank strings")

        new_quality = quality if quality is not None else self.quality
        if to == Stage.CONFLICT:
            return Quality.RED  # disagreement is never averaged into a guess (§19)
        if to == Stage.NEEDS_OWNER:
            return new_quality
        if self.stage in TERMINAL:
            raise IllegalTransition(f"item is already {self.stage.value}")
        if to == Stage.NOT_REQUIRED:
            return new_quality

        self._check_linear_move(to)
        if to == Stage.CLOSED and new_quality != Quality.GREEN:
            raise IllegalTransition("closure requires verified (GREEN) evidence")
        return new_quality

    def _check_linear_move(self, to: Stage) -> None:
        current = self.stage
        resuming = current in SIDE_STATES
        resume_from = self._last_linear_stage() if resuming else current
        i, j = ORDER.index(resume_from), ORDER.index(to)
        if j <= i and not resuming:
            raise IllegalTransition(f"cannot move backwards {current.value} -> {to.value}")
        skipped = ORDER[i + 1 : j]
        if any(s not in SKIPPABLE for s in skipped):
            raise IllegalTransition(
                f"cannot skip {[s.value for s in skipped]} on the way to {to.value}"
            )

    def _last_linear_stage(self) -> Stage:
        for t in reversed(self.history):
            if t.to_stage in ORDER:
                return t.to_stage
        return Stage.DISCOVERED

    @property
    def is_done(self) -> bool:
        return self.stage in TERMINAL
