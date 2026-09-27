"""The Golden Rule as an enforced state machine.

Discover -> Acquire -> Understand -> Verify -> Match -> Act -> Confirm -> Close

Nothing becomes CLOSED because an AI believes something probably happened.
Every transition must carry evidence; CLOSED additionally requires GREEN quality.
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


class IllegalTransition(Exception):
    pass


class Transition(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    from_stage: Stage | None
    to_stage: Stage
    actor: str
    evidence_ids: list[str]
    note: str = ""


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
        if quality is not None:
            self.quality = quality
        if not evidence_ids:
            raise IllegalTransition(f"{to.value}: every transition requires evidence")

        if to in (Stage.NEEDS_OWNER, Stage.CONFLICT):
            if to == Stage.CONFLICT:
                self.quality = Quality.RED
        elif to == Stage.NOT_REQUIRED:
            pass
        else:
            current = self.stage
            if current in (Stage.NEEDS_OWNER, Stage.CONFLICT):
                resume_from = self._last_linear_stage()
            else:
                resume_from = current
            if current == Stage.CLOSED:
                raise IllegalTransition("item is already closed")
            if resume_from not in ORDER:
                raise IllegalTransition(f"cannot move from {current.value} to {to.value}")
            i, j = ORDER.index(resume_from), ORDER.index(to)
            skipped = ORDER[i + 1 : j]
            if j <= i and current not in (Stage.NEEDS_OWNER, Stage.CONFLICT):
                raise IllegalTransition(f"cannot move backwards {current.value} -> {to.value}")
            if any(s not in SKIPPABLE for s in skipped):
                raise IllegalTransition(
                    f"cannot skip {[s.value for s in skipped]} on the way to {to.value}"
                )
            if to == Stage.CLOSED and self.quality != Quality.GREEN:
                raise IllegalTransition("closure requires verified (GREEN) evidence")

        self.history.append(
            Transition(
                from_stage=self.stage, to_stage=to, actor=actor, evidence_ids=evidence_ids, note=note
            )
        )
        self.stage = to

    def _last_linear_stage(self) -> Stage:
        for t in reversed(self.history):
            if t.to_stage in ORDER:
                return t.to_stage
        return Stage.DISCOVERED

    @property
    def is_done(self) -> bool:
        return self.stage in (Stage.CLOSED, Stage.NOT_REQUIRED)
