"""Shared learning building blocks: keys, plain formatting, robust stats, questions."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backoffice.learning.keys import (
    counterparty_key,
    display_name,
    fold,
    normalize_tax_id,
    same_tax_id,
    tax_id_country,
)
from backoffice.learning.plain import (
    card_mask,
    count_phrase,
    day_month,
    format_money,
    join_and,
    ordinal,
)
from backoffice.learning.questions import (
    Answer,
    OptionKind,
    Question,
    QuestionKind,
    QuestionOption,
    SubjectFacts,
    describe_subject,
)
from backoffice.learning.stats import mad, median, modified_z

# --------------------------------------------------------------------------- keys


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("VODAFONE PORTUGAL 123456", "vodafone portugal"),
        ("Vodafone Portugal", "vodafone portugal"),
        ("PAYPAL *ADOBE 402-935-9800", "adobe"),
        ("ADOBE *CREATIVE CLOUD", "adobe"),
        ("UBER *TRIP HELP.UBER.COM", "uber"),
        ("Hazel Tree, Lda.", "hazel tree"),
        ("Oak Holdings S.A.", "oak holdings"),
        ("Café Lisboa Unipessoal Lda", "cafe lisboa"),
        ("SQ *PADARIA 22", "padaria"),
    ],
)
def test_counterparty_key_normalizes_descriptors(raw: str, key: str) -> None:
    assert counterparty_key(raw) == key


def test_counterparty_key_empty_and_legal_form_only() -> None:
    assert counterparty_key(None) is None
    assert counterparty_key("   ") is None
    assert counterparty_key("123 456") is None
    # A legal form alone is kept rather than producing an empty key.
    assert counterparty_key("SA") == "sa"
    # Ordinary words that double as legal forms elsewhere are kept.
    assert counterparty_key("Lisbon Hotel & Spa") == "lisbon hotel spa"


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("Vodafone Portugal - Comunicações Pessoais, S.A.", "Vodafone Portugal"),
        ("IKEA ALFRAGIDE 1234", "IKEA Alfragide"),
        ("PAYPAL *ADOBE", "Adobe"),
        ("Hazel Tree, Lda.", "Hazel Tree"),
        ("EDP COMERCIAL", "EDP Comercial"),
        (None, "This supplier"),
    ],
)
def test_display_name(raw: str | None, shown: str) -> None:
    assert display_name(raw) == shown


def test_fold_removes_accents_and_case() -> None:
    assert fold("  Alteração   de IBAN ") == "alteracao de iban"


def test_tax_ids() -> None:
    assert normalize_tax_id("PT 509 123 456") == "509123456"
    assert normalize_tax_id("ESB12345678") == "B12345678"
    assert normalize_tax_id("B12345678") == "B12345678"
    assert normalize_tax_id("") is None
    assert tax_id_country("PT509123456") == "PT"
    assert tax_id_country("EL123456789") == "GR"
    assert tax_id_country("509123456") is None
    assert same_tax_id("PT509123456", "509 123 456")
    assert not same_tax_id("PT509123456", "ES509123456")
    assert not same_tax_id("509123456", "509123457")
    assert not same_tax_id(None, "509123456")


# --------------------------------------------------------------------------- plain


def test_format_money() -> None:
    assert format_money(Decimal("1492.3")) == "€1,492.30"
    assert format_money(Decimal("-12"), "GBP") == "−£12.00"
    assert format_money(Decimal("5"), "CHF") == "CHF 5.00"
    assert format_money(Decimal("0.005")) == "€0.01"  # half-up
    with pytest.raises(TypeError):
        format_money(1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        format_money(Decimal("1"), "euro")


@pytest.mark.parametrize(
    ("n", "text"),
    [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"), (13, "13th"),
     (21, "21st"), (22, "22nd"), (23, "23rd"), (26, "26th"), (31, "31st"), (111, "111th")],
)  # fmt: skip
def test_ordinal(n: int, text: str) -> None:
    assert ordinal(n) == text


def test_day_month_card_join_count() -> None:
    assert day_month(date(2026, 9, 18)) == "18 September"
    assert day_month(date(2025, 9, 18), date(2026, 1, 3)) == "18 September 2025"
    assert card_mask("4817") == "•••• 4817"
    with pytest.raises(ValueError):
        card_mask("48171")
    assert join_and([]) == ""
    assert join_and(["a"]) == "a"
    assert join_and(["a", "b"]) == "a and b"
    assert join_and(["a", "b", "c"]) == "a, b and c"
    assert count_phrase(1, "thing") == "one thing"
    assert count_phrase(4, "thing") == "4 things"


# --------------------------------------------------------------------------- stats


def test_median_and_mad() -> None:
    values = [Decimal("10"), Decimal("12"), Decimal("11"), Decimal("100")]
    assert median(values) == Decimal("11.5")
    assert median([Decimal("3"), Decimal("1"), Decimal("2")]) == Decimal("2")
    assert mad([Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"), Decimal("100")]) == Decimal("1")
    with pytest.raises(ValueError):
        median([])


def test_modified_z() -> None:
    assert modified_z(Decimal("10"), Decimal("10"), Decimal("0")) is None
    z = modified_z(Decimal("20"), Decimal("10"), Decimal("1"))
    assert z == Decimal("6.745")


# --------------------------------------------------------------------------- questions


def _facts(**kw) -> SubjectFacts:
    base = dict(subject_type="transaction", subject_id="tx_1", counterparty_label="IKEA",
                amount=Decimal("84.5"), on=date(2026, 9, 12), card_last4="4817")  # fmt: skip
    base.update(kw)
    return SubjectFacts(**base)


def test_describe_subject() -> None:
    assert describe_subject(_facts()) == "IKEA · €84.50 · 12 September · card •••• 4817"
    assert describe_subject(_facts(card_last4=None, account_label="Millennium •••• 1234")) == (
        "IKEA · €84.50 · 12 September · Millennium •••• 1234"
    )


def test_question_validation() -> None:
    yes = QuestionOption(id="a", label="A", kind=OptionKind.OTHER)
    with pytest.raises(ValidationError):
        Question(tenant_id="t", kind=QuestionKind.WHICH_COMPANY, prompt="p", options=(yes,), facts=_facts())
    with pytest.raises(ValidationError):
        Question(tenant_id="t", kind=QuestionKind.WHICH_COMPANY, prompt="p", options=(yes, yes), facts=_facts())
    with pytest.raises(ValidationError):
        QuestionOption(id="e", label="E", kind=OptionKind.ENTITY)
    with pytest.raises(ValidationError):
        _facts(amount=Decimal("-1"))
    with pytest.raises(ValidationError):
        _facts(amount=84.5)
    with pytest.raises(ValidationError):
        Answer(question_id="q", option_id="a", answered_by="u", answered_at=datetime(2026, 9, 1))
    ok = Answer(question_id="q", option_id="a", answered_by="u", answered_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert ok.option_id == "a"
