"""Self-healing connections (§47-48).

Every connector keeps a :class:`ConnectorState`: last successful sync, last
event, cursor, auth expiry, webhook state, historical coverage, known gaps and
failures. From that state alone this module decides

* **health** — HEALTHY / DEGRADED / BROKEN, with plain owner copy and a one-tap
  action (§48: "Gmail needs reconnecting. Your email has not synced since 14:42
  yesterday. [Reconnect]"); internal error codes never reach the copy;
* **backfill** — missed webhooks, silent webhooks, lost cursors and known gaps
  (§47: "Missed webhook → backfill");
* **coverage** — whether a period is fully synced. Month close uses it: a
  connector that stopped syncing means the month can never be green.

States are immutable; every update returns a new state to persist.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from backoffice.domain.models import SourceKind, new_id

__all__ = [
    "ActionKind",
    "BackfillPlan",
    "BackfillReason",
    "ConnectorError",
    "ConnectorKind",
    "ConnectorState",
    "CountingSink",
    "Coverage",
    "CoverageReason",
    "CursorExpired",
    "DEFAULT_POLICIES",
    "Health",
    "HealthPolicy",
    "HealthReason",
    "HealthReport",
    "MailItem",
    "MailSink",
    "OwnerAction",
    "ProviderError",
    "ReconnectRequired",
    "SOURCE_KIND",
    "SyncOutcome",
    "TimeRange",
    "TransientError",
    "WebhookState",
    "covers",
    "evaluate_health",
    "gap_after_cursor_loss",
    "needs_backfill",
    "policy_for",
    "record_backfill",
    "record_event",
    "record_failure",
    "record_gap",
    "record_reconnected",
    "record_success",
    "record_webhook",
    "since_phrase",
]


# --------------------------------------------------------------------------- errors


class ConnectorError(Exception):
    """A provider problem. ``code`` is internal (logs, ``last_error_code``), never owner copy."""

    retryable = True
    needs_reconnect = False

    def __init__(self, code: str, *, retry_after: float | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class ReconnectRequired(ConnectorError):
    """Authorisation is gone (refresh refused, consent expired): only the owner can fix it."""

    retryable = False
    needs_reconnect = True


class TransientError(ConnectorError):
    """Network, throttling or provider outage: retry later."""


class CursorExpired(ConnectorError):
    """The incremental cursor is no longer valid; a full sync is needed."""


class ProviderError(ConnectorError):
    """The provider answered in a way retrying will not fix (engineering attention)."""

    retryable = False


# --------------------------------------------------------------------------- state


class ConnectorKind(str, Enum):
    GMAIL = "gmail"
    MICROSOFT = "microsoft"
    IMAP = "imap"
    OPEN_BANKING = "open_banking"
    SUPPLIER_PORTAL = "supplier_portal"


SOURCE_KIND: dict[ConnectorKind, SourceKind] = {
    ConnectorKind.GMAIL: SourceKind.EMAIL,
    ConnectorKind.MICROSOFT: SourceKind.EMAIL,
    ConnectorKind.IMAP: SourceKind.EMAIL,
    ConnectorKind.OPEN_BANKING: SourceKind.BANK,
    ConnectorKind.SUPPLIER_PORTAL: SourceKind.SUPPLIER_PORTAL,
}

_DEFAULT_NAMES = {
    ConnectorKind.GMAIL: "Gmail",
    ConnectorKind.MICROSOFT: "Outlook",
    ConnectorKind.IMAP: "Your email",
    ConnectorKind.OPEN_BANKING: "Your bank",
    ConnectorKind.SUPPLIER_PORTAL: "The supplier portal",
}


class WebhookState(str, Enum):
    NOT_USED = "not_used"  # polling only
    ACTIVE = "active"
    EXPIRED = "expired"  # subscription/watch lapsed
    FAILED = "failed"  # provider reported missed or undeliverable notifications


def _aware(value: datetime | None, name: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{name} must be timezone-aware")
    return value


class TimeRange(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _ordered(self) -> TimeRange:
        _aware(self.start, "start")
        _aware(self.end, "end")
        if self.start >= self.end:
            raise ValueError("a time range must end after it starts")
        return self

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and start < self.end

    def within(self, start: datetime, end: datetime) -> bool:
        return start <= self.start and self.end <= end


class ConnectorState(BaseModel):
    """What a connector remembers between runs (§47). Persist every returned copy."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    connector_id: str = Field(default_factory=lambda: new_id("conn"))
    kind: ConnectorKind
    account: str  # mailbox address, bank account label or portal login; never a secret
    display_name: str | None = None  # "Gmail", "Millennium BCP", "Vodafone"
    last_successful_sync: datetime | None = None  # start time of the last complete sync
    last_attempt_at: datetime | None = None
    last_event_at: datetime | None = None  # last webhook / push notification
    cursor: str | None = None  # provider-specific (history id, delta link, UID map, date)
    auth_expires_at: datetime | None = None  # OAuth grant or bank consent end
    reconnect_required: bool = False
    webhook_state: WebhookState = WebhookState.NOT_USED
    webhook_expires_at: datetime | None = None
    coverage_start: datetime | None = None  # everything from here ...
    coverage_end: datetime | None = None  # ... to here has been synced, except known gaps
    known_gaps: tuple[TimeRange, ...] = ()
    consecutive_failures: int = Field(default=0, ge=0)
    last_error_code: str | None = Field(default=None, repr=False)  # internal only (§48, §70)

    @field_validator(
        "last_successful_sync", "last_attempt_at", "last_event_at", "auth_expires_at",
        "webhook_expires_at", "coverage_start", "coverage_end",
    )
    @classmethod
    def _tz(cls, value: datetime | None, info: ValidationInfo) -> datetime | None:
        return _aware(value, info.field_name or "datetime")

    @property
    def name(self) -> str:
        return self.display_name or _DEFAULT_NAMES[self.kind]

    @property
    def source_kind(self) -> SourceKind:
        return SOURCE_KIND[self.kind]

    def health(self, now: datetime, *, tz: tzinfo | None = None) -> HealthReport:
        """Shorthand for :func:`evaluate_health`."""
        return evaluate_health(self, now, tz=tz)

    def needs_backfill(self, now: datetime) -> BackfillPlan | None:
        """Shorthand for :func:`needs_backfill`."""
        return needs_backfill(self, now)

    def covers(self, start: date, end: date, *, tz: tzinfo = timezone.utc,
               settle: timedelta | None = None) -> Coverage:
        """Shorthand for :func:`covers` (month close: never green without it)."""
        return covers(self, start, end, tz=tz, settle=settle)


def record_success(
    state: ConnectorState,
    *,
    at: datetime,
    cursor: str | None,
    coverage_start: datetime | None = None,
    resolved: TimeRange | None = None,
) -> ConnectorState:
    """A complete sync that started at ``at``. ``resolved`` closes backfilled gaps."""
    _aware(at, "at")
    _aware(coverage_start, "coverage_start")
    start = state.coverage_start
    if coverage_start is not None:
        start = coverage_start if start is None else min(start, coverage_start)
    gaps = state.known_gaps
    if resolved is not None:
        gaps = tuple(g for g in gaps if not g.within(resolved.start, resolved.end))
    return state.model_copy(update={
        "last_successful_sync": at, "last_attempt_at": at, "cursor": cursor,
        "coverage_start": start, "coverage_end": max(at, state.coverage_end or at),
        "consecutive_failures": 0, "last_error_code": None, "reconnect_required": False,
        "known_gaps": gaps,
    })


def record_failure(state: ConnectorState, *, at: datetime, error: ConnectorError) -> ConnectorState:
    _aware(at, "at")
    return state.model_copy(update={
        "last_attempt_at": at,
        "consecutive_failures": state.consecutive_failures + 1,
        "last_error_code": error.code,
        "reconnect_required": state.reconnect_required or error.needs_reconnect,
    })


def record_event(state: ConnectorState, *, at: datetime) -> ConnectorState:
    _aware(at, "at")
    latest = at if state.last_event_at is None else max(at, state.last_event_at)
    return state.model_copy(update={"last_event_at": latest})


def record_webhook(
    state: ConnectorState, *, webhook_state: WebhookState, expires_at: datetime | None = None
) -> ConnectorState:
    _aware(expires_at, "expires_at")
    return state.model_copy(update={"webhook_state": webhook_state, "webhook_expires_at": expires_at})


def record_gap(state: ConnectorState, gap: TimeRange) -> ConnectorState:
    """A period the connector knows it did not sync (e.g. history expired)."""
    if gap in state.known_gaps:
        return state
    return state.model_copy(update={"known_gaps": (*state.known_gaps, gap)})


def gap_after_cursor_loss(state: ConnectorState, window_start: datetime) -> ConnectorState:
    """After a lost cursor the resync covers only the history window: a last good
    sync older than the window leaves a hole, recorded as a known gap to backfill."""
    last = state.last_successful_sync
    if last is not None and last < window_start:
        return record_gap(state, TimeRange(start=last, end=window_start))
    return state


def record_backfill(state: ConnectorState, window: TimeRange) -> ConnectorState:
    """A completed backfill of ``window``: closes gaps inside it, extends history.

    It does not count as a sync: cursor, last sync and failures are untouched.
    """
    gaps = tuple(g for g in state.known_gaps if not g.within(window.start, window.end))
    start = state.coverage_start
    if start is not None and window.start < start <= window.end:
        start = window.start
    return state.model_copy(update={"known_gaps": gaps, "coverage_start": start})


def record_reconnected(
    state: ConnectorState, *, at: datetime, auth_expires_at: datetime | None = None
) -> ConnectorState:
    """The owner tapped Reconnect and granted access again."""
    _aware(at, "at")
    _aware(auth_expires_at, "auth_expires_at")
    return state.model_copy(update={
        "reconnect_required": False, "auth_expires_at": auth_expires_at, "consecutive_failures": 0,
        "last_error_code": None, "last_attempt_at": at,
    })


# --------------------------------------------------------------------------- sync results


@dataclass(frozen=True)
class MailItem:
    """One message as the provider gave it: raw RFC 822 bytes plus provider ids."""

    provider_id: str
    raw: bytes
    received_at: datetime | None = None
    thread_id: str | None = None
    folder: str | None = None
    labels: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"MailItem(provider_id={self.provider_id!r}, size={len(self.raw)})"


MailSink = Callable[[MailItem], None]


class CountingSink:
    """Wraps a sink and counts what was delivered (partial progress survives errors)."""

    def __init__(self, sink: Callable[[Any], None]) -> None:
        self._sink = sink
        self.count = 0

    def __call__(self, item: Any) -> None:
        self._sink(item)
        self.count += 1


@dataclass(frozen=True)
class SyncOutcome:
    """Always carries the state to persist, success or not."""

    state: ConnectorState
    delivered: int
    full_sync: bool = False
    error: ConnectorError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# --------------------------------------------------------------------------- health


class Health(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"  # self-healing in progress, or action needed soon
    BROKEN = "broken"  # the owner must act


class HealthReason(str, Enum):
    RECONNECT_REQUIRED = "reconnect_required"
    AUTH_EXPIRED = "auth_expired"
    FAILING_PERSISTENTLY = "failing_persistently"
    VERY_STALE = "very_stale"
    AUTH_EXPIRING = "auth_expiring"
    NEVER_SYNCED = "never_synced"
    STALE = "stale"
    FAILING = "failing"
    WEBHOOK_LAPSED = "webhook_lapsed"
    KNOWN_GAP = "known_gap"


_BROKEN = frozenset(
    {HealthReason.RECONNECT_REQUIRED, HealthReason.AUTH_EXPIRED, HealthReason.FAILING_PERSISTENTLY,
     HealthReason.VERY_STALE}
)  # fmt: skip


@dataclass(frozen=True)
class HealthPolicy:
    stale_after: timedelta
    broken_after: timedelta
    failures_degraded: int = 1
    failures_broken: int = 5
    expiry_warning: timedelta = timedelta(days=7)
    webhook_silence: timedelta | None = None  # no sync for this long while relying on push
    history_window: timedelta = timedelta(days=90)  # §6 default import
    settle: timedelta = timedelta(0)  # how long after a period its data keeps arriving


# Product thresholds (not regulatory facts). Bank access is rate-limited by
# PSD2 providers to a few unattended calls a day, hence the longer windows.
# Banks book card payments days after the purchase and backdate them, so a
# bank only covers a period once it has synced a few days past its end
# (same margin as the sync overlap in ``open_banking.BankSyncConfig``).
DEFAULT_POLICIES: dict[ConnectorKind, HealthPolicy] = {
    ConnectorKind.GMAIL: HealthPolicy(timedelta(hours=6), timedelta(hours=48), webhook_silence=timedelta(hours=24)),
    ConnectorKind.MICROSOFT: HealthPolicy(timedelta(hours=6), timedelta(hours=48), webhook_silence=timedelta(hours=24)),
    ConnectorKind.IMAP: HealthPolicy(timedelta(hours=6), timedelta(hours=48)),
    ConnectorKind.OPEN_BANKING: HealthPolicy(timedelta(hours=36), timedelta(days=4), settle=timedelta(days=5)),
    ConnectorKind.SUPPLIER_PORTAL: HealthPolicy(timedelta(days=3), timedelta(days=10)),
}


def policy_for(kind: ConnectorKind) -> HealthPolicy:
    return DEFAULT_POLICIES[kind]


class ActionKind(str, Enum):
    RECONNECT = "reconnect"


@dataclass(frozen=True)
class OwnerAction:
    kind: ActionKind
    label: str
    connector_id: str


@dataclass(frozen=True)
class HealthReport:
    health: Health
    title: str
    detail: str
    action: OwnerAction | None
    notify: bool  # §42: only reconnection needs a push
    reasons: tuple[HealthReason, ...]  # internal


def _health_reasons(state: ConnectorState, now: datetime, policy: HealthPolicy) -> list[HealthReason]:
    reasons: list[HealthReason] = []
    last = state.last_successful_sync
    if state.reconnect_required:
        reasons.append(HealthReason.RECONNECT_REQUIRED)
    if state.auth_expires_at is not None and state.auth_expires_at <= now:
        reasons.append(HealthReason.AUTH_EXPIRED)
    if state.consecutive_failures >= policy.failures_broken:
        reasons.append(HealthReason.FAILING_PERSISTENTLY)
    if last is not None and now - last > policy.broken_after:
        reasons.append(HealthReason.VERY_STALE)
    if state.auth_expires_at is not None and now < state.auth_expires_at <= now + policy.expiry_warning:
        reasons.append(HealthReason.AUTH_EXPIRING)
    if last is None:
        reasons.append(HealthReason.NEVER_SYNCED)
    elif now - last > policy.stale_after:
        reasons.append(HealthReason.STALE)
    if 0 < state.consecutive_failures and state.consecutive_failures >= policy.failures_degraded:
        reasons.append(HealthReason.FAILING)
    if _webhook_lapsed(state, now):
        reasons.append(HealthReason.WEBHOOK_LAPSED)
    if state.known_gaps:
        reasons.append(HealthReason.KNOWN_GAP)
    return reasons


def _webhook_lapsed(state: ConnectorState, now: datetime) -> bool:
    if state.webhook_state in (WebhookState.EXPIRED, WebhookState.FAILED):
        return True
    return (
        state.webhook_state is WebhookState.ACTIVE
        and state.webhook_expires_at is not None
        and state.webhook_expires_at <= now
    )


def evaluate_health(
    state: ConnectorState,
    now: datetime,
    *,
    policy: HealthPolicy | None = None,
    tz: tzinfo | None = None,
) -> HealthReport:
    """§48 health with owner copy. ``tz`` is the owner's zone for "14:42 yesterday"."""
    _aware(now, "now")
    policy = policy or policy_for(state.kind)
    reasons = _health_reasons(state, now, policy)
    zone = tz or now.tzinfo or timezone.utc
    reconnect = OwnerAction(ActionKind.RECONNECT, "Reconnect", state.connector_id)
    if any(r in _BROKEN for r in reasons):
        title, detail = f"{state.name} needs reconnecting.", _not_synced(state, now, zone)
        return HealthReport(Health.BROKEN, title, detail, reconnect, True, tuple(reasons))
    if HealthReason.AUTH_EXPIRING in reasons:
        assert state.auth_expires_at is not None
        ends = _day_month(state.auth_expires_at.astimezone(zone), now.astimezone(zone))
        return HealthReport(Health.DEGRADED, f"{state.name} needs reconnecting soon.",
                            f"This connection ends on {ends}.", reconnect, False, tuple(reasons))
    if HealthReason.NEVER_SYNCED in reasons:
        subject, plural = _subject(state)
        verb = "are" if plural else "is"
        return HealthReport(Health.DEGRADED, f"{state.name} is syncing.",
                            f"{subject} {verb} syncing for the first time.", None, False, tuple(reasons))
    if reasons:
        detail = (_not_synced(state, now, zone) if {HealthReason.STALE, HealthReason.FAILING} & set(reasons)
                  else "I'm checking for anything missed.")
        return HealthReport(Health.DEGRADED, f"{state.name} is catching up.", detail, None, False, tuple(reasons))
    return HealthReport(Health.HEALTHY, f"{state.name} is connected.", "Up to date.", None, False, ())


_SUBJECTS = {
    SourceKind.EMAIL: ("Your email", False),
    SourceKind.BANK: ("Your bank account", False),
    SourceKind.SUPPLIER_PORTAL: ("Your invoices from this supplier", True),
}


def _subject(state: ConnectorState) -> tuple[str, bool]:
    return _SUBJECTS.get(state.source_kind, ("This account", False))


def _not_synced(state: ConnectorState, now: datetime, zone: tzinfo) -> str:
    subject, plural = _subject(state)
    verb = "have not synced" if plural else "has not synced"
    if state.last_successful_sync is None:
        return f"{subject} {verb} yet."
    return f"{subject} {verb} since {since_phrase(state.last_successful_sync, now, zone)}."


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December")  # fmt: skip


def _day_month(value: datetime, now: datetime) -> str:
    text = f"{value.day} {_MONTHS[value.month - 1]}"
    return text if value.year == now.year else f"{text} {value.year}"


def since_phrase(then: datetime, now: datetime, tz: tzinfo | None = None) -> str:
    """'14:42', '14:42 yesterday', 'Monday at 14:42', '3 September' (owner's zone)."""
    zone = tz or now.tzinfo or timezone.utc
    local_now = now.astimezone(zone)
    local_then = min(then.astimezone(zone), local_now)
    days = (local_now.date() - local_then.date()).days
    clock = f"{local_then:%H:%M}"
    if days == 0:
        return clock
    if days == 1:
        return f"{clock} yesterday"
    if days < 7:
        return f"{_WEEKDAYS[local_then.weekday()]} at {clock}"
    return _day_month(local_then, local_now)


# --------------------------------------------------------------------------- backfill


class BackfillReason(str, Enum):
    NEVER_SYNCED = "never_synced"
    CURSOR_LOST = "cursor_lost"
    WEBHOOK_LAPSED = "webhook_lapsed"
    MISSED_EVENTS = "missed_events"  # an event arrived after the last complete sync
    WEBHOOK_SILENT = "webhook_silent"
    KNOWN_GAP = "known_gap"
    HISTORY_INCOMPLETE = "history_incomplete"


@dataclass(frozen=True)
class BackfillPlan:
    start: datetime
    end: datetime
    reasons: tuple[BackfillReason, ...]


def needs_backfill(
    state: ConnectorState, now: datetime, *, policy: HealthPolicy | None = None
) -> BackfillPlan | None:
    """The period to re-read, or ``None`` when nothing may have been missed (§47)."""
    _aware(now, "now")
    policy = policy or policy_for(state.kind)
    history_start = now - policy.history_window
    last = state.last_successful_sync
    if last is None:
        return BackfillPlan(history_start, now, (BackfillReason.NEVER_SYNCED,))
    starts: dict[BackfillReason, datetime] = {}
    if state.cursor is None:
        starts[BackfillReason.CURSOR_LOST] = state.coverage_end or last
    if _webhook_lapsed(state, now):
        starts[BackfillReason.WEBHOOK_LAPSED] = last
    if state.last_event_at is not None and state.last_event_at > last:
        starts[BackfillReason.MISSED_EVENTS] = last
    silence = policy.webhook_silence
    if state.webhook_state is WebhookState.ACTIVE and silence is not None and now - last > silence:
        starts[BackfillReason.WEBHOOK_SILENT] = last
    if state.known_gaps:
        starts[BackfillReason.KNOWN_GAP] = min(g.start for g in state.known_gaps)
    if state.coverage_start is not None and state.coverage_start > history_start + timedelta(days=1):
        starts[BackfillReason.HISTORY_INCOMPLETE] = history_start
    if not starts:
        return None
    return BackfillPlan(min(starts.values()), now, tuple(starts))


# --------------------------------------------------------------------------- coverage


class CoverageReason(str, Enum):
    COVERED = "covered"
    NEVER_SYNCED = "never_synced"
    HISTORY_STARTS_LATER = "history_starts_later"
    NOT_SYNCED_SINCE_PERIOD_END = "not_synced_since_period_end"
    KNOWN_GAP = "known_gap"


@dataclass(frozen=True)
class Coverage:
    covered: bool
    reason: CoverageReason
    period_start: datetime
    period_end: datetime  # exclusive

    def __bool__(self) -> bool:
        return self.covered


def covers(
    state: ConnectorState,
    start: date,
    end: date,
    *,
    tz: tzinfo = timezone.utc,
    settle: timedelta | None = None,
) -> Coverage:
    """Is every day from ``start`` to ``end`` (inclusive, owner's zone) fully synced?

    ``settle`` asks for a sync that far past the period end; by default the
    connector kind's :attr:`HealthPolicy.settle` (days for banks, whose
    bookings arrive late). Health after the period does not matter: a
    connector that broke in October still covers a completely synced September.
    """
    if end < start:
        raise ValueError("period end is before its start")
    if settle is None:
        settle = policy_for(state.kind).settle
    period_start = datetime.combine(start, time.min, tzinfo=tz)
    period_end = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz)

    def result(reason: CoverageReason) -> Coverage:
        return Coverage(reason is CoverageReason.COVERED, reason, period_start, period_end)

    if state.coverage_end is None or state.last_successful_sync is None:
        return result(CoverageReason.NEVER_SYNCED)
    if state.coverage_start is None or state.coverage_start > period_start:
        return result(CoverageReason.HISTORY_STARTS_LATER)
    if state.coverage_end < period_end + settle:
        return result(CoverageReason.NOT_SYNCED_SINCE_PERIOD_END)
    if any(g.overlaps(period_start, period_end) for g in state.known_gaps):
        return result(CoverageReason.KNOWN_GAP)
    return result(CoverageReason.COVERED)
