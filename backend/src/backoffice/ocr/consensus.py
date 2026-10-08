"""Field-level agreement between sources and engines (§18, §19, §57).

Every observation of a critical field is a *vote* by its voter, the pair
(source, method): two engines reading the same evidence are two voters, the
XML and the embedded text of one PDF are two voters. Values are compared
through :func:`~backoffice.extraction.values.comparison_key`, so 483.6 and
"483,60" agree while an unreadable value never silently matches.

Observations split into *anchors* (the document's own structured
statements: XML, API, QR, embedded text, bank, human...) and *readings*
(OCR and VLM interpretations of pixels). The rules:

1. No observation, or none that is a usable value (it must parse for the
   field; an IBAN must pass mod-97): ``MISSING``. Unreadable readings are
   kept as candidates, so they still count as dissent, but they never
   settle a field and never win a vote.
2. One distinct value: ``AGREED`` with two or more voters, else ``SINGLE``.
3. The page itself shows several values (two accounts in a footer, two
   instalment dates): one structured source lists them, or two voters
   independently read the same several values, and every usable value is
   among them. ``CONFLICT`` of kind ``SEVERAL``, never resolvable: more
   reading cannot pick one, a person must (§19). A single reader seeing
   two values may still have misread one, so it stays resolvable.
4. Anchors that disagree with each other: ``CONFLICT`` of kind ``SOURCES``.
   The document contradicts itself; no further reading can settle it (§19).
5. Otherwise readings disagree. A value wins only with at least
   ``min_majority_votes`` voters, strictly more votes than all other values
   together, and (when anchors exist) only if it *is* the anchor value:
   readings may confirm an anchor, never overrule it (§19: OCR 483.60 vs
   QR 438.60 is a conflict). The result is ``MAJORITY``, dissent kept.
   Anything else is ``CONFLICT`` of kind ``READINGS``, marked
   ``resolvable`` when one more agreeing reading could still settle it.

Quality: CONFLICT is RED; every settled state is at most AMBER. This module
never produces GREEN: promotion needs independent evidence and belongs to
verification (§18, §57).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict

from backoffice.domain.models import CriticalField, ExtractionMethod, FieldObservation, Quality
from backoffice.extraction.fields import ranked
from backoffice.extraction.values import REFERENCE_FIELDS, TAX_ID_FIELDS, comparison_key, is_usable, typed_value

__all__ = [
    "DEFAULT_POLICY",
    "READING_METHODS",
    "Candidate",
    "ConflictKind",
    "Consensus",
    "ConsensusPolicy",
    "FieldConsensus",
    "FieldState",
    "build_consensus",
    "decide",
    "voter_id",
]

READING_METHODS: frozenset[ExtractionMethod] = frozenset({ExtractionMethod.OCR, ExtractionMethod.VLM})


class FieldState(str, Enum):
    AGREED = "agreed"
    SINGLE = "single"
    MAJORITY = "majority"
    CONFLICT = "conflict"
    MISSING = "missing"

    @property
    def settled(self) -> bool:
        return self in (FieldState.AGREED, FieldState.SINGLE, FieldState.MAJORITY)


class ConflictKind(str, Enum):
    SOURCES = "sources"
    READINGS = "readings"
    SEVERAL = "several"


@dataclass(frozen=True)
class ConsensusPolicy:
    """``allow_majority=False`` turns every disagreement into a conflict."""

    allow_majority: bool = True
    min_majority_votes: int = 2

    def __post_init__(self) -> None:
        if self.min_majority_votes < 2:
            raise ValueError("a majority needs at least two votes")


DEFAULT_POLICY = ConsensusPolicy()

_FROZEN = ConfigDict(frozen=True)


class Candidate(BaseModel):
    """One distinct value and who voted for it.

    ``usable`` is False for a value that does not parse for the field (or an
    IBAN failing mod-97): it is dissent, never an answer.
    """

    model_config = _FROZEN

    key: str
    value: Any
    voters: tuple[str, ...]
    methods: tuple[ExtractionMethod, ...]
    anchored: bool
    usable: bool = True


class FieldConsensus(BaseModel):
    model_config = _FROZEN

    field: CriticalField
    state: FieldState
    value: Any = None
    candidates: tuple[Candidate, ...] = ()
    observations: tuple[FieldObservation, ...] = ()
    conflict: ConflictKind | None = None
    resolvable: bool = False

    @property
    def settled(self) -> bool:
        return self.state.settled

    @property
    def quality(self) -> Quality | None:
        """RED for a conflict, AMBER when settled, None when missing. Never GREEN."""
        if self.state is FieldState.CONFLICT:
            return Quality.RED
        if self.state is FieldState.MISSING:
            return None
        return Quality.AMBER


def voter_id(observation: FieldObservation) -> str:
    return f"{observation.source}|{observation.method.value}"


def decide(
    field: CriticalField,
    observations: Iterable[FieldObservation],
    policy: ConsensusPolicy = DEFAULT_POLICY,
) -> FieldConsensus:
    """Consensus for one field (rules in the module docstring)."""
    obs = ranked(observations)
    if not obs:
        return FieldConsensus(field=field, state=FieldState.MISSING)
    candidates = _candidates(field, obs)
    base = {"field": field, "candidates": tuple(candidates), "observations": obs}
    if not any(c.usable for c in candidates):
        return FieldConsensus(**base, state=FieldState.MISSING)  # read, but nothing readable
    if len(candidates) == 1:
        only = candidates[0]
        state = FieldState.AGREED if len(only.voters) >= 2 else FieldState.SINGLE
        return FieldConsensus(**base, state=state, value=only.value)

    if _shows_several(field, obs):
        return FieldConsensus(**base, state=FieldState.CONFLICT, conflict=ConflictKind.SEVERAL)
    anchors = {c.key for c in candidates if c.anchored}
    if len(anchors) > 1:
        return FieldConsensus(**base, state=FieldState.CONFLICT, conflict=ConflictKind.SOURCES)

    votes = {c.key: len(c.voters) for c in candidates}
    total = sum(votes.values())
    winner = _winner(candidates, votes, total, anchors, policy)
    if winner is not None:
        return FieldConsensus(**base, state=FieldState.MAJORITY, value=winner.value)
    return FieldConsensus(
        **base,
        state=FieldState.CONFLICT,
        conflict=ConflictKind.READINGS,
        resolvable=_resolvable(candidates, votes, total, anchors, policy),
    )


def _candidates(field: CriticalField, observations: Sequence[FieldObservation]) -> list[Candidate]:
    """Distinct values in rank order of their strongest observation."""
    grouped: dict[str, list[FieldObservation]] = {}
    for observation in observations:
        grouped.setdefault(comparison_key(field, observation.value), []).append(observation)
    result = []
    for key, group in grouped.items():
        result.append(
            Candidate(
                key=key,
                value=_reported_value(field, group[0].value),
                voters=tuple(dict.fromkeys(voter_id(o) for o in group)),
                methods=tuple(dict.fromkeys(o.method for o in group)),
                anchored=any(o.method not in READING_METHODS for o in group),
                usable=is_usable(field, group[0].value),
            )
        )
    return result


def _shows_several(field: CriticalField, observations: Sequence[FieldObservation]) -> bool:
    """Rule 3: the page itself shows several values, and nothing else was read."""
    seen: dict[str, set[str]] = {}
    anchor_voters: set[str] = set()
    for observation in observations:
        if not is_usable(field, observation.value):
            continue
        voter = voter_id(observation)
        seen.setdefault(voter, set()).add(comparison_key(field, observation.value))
        if observation.method not in READING_METHODS:
            anchor_voters.add(voter)
    multi = [frozenset(keys) for keys in seen.values() if len(keys) >= 2]
    shown = [frozenset(seen[v]) for v in anchor_voters if len(seen[v]) >= 2]
    shown += [keys for keys in set(multi) if multi.count(keys) >= 2]
    if not shown:
        return False
    listed = frozenset().union(*shown)
    return all(keys <= listed for keys in seen.values())


def _reported_value(field: CriticalField, value: Any) -> Any:
    """Amounts, dates, currency and IBAN in canonical form; document numbers and
    tax ids as printed by the strongest source ("FT 2026/183", "PT509123457"),
    because their normalized comparison key drops meaningful spacing/prefixes."""
    if field in REFERENCE_FIELDS or field in TAX_ID_FIELDS:
        return " ".join(str(value).split())
    typed = typed_value(field, value)
    return typed if typed is not None else value


def _winner(
    candidates: list[Candidate],
    votes: Mapping[str, int],
    total: int,
    anchors: set[str],
    policy: ConsensusPolicy,
) -> Candidate | None:
    if not policy.allow_majority:
        return None
    leader = max(candidates, key=lambda c: votes[c.key])
    lead = votes[leader.key]
    if not leader.usable or lead < policy.min_majority_votes or lead <= total - lead:
        return None
    if anchors and anchors != {leader.key}:
        return None  # readings never overrule the document's own statement (§19)
    return leader


def _resolvable(
    candidates: Sequence[Candidate],
    votes: Mapping[str, int],
    total: int,
    anchors: set[str],
    policy: ConsensusPolicy,
) -> bool:
    """Could one more agreeing reading produce a valid majority for a usable value?"""
    if not policy.allow_majority:
        return False
    usable = {c.key for c in candidates if c.usable}
    keys = (anchors or set(votes)) & usable
    return any(
        votes[k] + 1 >= policy.min_majority_votes and votes[k] + 1 > total - votes[k] for k in keys
    )


@dataclass(frozen=True)
class Consensus:
    """Consensus for every observed or required critical field."""

    fields: Mapping[CriticalField, FieldConsensus]
    required: frozenset[CriticalField] = field(default_factory=frozenset)

    @property
    def missing(self) -> tuple[CriticalField, ...]:
        return tuple(f for f in CriticalField if f in self.required and self._state(f) is FieldState.MISSING)

    @property
    def conflicts(self) -> tuple[CriticalField, ...]:
        """Conflicting fields, required or not: a disputed IBAN matters either way."""
        return tuple(f for f in CriticalField if self._state(f) is FieldState.CONFLICT)

    @property
    def settled(self) -> bool:
        return not self.missing and not self.conflicts

    @property
    def improvable(self) -> bool:
        """Would another engine possibly help? (missing fields or resolvable conflicts)"""
        return bool(self.missing) or any(self.fields[f].resolvable for f in self.conflicts)

    @property
    def several(self) -> tuple[CriticalField, ...]:
        """Conflicts where the document itself shows several values."""
        return tuple(f for f in self.conflicts if self.fields[f].conflict is ConflictKind.SEVERAL)

    @property
    def unresolvable(self) -> tuple[CriticalField, ...]:
        """Conflicts no further reading can settle: the outcome is a human either way."""
        return tuple(f for f in self.conflicts if not self.fields[f].resolvable)

    def _state(self, f: CriticalField) -> FieldState:
        entry = self.fields.get(f)
        return entry.state if entry is not None else FieldState.MISSING


def build_consensus(
    observations: Mapping[CriticalField, Sequence[FieldObservation]],
    *,
    required: Collection[CriticalField] = (),
    policy: ConsensusPolicy = DEFAULT_POLICY,
) -> Consensus:
    required_set = frozenset(CriticalField(f) for f in required)
    wanted = [f for f in CriticalField if f in required_set or observations.get(f)]
    decided = {f: decide(f, observations.get(f, ()), policy) for f in wanted}
    return Consensus(fields=MappingProxyType(decided), required=required_set)
