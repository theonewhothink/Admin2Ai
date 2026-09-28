"""Owner- and supplier-facing words for the workflows (§22, §35-36, §69-70).

Short, calm, precise. No accounting jargon, no raw errors, no internal IDs,
no exclamation marks. Every sentence a workflow shows is produced here so it
can be checked with :func:`find_forbidden`.

Formatting mirrors ``backoffice.language`` (money as '€1,492.30', dates as
'18 September'); this module does not import it so the workflow package only
depends on ``backoffice.domain``. Dates are UTC calendar days.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext

from .contracts import (
    MONTH_NAMES,
    ApprovalRecord,
    ApprovalStatus,
    ChasePolicy,
    ItemStatus,
    MonthCloseState,
    MonthStep,
    MonthSummary,
    NeedsYouKind,
    OwnerAnswerKind,
    OwnerOption,
    Period,
    SupplierMessageKind,
    TransactionRef,
)
from .decisions import (
    CardResolution,
    ChasePhase,
    ChaseState,
    EscalationReason,
    Resolution,
    chase_phase,
    delivery_confirmed,
    open_query_ids,
)

__all__ = [
    "APPROVAL_HEADLINE",
    "APPROVAL_REMINDER_HEADLINE",
    "DONE",
    "MINUS",
    "MONTH_NAMES",
    "Card",
    "accountant_question_card",
    "approval_status_text",
    "approval_summary",
    "card_resolution_message",
    "chase_card",
    "chase_status_text",
    "chase_summary",
    "count_times",
    "delivery_unconfirmed_card",
    "document_word",
    "find_forbidden",
    "format_day",
    "format_money",
    "month_closed_headline",
    "month_status_text",
    "month_summary_line",
    "package_ready_card",
    "payment_phrase",
    "plural",
    "resolution_message",
    "safe_reference",
    "still_need",
    "supplier_request_body",
]

DONE = "Done."
MINUS = "−"  # true minus sign, as in backoffice.language
CURRENCY_SYMBOLS: dict[str, str] = {"EUR": "€", "GBP": "£", "USD": "$", "ILS": "₪"}

APPROVAL_HEADLINE = "Approval needed."
APPROVAL_REMINDER_HEADLINE = "This still needs your approval."


@dataclass(frozen=True)
class Card:
    """Text and choices for one Needs-You card (§35: a decision, not a form)."""

    kind: NeedsYouKind
    headline: str
    detail: str
    why: str
    options: tuple[OwnerOption, ...] = ()


# ---------- formatting


def format_money(amount: Decimal | int, currency: str = "EUR") -> str:
    """'€1,492.30' / '−£12.00' / 'CHF 1,492.30'. Decimal only, two decimals."""
    if isinstance(amount, (bool, float)) or not isinstance(amount, (Decimal, int)):
        raise TypeError("money must be Decimal (or int), never float")
    value = Decimal(amount)
    if not value.is_finite():
        raise ValueError("money must be a finite amount")
    code = currency.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ValueError(f"not a currency code: {currency!r}")
    with localcontext() as ctx:
        ctx.prec = max(28, value.adjusted() + 4)
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = MINUS if rounded < 0 else ""
    digits = f"{abs(rounded):,.2f}"
    symbol = CURRENCY_SYMBOLS.get(code)
    return f"{sign}{symbol}{digits}" if symbol else f"{sign}{code} {digits}"


def format_day(day: date | datetime, reference: date | None = None) -> str:
    """'18 September', with the year when it differs from ``reference``."""
    if isinstance(day, datetime):
        day = day.date()
    text = f"{day.day} {MONTH_NAMES[day.month - 1]}"
    if reference is None or reference.year != day.year:
        text = f"{text} {day.year}"
    return text


def plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word if count == 1 else (many or word + 's')}"


def count_times(count: int) -> str:
    return {1: "once", 2: "twice"}.get(count, f"{count} times")


def still_need(count: int) -> str:
    """§69: 'I still need one thing.' / 'I still need 2 things.' / 'Done.'"""
    if count < 0:
        raise ValueError("count cannot be negative")
    if count == 0:
        return DONE
    return "I still need one thing." if count == 1 else f"I still need {count} things."


def _amount(tx: TransactionRef) -> str:
    return format_money(abs(tx.amount), tx.currency)


@dataclass(frozen=True)
class _Words:
    """What the missing document and the money movement are called (§20).

    Money out (negative, as the bank books it) is a payment backed by an
    invoice; money in from a supplier is a refund backed by a credit note.
    """

    document: str
    event: str
    direction: str


_PAYMENT_WORDS = _Words(document="invoice", event="payment", direction="to")
_REFUND_WORDS = _Words(document="credit note", event="refund", direction="from")


def _words(tx: TransactionRef | None) -> _Words:
    return _REFUND_WORDS if tx is not None and tx.amount > 0 else _PAYMENT_WORDS


def document_word(tx: TransactionRef | None) -> str:
    """'invoice' for money out, 'credit note' for a refund."""
    return _words(tx).document


def payment_phrase(tx: TransactionRef, today: date | None = None) -> str:
    """'the €117.20 payment to Vodafone on 18 September' /
    'the €25.00 refund from Vodafone on 18 September'."""
    reference = today or tx.booked_on
    w = _words(tx)
    return (
        f"the {_amount(tx)} {w.event} {w.direction} {tx.supplier_display} "
        f"on {format_day(tx.booked_on, reference)}"
    )


# ---------- supplier (§22)


# An invoice number as suppliers write it: "FT 2026/183", "JJ37MMMM-183",
# "Nº 12.345". It may come from OCR or a bank reference, so anything else
# (line breaks, markup, sentences, very long text) is left out of the email.
_REFERENCE = re.compile(r"[\w./#º°-]+(?: [\w./#º°-]+){0,3}")
_REFERENCE_MAX = 40


def safe_reference(hint: str | None) -> str | None:
    """The hint if it looks like an invoice number, else None (never injected)."""
    if not hint or any(ord(ch) < 32 or ord(ch) == 127 for ch in hint):
        return None
    collapsed = " ".join(hint.split())
    if not collapsed or len(collapsed) > _REFERENCE_MAX:
        return None
    return collapsed if _REFERENCE.fullmatch(collapsed) else None


def supplier_request_body(tx: TransactionRef, kind: SupplierMessageKind, today: date) -> str:
    """§22 wording: 'Hello, could you please resend invoice FT 2026/183 relating
    to the €117.20 payment dated 18 September? Thank you.'"""
    w = _words(tx)
    hint = safe_reference(tx.invoice_number_hint)
    what = f"resend {w.document} {hint} relating to" if hint else f"send the {w.document} for"
    opener = "Hello, just following up:" if kind is SupplierMessageKind.REMINDER else "Hello,"
    return (
        f"{opener} could you please {what} the {_amount(tx)} {w.event} "
        f"dated {format_day(tx.booked_on, today)}? Thank you."
    )


# ---------- missing invoice (§22, §35-36)


def _missing(w: _Words) -> str:
    """§36: 'We can't find the invoice for this payment.'"""
    return f"We can't find the {w.document} for this {w.event}."


def _unconfirmed(w: _Words) -> str:
    return f"I found a document, but I can't confirm it's the {w.document} for this {w.event}."


def _conflict(w: _Words) -> str:
    return f"The {w.document} doesn't agree with the {w.event}."


def _why_missing(w: _Words) -> str:
    return f"Every {w.event} needs its {w.document} before the month can close."


_WHY_CONFLICT = "When documents disagree, a person decides. I never guess."


def _chase_options(tx: TransactionRef, reason: EscalationReason) -> tuple[OwnerOption, ...]:
    who, doc = tx.supplier_display, document_word(tx)
    keep = (
        f"I added {who}'s email address"
        if reason is EscalationReason.NO_SUPPLIER_CONTACT
        else f"Keep asking {who}"
    )
    return (
        OwnerOption(key=OwnerAnswerKind.DOCUMENT_PROVIDED.value, label=f"Upload the {doc}"),
        OwnerOption(key=OwnerAnswerKind.NO_DOCUMENT_NEEDED.value, label=f"No {doc} needed"),
        OwnerOption(key=OwnerAnswerKind.KEEP_CHASING.value, label=keep),
        OwnerOption(key=OwnerAnswerKind.OWNER_WILL_HANDLE.value, label="I'll handle it"),
    )


def chase_card(
    reason: EscalationReason, tx: TransactionRef, state: ChaseState, today: date
) -> Card:
    """The Needs-You card for a missing-invoice (or credit-note) escalation."""
    w = _words(tx)
    who = tx.supplier_display
    facts = f"{_amount(tx)} {w.direction} {who} on {format_day(tx.booked_on, today)}."
    detail = {
        EscalationReason.NO_REPLY: (
            f"{facts} I asked {who} {count_times(max(state.messages_sent, 1))} and didn't get it."
        ),
        EscalationReason.NOT_ALLOWED_TO_ASK: (
            f"{facts} I looked everywhere I can. I haven't asked {who}, "
            "because you haven't allowed me to contact suppliers."
        ),
        EscalationReason.NO_SUPPLIER_CONTACT: (f"{facts} I don't have an email address for {who}."),
        EscalationReason.UNCONFIRMED: facts,
        EscalationReason.CONFLICT: f"{facts} The details don't agree, so I didn't guess.",
    }[reason]
    kind, headline, why = {
        EscalationReason.CONFLICT: (NeedsYouKind.CONFLICT, _conflict(w), _WHY_CONFLICT),
        EscalationReason.UNCONFIRMED: (
            NeedsYouKind.UNCONFIRMED_DOCUMENT,
            _unconfirmed(w),
            _why_missing(w),
        ),
        EscalationReason.NO_SUPPLIER_CONTACT: (
            NeedsYouKind.NO_SUPPLIER_CONTACT,
            _missing(w),
            _why_missing(w),
        ),
    }.get(reason, (NeedsYouKind.MISSING_DOCUMENT, _missing(w), _why_missing(w)))
    return Card(kind, headline, detail, why, _chase_options(tx, reason))


_RESOLUTION_TEXT: dict[Resolution, str] = {
    Resolution.FOUND: "Done. I found the {doc}.",
    Resolution.NOT_NEEDED: DONE,
    Resolution.CHECKING_OWNER_DOCUMENT: "Thanks. I'm checking it now.",
    Resolution.KEEP_CHASING: "OK. I'll keep asking.",
    Resolution.OWNER_WILL_HANDLE: "OK. I'll close this when the {doc} arrives.",
    Resolution.SUPERSEDED: "I have a newer question about this {event}.",
    Resolution.CANCELLED: "No longer needed.",
}


def resolution_message(resolution: Resolution, tx: TransactionRef | None = None) -> str:
    """How an owner card is closed; ``tx`` picks invoice/credit-note wording."""
    w = _words(tx)
    return _RESOLUTION_TEXT[resolution].format(doc=w.document, event=w.event)


def _latest_day(state: ChaseState, tx: TransactionRef) -> date:
    stamps = [
        t for t in (state.searched_at, state.last_message_at, state.escalated_at) if t is not None
    ]
    return max(stamps).date() if stamps else tx.booked_on


def chase_summary(status: ItemStatus, tx: TransactionRef) -> str:
    phrase, doc = payment_phrase(tx), document_word(tx)
    return {
        ItemStatus.CLOSED: f"Done. The {doc} for {phrase} is here and checks out.",
        ItemStatus.NOT_REQUIRED: f"Done. No {doc} needed for {phrase}.",
        ItemStatus.CANCELLED: f"Stopped looking for the {doc} for {phrase}.",
    }[status]


def chase_status_text(state: ChaseState, tx: TransactionRef, policy: ChasePolicy) -> str:
    """Plain answer to 'what is happening with this invoice?' (§34, §69)."""
    if state.final is not None:
        return chase_summary(state.final.status, tx)
    today = _latest_day(state, tx)
    who, w = tx.supplier_display, _words(tx)
    phase = chase_phase(state, policy)
    if phase is ChasePhase.NEEDS_OWNER:
        reason = state.escalation or EscalationReason.CONFLICT
        line = {
            EscalationReason.CONFLICT: _conflict(w),
            EscalationReason.UNCONFIRMED: _unconfirmed(w),
        }.get(reason, _missing(w))
        return f"I still need one thing. {line}"
    if phase is ChasePhase.OWNER_HANDLING:
        return f"You said you'll handle this. I'll close it when the {w.document} arrives."
    if phase is ChasePhase.WAITING_FOR_SUPPLIER:
        assert state.last_message_at is not None
        sent_on = format_day(state.last_message_at, today)
        first = (
            f"I asked {who} for the {w.document} on {sent_on}."
            if state.messages_sent == 1
            else f"I reminded {who} on {sent_on}."
        )
        if state.supplier_replied:
            return f"{first} They replied, but the {w.document} wasn't there yet."
        return f"{first} Waiting for their reply."
    if phase is ChasePhase.WAITING_TO_ASK:
        assert policy.chase_not_before is not None
        return (
            f"I'm still looking for the {w.document} for {payment_phrase(tx, today)}. "
            f"If it doesn't turn up, I'll ask {who} on "
            f"{format_day(policy.chase_not_before, today)}."
        )
    return f"Looking for the {w.document} for {payment_phrase(tx, today)}."


# ---------- approval (§25)


def approval_summary(status: ApprovalStatus, record: ApprovalRecord | None) -> str:
    """'Approved by Ana Silva on 18 September.' / 'No longer needed. Nothing was done.'"""
    if status is ApprovalStatus.WITHDRAWN or record is None:
        return "No longer needed. Nothing was done."
    who = f" by {record.actor_display_name}" if record.actor_display_name else ""
    when = format_day(record.decided_at, record.decided_at.date())
    if status is ApprovalStatus.APPROVED:
        return f"Approved{who} on {when}."
    return f"Rejected{who} on {when}. Nothing was done."


def approval_status_text(
    summary: str,
    status: ApprovalStatus | None,
    record: ApprovalRecord | None = None,
) -> str:
    if status is None:
        return f"Waiting for your approval. {summary.strip()}"
    return approval_summary(status, record)


# ---------- month close (§2, §27)


def month_closed_headline(period: Period) -> str:
    """§2: 'September is closed.'"""
    return f"{MONTH_NAMES[period.month - 1]} is closed."


def month_summary_line(summary: MonthSummary) -> str:
    """§2: '218 transactions checked · 186 documents collected · …'"""
    parts = [
        f"{plural(summary.transactions_checked, 'transaction')} checked",
        f"{plural(summary.documents_collected, 'document')} collected",
        f"{plural(summary.missing_documents_retrieved, 'missing document')} "
        "retrieved automatically",
        f"{plural(summary.suppliers_chased, 'supplier')} chased",
        f"{plural(summary.accountant_questions_resolved, 'accountant question')} resolved",
        f"{plural(summary.tax_obligations_verified, 'tax obligation')} verified",
        f"{plural(summary.unresolved_issues, 'unresolved issue')}",
    ]
    line = " · ".join(parts) + "."
    if summary.owner_minutes is not None:
        line += f" You spent {plural(summary.owner_minutes, 'minute')}."
    return line


def _month_blockers(state: MonthCloseState) -> int:
    verdict = state.last_verdict
    open_items = verdict.open_items if verdict else 0
    return open_items + len(open_query_ids(state)) + (0 if delivery_confirmed(state) else 1)


def month_status_text(
    state: MonthCloseState,
    period: Period,
    next_step: MonthStep | None,
    next_at: datetime | None,
) -> str:
    """Where the month stands, in one calm line (§34)."""
    month = MONTH_NAMES[period.month - 1]
    resolved = set(state.resolved_subject_ids)
    missing = sum(1 for gap in state.gaps if gap.transaction_id not in resolved)
    if next_step is None:
        return month_closed_headline(period)
    if next_step is MonthStep.COMPLETENESS_AUDIT:
        when = f" on {format_day(next_at, next_at.date())}" if next_at else ""
        return f"{month}: I'll check what's missing{when}."
    if next_step is MonthStep.EVIDENCE_RETRIEVAL:
        if missing == 0:
            return f"{month}: nothing missing so far."
        return f"{month}: {plural(missing, 'document')} missing. I'll look for them."
    if next_step is MonthStep.SUPPLIER_CHASES:
        return f"{month}: looking for {plural(missing, 'missing document')}."
    if next_step is MonthStep.PACKAGE:
        if missing == 0:
            return f"{month}: nothing missing. Preparing your accountant's package."
        # Not "asking suppliers": that may not be allowed (§25); say only what is true.
        return f"{month}: still looking for {plural(missing, 'missing document')}."
    if next_step is MonthStep.DELIVERY_CONFIRMATION:
        if state.delivery and state.delivery.delivered:
            return f"{month}: sent to your accountant."
        return f"{month}: your accountant's package is ready."
    if next_step is MonthStep.ACCOUNTANT_QUERIES:
        if delivery_confirmed(state):
            return f"{month}: your accountant has everything."
        return f"{month}: sent to your accountant."
    blockers = _month_blockers(state)
    if state.last_verdict is None or blockers == 0:
        return f"{month}: making sure everything is closed."
    return f"{month}: {still_need(blockers)}"


def package_ready_card(period: Period, allowed: bool) -> Card:
    """``allowed``: delivery was permitted but did not go through."""
    month = MONTH_NAMES[period.month - 1]
    return Card(
        kind=NeedsYouKind.PACKAGE_READY,
        headline=f"Your {month} package for your accountant is ready.",
        detail=(
            "I couldn't send it to your accountant."
            if allowed
            else "I'm not allowed to send it for you yet."
        ),
        why="Your accountant needs it to finish the month.",
        options=(
            OwnerOption(key="send_for_me", label="Send it for me"),
            OwnerOption(key="sent_myself", label="I sent it myself"),
        ),
    )


def delivery_unconfirmed_card(period: Period, sent_on: date | None) -> Card:
    month = MONTH_NAMES[period.month - 1]
    sent = f" It was sent on {format_day(sent_on, sent_on)}." if sent_on else ""
    return Card(
        kind=NeedsYouKind.DELIVERY_UNCONFIRMED,
        headline=f"I couldn't confirm your accountant received the {month} package.",
        detail=f"{sent.strip()} Could you check with them?".strip(),
        why="The month closes only when your accountant has everything.",
        options=(OwnerOption(key="they_have_it", label="They have it"),),
    )


_CARD_RESOLUTION_TEXT: dict[CardResolution, str] = {
    CardResolution.SENT: "Done. I sent it to your accountant.",
    CardResolution.RECEIVED: "Done. Your accountant has it.",
    CardResolution.ANSWERED: DONE,
    CardResolution.SUPERSEDED: "No longer needed.",
    CardResolution.MONTH_CLOSED: DONE,
}


def card_resolution_message(resolution: CardResolution) -> str:
    """How a month-close card is taken down once its situation resolved."""
    return _CARD_RESOLUTION_TEXT[resolution]


def accountant_question_card(question: str) -> Card:
    return Card(
        kind=NeedsYouKind.ACCOUNTANT_QUESTION,
        headline="Your accountant has a question.",
        detail=question.strip(),
        why="Your accountant needs this to finish the month.",
    )


# ---------- linter (§36, §69-70)

_I = re.IGNORECASE
_FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("reconciliation", re.compile(r"\breconcil\w*", _I)),
    ("exception", re.compile(r"\bexceptions?\b", _I)),
    ("entity", re.compile(r"\bentit(?:y|ies)\b", _I)),
    ("ledger", re.compile(r"\bledgers?\b", _I)),
    ("accrual", re.compile(r"\baccruals?\b", _I)),
    ("journal entry", re.compile(r"\bjournal\s+entr(?:y|ies)\b", _I)),
    ("source document", re.compile(r"\bsource\s+documents?\b", _I)),
    ("OCR", re.compile(r"\bOCR\w*", _I)),
    ("workflow", re.compile(r"\bwork[\s-]?flows?\b", _I)),
    ("activity", re.compile(r"\bactivit(?:y|ies)\b", _I)),
    ("signal", re.compile(r"\bsignals?\b", _I)),
    ("queue", re.compile(r"\bqueu\w*", _I)),
    ("Temporal", re.compile(r"\btemporal\b", _I)),
    ("timeout", re.compile(r"\btime[\s-]?outs?\b", _I)),
    ("API", re.compile(r"\bAPIs?\b")),
    ("payload", re.compile(r"\bpayloads?\b", _I)),
    ("JSON", re.compile(r"\bJSON\b", _I)),
    ("raw error", re.compile(r"\b[A-Z][A-Za-z]*(?:Error|Exception)\b|\bTraceback\b")),
    (
        "internal ID",
        re.compile(
            r"\b[a-z]{2,8}_[0-9a-f]{16}\b"
            r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
            r"|\bmissing-invoice:",
            _I,
        ),
    ),
    ("null", re.compile(r"\b(?:null|None|undefined|NaN)\b")),
    ("exclamation mark", re.compile(r"!")),
    ("cheerleading", re.compile(r"\b(?:great\s+job|awesome|congrat\w*|oops\w*)\b", _I)),
)  # fmt: skip


def find_forbidden(text: str) -> list[str]:
    """Jargon, raw errors, internal IDs and off-tone words in owner text."""
    return [term for term, pattern in _FORBIDDEN if pattern.search(text)]
