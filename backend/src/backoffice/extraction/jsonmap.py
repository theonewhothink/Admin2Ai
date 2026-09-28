"""JSON API payloads as Stage 0 evidence (§10, §13).

Supplier portals and accounting platforms answer with JSON. Each connector
declares where its critical fields live with key paths; the mapper reads
them without any per-vendor code:

    mapper = JsonFieldMapper({
        CriticalField.INVOICE_NUMBER: "data.number",
        CriticalField.GROSS_AMOUNT: FieldPath("data.total", minor_units=True),
        CriticalField.ISSUE_DATE: FieldPath("data.created", date_format="unix"),
    })
    result = mapper.extract(response_bytes, source=evidence_id)

Paths use dots and ``[n]`` indexes ("lines[0].total"); a leading ``$.`` is
allowed. Several paths for one field each yield an observation, so values
that disagree surface as a conflict instead of one being picked (§19).
Numbers are parsed as Decimal, never float.

Dates and time zones: a calendar date depends on where it is read. Epoch
timestamps and zoned date-times ("2026-09-30T23:30:00Z") are converted to
``FieldPath.timezone`` (the issuer's IANA zone, e.g. "Europe/Lisbon") before
the date is taken; without it, epoch seconds are read in UTC and zoned
date-times keep the date as written. 23:30 UTC on 30 September is already
1 October in Lisbon, which decides the month an invoice closes in.

Payloads are bounded (``MAX_JSON_BYTES``) and hostile nesting depth is a
typed :class:`StructuredDataError`, never a crash (§52).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone, tzinfo
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backoffice.domain.models import CriticalField, ExtractionMethod

from ._collect import Collector
from .fields import Stage0Result, StructuredDataError
from .values import AMOUNT_FIELDS, DATE_FIELDS, parse_amount, parse_date

__all__ = ["API_CONFIDENCE", "MAX_JSON_BYTES", "FieldPath", "JsonFieldMapper", "resolve_path"]

API_CONFIDENCE = 0.97
MAX_JSON_BYTES = 10 * 1024 * 1024

_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


@dataclass(frozen=True)
class FieldPath:
    """Where one field lives and how to read it.

    ``minor_units``: the amount is an integer count of minor units
    (48360 -> 483.60 with ``exponent=2``).
    ``date_format``: None for ISO 8601, ``"unix"`` for epoch seconds, or a
    :func:`datetime.strptime` pattern.
    ``timezone``: IANA zone the date is read in (see the module docstring);
    None reads epoch seconds in UTC and zoned date-times as written.
    """

    path: str
    minor_units: bool = False
    exponent: int = 2
    date_format: str | None = None
    timezone: str | None = None

    def __post_init__(self) -> None:
        if not _tokens(self.path):
            raise ValueError(f"empty key path: {self.path!r}")
        if not 0 <= self.exponent <= 6:
            raise ValueError("exponent must be between 0 and 6")
        if self.timezone is not None:
            _zone(self.timezone)  # fail fast on an unknown zone

    @property
    def zone(self) -> tzinfo | None:
        return _zone(self.timezone) if self.timezone is not None else None


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone: {name!r}") from None


def _tokens(path: str) -> list[str | int]:
    body = path[2:] if path.startswith("$.") else path.lstrip("$")
    tokens: list[str | int] = []
    pos = 0
    for m in _TOKEN.finditer(body):
        gap = body[pos : m.start()]
        if gap not in ("", "."):
            raise ValueError(f"invalid key path: {path!r}")
        tokens.append(m[1] if m[1] is not None else int(m[2]))
        pos = m.end()
    if body[pos:] not in ("",):
        raise ValueError(f"invalid key path: {path!r}")
    return tokens


def resolve_path(document: Any, path: str) -> Any:
    """The value at ``path``, or None when any step is absent."""
    node = document
    for token in _tokens(path):
        if isinstance(token, int):
            if not isinstance(node, list) or token >= len(node):
                return None
            node = node[token]
        else:
            if not isinstance(node, Mapping) or token not in node:
                return None
            node = node[token]
    return node


class JsonFieldMapper:
    """Maps a JSON payload to critical-field observations with key paths."""

    def __init__(
        self,
        fields: Mapping[CriticalField, str | FieldPath | Sequence[str | FieldPath]],
        *,
        extras: Mapping[str, str] | None = None,
        kind: str = "json_api",
        confidence: float = API_CONFIDENCE,
    ) -> None:
        self._fields = {
            CriticalField(f): tuple(_as_path(p) for p in _as_list(spec)) for f, spec in fields.items()
        }
        self._extras = dict(extras or {})
        for path in self._extras.values():
            _tokens(path)  # fail fast on a malformed extras path
        self._kind = kind
        self._confidence = confidence

    def extract(self, payload: bytes | str | Mapping[str, Any] | Sequence[Any], *, source: str) -> Stage0Result:
        document = _load(payload)
        c = Collector(self._kind, source, ExtractionMethod.API, self._confidence)
        for field, paths in self._fields.items():
            for spec in paths:
                raw = resolve_path(document, spec.path)
                if raw is None:
                    continue
                value = _convert(field, raw, spec)
                if value is None:
                    c.note(f"unreadable:{field.value}:{spec.path}")
                    continue
                c.add(field, value, f"$.{spec.path.removeprefix('$.')}")
        for key, path in self._extras.items():
            value = resolve_path(document, path)
            if value is not None and not isinstance(value, (Mapping, list)):
                c.extra(key, value)
        return c.result()


def _as_list(spec: str | FieldPath | Sequence[str | FieldPath]) -> Sequence[str | FieldPath]:
    return [spec] if isinstance(spec, (str, FieldPath)) else spec


def _as_path(spec: str | FieldPath) -> FieldPath:
    return spec if isinstance(spec, FieldPath) else FieldPath(spec)


def _load(payload: bytes | str | Mapping[str, Any] | Sequence[Any]) -> Any:
    if isinstance(payload, (bytes, str)):
        size = len(payload) if isinstance(payload, bytes) else len(payload.encode("utf-8"))
        if size > MAX_JSON_BYTES:
            raise StructuredDataError("json_too_large", f"{size} bytes")
        try:
            return json.loads(payload, parse_float=Decimal)
        except RecursionError:
            raise StructuredDataError("json_unreadable", "nested too deeply") from None
        except ValueError as exc:
            raise StructuredDataError("json_unreadable", str(exc.args[0])[:80]) from None
    return payload


def _convert(field: CriticalField, raw: Any, spec: FieldPath) -> Any:
    if field in AMOUNT_FIELDS:
        return _amount(raw, spec)
    if field in DATE_FIELDS:
        return _date(raw, spec)
    if isinstance(raw, (Mapping, list, bool)):
        return None
    text = str(raw).strip()
    if field is CriticalField.CURRENCY:
        return text.upper() if re.fullmatch(r"[A-Za-z]{3}", text) else None
    return text or None


def _amount(raw: Any, spec: FieldPath) -> Decimal | None:
    if not spec.minor_units:
        return parse_amount(raw)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) or (isinstance(raw, str) and re.fullmatch(r"-?\d+", raw.strip())):
        return Decimal(int(raw)).scaleb(-spec.exponent)
    if isinstance(raw, Decimal) and raw == raw.to_integral_value():
        return raw.scaleb(-spec.exponent)
    return None  # minor units must be whole numbers


def _date(raw: Any, spec: FieldPath) -> date | None:
    zone = spec.zone
    if spec.date_format == "unix":
        if isinstance(raw, bool) or not isinstance(raw, (int, Decimal, str)):
            return None
        try:
            seconds = int(Decimal(str(raw)))
            return datetime.fromtimestamp(seconds, tz=zone or timezone.utc).date()
        except (ArithmeticError, ValueError, OSError):
            return None
    if spec.date_format:
        try:
            return _local_date(datetime.strptime(str(raw).strip(), spec.date_format), zone)
        except ValueError:
            return None
    if zone is not None and isinstance(raw, str) and ("T" in raw or " " in raw.strip()):
        try:
            return _local_date(datetime.fromisoformat(raw.strip()), zone)
        except ValueError:
            pass
    return parse_date(raw)


def _local_date(moment: datetime, zone: tzinfo | None) -> date:
    """The calendar date in ``zone`` of a zoned moment; naive moments keep their date."""
    if zone is not None and moment.tzinfo is not None:
        return moment.astimezone(zone).date()
    return moment.date()
