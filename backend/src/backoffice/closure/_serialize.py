"""JSON-safe conversion for closure outputs (API responses, package manifest).

Money stays exact: Decimals become strings ("405.00"), never floats; a bare
float in a closure output is refused. Dates and datetimes become ISO 8601,
enums their value, months ``'2026-09'``; domain (pydantic) models use their
own JSON serialisation.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

from .period import Month

__all__ = ["to_jsonable"]


def to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses, pydantic models, Decimals, dates and enums."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        raise TypeError("floats are not allowed in closure outputs; use Decimal")
    if isinstance(value, Decimal):
        return f"{value:f}"
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, Month):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, BaseModel):
        # Domain models serialise themselves (Decimal -> str); their non-money floats
        # (e.g. OCR confidence) are legitimate and left to pydantic.
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(to_jsonable(k)): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [to_jsonable(v) for v in value]
        return sorted(items, key=repr) if isinstance(value, (set, frozenset)) else items
    if isinstance(value, bytes):
        raise TypeError("bytes are not JSON; publish a hash or a path instead")
    raise TypeError(f"cannot convert {type(value).__name__} to JSON")
