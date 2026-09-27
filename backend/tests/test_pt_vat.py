"""Portuguese VAT rate table and plausibility (§50 regional VAT, §18 arithmetic)."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backoffice.countries.base import VATBucket
from backoffice.countries.pt import (
    PT_VAT_RATES,
    PTRegion,
    RateCheck,
    RateDataUnavailable,
    check_rate,
    is_plausible_rate,
    match_rate,
    rate_for,
    rates_on,
)
from backoffice.countries.pt.vat import coverage_start, expected_vat

D = Decimal
TODAY = date(2026, 9, 27)


def test_section_19_is_plausible_at_23_percent():
    assert is_plausible_rate(D("393.17"), D("90.43"), "PT", TODAY)
    entry = match_rate(D("393.17"), D("90.43"), "PT", TODAY)
    assert entry is not None and entry.bucket == VATBucket.NORMAL and entry.rate == D("0.23")


def test_current_rates_by_region():
    def rates(region):
        return {r.bucket: r.rate for r in rates_on(TODAY, region)}

    assert rates("PT") == {VATBucket.REDUCED: D("0.06"), VATBucket.INTERMEDIATE: D("0.13"),
                           VATBucket.NORMAL: D("0.23")}
    assert rates("PT-AC") == {VATBucket.REDUCED: D("0.04"), VATBucket.INTERMEDIATE: D("0.09"),
                              VATBucket.NORMAL: D("0.16")}
    assert rates("PT-MA") == {VATBucket.REDUCED: D("0.04"), VATBucket.INTERMEDIATE: D("0.12"),
                              VATBucket.NORMAL: D("0.22")}
    assert len(rates_on(TODAY)) == 9


def test_azores_normal_rate_change_on_2021_07_01():
    assert rate_for("PT-AC", VATBucket.NORMAL, date(2021, 6, 30)) == D("0.18")
    assert rate_for("PT-AC", VATBucket.NORMAL, date(2021, 7, 1)) == D("0.16")
    assert is_plausible_rate(D("100.00"), D("18.00"), "PT-AC", date(2020, 5, 1))
    assert not is_plausible_rate(D("100.00"), D("18.00"), "PT-AC", date(2022, 5, 1))


def test_madeira_reduced_rate_change_on_2024_10_01():
    before, after = date(2024, 9, 30), date(2024, 10, 1)
    red = VATBucket.REDUCED
    assert is_plausible_rate(D("100.00"), D("5.00"), "PT-MA", before, bucket=red)
    assert not is_plausible_rate(D("100.00"), D("4.00"), "PT-MA", before, bucket=red)
    assert is_plausible_rate(D("100.00"), D("4.00"), "PT-MA", after, bucket=red)
    assert not is_plausible_rate(D("100.00"), D("5.00"), "PT-MA", after, bucket=red)


def test_bucket_restricts_the_match():
    assert is_plausible_rate(D("100.00"), D("13.00"), "PT", TODAY)
    assert not is_plausible_rate(D("100.00"), D("13.00"), "PT", TODAY, bucket=VATBucket.NORMAL)


def test_mixed_rate_totals_are_not_a_single_rate():
    # 100 @ 6% + 100 @ 23% = 29.00 on 200.00 (14.5%): not one rate.
    assert not is_plausible_rate(D("200.00"), D("29.00"), "PT", TODAY)


def test_credit_notes_and_signs():
    assert is_plausible_rate(D("-393.17"), D("-90.43"), "PT", TODAY)
    assert not is_plausible_rate(D("-393.17"), D("90.43"), "PT", TODAY)


def test_zero_vat_needs_explicit_exemption():
    assert not is_plausible_rate(D("100.00"), D("0.00"), "PT", TODAY)
    assert is_plausible_rate(D("100.00"), D("0.00"), "PT", TODAY, allow_zero=True)
    assert is_plausible_rate(D("0.00"), D("0.00"), "PT", TODAY)
    assert not is_plausible_rate(D("0.00"), D("5.00"), "PT", TODAY)


def test_rounding_tolerance_separates_neighbouring_rates():
    assert is_plausible_rate(D("1000.00"), D("230.50"), "PT", TODAY)  # per-line rounding drift
    assert not is_plausible_rate(D("1000.00"), D("250.00"), "PT", TODAY)
    assert not is_plausible_rate(D("100.00"), D("7.00"), "PT-AC", TODAY)  # between 4% and 9%


def test_dates_outside_the_table_are_unknown():
    old = date(2009, 6, 1)
    assert check_rate(D("100.00"), D("20.00"), "PT", old) == RateCheck.UNKNOWN
    with pytest.raises(RateDataUnavailable):
        is_plausible_rate(D("100.00"), D("20.00"), "PT", old)
    assert check_rate(D("100.00"), D("18.00"), "PT-AC", date(2014, 12, 31)) == RateCheck.UNKNOWN


def test_region_parsing():
    assert PTRegion.parse("pt-ac") == PTRegion.AZORES
    assert PTRegion.parse("PT-20") == PTRegion.AZORES
    assert PTRegion.parse("PT-30") == PTRegion.MADEIRA
    assert PTRegion.parse(PTRegion.MAINLAND) == PTRegion.MAINLAND
    with pytest.raises(ValueError):
        PTRegion.parse("ES")
    with pytest.raises(ValueError):
        rates_on(TODAY, "PT-XX")


def test_expected_vat_rounds_half_up():
    assert expected_vat(D("0.50"), D("0.23")) == D("0.12")  # 0.115 -> 0.12
    assert expected_vat(D("393.17"), D("0.23")) == D("90.43")


def test_table_entries_are_documented_and_decimal():
    for entry in PT_VAT_RATES:
        assert isinstance(entry.rate, Decimal) and D("0") < entry.rate < D("1")
        assert entry.source and entry.verified_as_of >= date(2026, 1, 1)
        assert entry.valid_to is None or entry.valid_to >= entry.valid_from


def test_table_has_no_overlaps_or_gaps_per_region_and_bucket():
    groups = defaultdict(list)
    for entry in PT_VAT_RATES:
        groups[(entry.region, entry.bucket)].append(entry)
    for key, entries in groups.items():
        entries.sort(key=lambda e: e.valid_from)
        assert entries[-1].valid_to is None, key  # still in force
        for prev, nxt in zip(entries, entries[1:]):
            assert prev.valid_to is not None
            assert prev.valid_to + timedelta(days=1) == nxt.valid_from, key
    for region in PTRegion:
        start = coverage_start(region)
        assert len(rates_on(start, region)) == 3
        assert rates_on(start - timedelta(days=1), region) == ()
