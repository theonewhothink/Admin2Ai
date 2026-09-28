"""Month value type (§2, §27): calendar arithmetic and time-zone-aware boundaries."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backoffice.closure import Month

LISBON = ZoneInfo("Europe/Lisbon")


def test_parse_and_str_round_trip():
    m = Month.parse("2026-09")
    assert m == Month(2026, 9)
    assert str(m) == "2026-09"
    assert Month.parse(" 2026-1 ") == Month(2026, 1)


@pytest.mark.parametrize("text", ["2026/09", "26-09", "2026-13", "2026-00", "", "september"])
def test_parse_rejects_non_months(text):
    with pytest.raises(ValueError):
        Month.parse(text)


def test_constructor_validates_types_and_ranges():
    with pytest.raises(TypeError):
        Month(2026.0, 9)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Month(2026, True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Month(2026, 13)


def test_calendar_properties_including_leap_years():
    assert Month(2028, 2).days == 29
    assert Month(2026, 2).last_day == date(2026, 2, 28)
    assert Month(2026, 9).first_day == date(2026, 9, 1)
    assert Month(2026, 9).name == "September"


def test_next_and_previous_cross_year_boundaries():
    assert Month(2026, 12).next() == Month(2027, 1)
    assert Month(2027, 1).previous() == Month(2026, 12)


def test_months_are_ordered_and_hashable():
    assert Month(2026, 9) < Month(2026, 10) < Month(2027, 1)
    assert len({Month(2026, 9), Month(2026, 9)}) == 1


def test_of_reads_datetimes_in_the_business_time_zone():
    # 23:30 UTC on 30 September is already 1 October in Lisbon (UTC+1 in summer).
    moment = datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc)
    assert Month.of(moment) == Month(2026, 9)
    assert Month.of(moment, LISBON) == Month(2026, 10)
    assert Month.of(date(2026, 9, 30)) == Month(2026, 9)


def test_of_refuses_naive_datetimes():
    with pytest.raises(ValueError):
        Month.of(datetime(2026, 9, 1, 12, 0))


def test_start_and_end_follow_local_offsets_across_dst():
    october = Month(2026, 10)  # DST ends on 25 October 2026 in Lisbon
    start, end = october.start(LISBON), october.end(LISBON)
    assert start.utcoffset() == timedelta(hours=1)
    assert end.utcoffset() == timedelta(0)
    elapsed = end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    assert elapsed == timedelta(days=31, hours=1)
    assert Month(2026, 9).end(LISBON) == Month(2026, 10).start(LISBON)


def test_label_adds_the_year_only_when_needed():
    assert Month(2026, 9).label(date(2026, 10, 3)) == "September"
    assert Month(2025, 12).label(date(2026, 1, 3)) == "December 2025"
    assert Month(2026, 9).label() == "September 2026"


def test_contains():
    assert Month(2026, 9).contains(date(2026, 9, 30))
    assert not Month(2026, 9).contains(date(2025, 9, 30))
