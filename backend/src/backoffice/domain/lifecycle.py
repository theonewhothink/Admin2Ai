"""The Golden Rule as an enforced state machine (§3).

Discover -> Acquire -> Understand -> Verify -> Match -> Act -> Confirm -> Close

Nothing becomes CLOSED because an AI believes something probably happened.
Every transition must carry evidence; CLOSED additionally requires GREEN quality.

Rules enforced by :meth:`TrackedItem.advance`:

* Every transition names its actor and carries at least one non-blank
  evidence id (a list of ids, never a bare string).
* Linear stages move forward one step at a time; only ``ACTED`` may be skipped
  (nothing needed doing).
* ``NEEDS_OWNER`` and ``CONFLICT`` can be entered from any stage, including a
  closed one (a later contradiction must never be hidden). ``CONFLICT`` always
  sets quality to RED (§19, §57). Entering ``NEEDS_OWNER`` may lower quality
  but never raise it: a question is not verification (§57).
* Leaving ``NEEDS_OWNER`` / ``CONFLICT`` resumes from the last linear stage; the
  item may resume there, move forward, or rewind to an earlier stage to redo work.
* Golden-path stages never carry RED: a conflicting item waits (CONFLICT /
  NEEDS_OWNER) or is dismissed (NOT_REQUIRED); moving on needs a new quality.
* ``CLOSED`` and ``NOT_REQUIRED`` are final for linear progress.
* A rejected transition leaves the item completely unchanged.

Stored items are validated on load too: a CLOSED item is always GREEN, a
CONFLICT item always RED, and no other golden-path stage is RED.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator

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

# Strength of support, for "never raise quality without verification" (§57).
_QUALITY_RANK = {Quality.RED: 0, Quality.AMBER: 1, Quality.GREEN: 2}


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

    @model_validator(mode="after")
    def _golden_rule_holds(self) -> TrackedItem:
        """A stored item may not claim closure without GREEN, a conflict without RED,
        or golden-path progress while RED (§3, §19, §57)."""
        if self.stage is Stage.CLOSED and self.quality is not Quality.GREEN:
            raise ValueError("a closed item must be verified (GREEN)")
        if self.stage is Stage.CONFLICT and self.quality is not Quality.RED:
            raise ValueError("an item in conflict must be RED")
        if self.stage in ORDER and self.quality is Quality.RED:
            raise ValueError("a RED item must be in conflict, waiting for the owner or not required")
        return self

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

        ``to`` and ``quality`` may also be given as their string values.
        Validation happens before any state changes, so a rejected call never
        leaves a half-applied quality or stage behind (§3, §57).
        """
        target = _as_stage(to)
        given = _as_quality(quality)
        evidence = _evidence_list(target, evidence_ids)
        if not isinstance(actor, str) or not actor.strip():
            raise IllegalTransition(f"{target.value}: every transition needs an actor")
        new_quality = self._validate(target, given)
        self.history.append(
            Transition(
                from_stage=self.stage,
                to_stage=target,
                actor=actor,
                evidence_ids=evidence,
                note=note,
                quality=new_quality,
            )
        )
        self.quality = new_quality
        self.stage = target

    def _validate(self, to: Stage, quality: Quality | None) -> Quality:
        """Check the transition and return the item's quality after it."""
        new_quality = quality if quality is not None else self.quality
        if to is Stage.CONFLICT:
            return Quality.RED  # disagreement is never averaged into a guess (§19)
        if to is Stage.NEEDS_OWNER:
            if _QUALITY_RANK[new_quality] > _QUALITY_RANK[self.quality]:
                raise IllegalTransition(
                    "asking the owner cannot raise quality; verify with evidence instead"
                )
            return new_quality
        if self.stage in TERMINAL:
            raise IllegalTransition(f"item is already {self.stage.value}")
        if to is Stage.NOT_REQUIRED:
            return new_quality

        self._check_linear_move(to)
        if to is Stage.CLOSED and new_quality is not Quality.GREEN:
            raise IllegalTransition("closure requires verified (GREEN) evidence")
        if new_quality is Quality.RED:
            raise IllegalTransition(
                "a conflict must be resolved first: give the new quality (AMBER or GREEN)"
            )
        return new_quality

    def _check_linear_move(self, to: Stage) -> None:
        current = self.stage
        resuming = current in SIDE_STATES
        resume_from = self._last_linear_stage() if resuming else current
        i, j = ORDER.index(resume_from), ORDER.index(to)
        if j <= i and not resuming:
            raise IllegalTransition(
                f"cannot move backwards {current.value} -> {to.value}"
            )
        skipped = ORDER[i + 1 : j]
        if any(s not in SKIPPABLE for s in skipped):
            raise IllegalTransition(
                f"cannot skip {[s.value for s in skipped]} on the way to {to.value}"
            )

    def _last_linear_stage(self) -> Stage:
        """The latest golden-path stage, looking through side and terminal states.

        ``from_stage`` counts too, so an item restored mid-path without its
        earlier history resumes where it was, not from the start.
        """
        for t in reversed(self.history):
            if t.to_stage in ORDER:
                return t.to_stage
            if t.from_stage in ORDER:
                return t.from_stage
        return Stage.DISCOVERED

    @property
    def is_done(self) -> bool:
        return self.stage in TERMINAL


def _as_stage(value: Stage | str) -> Stage:
    try:
        return Stage(value)
    except ValueError:
        raise IllegalTransition(f"unknown stage {value!r}") from None


def _as_quality(value: Quality | str | None) -> Quality | None:
    if value is None:
        return None
    try:
        return Quality(value)
    except ValueError:
        raise IllegalTransition(f"unknown quality {value!r}") from None


def _evidence_list(to: Stage, evidence_ids: Iterable[str]) -> list[str]:
    """A copy of the evidence ids; a bare string is refused, not split into letters."""
    if isinstance(evidence_ids, (str, bytes)):
        raise IllegalTransition(f"{to.value}: evidence_ids must be a list of ids")
    evidence = list(evidence_ids)
    if not evidence:
        raise IllegalTransition(f"{to.value}: every transition requires evidence")
    if any(not isinstance(e, str) or not e.strip() for e in evidence):
        raise IllegalTransition(f"{to.value}: evidence ids must be non-blank strings")
    return evidence
