"""Owner-facing language (§36, §42, §48, §54, §69–70)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backoffice.domain.models import SourceKind
from backoffice.language import (
    ACTION_REQUIRED,
    ALL_GOOD,
    DONE,
    I_WILL_REMEMBER,
    MINUS,
    NOTIFY_EVENTS,
    Cadence,
    Issue,
    NotifyEvent,
    WhyFactor,
    WhyKind,
    connector_problem,
    explain,
    find_jargon,
    find_off_tone,
    format_money,
    greeting,
    render_why,
    should_notify,
    since_phrase,
    status_headline,
    still_need,
)

LISBON_SUMMER = timezone(timedelta(hours=1))
# Sunday 27 September 2026, 09:10 in Lisbon.
NOW = datetime(2026, 9, 27, 9, 10, tzinfo=LISBON_SUMMER)


# --------------------------------------------------------------------------- headline & personality


@pytest.mark.parametrize(
    "needs, risks, expected",
    [
        (0, 0, "Everything is under control."),
        (1, 0, "I need one thing from you."),
        (2, 0, "I need 2 things from you."),
        (14, 0, "I need 14 things from you."),
        (0, 1, "Action required."),
        (3, 2, "Action required."),
    ],
)
def test_status_headline(needs, risks, expected):
    assert status_headline(needs, risks) == expected


def test_status_headline_rejects_negative_counts():
    with pytest.raises(ValueError):
        status_headline(-1, 0)
    with pytest.raises(ValueError):
        status_headline(0, -1)


def test_still_need():
    assert still_need(0) == "Done."
    assert still_need(1) == "I still need one thing."
    assert still_need(3) == "I still need 3 things."
    with pytest.raises(ValueError):
        still_need(-1)


@pytest.mark.parametrize(
    "hour, expected",
    [(4, "Good evening."), (5, "Good morning."), (11, "Good morning."), (12, "Good afternoon."),
     (17, "Good afternoon."), (18, "Good evening."), (23, "Good evening.")],
)  # fmt: skip
def test_greeting(hour, expected):
    assert greeting(NOW.replace(hour=hour)) == expected


# --------------------------------------------------------------------------- money


@pytest.mark.parametrize(
    "amount, currency, expected",
    [
        (Decimal("1492.30"), "EUR", "€1,492.30"),
        (Decimal("1492.3"), "EUR", "€1,492.30"),
        (Decimal("83.21"), "GBP", "£83.21"),
        (Decimal("39"), "USD", "$39.00"),
        (Decimal("1234567.8"), "ILS", "₪1,234,567.80"),
        (Decimal("0"), "EUR", "€0.00"),
        (Decimal("-117.20"), "EUR", f"{MINUS}€117.20"),
        (Decimal("-0.001"), "EUR", "€0.00"),
        (Decimal("0.005"), "EUR", "€0.01"),
        (Decimal("2.675"), "EUR", "€2.68"),
        (Decimal("1E+3"), "EUR", "€1,000.00"),
        (Decimal("1E+30"), "EUR", "€1,000,000,000,000,000,000,000,000,000,000.00"),
        (
            Decimal("12345678901234567890123456.785"),
            "EUR",
            "€12,345,678,901,234,567,890,123,456.79",
        ),
        (500, "eur", "€500.00"),
        (Decimal("1492.30"), "CHF", "CHF 1,492.30"),
        (Decimal("-5"), "CHF", f"{MINUS}CHF 5.00"),
    ],
)
def test_format_money(amount, currency, expected):
    assert format_money(amount, currency) == expected


def test_format_money_defaults_to_euro():
    assert format_money(Decimal("92.40")) == "€92.40"


@pytest.mark.parametrize("amount", [1492.30, True, "1492.30", None])
def test_format_money_refuses_non_decimal(amount):
    with pytest.raises(TypeError):
        format_money(amount)  # type: ignore[arg-type]


@pytest.mark.parametrize("amount, currency", [(Decimal("NaN"), "EUR"), (Decimal("Infinity"), "EUR"),
                                              (Decimal("1"), "EURO"), (Decimal("1"), "€")])  # fmt: skip
def test_format_money_rejects_bad_values(amount, currency):
    with pytest.raises(ValueError):
        format_money(amount, currency)


def test_money_strings_have_fixed_decimals_for_tabular_columns():
    rendered = [format_money(Decimal(v)) for v in ("1", "10.5", "100.25", "1000")]
    assert all(
        s.split(".")[1].isdigit() and len(s.split(".")[1]) == 2 for s in rendered
    )


# --------------------------------------------------------------------------- §36 explanations


def test_section_36_plain_explanations():
    assert explain(Issue.UNMATCHED_PAYMENT) == "We couldn't match this payment."
    assert (
        explain(Issue.MISSING_INVOICE) == "We can't find the invoice for this payment."
    )
    assert (
        explain(Issue.UNSURE_WHICH_COMPANY)
        == "We aren't sure which company this belongs to."
    )


def test_explain_accepts_the_internal_jargon_key():
    assert explain("reconciliation_exception") == "We couldn't match this payment."
    assert (
        explain("missing_source_document")
        == "We can't find the invoice for this payment."
    )
    assert (
        explain("entity_classification_uncertain")
        == "We aren't sure which company this belongs to."
    )
    with pytest.raises(ValueError):
        explain("something_else")


def test_every_issue_has_an_explanation():
    assert all(explain(i).endswith(".") for i in Issue)


# --------------------------------------------------------------------------- §48 connector copy


def test_section_48_example():
    msg = connector_problem(
        "Gmail", SourceKind.EMAIL, NOW.replace(day=26, hour=14, minute=42), NOW
    )
    assert msg.title == "Gmail needs reconnecting."
    assert msg.detail == "Your email has not synced since 14:42 yesterday."
    assert msg.action_label == "Reconnect"


@pytest.mark.parametrize(
    "then, expected",
    [
        (NOW.replace(hour=7, minute=5), "07:05"),
        (NOW.replace(day=26, hour=14, minute=42), "14:42 yesterday"),
        (NOW.replace(day=24, hour=14, minute=42), "Thursday at 14:42"),
        (NOW.replace(day=21, hour=8, minute=0), "Monday at 08:00"),
        (NOW.replace(day=20, hour=8, minute=0), "20 September"),
        (NOW.replace(month=3, day=3), "3 March"),
        (NOW.replace(year=2025, month=12, day=31), "31 December 2025"),
        (NOW + timedelta(minutes=5), "09:10"),  # clock skew never says "in the future"
    ],
)
def test_since_phrase(then, expected):
    assert since_phrase(then, NOW) == expected


def test_since_phrase_counts_days_in_the_owners_time_zone():
    then_utc = datetime(
        2026, 9, 26, 23, 30, tzinfo=timezone.utc
    )  # 00:30 on the 27th in Lisbon
    now_utc = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)
    assert since_phrase(then_utc, now_utc) == "23:30 yesterday"
    assert since_phrase(then_utc, now_utc, LISBON_SUMMER) == "00:30"
    assert (
        since_phrase(then_utc, now_utc, timezone(timedelta(hours=-5)))
        == "18:30 yesterday"
    )


def test_since_phrase_requires_aware_datetimes():
    with pytest.raises(ValueError):
        since_phrase(datetime(2026, 9, 26), NOW)
    with pytest.raises(ValueError):
        since_phrase(NOW, datetime(2026, 9, 27))


@pytest.mark.parametrize(
    "name, kind, detail",
    [
        (
            "Millennium BCP",
            SourceKind.BANK,
            "Your bank account has not synced since 14:42 yesterday.",
        ),
        ("Revolut", SourceKind.CARD, "Your card has not synced since 14:42 yesterday."),
        (
            "Google Drive",
            SourceKind.CLOUD_STORAGE,
            "Your files have not synced since 14:42 yesterday.",
        ),
        (
            "TOConline",
            SourceKind.ACCOUNTING_SYSTEM,
            "Your accounting software has not synced since 14:42 yesterday.",
        ),
        (
            "Vodafone",
            SourceKind.SUPPLIER_PORTAL,
            "Your Vodafone account has not synced since 14:42 yesterday.",
        ),
    ],
)
def test_connector_copy_per_source(name, kind, detail):
    msg = connector_problem(name, kind, NOW.replace(day=26, hour=14, minute=42), NOW)
    assert msg.title == f"{name} needs reconnecting."
    assert msg.detail == detail


def test_connector_that_never_synced():
    msg = connector_problem("Outlook", SourceKind.EMAIL, None, NOW)
    assert msg.detail == "Your email has not synced yet."


# --------------------------------------------------------------------------- §42 notifications


@pytest.mark.parametrize("event", sorted(NOTIFY_EVENTS), ids=lambda e: e.value)
def test_notifies_only_what_needs_the_owner(event):
    assert should_notify(event)
    assert should_notify(event.value)


@pytest.mark.parametrize(
    "event", sorted(set(NotifyEvent) - NOTIFY_EVENTS), ids=lambda e: e.value
)
def test_success_is_quiet(event):
    assert not should_notify(event)


def test_section_42_set_is_exactly_approvals_bank_changes_and_reconnection():
    assert {e.value for e in NOTIFY_EVENTS} == {
        "approval_needed", "bank_details_changed", "payment_blocked",
        "reconnect_needed", "bank_consent_expired", "authentication_needed",
    }  # fmt: skip
    assert not should_notify(
        NotifyEvent.DOCUMENT_PROCESSED
    )  # never "Invoice successfully processed"


@pytest.mark.parametrize(
    "unknown", ["", "invoice_successfully_processed", "APPROVAL_NEEDED"]
)
def test_unknown_events_stay_quiet(unknown):
    assert not should_notify(unknown)


# --------------------------------------------------------------------------- §54 why


def test_section_54_example():
    lines = render_why(
        [
            WhyFactor.invoice_total(Decimal("83.21")),
            WhyFactor.bank_charge(Decimal("83.21")),
            WhyFactor.days_apart(1),
            WhyFactor.check(WhyKind.SUPPLIER_VAT_NUMBER),
            WhyFactor.check(WhyKind.CARD_ENDING),
            WhyFactor.recurring(Cadence.MONTHLY),
        ]
    )
    assert lines == [
        "Invoice total €83.21",
        "Bank charge €83.21",
        "Dates 1 day apart",
        "Supplier VAT number matches",
        "Card ending matches",
        "Usually paid every month",
    ]


def test_why_for_a_conflict():
    lines = render_why(
        [
            WhyFactor.invoice_total(Decimal("483.60")),
            WhyFactor.qr_total(Decimal("438.60")),
            WhyFactor.check(WhyKind.AMOUNTS_ADD_UP, agrees=False),
            WhyFactor.check(WhyKind.BANK_DETAILS, agrees=False),
        ]
    )
    assert lines == [
        "Invoice total €483.60",
        "QR code total €438.60",
        "Amounts do not add up",
        "Bank details do not match",
    ]


@pytest.mark.parametrize(
    "days, text", [(0, "Same day"), (1, "Dates 1 day apart"), (3, "Dates 3 days apart")]
)
def test_days_apart_wording(days, text):
    assert render_why([WhyFactor.days_apart(days)]) == [text]


@pytest.mark.parametrize("cadence, text", [(Cadence.WEEKLY, "every week"), (Cadence.QUARTERLY, "every 3 months"),
                                           (Cadence.YEARLY, "every year")])  # fmt: skip
def test_recurring_wording(cadence, text):
    assert render_why([WhyFactor.recurring(cadence)]) == [f"Usually paid {text}"]


def test_why_amounts_use_their_currency():
    assert render_why([WhyFactor.bank_charge(Decimal("12"), "GBP")]) == [
        "Bank charge £12.00"
    ]


@pytest.mark.parametrize(
    "build",
    [
        lambda: WhyFactor(WhyKind.INVOICE_TOTAL),
        lambda: WhyFactor(WhyKind.DAYS_APART),
        lambda: WhyFactor.days_apart(-1),
        lambda: WhyFactor(WhyKind.RECURRING),
        lambda: WhyFactor.check(WhyKind.INVOICE_TOTAL),
    ],
)
def test_incomplete_factors_are_rejected(build):
    with pytest.raises(ValueError):
        build()


def test_why_refuses_float_money():
    with pytest.raises(TypeError):
        render_why([WhyFactor.invoice_total(83.21)])  # type: ignore[arg-type]


# --------------------------------------------------------------------------- jargon linter


@pytest.mark.parametrize(
    "text, terms",
    [
        ("Reconciliation exception on this payment", ["reconciliation", "exception"]),
        ("We reconciled 3 payments", ["reconciliation"]),
        ("Missing source document", ["source document"]),
        ("Entity classification uncertain", ["entity classification"]),
        ("Which entity is this?", ["entity"]),
        ("OCR failed", ["OCR"]),
        ("Workflow wf_0123456789abcdef stalled", ["workflow", "internal ID"]),
        ("Item queued", ["queue"]),
        ("Could not parse the file", ["parse"]),
        ("Parsing…", ["parse"]),
        ("Confidence: 87%", ["confidence %"]),
        ("87 % confidence", ["confidence %"]),
        ("confidence score low", ["confidence %"]),
        ("API error", ["API"]),
        ("Token expired", ["token"]),
        ("OAuth refresh failed", ["OAuth"]),
        ("Webhook missed", ["webhook"]),
        ("HTTP 500 from server", ["error code"]),
        ("Error 401", ["error code"]),
        ("ConnectionError: reset", ["raw error"]),
        ("Traceback (most recent call last)", ["raw error"]),
        ("Document 3f2b8c1e-9d4a-4b7e-8c21-5f0a9e7d1b23", ["internal ID"]),
        ("Evidence ev_1a2b3c4d5e6f7a8b", ["internal ID"]),
        ("Amount: undefined", ["null"]),
        ("Posted to the general ledger as an accrual", ["ledger", "accrual"]),
        ("New journal entry", ["journal entry"]),
    ],
)
def test_find_jargon(text, terms):
    assert find_jargon(text) == terms


@pytest.mark.parametrize(
    "text",
    [
        "We understand 96% of your business. We need you to confirm 4 things.",
        "This €92.40 Vodafone expense appears every month.",
        "Exceptional service from your accountant.",
        "Your identity is verified.",
        "Add parsley to the order.",
        "Gmail needs reconnecting.",
        "September is closed.",
        "None of your invoices are missing.",
        "I am confident this is the Vodafone invoice.",
    ],
)
def test_plain_text_passes(text):
    assert find_jargon(text) == []


def test_jargon_terms_are_reported_once_in_order():
    assert find_jargon("queue, API, queue again, API") == ["queue", "API"]


def test_off_tone():
    assert find_off_tone("Great job!") == ["great job", "exclamation mark"]
    assert find_off_tone("Oops, awesome. Congratulations") == [
        "oops",
        "awesome",
        "congratulations",
    ]
    assert find_off_tone("Done.") == []


# --------------------------------------------------------------------------- everything we say is plain


def _all_owner_strings() -> list[str]:
    strings = [
        DONE,
        I_WILL_REMEMBER,
        ALL_GOOD,
        ACTION_REQUIRED,
        still_need(1),
        still_need(2),
    ]
    strings += [status_headline(n, r) for n in range(3) for r in range(2)]
    strings += [explain(i) for i in Issue]
    never_sync = {SourceKind.UPLOAD, SourceKind.MOBILE_SCAN, SourceKind.MOBILE_SHARE}
    for kind in set(SourceKind) - never_sync:
        for then in (None, NOW - timedelta(days=1), NOW - timedelta(days=40)):
            for name in ("Gmail", "", "HTTPError 503"):
                msg = connector_problem(name, kind, then, NOW)
                strings += [msg.title, msg.detail, msg.action_label]
    for kind in WhyKind:
        amount_kinds = (WhyKind.INVOICE_TOTAL, WhyKind.CREDIT_NOTE_TOTAL, WhyKind.BANK_CHARGE,
                        WhyKind.MONEY_RECEIVED, WhyKind.QR_TOTAL)  # fmt: skip
        if kind in amount_kinds:
            strings += render_why([WhyFactor(kind, amount=Decimal("1"))])
        elif kind is WhyKind.DAYS_APART:
            strings += render_why([WhyFactor.days_apart(2)])
        elif kind is WhyKind.RECURRING:
            strings += render_why([WhyFactor.recurring(c) for c in Cadence])
        else:
            strings += render_why(
                [WhyFactor.check(kind, True), WhyFactor.check(kind, False)]
            )
    return strings


def test_every_owner_facing_string_is_plain_and_calm():
    for s in _all_owner_strings():
        assert find_jargon(s) == [], s
        assert find_off_tone(s) == [], s


# --------------------------------------------------------------------------- review regressions


@pytest.mark.parametrize(
    "amount, expected",
    [
        (Decimal("123456789012345678901234567890.125"), "€123,456,789,012,345,678,901,234,567,890.13"),
        (Decimal("-99999999999999999999999999999.995"), f"{MINUS}€100,000,000,000,000,000,000,000,000,000.00"),
        (Decimal("12345678901234567890123456789"), "€12,345,678,901,234,567,890,123,456,789.00"),
    ],
)
def test_format_money_never_rounds_the_integer_part(amount, expected):
    """abs() ran outside the widened context and silently rounded to 28 digits."""
    assert format_money(amount) == expected


@pytest.mark.parametrize("bad", [True, False, 1.0, 2.5, "2", None])
def test_counts_must_be_whole_numbers(bad):
    """status_headline(True, 0) used to say 'I need one thing from you.'"""
    with pytest.raises(TypeError):
        status_headline(bad, 0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        status_headline(0, bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        still_need(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["", "   ", "ConnectionError: 401", "gmail_oauth", "API token"])
def test_connector_names_that_are_blank_or_technical_get_generic_copy(name):
    """A raw error or internal name passed as the connector name used to reach the owner."""
    then = NOW.replace(day=26, hour=14, minute=42)
    msg = connector_problem(name, SourceKind.EMAIL, then, NOW)
    assert msg.title == "One of your accounts needs reconnecting."
    assert msg.detail == "Your email has not synced since 14:42 yesterday."
    portal = connector_problem(name, SourceKind.SUPPLIER_PORTAL, then, NOW)
    assert portal.detail == "This account has not synced since 14:42 yesterday."
    for text in (msg.title, msg.detail, portal.title, portal.detail):
        assert find_jargon(text) == [] and name.strip() not in text or not name.strip()


@pytest.mark.parametrize(
    "name, kind, detail",
    [
        ("Your accountant", SourceKind.ACCOUNTANT, "Messages from your accountant have not synced yet."),
        ("Ana Costa", SourceKind.ACCOUNTANT, "Messages from your accountant have not synced yet."),
        ("Portal das Finanças", SourceKind.GOVERNMENT, "Your Portal das Finanças account has not synced yet."),
        ("your Vodafone portal", SourceKind.SUPPLIER_PORTAL, "Your Vodafone portal account has not synced yet."),
    ],
)
def test_connector_copy_reads_naturally_for_every_connected_source(name, kind, detail):
    """ACCOUNTANT used to read 'Your Your accountant account has not synced yet.'"""
    msg = connector_problem(name, kind, None, NOW)
    assert msg.detail == detail
    assert "Your Your" not in msg.title + msg.detail and "your your" not in msg.detail.lower()


@pytest.mark.parametrize("kind", [SourceKind.UPLOAD, SourceKind.MOBILE_SCAN, SourceKind.MOBILE_SHARE])
def test_sources_that_never_sync_cannot_need_reconnecting(kind):
    with pytest.raises(ValueError):
        connector_problem("Scan", kind, None, NOW)


@pytest.mark.parametrize(
    "text, terms",
    [
        ("Payment NEEDS_OWNER", ["internal name"]),
        ("status: needs_owner", ["internal name"]),
        ("reconciliation_exception", ["reconciliation"]),
        ("404 Not Found", ["raw error"]),
        ("500 Internal Server Error", ["raw error"]),
        ("Internal Server Error", ["raw error"]),
        ("[Errno 111] Connection refused", ["raw error"]),
        ("Connection reset by peer", ["raw error"]),
        ("Supplier: None", ["null"]),
        ("Total: €None", ["null"]),
        ("Total: nan", ["null"]),
        ("Confidence 0.87", ["confidence %"]),
        ("confidence: .9", ["confidence %"]),
        ("{'iban': 'PT50...'}", ["raw data"]),
        ('{"status": "ok"}', ["raw data"]),
        ("[object Object]", ["raw data"]),
    ],
)
def test_find_jargon_catches_leaked_internals(text, terms):
    assert find_jargon(text) == terms


@pytest.mark.parametrize(
    "text",
    [
        "We sent a reminder to billing_team@vodafone.pt.",
        "None of these payments need you.",
        "Nothing found for September.",
        "I am confident about 3 of them.",
        "Your accountant replied: {see attached}.",
    ],
)
def test_plain_text_with_look_alikes_passes(text):
    assert find_jargon(text) == []


@pytest.mark.parametrize(
    "build, error",
    [
        (lambda: WhyFactor.invoice_total(83.21), TypeError),  # type: ignore[arg-type]
        (lambda: WhyFactor.bank_charge(True), TypeError),  # type: ignore[arg-type]
        (lambda: WhyFactor.qr_total(Decimal("1"), "EURO"), ValueError),
        (lambda: WhyFactor.days_apart(True), TypeError),  # type: ignore[arg-type]
        (lambda: WhyFactor.days_apart(1.5), TypeError),  # type: ignore[arg-type]
        (lambda: WhyFactor.recurring("monthly"), TypeError),  # type: ignore[arg-type]
    ],
)
def test_why_factors_are_checked_when_built(build, error):
    """'Dates True day apart' used to render; float money failed only when shown."""
    with pytest.raises(error):
        build()


@pytest.mark.parametrize(
    "text",
    [
        "Unauthorized payment blocked.",
        "Forbidden by your bank.",
        "We saved Fatura_2026_183.pdf for you.",
        "The file vodafone_setembro.PDF is attached.",
    ],
)
def test_linter_leaves_plain_words_and_file_names_alone(text):
    assert find_jargon(text) == []


@pytest.mark.parametrize(
    "text", ["401 Unauthorized", "HTTP/1.1 403 Forbidden", "Bad Request", "Gateway Timeout"]
)
def test_linter_flags_http_reason_phrases(text):
    assert "raw error" in find_jargon(text) or "error code" in find_jargon(text)


def test_bank_charge_shows_the_amount_charged_whatever_the_sign():
    """Transaction.amount is negative for money out; '−€83.21' next to the invoice's
    '€83.21' read like a mismatch."""
    assert render_why([WhyFactor.bank_charge(Decimal("-83.21"))]) == ["Bank charge €83.21"]
    assert render_why([WhyFactor.bank_charge(Decimal("83.21"))]) == ["Bank charge €83.21"]


@pytest.mark.parametrize(
    "amount, line",
    [
        (Decimal("-83.21"), "Bank charge €83.21"),
        (Decimal("50.00"), "Money received €50.00"),
        (Decimal("0"), "Bank charge €0.00"),
    ],
)
def test_bank_movement_words_the_direction(amount, line):
    assert render_why([WhyFactor.bank_movement(amount)]) == [line]


def test_credit_notes_and_refunds_have_their_own_words():
    lines = render_why(
        [
            WhyFactor.credit_note_total(Decimal("-50.00")),
            WhyFactor.bank_movement(Decimal("50.00")),
            WhyFactor.days_apart(2),
        ]
    )
    assert lines == ["Credit note total €50.00", "Money received €50.00", "Dates 2 days apart"]
    for line in lines:
        assert find_jargon(line) == [] and find_off_tone(line) == []


def test_unsigned_why_amounts_never_round_large_values():
    big = Decimal("-123456789012345678901234567890.12")
    assert render_why([WhyFactor.bank_charge(big)]) == [
        "Bank charge €123,456,789,012,345,678,901,234,567,890.12"
    ]
