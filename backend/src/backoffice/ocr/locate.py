"""Attach bounding boxes to OCR field observations (§18).

Text extractors see plain text, so their observations arrive without a
location. The value is looked up in the engine's boxed lines under its
usual printed forms ("1492.30", "1.492,30", "1 492,30"; "2026-09-18",
"18/09/2026"...). A box is attached only when exactly one line matches:
an ambiguous location is left empty rather than guessed.

An extractor that already names the line it read ("text:line 11", as the
Portugal pack does) gets that line's box, provided the line really shows
the value; any other location is kept as it is.

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

from .base import OCRLine, OCRResult

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


_TEXT_LINE = re.compile(r"text:line (\d+)")


def _line_at(result: OCRResult, number: int) -> OCRLine | None:
    """The engine line behind line ``number`` (1-based) of ``result.full_text``, if there is one."""
    flat: list[OCRLine | None] = []
    for page in result.pages:
        if not page.text:
            continue
        if flat:
            flat.append(None)  # full_text puts a blank line between pages
        flat.extend(page.lines if page.lines else [None] * len(page.text.split("\n")))
    return flat[number - 1] if 0 < number <= len(flat) else None


def locate(observation: FieldObservation, field: CriticalField, result: OCRResult) -> FieldObservation:
    """``observation`` with the box (and capped confidence) of the single line showing its value."""
    patterns = [
        re.compile(rf"(?<![0-9A-Za-z]){re.escape(form)}(?![0-9A-Za-z])", re.IGNORECASE)
        for form in sorted(renderings(field, observation.value), key=len, reverse=True)
    ]
    if isinstance(observation.location, str):
        named = _TEXT_LINE.fullmatch(observation.location)
        line = _line_at(result, int(named[1])) if named else None
        if line is None or line.bbox is None or not any(p.search(line.text) for p in patterns):
            return observation
        return _boxed(observation, line)
    if observation.location is not None:
        return observation
    hits = [line for line in result.lines if line.bbox is not None and any(p.search(line.text) for p in patterns)]
    if len(hits) != 1:
        return observation
    return _boxed(observation, hits[0])


def _boxed(observation: FieldObservation, line: OCRLine) -> FieldObservation:
    update: dict[str, object] = {"location": line.bbox}
    if line.confidence is not None and line.confidence < observation.confidence:
        update["confidence"] = line.confidence
    return observation.model_copy(update=update)
