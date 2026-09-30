"""Activity and owner-interaction records the closure counts are built from (§2, §59).

Other modules (missing-document autopilot, supplier chasing, accountant
agent, the apps) append these; closure only reads them. Keeping the log
explicit means every number in "September is closed." can be traced back to
a recorded event rather than an estimate.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from enum import Enum

from backoffice.domain.lifecycle import Stage, TrackedItem

from ._text import require_aware, require_count
from .period import Month

__all__ = [
    "OWNER_ACTOR",
    "Activity",
    "ActivityKind",
    "Actor",
    "InteractionKind",
    "OwnerInteraction",
    "activities_for",
    "distinct_subjects",
    "interactions_for",
    "is_owner_actor",
    "owner_touched",
]

OWNER_ACTOR = "owner"


class Actor(str, Enum):
    """Who did something. Only SYSTEM counts as "automatically" (§2, §22)."""

    SYSTEM = "system"
    OWNER = "owner"
    ACCOUNTANT = "accountant"


class ActivityKind(str, Enum):
    MISSING_DOCUMENT_DETECTED = "missing_document_detected"  # §22 start
    MISSING_DOCUMENT_RETRIEVED = "missing_document_retrieved"  # subject: transaction/document id
    SUPPLIER_CHASED = "supplier_chased"  # subject: supplier id
    ACCOUNTANT_QUESTION_ASKED = "accountant_question_asked"  # subject: question id
    ACCOUNTANT_QUESTION_RESOLVED = "accountant_question_resolved"  # actor OWNER = needed the owner
    SILENT_ERROR_FOUND = "silent_error_found"  # a verified critical value was later proven wrong (§59)


class InteractionKind(str, Enum):
    ONBOARDING = "onboarding"  # set-up time, reported separately (§59)
    ANSWER = "answer"
    APPROVAL = "approval"
    CAPTURE = "capture"
    RECONNECT = "reconnect"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class Activity:
    """Something that happened for an entity. ``period`` defaults to the month of ``at``."""

    kind: ActivityKind
    at: datetime
    entity_id: str
    actor: Actor = Actor.SYSTEM
    subject_id: str | None = None
    period: Month | None = None

    def __post_init__(self) -> None:
        require_aware(self.at, "at")
        if not self.entity_id:
            raise ValueError("entity_id is required")
        # Records loaded from storage carry plain strings; counts compare enums by identity.
        object.__setattr__(self, "kind", ActivityKind(self.kind))
        object.__setattr__(self, "actor", Actor(self.actor))
        object.__setattr__(self, "period", _as_month(self.period))

    def month(self, tz: tzinfo) -> Month:
        return self.period or Month.of(self.at, tz)


@dataclass(frozen=True, slots=True)
class OwnerInteraction:
    """Active owner time from the apps (foreground, focused), in whole seconds.

    ``entity_id=None`` is time spent across companies (e.g. the Home screen) and
    counts towards every company's month summary.
    """

    at: datetime
    active_seconds: int
    kind: InteractionKind = InteractionKind.ANSWER
    entity_id: str | None = None
    period: Month | None = None

    def __post_init__(self) -> None:
        require_aware(self.at, "at")
        require_count(self.active_seconds, "active_seconds")
        object.__setattr__(self, "kind", InteractionKind(self.kind))
        object.__setattr__(self, "period", _as_month(self.period))

    def month(self, tz: tzinfo) -> Month:
        return self.period or Month.of(self.at, tz)


def _as_month(value: Month | str | None) -> Month | None:
    """``'2026-09'`` -> Month; a Month or None passes through."""
    if value is None or isinstance(value, Month):
        return value
    if isinstance(value, str):
        return Month.parse(value)
    raise TypeError("period must be a Month or 'YYYY-MM'")


def activities_for(
    activities: Iterable[Activity], month: Month, tz: tzinfo, entity_id: str | None = None
) -> list[Activity]:
    """Activities about ``month`` (and ``entity_id`` when given)."""
    return [
        a
        for a in activities
        if a.month(tz) == month and (entity_id is None or a.entity_id == entity_id)
    ]


def interactions_for(
    interactions: Iterable[OwnerInteraction],
    month: Month,
    tz: tzinfo,
    entity_id: str | None = None,
    *,
    include_onboarding: bool = False,
) -> list[OwnerInteraction]:
    """Owner time about ``month``; shared (entity-less) time counts for every entity."""
    return [
        i
        for i in interactions
        if i.month(tz) == month
        and (entity_id is None or i.entity_id in (None, entity_id))
        and (include_onboarding or i.kind is not InteractionKind.ONBOARDING)
    ]


def distinct_subjects(activities: Iterable[Activity]) -> int:
    """Count distinct subjects; events without a subject each count once."""
    seen: set[str] = set()
    anonymous = 0
    for a in activities:
        if a.subject_id is None:
            anonymous += 1
        else:
            seen.add(a.subject_id)
    return len(seen) + anonymous


def is_owner_actor(actor: str) -> bool:
    """Transition actors are free text; ``'owner'`` or ``'owner:<user>'`` mean the owner."""
    return actor == OWNER_ACTOR or actor.startswith(OWNER_ACTOR + ":")


def owner_touched(item: TrackedItem) -> bool:
    """True if the owner had to act on the item or was asked to (zero-touch, §59)."""
    return any(
        t.to_stage is Stage.NEEDS_OWNER or is_owner_actor(t.actor) for t in item.history
    ) or item.stage is Stage.NEEDS_OWNER
