"""Small accumulator shared by the Stage 0 extractors."""

from __future__ import annotations

from backoffice.domain.models import (
    BoundingBox,
    CriticalField,
    DocumentType,
    ExtractionMethod,
    FieldObservation,
)

from .fields import Stage0Result


class Collector:
    """Collects observations, extras and notes for one Stage0Result."""

    def __init__(
        self, kind: str, source: str, method: ExtractionMethod, confidence: float
    ) -> None:
        self.kind = kind
        self.source = source
        self.method = method
        self.confidence = confidence
        self._pairs: list[tuple[CriticalField, FieldObservation]] = []
        self._extras: dict[str, str] = {}
        self._notes: list[str] = []

    def add(
        self,
        field: CriticalField,
        value: object,
        location: BoundingBox | str | None,
        *,
        confidence: float | None = None,
        method: ExtractionMethod | None = None,
    ) -> None:
        """Record one observation; blank values are ignored."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return
        if isinstance(value, str):
            value = value.strip()
        self._pairs.append(
            (
                field,
                FieldObservation(
                    value=value,
                    source=self.source,
                    method=method or self.method,
                    confidence=self.confidence if confidence is None else confidence,
                    location=location,
                ),
            )
        )

    def extra(self, key: str, value: object) -> None:
        if value is None:
            return
        text = " ".join(str(value).split())
        if text and key not in self._extras:
            self._extras[key] = text

    def note(self, code: str) -> None:
        self._notes.append(code)

    @property
    def count(self) -> int:
        return len(self._pairs)

    def result(self, doc_type: DocumentType | None = None) -> Stage0Result:
        return Stage0Result.build(
            self.kind,
            self.source,
            self._pairs,
            doc_type=doc_type,
            extras=self._extras,
            notes=self._notes,
        )
