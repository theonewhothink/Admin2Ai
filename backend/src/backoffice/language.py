"""Owner-facing phrase layer (§36, §42, §48, §54, §69–70).

Short, calm, precise. No accounting jargon, no raw errors, no internal IDs.
Every string an owner sees should come from here or pass :func:`find_jargon`
and :func:`find_off_tone`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import Enum

from backoffice.domain.models import SourceKind

__all__ = [
    "ACTION_REQUIRED",
    "ALL_GOOD",
    "CURRENCY_SYMBOLS",
    "DONE",
    "I_WILL_REMEMBER",
    "MINUS",
    "NOTIFY_EVENTS",
    "Cadence",
    "ConnectorMessage",
    "Issue",
    "NotifyEvent",
    "WhyFactor",
    "WhyKind",
    "connector_problem",
    "explain",
    "find_jargon",
    "find_off_tone",
    "format_money",
    "greeting",
    "render_why",
    "should_notify",
    "since_phrase",
    "status_headline",
    "still_need",
]

# --------------------------------------------------------------------------- personality (§69)

DONE = "Done."
I_WILL_REMEMBER = "I will remember this."
ALL_GOOD = "Everything is under control."
ACTION_REQUIRED = "Action required."


def _count_things(n: int) -> str:
    return "one thing" if n == 1 else f"{n} things"


def status_headline(needs_count: int, risk_count: int) -> str:
    """Home headline (§34, §69). Real risk outranks everything else.

    A source that stopped syncing is a need (it must be reconnected), so
    callers count it in ``needs_count``; the month is never green without it (§48).
    """
    if needs_count < 0 or risk_count < 0:
        raise ValueError("counts cannot be negative")
    if risk_count:
        return ACTION_REQUIRED
    if needs_count:
        return f"I need {_count_things(needs_count)} from you."
    return ALL_GOOD


def still_need(count: int) -> str:
    """'I still need one thing.' / 'I still need 2 things.' / 'Done.' (§69)."""
    if count < 0:
        raise ValueError("count cannot be negative")
    return f"I still need {_count_things(count)}." if count else DONE


def greeting(local_now: datetime) -> str:
    """'Good morning.' etc., from the owner's local time (§34)."""
    hour = local_now.hour
    if 5 <= hour < 12:
        return "Good morning."
    if 12 <= hour < 18:
        return "Good afternoon."
    return "Good evening."


# --------------------------------------------------------------------------- money

CURRENCY_SYMBOLS: dict[str, str] = {"EUR": "€", "GBP": "£", "USD": "$", "ILS": "₪"}
MINUS = "−"  # true minus sign: same width as '+' and digits in tabular fonts
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")


def format_money(amount: Decimal | int, currency: str = "EUR") -> str:
    """'€1,492.30' / '−£12.00' / 'CHF 1,492.30'. Always two decimals.

    Money is Decimal: floats are refused rather than silently rounded.
    """
    if isinstance(amount, (bool, float)) or not isinstance(amount, (Decimal, int)):
        raise TypeError("money must be Decimal (or int), never float")
    value = Decimal(amount)
    if not value.is_finite():
        raise ValueError("money must be a finite amount")
    code = currency.strip().upper()
    if not _CURRENCY_CODE.match(code):
        raise ValueError(f"not a currency code: {currency!r}")
    with localcontext() as exact:
        exact.prec = max(
            28, value.adjusted() + 4
        )  # enough digits to never round the integer part
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = MINUS if rounded < 0 else ""
    digits = f"{abs(rounded):,.2f}"
    symbol = CURRENCY_SYMBOLS.get(code)
    return f"{sign}{symbol}{digits}" if symbol else f"{sign}{code} {digits}"


# --------------------------------------------------------------------------- plain explanations (§36)


class Issue(str, Enum):
    """Internal situations, keyed by the jargon they replace."""

    UNMATCHED_PAYMENT = "reconciliation_exception"
    MISSING_INVOICE = "missing_source_document"
    UNSURE_WHICH_COMPANY = "entity_classification_uncertain"
    AMOUNTS_DISAGREE = "field_conflict"
    POSSIBLE_DUPLICATE = "duplicate_candidate"
    UNREADABLE_DOCUMENT = "low_confidence_ocr"
    BANK_DETAILS_CHANGED = "beneficiary_changed"
    SOURCE_DISCONNECTED = "connector_auth_expired"


_EXPLANATIONS: dict[Issue, str] = {
    Issue.UNMATCHED_PAYMENT: "We couldn't match this payment.",
    Issue.MISSING_INVOICE: "We can't find the invoice for this payment.",
    Issue.UNSURE_WHICH_COMPANY: "We aren't sure which company this belongs to.",
    Issue.AMOUNTS_DISAGREE: "The amounts on this document don't agree.",
    Issue.POSSIBLE_DUPLICATE: "This looks like a copy of something we already have.",
    Issue.UNREADABLE_DOCUMENT: "We couldn't read this document clearly.",
    Issue.BANK_DETAILS_CHANGED: "This supplier's bank details changed.",
    Issue.SOURCE_DISCONNECTED: "One of your accounts needs reconnecting.",
}


def explain(issue: Issue | str) -> str:
    """Plain sentence for an internal situation (§36). Accepts the jargon key."""
    return _EXPLANATIONS[Issue(issue)]


# --------------------------------------------------------------------------- connector problems (§47–48)

_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def since_phrase(then: datetime, now: datetime, tz: tzinfo | None = None) -> str:
    """'14:42', '14:42 yesterday', 'Monday at 14:42', '3 September', '3 September 2025'.

    Days are counted in the owner's time zone ``tz`` (default: ``now``'s zone).
    """
    zone = tz or _aware(now, "now").tzinfo
    local_now = _aware(now, "now").astimezone(zone)
    local_then = min(_aware(then, "then").astimezone(zone), local_now)
    days = (local_now.date() - local_then.date()).days
    clock = f"{local_then:%H:%M}"
    if days == 0:
        return clock
    if days == 1:
        return f"{clock} yesterday"
    if days < 7:
        return f"{_WEEKDAYS[local_then.weekday()]} at {clock}"
    day_month = f"{local_then.day} {_MONTHS[local_then.month - 1]}"
    return (
        day_month
        if local_then.year == local_now.year
        else f"{day_month} {local_then.year}"
    )


@dataclass(frozen=True)
class ConnectorMessage:
    title: str
    detail: str
    action_label: str = "Reconnect"


# (subject, is_plural) for "Your email has not synced ..."
_SYNC_SUBJECT: dict[SourceKind, tuple[str, bool]] = {
    SourceKind.EMAIL: ("Your email", False),
    SourceKind.BANK: ("Your bank account", False),
    SourceKind.CARD: ("Your card", False),
    SourceKind.CLOUD_STORAGE: ("Your files", True),
    SourceKind.ACCOUNTING_SYSTEM: ("Your accounting software", False),
}


def connector_problem(
    name: str,
    kind: SourceKind,
    last_synced_at: datetime | None,
    now: datetime,
    *,
    tz: tzinfo | None = None,
) -> ConnectorMessage:
    """§48 copy: 'Gmail needs reconnecting.' + 'Your email has not synced since 14:42 yesterday.'"""
    subject, plural = _SYNC_SUBJECT.get(kind, (f"Your {name} account", False))
    verb = "have not synced" if plural else "has not synced"
    if last_synced_at is None:
        detail = f"{subject} {verb} yet."
    else:
        detail = f"{subject} {verb} since {since_phrase(last_synced_at, now, tz)}."
    return ConnectorMessage(title=f"{name} needs reconnecting.", detail=detail)


# --------------------------------------------------------------------------- notifications (§42)


class NotifyEvent(str, Enum):
    # Worth interrupting the owner for.
    APPROVAL_NEEDED = "approval_needed"
    BANK_DETAILS_CHANGED = "bank_details_changed"
    PAYMENT_BLOCKED = "payment_blocked"
    RECONNECT_NEEDED = "reconnect_needed"
    BANK_CONSENT_EXPIRED = "bank_consent_expired"  # §47
    AUTHENTICATION_NEEDED = "authentication_needed"  # §9: supplier login needs MFA
    # Quiet success: shown in Activity, never pushed.
    DOCUMENT_PROCESSED = "document_processed"
    INVOICE_RETRIEVED = "invoice_retrieved"
    PAYMENT_MATCHED = "payment_matched"
    DUPLICATE_MERGED = "duplicate_merged"
    SUPPLIER_CHASED = "supplier_chased"
    ACCOUNTANT_ANSWERED = "accountant_answered"
    SYNC_COMPLETED = "sync_completed"
    ITEM_CLOSED = "item_closed"
    MONTH_CLOSED = "month_closed"


NOTIFY_EVENTS: frozenset[NotifyEvent] = frozenset(
    {
        NotifyEvent.APPROVAL_NEEDED,
        NotifyEvent.BANK_DETAILS_CHANGED,
        NotifyEvent.PAYMENT_BLOCKED,
        NotifyEvent.RECONNECT_NEEDED,
        NotifyEvent.BANK_CONSENT_EXPIRED,
        NotifyEvent.AUTHENTICATION_NEEDED,
    }
)


def should_notify(event_kind: NotifyEvent | str) -> bool:
    """§42: notify only for approvals, bank-detail changes / blocked payments and
    reconnection. Everything else, including unknown kinds, stays quiet."""
    try:
        return NotifyEvent(event_kind) in NOTIFY_EVENTS
    except ValueError:
        return False


# --------------------------------------------------------------------------- "Why?" (§54)


class WhyKind(str, Enum):
    INVOICE_TOTAL = "invoice_total"
    BANK_CHARGE = "bank_charge"
    QR_TOTAL = "qr_total"
    DAYS_APART = "days_apart"
    SUPPLIER_VAT_NUMBER = "supplier_vat_number"
    CARD_ENDING = "card_ending"
    BANK_DETAILS = "bank_details"
    PAYMENT_REFERENCE = "payment_reference"
    INVOICE_NUMBER = "invoice_number"
    AMOUNTS_ADD_UP = "amounts_add_up"
    RECURRING = "recurring"


class Cadence(str, Enum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


_AMOUNT_LABEL = {
    WhyKind.INVOICE_TOTAL: "Invoice total",
    WhyKind.BANK_CHARGE: "Bank charge",
    WhyKind.QR_TOTAL: "QR code total",
}
_AGREEMENT_LINES = {
    WhyKind.SUPPLIER_VAT_NUMBER: (
        "Supplier VAT number matches",
        "Supplier VAT number does not match",
    ),
    WhyKind.CARD_ENDING: ("Card ending matches", "Card ending does not match"),
    WhyKind.BANK_DETAILS: ("Bank details match", "Bank details do not match"),
    WhyKind.PAYMENT_REFERENCE: (
        "Payment reference matches",
        "Payment reference does not match",
    ),
    WhyKind.INVOICE_NUMBER: ("Invoice number matches", "Invoice number does not match"),
    WhyKind.AMOUNTS_ADD_UP: ("Amounts add up", "Amounts do not add up"),
}
_CADENCE_TEXT = {
    Cadence.WEEKLY: "every week",
    Cadence.MONTHLY: "every month",
    Cadence.QUARTERLY: "every 3 months",
    Cadence.YEARLY: "every year",
}


@dataclass(frozen=True)
class WhyFactor:
    """One reason behind a conclusion. Build with the classmethods."""

    kind: WhyKind
    amount: Decimal | None = None
    currency: str = "EUR"
    days: int | None = None
    agrees: bool = True
    cadence: Cadence | None = None

    def __post_init__(self) -> None:
        if self.kind in _AMOUNT_LABEL and self.amount is None:
            raise ValueError(f"{self.kind.value} needs an amount")
        if self.kind is WhyKind.DAYS_APART and (self.days is None or self.days < 0):
            raise ValueError("days_apart needs a non-negative number of days")
        if self.kind is WhyKind.RECURRING and self.cadence is None:
            raise ValueError("recurring needs a cadence")

    @classmethod
    def invoice_total(cls, amount: Decimal, currency: str = "EUR") -> WhyFactor:
        return cls(WhyKind.INVOICE_TOTAL, amount=amount, currency=currency)

    @classmethod
    def bank_charge(cls, amount: Decimal, currency: str = "EUR") -> WhyFactor:
        return cls(WhyKind.BANK_CHARGE, amount=amount, currency=currency)

    @classmethod
    def qr_total(cls, amount: Decimal, currency: str = "EUR") -> WhyFactor:
        return cls(WhyKind.QR_TOTAL, amount=amount, currency=currency)

    @classmethod
    def days_apart(cls, days: int) -> WhyFactor:
        return cls(WhyKind.DAYS_APART, days=days)

    @classmethod
    def check(cls, kind: WhyKind, agrees: bool = True) -> WhyFactor:
        if kind not in _AGREEMENT_LINES:
            raise ValueError(f"{kind.value} is not a match check")
        return cls(kind, agrees=agrees)

    @classmethod
    def recurring(cls, cadence: Cadence) -> WhyFactor:
        return cls(WhyKind.RECURRING, cadence=cadence)


def _why_line(f: WhyFactor) -> str:
    if f.kind in _AMOUNT_LABEL:
        assert f.amount is not None  # enforced in __post_init__
        return f"{_AMOUNT_LABEL[f.kind]} {format_money(f.amount, f.currency)}"
    if f.kind is WhyKind.DAYS_APART:
        if f.days == 0:
            return "Same day"
        return f"Dates {f.days} day{'s' if f.days != 1 else ''} apart"
    if f.kind is WhyKind.RECURRING:
        assert f.cadence is not None  # enforced in __post_init__
        return f"Usually paid {_CADENCE_TEXT[f.cadence]}"
    agree, disagree = _AGREEMENT_LINES[f.kind]
    return agree if f.agrees else disagree


def render_why(factors: Sequence[WhyFactor]) -> list[str]:
    """§54 provenance lines, in the given order, e.g. 'Invoice total €83.21'."""
    return [_why_line(f) for f in factors]


# --------------------------------------------------------------------------- linters (§36, §69–70)

_I = re.IGNORECASE
# Most specific first: a later pattern never reports text an earlier one covered.
_JARGON: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("entity classification", re.compile(r"\bentity\s+classification\b", _I)),
    ("source document", re.compile(r"\bsource\s+documents?\b", _I)),
    ("journal entry", re.compile(r"\bjournal\s+entr(?:y|ies)\b", _I)),
    (
        "confidence %",
        re.compile(
            r"\bconfidence\b[^.\n]{0,24}?\d+(?:[.,]\d+)?\s?%"
            r"|\d+(?:[.,]\d+)?\s?%\s+(?:confidence|confident)\b"
            r"|\bconfidence\s+(?:score|level|interval)s?\b",
            _I,
        ),
    ),
    ("reconciliation", re.compile(r"\breconcil\w*", _I)),
    ("exception", re.compile(r"\bexceptions?\b", _I)),
    ("entity", re.compile(r"\bentit(?:y|ies)\b", _I)),
    ("OCR", re.compile(r"\bOCR\w*", _I)),
    ("workflow", re.compile(r"\bwork[\s-]?flows?\b", _I)),
    ("queue", re.compile(r"\bqueu\w*", _I)),
    ("parse", re.compile(r"\bpars(?:e[sdr]?|ers|ing)\b", _I)),
    ("API", re.compile(r"\bAPIs?\b", _I)),
    ("token", re.compile(r"\btokens?\b|\btokeni[sz]\w*", _I)),
    ("ledger", re.compile(r"\bledgers?\b", _I)),
    ("accrual", re.compile(r"\baccruals?\b", _I)),
    ("webhook", re.compile(r"\bweb[\s-]?hooks?\b", _I)),
    ("OAuth", re.compile(r"\bOAuth\b", _I)),
    ("payload", re.compile(r"\bpayloads?\b", _I)),
    ("JSON", re.compile(r"\bJSON\b", _I)),
    ("error code", re.compile(r"\b(?:error|status|http)\s*(?:code\s*)?[1-5]\d{2}\b", _I)),
    ("raw error", re.compile(r"\b[A-Z][A-Za-z]*(?:Error|Exception)\b|\bTraceback\b|\bstack\s?trace\b")),
    (
        "internal ID",
        re.compile(
            r"\b[a-z]{2,8}_[0-9a-f]{16}\b"
            r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            _I,
        ),
    ),
    ("null", re.compile(r"\b(?:null|undefined|NaN)\b")),
)  # fmt: skip

_OFF_TONE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("exclamation mark", re.compile(r"!")),
    ("great job", re.compile(r"\bgreat\s+job\b", _I)),
    ("awesome", re.compile(r"\bawesome\b", _I)),
    ("congratulations", re.compile(r"\bcongrat\w*", _I)),
    ("oops", re.compile(r"\boops\w*", _I)),
)


def _scan(text: str, rules: tuple[tuple[str, re.Pattern[str]], ...]) -> list[str]:
    taken: list[tuple[int, int]] = []
    hits: list[tuple[int, str]] = []
    for term, pattern in rules:
        for m in pattern.finditer(text):
            start, end = m.span()
            if any(start < e and s < end for s, e in taken):
                continue
            taken.append((start, end))
            hits.append((start, term))
    seen: list[str] = []
    for _, term in sorted(hits):
        if term not in seen:
            seen.append(term)
    return seen


def find_jargon(text: str) -> list[str]:
    """Banned terms in owner-facing text, in order of first appearance (§36, §70).

    Covers accounting and engineering jargon, raw errors and internal IDs.
    """
    return _scan(text, _JARGON)


def find_off_tone(text: str) -> list[str]:
    """Personality violations (§69): exclamations and cheerleading."""
    return _scan(text, _OFF_TONE)
