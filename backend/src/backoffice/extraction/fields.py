"""Field observations shared by Stage 0, OCR routing and benchmarks (§13, §18).

A :data:`FieldMap` maps each critical field to the observations of it. Every
observation keeps value, source, method, confidence and location (§18);
ranking follows the domain ``METHOD_RANK``, so structured evidence outranks
OCR (§13).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from backoffice.domain.models import (
    METHOD_RANK,
    CriticalField,
    DocumentType,
    ExtractionMethod,
    FieldObservation,
)

__all__ = [
    "FieldExtractor",
    "FieldMap",
    "Stage0Result",
    "StructuredDataError",
    "best",
    "combine",
    "from_named_observations",
    "group_named",
    "merge_field_maps",
    "rank_key",
    "ranked",
]

FieldMap = Mapping[CriticalField, Sequence[FieldObservation]]


class StructuredDataError(ValueError):
    """Structured evidence could not be read. ``code`` is a stable machine code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


@runtime_checkable
class FieldExtractor(Protocol):
    """Finds critical fields in plain text (OCR output or embedded PDF text).

    Implementations set ``source`` and ``method`` on every observation they
    return. A country text extractor is adapted with
    :func:`from_named_observations`.
    """

    def __call__(self, text: str, source: str, method: ExtractionMethod) -> FieldMap: ...


def rank_key(observation: FieldObservation) -> tuple[int, float]:
    """Sort key: method rank first (§13), then confidence."""
    return METHOD_RANK.get(observation.method, 0), observation.confidence


def ranked(observations: Iterable[FieldObservation]) -> tuple[FieldObservation, ...]:
    """Observations strongest first; ties keep their input order."""
    return tuple(sorted(observations, key=rank_key, reverse=True))


def best(observations: Iterable[FieldObservation]) -> FieldObservation | None:
    """The strongest observation, or None."""
    top = ranked(observations)
    return top[0] if top else None


def group_named(items: Iterable[object]) -> dict[CriticalField, tuple[FieldObservation, ...]]:
    """Group observations that name their field.

    Accepts objects with a ``field`` attribute (such as a country pack's
    named observations) or ``(field, observation)`` pairs.
    """
    grouped: dict[CriticalField, list[FieldObservation]] = {}
    for item in items:
        if isinstance(item, tuple) and len(item) == 2:
            name, observation = item
        else:
            name, observation = item.field, item  # type: ignore[attr-defined]
        if not isinstance(observation, FieldObservation):
            raise TypeError(f"not a FieldObservation: {type(observation).__name__}")
        grouped.setdefault(CriticalField(name), []).append(observation)
    return {f: tuple(obs) for f, obs in grouped.items()}


def from_named_observations(
    extract: Callable[..., Iterable[object]],
) -> FieldExtractor:
    """Adapt ``extract(text, source, *, method=...)`` returning named observations.

    Fits a country pack's ``extract_text_fields``, so the Portuguese text
    extractor plugs into OCR routing without this package importing it.
    """

    def extractor(text: str, source: str, method: ExtractionMethod) -> FieldMap:
        return group_named(extract(text, source, method=method))

    return extractor


def merge_field_maps(*maps: FieldMap) -> dict[CriticalField, tuple[FieldObservation, ...]]:
    """All observations per field from several maps, strongest first."""
    merged: dict[CriticalField, list[FieldObservation]] = {}
    for fmap in maps:
        for name, observations in fmap.items():
            merged.setdefault(CriticalField(name), []).extend(observations)
    return {f: ranked(obs) for f, obs in merged.items()}


@dataclass(frozen=True)
class Stage0Result:
    """What one structured extractor found in one piece of evidence (§13).

    ``kind`` names the extractor ("ubl_invoice", "cii", "html_jsonld", ...).
    ``extras`` carries useful non-critical values (supplier name, order
    number, amount payable). ``notes`` are stable machine codes describing
    what was skipped or looked wrong; they are not owner-facing.
    """

    kind: str
    source: str
    fields: Mapping[CriticalField, tuple[FieldObservation, ...]]
    doc_type: DocumentType | None = None
    extras: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    notes: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        kind: str,
        source: str,
        pairs: Iterable[tuple[CriticalField, FieldObservation]],
        *,
        doc_type: DocumentType | None = None,
        extras: Mapping[str, str] | None = None,
        notes: Iterable[str] = (),
    ) -> Stage0Result:
        grouped = {f: ranked(obs) for f, obs in group_named(pairs).items()}
        return cls(
            kind=kind,
            source=source,
            fields=MappingProxyType(grouped),
            doc_type=doc_type,
            extras=MappingProxyType(dict(extras or {})),
            notes=tuple(dict.fromkeys(notes)),
        )

    @property
    def empty(self) -> bool:
        return not self.fields

    def missing(self, required: Collection[CriticalField]) -> frozenset[CriticalField]:
        """Required fields this result says nothing about."""
        return frozenset(f for f in required if not self.fields.get(f))

    def covers(self, required: Collection[CriticalField]) -> bool:
        return not self.missing(required)


def combine(results: Iterable[Stage0Result]) -> dict[CriticalField, tuple[FieldObservation, ...]]:
    """One field map from several Stage 0 results (input for OCR routing)."""
    return merge_field_maps(*(r.fields for r in results))
