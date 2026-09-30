"""Self-healing connections: health, backfill and coverage (§47-48)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from backoffice.connectors.base import (
    BackfillReason,
    ConnectorKind,
    ConnectorState,
    CoverageReason,
    Health,
    HealthReason,
    ProviderError,
    ReconnectRequired,
    TimeRange,
    TransientError,
    WebhookState,
    covers,
    evaluate_health,
    gap_after_cursor_loss,
    needs_backfill,
    record_backfill,
    record_event,
    record_failure,
    record_gap,
    record_reconnected,
    record_success,
    record_webhook,
    since_phrase,
)

LISBON = ZoneInfo("Europe/Lisbon")
NOW = datetime(2026, 9, 25, 9, 30, tzinfo=LISBON)
YESTERDAY_1442 = datetime(2026, 9, 24, 14, 42, tzinfo=LISBON)
# Owner-facing copy must never show internals (§48, §70).
FORBIDDEN = re.compile(r"error|exception|oauth|token|http|api|invalid_grant|cursor|webhook|sync_state|_", re.I)


def state(**kw) -> ConnectorState:
    base = dict(tenant_id="t1", connector_id="conn_1", kind=ConnectorKind.GMAIL, account="ana@padaria.pt",
                last_successful_sync=NOW - timedelta(minutes=10), cursor="h-100",
                coverage_start=NOW - timedelta(days=90), coverage_end=NOW - timedelta(minutes=10))
    base.update(kw)
    return ConnectorState(**base)


def plain(report) -> None:
    for text in (report.title, report.detail):
        assert not FORBIDDEN.search(text), text


# --------------------------------------------------------------------------- health


def test_healthy_connector():
    report = evaluate_health(state(), NOW)
    assert report.health is Health.HEALTHY and report.action is None and not report.notify
    assert report.title == "Gmail is connected." and report.detail == "Up to date."


def test_reconnect_copy_matches_the_spec_exactly():
    broken = record_failure(state(last_successful_sync=YESTERDAY_1442), at=NOW,
                            error=ReconnectRequired("gmail_invalid_grant"))
    report = evaluate_health(broken, NOW, tz=LISBON)
    assert report.health is Health.BROKEN
    assert report.title == "Gmail needs reconnecting."
    assert report.detail == "Your email has not synced since 14:42 yesterday."
    assert report.action.label == "Reconnect" and report.action.connector_id == "conn_1"
    assert report.notify  # §42: reconnection is worth a notification
    assert HealthReason.RECONNECT_REQUIRED in report.reasons
    assert "invalid_grant" not in report.title + report.detail
    plain(report)


def test_owner_time_zone_is_used_for_the_clock():
    broken = state(reconnect_required=True, last_successful_sync=datetime(2026, 9, 24, 13, 42, tzinfo=timezone.utc))
    assert evaluate_health(broken, NOW, tz=LISBON).detail.endswith("14:42 yesterday.")


def test_never_synced_then_failing_forever_is_broken():
    fresh = state(last_successful_sync=None, cursor=None, coverage_start=None, coverage_end=None)
    first = evaluate_health(fresh, NOW)
    assert first.health is Health.DEGRADED and first.title == "Gmail is syncing."
    assert first.detail == "Your email is syncing for the first time." and first.action is None
    failing = fresh
    for _ in range(5):
        failing = record_failure(failing, at=NOW, error=TransientError("gmail_http_503"))
    report = evaluate_health(failing, NOW)
    assert report.health is Health.BROKEN and report.detail == "Your email has not synced yet."
    plain(report)


def test_stale_is_degraded_and_self_healing_then_broken_when_very_stale():
    stale = record_failure(state(last_successful_sync=NOW - timedelta(hours=8)), at=NOW,
                           error=TransientError("gmail_http_503"))
    report = evaluate_health(stale, NOW)
    assert report.health is Health.DEGRADED and report.title == "Gmail is catching up."
    assert report.action is None and not report.notify
    assert {HealthReason.STALE, HealthReason.FAILING} <= set(report.reasons)
    very = evaluate_health(state(last_successful_sync=NOW - timedelta(days=3)), NOW)
    assert very.health is Health.BROKEN and very.detail == "Your email has not synced since Tuesday at 09:30."


def test_bank_consent_expiring_and_expired():
    bank = state(kind=ConnectorKind.OPEN_BANKING, display_name="Millennium BCP", account="PT50 ...1234",
                 auth_expires_at=datetime(2026, 10, 1, 12, 0, tzinfo=LISBON))
    soon = evaluate_health(bank, NOW)
    assert soon.health is Health.DEGRADED and soon.title == "Millennium BCP needs reconnecting soon."
    assert soon.detail == "This connection ends on 1 October." and soon.action.label == "Reconnect"
    assert not soon.notify
    expired = evaluate_health(bank, datetime(2026, 10, 4, 9, 0, tzinfo=LISBON))
    assert expired.health is Health.BROKEN and expired.title == "Millennium BCP needs reconnecting."
    assert expired.detail.startswith("Your bank account has not synced since")
    plain(expired)


def test_lapsed_webhook_is_degraded_with_calm_copy():
    lapsed = record_webhook(state(), webhook_state=WebhookState.ACTIVE, expires_at=NOW - timedelta(hours=1))
    report = evaluate_health(lapsed, NOW)
    assert report.health is Health.DEGRADED and report.detail == "I'm checking for anything missed."


def test_portal_copy_is_plural():
    portal = state(kind=ConnectorKind.SUPPLIER_PORTAL, display_name="Vodafone", reconnect_required=True)
    assert evaluate_health(portal, NOW).detail.startswith("Your invoices from this supplier have not synced")


@pytest.mark.parametrize(
    ("then", "expected"),
    [
        (datetime(2026, 9, 25, 8, 5, tzinfo=LISBON), "08:05"),
        (datetime(2026, 9, 24, 23, 59, tzinfo=LISBON), "23:59 yesterday"),
        (datetime(2026, 9, 21, 7, 0, tzinfo=LISBON), "Monday at 07:00"),
        (datetime(2026, 9, 3, 7, 0, tzinfo=LISBON), "3 September"),
        (datetime(2025, 12, 30, 7, 0, tzinfo=LISBON), "30 December 2025"),
        (datetime(2026, 9, 26, 7, 0, tzinfo=LISBON), "09:30"),  # future clamps to now
    ],
)
def test_since_phrase(then, expected):
    assert since_phrase(then, NOW) == expected


# --------------------------------------------------------------------------- transitions


def test_success_resets_failures_and_reconnect_flag():
    s = record_failure(state(), at=NOW, error=ReconnectRequired("x"))
    assert s.reconnect_required and s.consecutive_failures == 1 and s.last_error_code == "x"
    ok = record_success(s, at=NOW, cursor="h-200", coverage_start=NOW - timedelta(days=120))
    assert not ok.reconnect_required and ok.consecutive_failures == 0 and ok.last_error_code is None
    assert ok.cursor == "h-200" and ok.coverage_end == NOW
    assert ok.coverage_start == NOW - timedelta(days=120)


def test_transient_failure_does_not_demand_reconnect():
    s = record_failure(state(), at=NOW, error=ProviderError("gmail_http_400"))
    assert not s.reconnect_required and s.last_attempt_at == NOW


def test_states_are_immutable_and_tz_aware():
    s = state()
    with pytest.raises(ValidationError):
        s.cursor = "x"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        state(last_successful_sync=datetime(2026, 9, 1, 12, 0))
    with pytest.raises(ValueError):
        record_success(s, at=datetime(2026, 9, 1, 12, 0), cursor=None)
    with pytest.raises(ValidationError):
        TimeRange(start=NOW, end=NOW)


def test_reconnected_clears_the_problem():
    s = record_failure(state(), at=NOW, error=ReconnectRequired("x"))
    again = record_reconnected(s, at=NOW, auth_expires_at=NOW + timedelta(days=90))
    assert not again.reconnect_required and again.auth_expires_at == NOW + timedelta(days=90)
    assert evaluate_health(again, NOW).health is Health.HEALTHY


def test_last_error_code_is_not_in_repr():
    s = record_failure(state(), at=NOW, error=TransientError("gmail_secret_detail"))
    assert "gmail_secret_detail" not in repr(s)


# --------------------------------------------------------------------------- backfill


def test_no_backfill_when_everything_is_current():
    assert needs_backfill(state(), NOW) is None


def test_backfill_for_never_synced_covers_history_window():
    plan = needs_backfill(state(last_successful_sync=None, cursor=None), NOW)
    assert plan.reasons == (BackfillReason.NEVER_SYNCED,) and plan.start == NOW - timedelta(days=90)


def test_missed_and_silent_webhooks_trigger_backfill_from_last_sync():
    last = NOW - timedelta(hours=2)
    missed = record_event(state(last_successful_sync=last), at=NOW - timedelta(minutes=5))
    plan = needs_backfill(missed, NOW)
    assert plan.reasons == (BackfillReason.MISSED_EVENTS,) and plan.start == last
    silent = record_webhook(state(last_successful_sync=NOW - timedelta(hours=30)), webhook_state=WebhookState.ACTIVE,
                            expires_at=NOW + timedelta(days=3))
    assert BackfillReason.WEBHOOK_SILENT in needs_backfill(silent, NOW).reasons
    failed = record_webhook(state(), webhook_state=WebhookState.FAILED)
    assert needs_backfill(failed, NOW).reasons == (BackfillReason.WEBHOOK_LAPSED,)


def test_lost_cursor_and_known_gap_backfill():
    gap = TimeRange(start=NOW - timedelta(days=200), end=NOW - timedelta(days=90))
    s = record_gap(state(cursor=None), gap)
    assert record_gap(s, gap) == s  # idempotent
    plan = needs_backfill(s, NOW)
    assert set(plan.reasons) == {BackfillReason.CURSOR_LOST, BackfillReason.KNOWN_GAP}
    assert plan.start == gap.start and plan.end == NOW


def test_history_incomplete_when_window_grows():
    s = state(coverage_start=NOW - timedelta(days=90))
    from backoffice.connectors.base import HealthPolicy

    policy = HealthPolicy(timedelta(hours=6), timedelta(hours=48), history_window=timedelta(days=365))
    plan = needs_backfill(s, NOW, policy=policy)
    assert plan.reasons == (BackfillReason.HISTORY_INCOMPLETE,) and plan.start == NOW - timedelta(days=365)


def test_backfill_resolves_gap_and_extends_history():
    gap = TimeRange(start=NOW - timedelta(days=120), end=NOW - timedelta(days=90))
    s = record_gap(state(coverage_start=NOW - timedelta(days=90)), gap)
    done = record_backfill(s, gap)
    assert done.known_gaps == () and done.coverage_start == gap.start
    assert done.cursor == s.cursor and done.last_successful_sync == s.last_successful_sync


def test_gap_after_cursor_loss_only_when_last_sync_predates_window():
    window_start = NOW - timedelta(days=90)
    recent = state(last_successful_sync=NOW - timedelta(days=2))
    assert gap_after_cursor_loss(recent, window_start).known_gaps == ()
    old = state(last_successful_sync=NOW - timedelta(days=100))
    (gap,) = gap_after_cursor_loss(old, window_start).known_gaps
    assert gap.start == NOW - timedelta(days=100) and gap.end == window_start


# --------------------------------------------------------------------------- coverage


def month_state(**kw) -> ConnectorState:
    return state(coverage_start=datetime(2026, 6, 1, tzinfo=LISBON), **kw)


def test_fully_synced_month_is_covered():
    s = month_state(last_successful_sync=datetime(2026, 10, 1, 6, 0, tzinfo=LISBON),
                    coverage_end=datetime(2026, 10, 1, 6, 0, tzinfo=LISBON))
    result = covers(s, date(2026, 9, 1), date(2026, 9, 30), tz=LISBON)
    assert result and result.reason is CoverageReason.COVERED
    assert result.period_end == datetime(2026, 10, 1, tzinfo=LISBON)


def test_connector_that_stopped_mid_month_never_covers_it():
    stopped = datetime(2026, 9, 24, 14, 42, tzinfo=LISBON)
    s = month_state(last_successful_sync=stopped, coverage_end=stopped, reconnect_required=True)
    result = covers(s, date(2026, 9, 1), date(2026, 9, 30), tz=LISBON)
    assert not result and result.reason is CoverageReason.NOT_SYNCED_SINCE_PERIOD_END


def test_broken_after_the_month_still_covers_the_month():
    after = datetime(2026, 10, 2, 8, 0, tzinfo=LISBON)
    s = month_state(last_successful_sync=after, coverage_end=after, reconnect_required=True)
    assert covers(s, date(2026, 9, 1), date(2026, 9, 30), tz=LISBON)


def test_coverage_needs_history_settle_time_and_no_gaps():
    end = datetime(2026, 10, 1, 6, 0, tzinfo=LISBON)
    late_history = state(coverage_start=datetime(2026, 9, 10, tzinfo=LISBON), last_successful_sync=end,
                         coverage_end=end)
    assert covers(late_history, date(2026, 9, 1), date(2026, 9, 30), tz=LISBON).reason is (
        CoverageReason.HISTORY_STARTS_LATER)
    s = month_state(last_successful_sync=end, coverage_end=end)
    assert not covers(s, date(2026, 9, 1), date(2026, 9, 30), tz=LISBON, settle=timedelta(days=3))
    gap = TimeRange(start=datetime(2026, 9, 10, tzinfo=LISBON), end=datetime(2026, 9, 11, tzinfo=LISBON))
    assert covers(record_gap(s, gap), date(2026, 9, 1), date(2026, 9, 30), tz=LISBON).reason is (
        CoverageReason.KNOWN_GAP)
    never = state(last_successful_sync=None, coverage_end=None)
    assert covers(never, date(2026, 9, 1), date(2026, 9, 30)).reason is CoverageReason.NEVER_SYNCED
    with pytest.raises(ValueError):
        covers(s, date(2026, 9, 30), date(2026, 9, 1))


def test_state_shorthands_delegate():
    end = datetime(2026, 10, 1, 6, 0, tzinfo=LISBON)
    s = month_state(last_successful_sync=end, coverage_end=end)
    assert s.covers(date(2026, 9, 1), date(2026, 9, 30), tz=LISBON)
    assert s.needs_backfill(end) is None
    assert s.health(end).health is Health.HEALTHY
