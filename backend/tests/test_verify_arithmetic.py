"""Sum and rate checks, and ARITHMETIC observations (§18, §19)."""

from dataclasses import dataclass
from decimal import Decimal as D

import pytest

from backoffice.domain.models import CriticalField as F
from backoffice.domain.models import ExtractionMethod as M
from backoffice.domain.models import FieldObservation
from backoffice.verification import (
    RateFit,
    TaxBreakdown,
    TaxLine,
    allowed_rate_values,
    check_rate,
    check_sum,
    check_tax_lines,
    derive_gross,
    derive_observations,
    lineage,
    sum_tolerance,
)

PT_MAINLAND = (D("0.06"), D("0.13"), D("0.23"))
PP = "ev_1@pp-ocrv6"


def see(value, method=M.OCR, source=PP, confidence=0.6):
    return FieldObservation(value=value, source=source, method=method, confidence=confidence)


def qr(value):
    return see(value, M.QR, "ev_1", 0.98)


# --------------------------------------------------------------------------- sum


def test_section_19_sum():
    check = check_sum(D("393.17"), D("90.43"), D("483.60"))
    assert check.ok and check.expected == D("483.60") and check.difference == 0
    assert check.explain() == "The amounts add up."


def test_sum_tolerance_is_one_cent_per_tax_line():
    assert sum_tolerance() == sum_tolerance(0) == D("0.01")
    assert sum_tolerance(3) == D("0.03")
    assert check_sum(D("393.17"), D("90.43"), D("483.61")).ok
    assert not check_sum(D("393.17"), D("90.43"), D("483.62")).ok
    assert check_sum(D("393.17"), D("90.43"), D("483.63"), lines=3).ok


def test_sum_mismatch_explains_in_plain_words():
    check = check_sum(D("393.17"), D("90.43"), D("438.60"))
    assert not check.ok
    assert check.explain("EUR") == (
        "The amounts don't add up: €393.17 + €90.43 VAT is €483.60, but the total shows €438.60."
    )


def test_sum_with_other_charges_and_credit_note_signs():
    assert check_sum(D("100.00"), D("23.00"), D("123.40"), other_charges=D("0.40")).ok
    assert "other charges" in check_sum(D("1"), D("0"), D("5"), other_charges=D("1")).explain()
    assert check_sum(D("-100.00"), D("-23.00"), D("123.00")).ok  # magnitudes


def test_money_must_not_be_float():
    with pytest.raises(TypeError):
        check_sum(393.17, D("90.43"), D("483.60"))
    with pytest.raises(TypeError):
        TaxLine(net=D("1"), vat=0.23)
    with pytest.raises(ValueError):
        check_sum(D("Infinity"), D("0"), D("0"))


# --------------------------------------------------------------------------- rate


def test_rate_matches_an_allowed_rate():
    check = check_rate(D("393.17"), D("90.43"), PT_MAINLAND)
    assert check.fit is RateFit.MATCHES and check.rate == D("0.23")
    assert check.ratio == D("0.2300")
    assert check.explain() == "The VAT matches the 23% rate."


def test_rate_that_no_allowed_rate_explains():
    check = check_rate(D("100.00"), D("20.00"), PT_MAINLAND)
    assert check.fit is RateFit.UNEXPECTED and check.rate is None
    assert check.explain() == "The VAT doesn't match any expected rate."


def test_neighbouring_rates_are_told_apart_but_item_rounding_is_tolerated():
    assert check_rate(D("1000.00"), D("220.00"), [D("0.23")]).fit is RateFit.UNEXPECTED
    assert check_rate(D("1000.00"), D("220.00"), [D("0.22"), D("0.23")]).rate == D("0.22")
    assert check_rate(D("1000.00"), D("230.40"), [D("0.23")]).fit is RateFit.MATCHES  # per-item rounding
    assert check_rate(D("10.00"), D("2.31"), [D("0.23")]).fit is RateFit.MATCHES  # one cent
    assert check_rate(D("10.00"), D("2.33"), [D("0.23")]).fit is RateFit.UNEXPECTED


def test_blended_totals_are_mixed_only_when_allowed():
    # 100 at 23% + 100 at 6% -> 200 net, 29 VAT (14.5%)
    assert check_rate(D("200"), D("29"), PT_MAINLAND, allow_mixed=True).fit is RateFit.MIXED
    assert check_rate(D("200"), D("29"), PT_MAINLAND).fit is RateFit.UNEXPECTED
    assert check_rate(D("200"), D("60"), PT_MAINLAND, allow_mixed=True).fit is RateFit.UNEXPECTED  # 30%


def test_zero_vat_is_only_checked_when_zero_is_allowed():
    assert check_rate(D("100"), D("0"), PT_MAINLAND).fit is RateFit.NOT_CHECKED
    assert check_rate(D("100"), D("0"), (*PT_MAINLAND, D("0"))).fit is RateFit.MATCHES
    assert check_rate(D("0"), D("5"), PT_MAINLAND).fit is RateFit.UNEXPECTED
    assert check_rate(D("100"), D("23"), ()).fit is RateFit.NOT_CHECKED


def test_stated_rate_must_be_allowed_and_match():
    ok = check_rate(D("100"), D("23"), PT_MAINLAND, stated_rate=D("0.23"), line=1)
    assert ok.fit is RateFit.MATCHES and ok.explain() == "The VAT on line 1 matches the 23% rate."
    wrong_rate = check_rate(D("100"), D("20"), PT_MAINLAND, stated_rate=D("0.20"), line=2)
    assert wrong_rate.fit is RateFit.UNEXPECTED
    assert wrong_rate.explain() == "The 20% VAT rate on line 2 isn't one of the expected rates."
    mismatch = check_rate(D("100"), D("13"), PT_MAINLAND, stated_rate=D("0.23"))
    assert mismatch.fit is RateFit.UNEXPECTED and mismatch.explain() == "The VAT doesn't match its 23% rate."


@dataclass(frozen=True)
class _PackRate:  # the shape of a country pack's VATRate
    rate: D


def test_rates_can_come_from_a_country_pack():
    assert allowed_rate_values([_PackRate(D("0.23")), D("0.06"), _PackRate(D("0.23"))]) == (
        D("0.06"),
        D("0.23"),
    )
    assert check_rate(D("100"), D("6"), [_PackRate(D("0.06"))]).fit is RateFit.MATCHES
    with pytest.raises(ValueError):
        allowed_rate_values([D("23")])  # percent, not a fraction
    with pytest.raises(TypeError):
        allowed_rate_values([0.23])


def test_tax_lines_are_checked_one_by_one():
    lines = [
        TaxLine(D("100.00"), D("23.00"), D("0.23")),
        TaxLine(D("50.00"), D("3.00")),
        TaxLine(D("10"), D("0")),
    ]
    checks = check_tax_lines(lines, PT_MAINLAND)
    assert [c.fit for c in checks] == [RateFit.MATCHES, RateFit.MATCHES, RateFit.NOT_CHECKED]
    assert [c.line for c in checks] == [1, 2, 3] and checks[1].rate == D("0.06")


# --------------------------------------------------------------------------- derived observations


def test_gross_is_derived_within_one_channel():
    derived = derive_observations({F.NET_AMOUNT: [see("393,17")], F.VAT_AMOUNT: [see("90,43")]})
    (gross,) = derived[F.GROSS_AMOUNT]
    assert gross.method is M.ARITHMETIC and gross.value == D("483.60")
    assert gross.confidence == 0.6 and gross.source == "ev_1@pp-ocrv6"
    assert lineage(gross) == {"ocr:pp-ocrv6"}
    assert "net_amount+vat_amount=483.60" in gross.location


def test_numbers_from_different_channels_are_never_mixed():
    derived = derive_observations({F.NET_AMOUNT: [see("393,17")], F.VAT_AMOUNT: [qr(D("90.43"))]})
    assert derived == {}


def test_rounding_within_tolerance_reports_the_stated_value():
    observations = {
        F.NET_AMOUNT: [qr("393.17")],
        F.VAT_AMOUNT: [qr("90.43")],
        F.GROSS_AMOUNT: [see("483,61")],
    }
    (gross,) = derive_observations(observations)[F.GROSS_AMOUNT]
    assert gross.value == D("483.61") and "=483.60" in gross.location


def test_a_real_difference_keeps_the_computed_value():
    observations = {F.NET_AMOUNT: [qr("393.17")], F.VAT_AMOUNT: [qr("90.43")], F.GROSS_AMOUNT: [qr("438.60")]}
    (gross,) = derive_observations(observations)[F.GROSS_AMOUNT]
    assert gross.value == D("483.60")


def test_a_missing_net_or_vat_is_derived_from_the_other_two():
    derived = derive_observations({F.GROSS_AMOUNT: [qr("483.60")], F.VAT_AMOUNT: [qr("90.43")]})
    assert [o.value for o in derived[F.NET_AMOUNT]] == [D("393.17")]
    assert F.VAT_AMOUNT not in derived  # the channel states its VAT itself
    derived = derive_observations({F.GROSS_AMOUNT: [qr("483.60")], F.NET_AMOUNT: [qr("393.17")]})
    assert [o.value for o in derived[F.VAT_AMOUNT]] == [D("90.43")]


def test_other_charges_and_negative_results():
    derived = derive_observations(
        {F.NET_AMOUNT: [qr("100.00")], F.VAT_AMOUNT: [qr("23.00")]}, other_charges=D("0.40")
    )
    assert derived[F.GROSS_AMOUNT][0].value == D("123.40")
    assert "other_charges" in derived[F.GROSS_AMOUNT][0].location
    assert derive_observations({F.GROSS_AMOUNT: [qr("10.00")], F.VAT_AMOUNT: [qr("23.00")]}) == {}


def test_tax_lines_give_totals_in_their_own_channel():
    breakdown = TaxBreakdown(
        lines=(TaxLine(D("300.00"), D("69.00"), D("0.23")), TaxLine(D("93.17"), D("21.43"), D("0.23"))),
        source="ev_1",
        method=M.QR,
        confidence=0.98,
    )
    assert breakdown.total_net == D("393.17") and breakdown.total_vat == D("90.43")
    derived = derive_observations(
        {F.NET_AMOUNT: [see("393,17", M.EMBEDDED_TEXT, "ev_1")]}, breakdowns=[breakdown]
    )
    assert [o.value for o in derived[F.NET_AMOUNT]] == [D("393.17")]
    assert [o.value for o in derived[F.VAT_AMOUNT]] == [D("90.43")]
    assert [o.value for o in derived[F.GROSS_AMOUNT]] == [D("483.60")]
    assert all(lineage(o) == {"qr"} for obs in derived.values() for o in obs)
    with pytest.raises(ValueError):
        TaxBreakdown(lines=(), source="ev_1", method=M.QR, confidence=0.9)


def test_derive_gross_helper():
    gross = derive_gross(see("393,17"), see("90,43"), other_charges=D("1.00"))
    assert gross.value == D("484.60")
    assert derive_gross(see("n/a"), see("90,43")) is None


def test_keys_may_be_names_or_fields():
    derived = derive_observations({"net_amount": [see("393,17")], F.VAT_AMOUNT: [see("90,43")]})
    assert derived[F.GROSS_AMOUNT][0].value == D("483.60")
