"""Calendar month value type used by every closure calculation (§2, §27).

A month is closed in the business's own time zone: "September" for a
Portuguese company runs from 1 September 00:00 to 1 October 00:00 in
Europe/Lisbon, not in UTC. :meth:`Month.start` and :meth:`Month.end` take the
zone explicitly so nothing silently falls back to the server's clock.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone, tzinfo

from ._text import MONTH_NAMES, require_aware

__all__ = ["Month"]

_MONTH_TEXT = re.compile(r"^\s*(\d{4})-(\d{1,2})\s*$")


@dataclass(frozen=True, order=True, slots=True)
class Month:
    """A calendar month, ordered chronologically. ``str(Month(2026, 9)) == '2026-09'``."""

    year: int
    month: int

    def __post_init__(self) -> None:
        if isinstance(self.year, bool) or not isinstance(self.year, int):
            raise TypeError("year must be an int")
        if isinstance(self.month, bool) or not isinstance(self.month, int):
            raise TypeError("month must be an int")
        if not 1900 <= self.year <= 9998:
            raise ValueError(f"year out of range: {self.year}")
        if not 1 <= self.month <= 12:
            raise ValueError(f"month out of range: {self.month}")

    # ------------------------------------------------------------------ constructors

    @classmethod
    def of(cls, value: date | datetime, tz: tzinfo | None = None) -> Month:
        """Month containing ``value``. Datetimes must be aware and are read in ``tz``."""
        if isinstance(value, datetime):
            moment = require_aware(value, "value")
            local = moment.astimezone(tz) if tz is not None else moment
            return cls(local.year, local.month)
        return cls(value.year, value.month)

    @classmethod
    def parse(cls, text: str) -> Month:
        """Parse ``'2026-09'``."""
        m = _MONTH_TEXT.match(text)
        if not m:
            raise ValueError(f"not a month (expected YYYY-MM): {text!r}")
        return cls(int(m.group(1)), int(m.group(2)))

    # ------------------------------------------------------------------ calendar

    @property
    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def last_day(self) -> date:
        return date(self.year, self.month, self.days)

    @property
    def days(self) -> int:
        return calendar.monthrange(self.year, self.month)[1]

    @property
    def name(self) -> str:
        """Owner-facing month name, e.g. ``'September'``."""
        return MONTH_NAMES[self.month - 1]

    def label(self, today: date | None = None) -> str:
        """``'September'`` in the current year, ``'September 2025'`` otherwise."""
        if today is not None and today.year == self.year:
            return self.name
        return f"{self.name} {self.year}"

    def contains(self, day: date) -> bool:
        return (day.year, day.month) == (self.year, self.month)

    def next(self) -> Month:
        return Month(self.year + 1, 1) if self.month == 12 else Month(self.year, self.month + 1)

    def previous(self) -> Month:
        return Month(self.year - 1, 12) if self.month == 1 else Month(self.year, self.month - 1)

    # ------------------------------------------------------------------ instants

    def start(self, tz: tzinfo = timezone.utc) -> datetime:
        """First instant of the month in ``tz`` (inclusive)."""
        return datetime.combine(self.first_day, time.min, tzinfo=tz)

    def end(self, tz: tzinfo = timezone.utc) -> datetime:
        """First instant of the next month in ``tz`` (exclusive end)."""
        return self.next().start(tz)

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"
