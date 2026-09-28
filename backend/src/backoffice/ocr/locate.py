"""Attach bounding boxes to OCR field observations (§18).

Text extractors see plain text, so their observations arrive without a
location. The value is looked up in the engine's boxed lines under its
usual printed forms ("1492.30", "1.492,30", "1 492,30"; "2026-09-18",
"18/09/2026"...). A box is attached only when exactly one line matches:
an ambiguous location is left empty rather than guessed.

The engine's own score for that line then caps the observation's
confidence, so a value read from a shaky line is never stored with the
extractor's flat confidence (§18). Confidence only goes down here.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from backoffice.domain.models import CriticalField, FieldObservation
from backoffice.extraction.values import typed_value

from .base import OCRResult

__all__ = ["locate", "renderings"]


def _amount_forms(amount: Decimal) -> set[str]:
    value = abs(amount)
    cents = value.quantize(Decimal("0.01"))
    if cents == value:  # print with two decimals unless more are significant
        value = cents
    plain = format(value, "f")
    whole, _, frac = plain.partition(".")
    grouped = f"{int(whole):,}"
    forms = {plain, plain.replace(".", ",")}
    if frac:
        forms |= {
            f"{grouped}.{frac}",
            f"{grouped.replace(',', '.')},{frac}",
            f"{grouped.replace(',', ' ')},{frac}",
            f"{grouped.replace(',', ' ')}.{frac}",
        }
    return forms


def _date_forms(day: date) -> set[str]:
    d, m, y = f"{day.day:02d}", f"{day.month:02d}", str(day.year)
    return {day.isoformat(), f"{d}/{m}/{y}", f"{d}-{m}-{y}", f"{d}.{m}.{y}", f"{y}/{m}/{d}"}


def renderings(field: CriticalField, value: object) -> set[str]:
    """Printed forms under which ``value`` may appear on the page."""
    typed = typed_value(field, value)
    forms = {str(value).strip()} if value is not None else set()
    if isinstance(typed, Decimal):
        forms |= _amount_forms(typed)
    elif isinstance(typed, date):
        forms |= _date_forms(typed)
    elif isinstance(typed, str):
        forms.add(typed)
    return {f for f in forms if f}


def locate(observation: FieldObservation, field: CriticalField, result: OCRResult) -> FieldObservation:
    """``observation`` with the box (and capped confidence) of the single line showing its value."""
    if observation.location is not None:
        return observation
    patterns = [
        re.compile(rf"(?<![0-9A-Za-z]){re.escape(form)}(?![0-9A-Za-z])", re.IGNORECASE)
        for form in sorted(renderings(field, observation.value), key=len, reverse=True)
    ]
    hits = [line for line in result.lines if line.bbox is not None and any(p.search(line.text) for p in patterns)]
    if len(hits) != 1:
        return observation
    line = hits[0]
    update: dict[str, object] = {"location": line.bbox}
    if line.confidence is not None and line.confidence < observation.confidence:
        update["confidence"] = line.confidence
    return observation.model_copy(update=update)
