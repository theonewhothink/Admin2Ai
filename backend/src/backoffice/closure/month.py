"""Month status: is this company's month closed, and if not, why not (§2, §3, §35, §47–48, §57).

A month is ``CLOSED`` only when all of these hold:

1. at least one source is connected, and every connector is healthy and has
   synced the whole month (a gap means the month can never be green, §48);
2. the month is over in the business's time zone;
3. every tracked item is ``CLOSED`` or ``NOT_REQUIRED`` **with GREEN quality**
   (AMBER is never promoted to GREEN, §57; a conflict is never averaged, §19);
4. every transaction that has an expected-evidence decision has a tracked item;
5. no obligation of the company due on or before the month's last day is
   still waiting for proof (§24).

``percent_closed`` is ``done / total`` over tracked items plus untracked
transactions, optionally weighted (e.g. by amount), rounded **down**, and never
100 unless the month is actually closed.

Counts for the §2 north-star summary come from the items, the obligations and
the activity/interaction log (:mod:`.activity`); nothing is estimated.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from typing import Protocol, runtime_checkable

from backoffice.domain.lifecycle import ORDER, TERMINAL, Stage, TrackedItem
from backoffice.domain.models import Obligation, ObligationKind, Quality

from ._text import (
    MIDDLE_DOT,
    count_phrase,
    day_month,
    minutes_from_seconds,
    require_aware,
    require_money,
    since_phrase,
)
from .activity import (
    Activity,
    ActivityKind,
    Actor,
    OwnerInteraction,
    activities_for,
    distinct_subjects,
    interactions_for,
)
from .period import Month

__all__ = [
    "EXPECTED_INVOICE",
    "TAX_OBLIGATION_KINDS",
    "Blocker",
    "BlockerKind",
    "CloseSummary",
    "ClosedSummaryText",
    "ConnectorCoverage",
    "EvidenceDecision",
    "ItemCounts",
    "ItemState",
    "MonthNotClosed",
    "MonthState",
    "MonthStatus",
    "classify_item",
    "closed_summary",
    "compute_month_status",
    "is_open_obligation",
    "render_closed_summary",
]

# Obligations counted as "tax obligations verified" in the §2 summary.
TAX_OBLIGATION_KINDS: frozenset[ObligationKind] = frozenset(
    {ObligationKind.TAX_DEADLINE, ObligationKind.FILING, ObligationKind.VAT_RETURN}
)
# Tracked subject: a recurring supplier invoice that is overdue (§23). Missing until it arrives.
EXPECTED_INVOICE = "expected_invoice"
_MICROSECOND = timedelta(microseconds=1)


# --------------------------------------------------------------------------- inputs


@runtime_checkable
class ConnectorCoverage(Protocol):
    """What month closing needs to know about a connector (§47).

    ``covered_from``/``covered_until`` bound the *contiguous* synced window.
    An optional ``last_synced_at`` attribute, when present, is used for the
    "has not synced since" wording instead of ``covered_until``.
    """

    @property
    def name(self) -> str: ...

    @property
    def healthy(self) -> bool: ...

    @property
    def covered_from(self) -> datetime | None: ...

    @property
    def covered_until(self) -> datetime | None: ...


@runtime_checkable
class EvidenceDecision(Protocol):
    """Expected-evidence decision for one transaction (§21)."""

    @property
    def transaction_id(self) -> str: ...

    @property
    def quality(self) -> Quality: ...

    @property
    def requires_document(self) -> bool: ...


# --------------------------------------------------------------------------- outputs


class MonthState(str, Enum):
    CLOSED = "closed"
    ON_TRACK = "on_track"  # the system is still working; nothing needed from the owner
    NEEDS_OWNER = "needs_owner"  # an answer, a conflict or a reconnection


class ItemState(str, Enum):
    DONE = "done"  # CLOSED / NOT_REQUIRED with GREEN quality
    UNPROVEN = "unproven"  # set aside without verified evidence (AMBER/RED)
    NEEDS_OWNER = "needs_owner"
    CONFLICT = "conflict"
    IN_PROGRESS = "in_progress"
    UNTRACKED = "untracked"  # a decided transaction nobody is tracking yet


class BlockerKind(str, Enum):
    NO_SOURCES = "no_sources"
    CONNECTOR = "connector"
    # A connector catching up on days it missed (a known gap inside its window), or days its provider no longer
    # serves: the month stays open, never green, until they are read (§47).
    CATCHING_UP = "catching_up"
    NEEDS_OWNER = "needs_owner"
    CONFLICT = "conflict"
    OBLIGATION = "obligation"
    MISSING_DOCUMENTS = "missing_documents"
    UNPROVEN = "unproven"
    UNTRACKED = "untracked"
    IN_PROGRESS = "in_progress"
    MONTH_NOT_OVER = "month_not_over"


_BLOCKER_ORDER = {kind: i for i, kind in enumerate(BlockerKind)}


@dataclass(frozen=True, slots=True)
class Blocker:
    """One reason the month is not closed. ``message`` is owner-facing; ``refs`` never are."""

    kind: BlockerKind
    message: str
    count: int = 1
    needs_owner: bool = False
    action: str | None = None  # button label, e.g. "Reconnect"
    refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ItemCounts:
    done: int = 0
    unproven: int = 0
    needs_owner: int = 0
    conflict: int = 0
    in_progress: int = 0
    untracked: int = 0

    @property
    def total(self) -> int:
        return (
            self.done + self.unproven + self.needs_owner
            + self.conflict + self.in_progress + self.untracked
        )  # fmt: skip


@dataclass(frozen=True, slots=True)
class CloseSummary:
    """The §2 north-star counts for one company and month."""

    transactions_checked: int = 0
    documents_collected: int = 0
    missing_documents_retrieved: int = 0  # automatically, by the system (§22)
    suppliers_chased: int = 0
    accountant_questions_resolved: int = 0
    tax_obligations_verified: int = 0
    unresolved_issues: int = 0
    owner_minutes: int = 0


@dataclass(frozen=True, slots=True)
class MonthStatus:
    entity_id: str
    month: Month
    as_of: datetime
    state: MonthState
    percent_closed: int
    counts: ItemCounts
    needs_you: int
    missing_documents: int
    open_obligations: tuple[str, ...]
    blockers: tuple[Blocker, ...]
    summary: CloseSummary
    connectors_ok: bool = field(default=False)
    # The (optionally weighted) figures behind percent_closed, so views that combine
    # companies agree with each company's own percentage (§35).
    weighted_done: Decimal = Decimal(0)
    weighted_total: Decimal = Decimal(0)

    @property
    def closed(self) -> bool:
        return self.state is MonthState.CLOSED

    @property
    def done_share(self) -> Fraction:
        """Exact done share behind ``percent_closed`` (1 when closed, 0 when nothing is weighed)."""
        if self.closed:
            return Fraction(1)
        if self.weighted_total <= 0:
            return Fraction(0)
        return Fraction(self.weighted_done) / Fraction(self.weighted_total)

    @property
    def items_total(self) -> int:
        return self.counts.total

    @property
    def items_done(self) -> int:
        return self.counts.done

    @property
    def label(self) -> str:
        """``'September'`` (or ``'September 2025'`` when not the current year)."""
        return self.month.label(self.as_of.date())

    @property
    def headline(self) -> str:
        """``'September is closed.'`` / ``'September is 94% closed.'``"""
        if self.closed:
            return f"{self.label} is closed."
        return f"{self.label} is {self.percent_closed}% closed."

    @property
    def company_status(self) -> str:
        """§35 per-company status: Closed / On track / Needs one answer / Needs reconnecting."""
        if self.closed:
            return "Closed"
        kinds = {b.kind for b in self.blockers if b.needs_owner}
        if BlockerKind.NO_SOURCES in kinds:
            return "Needs connecting"
        if BlockerKind.CONNECTOR in kinds:
            return "Needs reconnecting"
        if self.needs_you == 1:
            return "Needs one answer"
        if self.needs_you > 1:
            return f"Needs {self.needs_you} answers"
        return "On track"

    def reasons(self) -> list[str]:
        """Owner-facing reasons the month is not closed, most important first."""
        return [b.message for b in self.blockers]


class MonthNotClosed(ValueError):
    """Raised when a closed-month summary is requested for an open month (§3)."""


# --------------------------------------------------------------------------- item classification


def _linear_stage(item: TrackedItem) -> Stage:
    """Where the item is on the golden path, looking through side states."""
    if item.stage in ORDER or item.stage is Stage.NOT_REQUIRED:
        return item.stage
    for t in reversed(item.history):
        if t.to_stage in ORDER:
            return t.to_stage
    return Stage.DISCOVERED


def _reached(item: TrackedItem, stage: Stage) -> bool:
    """True if the item ever reached ``stage`` (or later) on the golden path."""
    target = ORDER.index(stage)
    stages = [item.stage, *(t.to_stage for t in item.history)]
    return any(s in ORDER and ORDER.index(s) >= target for s in stages)


def classify_item(item: TrackedItem) -> ItemState:
    """Map a tracked item to its closing state. Only GREEN terminal items are DONE (§57)."""
    if item.stage in TERMINAL:
        return ItemState.DONE if item.quality is Quality.GREEN else ItemState.UNPROVEN
    if item.stage is Stage.CONFLICT or item.quality is Quality.RED:
        return ItemState.CONFLICT
    if item.stage is Stage.NEEDS_OWNER:
        return ItemState.NEEDS_OWNER
    return ItemState.IN_PROGRESS


def is_open_obligation(obligation: Obligation, month: Month) -> bool:
    """Due on or before the month's last day and not yet satisfied by evidence (§24)."""
    return not obligation.satisfied_by_evidence_ids and obligation.due_on <= month.last_day


# --------------------------------------------------------------------------- wording

_NOUNS = {
    "transaction": ("payment", "payments"),
    "document": ("document", "documents"),
    "obligation": ("deadline", "deadlines"),
    "expected_invoice": ("invoice", "invoices"),
}


def _things(n: int, subject_types: Sequence[str]) -> str:
    """'3 payments' when all items share a known type, otherwise '3 things'."""
    kinds = set(subject_types)
    one, many = ("thing", "things")
    if len(kinds) == 1:
        one, many = _NOUNS.get(next(iter(kinds)), (one, many))
    return count_phrase(n, one, many)


def _verb(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


# --------------------------------------------------------------------------- connectors (§47–48)


def _connector_blockers(
    connectors: Sequence[ConnectorCoverage], month: Month, now: datetime, tz: tzinfo
) -> list[Blocker]:
    if not connectors:
        return [
            Blocker(
                BlockerKind.NO_SOURCES,
                "No accounts are connected yet.",
                needs_owner=True,
                action="Connect",
            )
        ]
    start, end = month.start(tz), month.end(tz)
    blockers = []
    for connector in sorted(connectors, key=lambda c: c.name):
        blocker = _connector_blocker(connector, month, start, end, now, tz)
        if blocker is not None:
            blockers.append(blocker)
            continue
        blocker = _gap_blocker(connector, start, end, now, tz)
        if blocker is not None:
            blockers.append(blocker)
    return blockers


def _gap_blocker(c: ConnectorCoverage, start: datetime, end: datetime, now: datetime, tz: tzinfo) -> Blocker | None:
    """Days inside the connector's window it has not read yet and that touch the month (``gaps`` on the
    connector, optional: (start, end, reachable)). A mailbox catches up on them by itself; days a bank no longer
    serves need a statement from the owner."""
    gaps = [g for g in getattr(c, "gaps", ()) or () if g[0] < end and start < g[1]]
    if not gaps:
        return None
    kind = getattr(c, "kind", "")
    account = getattr(c, "account", "") or c.name
    missing = [g for g in gaps if len(g) > 2 and not g[2]]
    if missing:
        first = min(g[0] for g in missing).astimezone(tz).date()
        last = (max(g[1] for g in missing) - _MICROSECOND).astimezone(tz).date()
        today = now.astimezone(tz).date()
        when = (f"on {day_month(first, today)}" if first == last
                else f"from {day_month(first, today)} to {day_month(last, today)}")
        return Blocker(BlockerKind.CATCHING_UP,
                       f"{c.name} no longer shares the payments {when} with me. Send me a bank statement for "
                       "those days so I can close the month.",
                       needs_owner=True, refs=(c.name,))
    seconds = sum((g[1] - g[0]).total_seconds() for g in gaps)
    days = max(1, int(seconds / 86400 + 0.5))  # whole days, as the owner counts them
    what = "email" if kind == "email" else "payments" if kind == "bank" else "documents"
    return Blocker(BlockerKind.CATCHING_UP, f"Catching up on {count_phrase(days, 'day')} of {what} from {account}.",
                   refs=(c.name,))


def _connector_blocker(
    c: ConnectorCoverage,
    month: Month,
    start: datetime,
    end: datetime,
    now: datetime,
    tz: tzinfo,
) -> Blocker | None:
    covered_from = None if c.covered_from is None else require_aware(c.covered_from, "covered_from")
    covered_until = None if c.covered_until is None else require_aware(c.covered_until, "covered_until")
    last_synced = getattr(c, "last_synced_at", None) or covered_until
    if not c.healthy:
        if last_synced is None:
            detail = f"{c.name} has not synced yet."
        else:
            detail = f"{c.name} has not synced since {since_phrase(last_synced, now, tz)}."
        return Blocker(BlockerKind.CONNECTOR, detail, needs_owner=True, action="Reconnect", refs=(c.name,))
    if covered_from is None or covered_until is None:
        return Blocker(BlockerKind.CONNECTOR, f"{c.name} has not synced yet.", refs=(c.name,))
    if covered_from > start:
        return Blocker(
            BlockerKind.CONNECTOR,
            f"{c.name} is still syncing back to {day_month(month.first_day, now.astimezone(tz).date())}.",
            refs=(c.name,),
        )
    if now >= end and covered_until < end:
        return Blocker(
            BlockerKind.CONNECTOR,
            f"Waiting for {c.name} to catch up to the end of {month.name}.",
            refs=(c.name,),
        )
    return None


# --------------------------------------------------------------------------- items


@dataclass(slots=True)
class _ItemBuckets:
    states: dict[str, ItemState] = field(default_factory=dict)
    missing: list[TrackedItem] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)


def _requires_document(decisions: Sequence[EvidenceDecision]) -> dict[str, bool]:
    """Transaction id -> needs a document. Conflicting decisions: the stricter one wins."""
    needs: dict[str, bool] = {}
    for d in decisions:
        needs[d.transaction_id] = needs.get(d.transaction_id, False) or bool(d.requires_document)
    return needs


def _before_match(item: TrackedItem) -> bool:
    """No document has been matched to it yet."""
    stage = _linear_stage(item)
    return stage in ORDER and ORDER.index(stage) < ORDER.index(Stage.MATCHED)


def _bucket_items(items: Sequence[TrackedItem], decisions: Sequence[EvidenceDecision]) -> _ItemBuckets:
    buckets = _ItemBuckets()
    needs_doc = _requires_document(decisions)
    tracked_tx: set[str] = set()
    for item in items:
        if item.id in buckets.states:
            raise ValueError(f"duplicate tracked item {item.id}")
        state = classify_item(item)
        buckets.states[item.id] = state
        if item.subject_type == EXPECTED_INVOICE:
            # A supplier's usual invoice that has not arrived (§23): a missing document until it does.
            if state is not ItemState.DONE and _before_match(item):
                buckets.missing.append(item)
            continue
        if item.subject_type != "transaction":
            continue
        tracked_tx.add(item.subject_id)
        if state is not ItemState.DONE and needs_doc.get(item.subject_id, False) and _before_match(item):
            buckets.missing.append(item)
    buckets.untracked = sorted(tx for tx in needs_doc if tx not in tracked_tx)
    return buckets


def _item_blockers(items: Sequence[TrackedItem], buckets: _ItemBuckets) -> list[Blocker]:
    by_state: dict[ItemState, list[TrackedItem]] = {s: [] for s in ItemState}
    missing_ids = {i.id for i in buckets.missing}
    for item in items:
        state = buckets.states[item.id]
        if state is ItemState.IN_PROGRESS and item.id in missing_ids:
            continue  # reported as missing documents instead
        by_state[state].append(item)

    blockers: list[Blocker] = []
    asks = by_state[ItemState.NEEDS_OWNER]
    if asks:
        n = len(asks)
        text = "I need one answer from you." if n == 1 else f"I need {n} answers from you."
        blockers.append(Blocker(BlockerKind.NEEDS_OWNER, text, n, True, refs=_ids(asks)))
    conflicts = by_state[ItemState.CONFLICT]
    if conflicts:
        n = len(conflicts)
        text = f"{_things(n, [i.subject_type for i in conflicts])} {_verb(n, 'has', 'have')} details that don't agree."
        blockers.append(Blocker(BlockerKind.CONFLICT, text, n, True, refs=_ids(conflicts)))
    missing = [i for i in buckets.missing if buckets.states[i.id] is ItemState.IN_PROGRESS]
    if missing:
        n = len(missing)
        text = f"I'm still looking for {count_phrase(n, 'document')}."
        blockers.append(Blocker(BlockerKind.MISSING_DOCUMENTS, text, n, refs=_ids(missing)))
    unproven = by_state[ItemState.UNPROVEN]
    if unproven:
        n = len(unproven)
        text = f"{_things(n, [i.subject_type for i in unproven])} still {_verb(n, 'needs', 'need')} proof."
        blockers.append(Blocker(BlockerKind.UNPROVEN, text, n, refs=_ids(unproven)))
    if buckets.untracked:
        n = len(buckets.untracked)
        text = f"I haven't started on {count_phrase(n, 'payment')} yet."
        blockers.append(Blocker(BlockerKind.UNTRACKED, text, n, refs=tuple(buckets.untracked)))
    working = by_state[ItemState.IN_PROGRESS]
    if working:
        n = len(working)
        text = f"I'm still checking {_things(n, [i.subject_type for i in working])}."
        blockers.append(Blocker(BlockerKind.IN_PROGRESS, text, n, refs=_ids(working)))
    return blockers


def _ids(items: Sequence[TrackedItem]) -> tuple[str, ...]:
    return tuple(sorted(i.id for i in items))


def _counts(buckets: _ItemBuckets) -> ItemCounts:
    c = Counter(buckets.states.values())
    return ItemCounts(
        done=c[ItemState.DONE],
        unproven=c[ItemState.UNPROVEN],
        needs_owner=c[ItemState.NEEDS_OWNER],
        conflict=c[ItemState.CONFLICT],
        in_progress=c[ItemState.IN_PROGRESS],
        untracked=len(buckets.untracked),
    )


# --------------------------------------------------------------------------- obligations (§24)


def _owner_overdue(obligation: Obligation, today: date) -> bool:
    return obligation.responsible == "owner" and obligation.due_on < today


def _obligation_blockers(open_obligations: Sequence[Obligation], today: date) -> list[Blocker]:
    blockers = []
    for o in sorted(open_obligations, key=lambda o: (o.due_on, o.id)):
        when = day_month(o.due_on, today)
        if o.due_on < today:
            text = f"{o.title} was due on {when}. I haven't seen proof yet."
        else:
            text = f"{o.title} is due on {when}."
        blockers.append(
            Blocker(BlockerKind.OBLIGATION, text, 1, _owner_overdue(o, today), refs=(o.id,))
        )
    return blockers


# --------------------------------------------------------------------------- percent


def _weighed(
    items: Sequence[TrackedItem],
    buckets: _ItemBuckets,
    weights: Mapping[str, Decimal] | None,
    default_weight: Decimal,
) -> tuple[Decimal, Decimal]:
    """(done weight, total weight) over tracked items plus untracked transactions."""

    def weight(subject_id: str) -> Decimal:
        if weights is None:
            return Decimal(1)
        w = require_money(weights.get(subject_id, default_weight), "weight")
        if w < 0:
            raise ValueError("weights cannot be negative")
        return w

    total = Decimal(0)
    done = Decimal(0)
    for item in items:
        w = weight(item.subject_id)
        total += w
        if buckets.states[item.id] is ItemState.DONE:
            done += w
    for tx in buckets.untracked:
        total += weight(tx)
    return done, total


def _percent(done: Decimal, total: Decimal, closed: bool) -> int:
    """Done share, rounded down; 100 only for a closed month (never overstate, §57)."""
    if closed:
        return 100
    if total == 0:
        return 0
    return min(int((done * 100) // total), 99)


# --------------------------------------------------------------------------- summary counts (§2)


def _summary(
    entity_id: str,
    month: Month,
    tz: tzinfo,
    items: Sequence[TrackedItem],
    buckets: _ItemBuckets,
    obligations: Sequence[Obligation],
    activities: Sequence[Activity],
    interactions: Sequence[OwnerInteraction],
    unresolved: int,
) -> CloseSummary:
    done = [i for i in items if buckets.states[i.id] is ItemState.DONE]
    acts = activities_for(activities, month, tz, entity_id)

    def of_kind(kind: ActivityKind, actor: Actor | None = None) -> list[Activity]:
        return [a for a in acts if a.kind is kind and (actor is None or a.actor is actor)]

    owner_seconds = sum(i.active_seconds for i in interactions_for(interactions, month, tz, entity_id))
    # Distinct subjects: a transaction tracked twice (e.g. re-imported) is one transaction.
    return CloseSummary(
        transactions_checked=len({i.subject_id for i in done if i.subject_type == "transaction"}),
        documents_collected=len(
            {
                i.subject_id
                for i in items
                if i.subject_type == "document"
                and i.stage is not Stage.NOT_REQUIRED
                and _reached(i, Stage.ACQUIRED)
            }
        ),
        missing_documents_retrieved=distinct_subjects(
            of_kind(ActivityKind.MISSING_DOCUMENT_RETRIEVED, Actor.SYSTEM)
        ),
        suppliers_chased=distinct_subjects(of_kind(ActivityKind.SUPPLIER_CHASED)),
        accountant_questions_resolved=distinct_subjects(
            of_kind(ActivityKind.ACCOUNTANT_QUESTION_RESOLVED)
        ),
        tax_obligations_verified=sum(
            1
            for o in obligations
            if o.kind in TAX_OBLIGATION_KINDS
            and month.contains(o.due_on)
            and o.satisfied_by_evidence_ids
        ),
        unresolved_issues=unresolved,
        owner_minutes=minutes_from_seconds(owner_seconds),
    )


# --------------------------------------------------------------------------- entry point


def compute_month_status(
    entity_id: str,
    month: Month,
    items: Sequence[TrackedItem],
    *,
    now: datetime,
    connectors: Sequence[ConnectorCoverage],
    decisions: Sequence[EvidenceDecision] = (),
    obligations: Sequence[Obligation] = (),
    activities: Sequence[Activity] = (),
    interactions: Sequence[OwnerInteraction] = (),
    weights: Mapping[str, Decimal] | None = None,
    default_weight: Decimal = Decimal(1),
    tz: tzinfo = timezone.utc,
) -> MonthStatus:
    """Status of ``entity_id``'s ``month`` as of ``now`` (see module docstring).

    ``items`` must already be scoped to this company and month (tracked items do
    not carry an entity). ``obligations``, ``activities`` and ``interactions``
    may span companies and months; they are filtered here. ``weights`` maps a
    subject id (transaction/document/obligation id) to a non-negative Decimal.
    """
    if not entity_id:
        raise ValueError("entity_id is required")
    now = require_aware(now, "now")
    local_now = now.astimezone(tz)

    buckets = _bucket_items(items, decisions)
    counts = _counts(buckets)
    own = [o for o in obligations if o.entity_id == entity_id]
    open_obls = [o for o in own if is_open_obligation(o, month)]
    tracked_subjects = {i.subject_id for i in items if i.subject_type == "obligation"}

    today = local_now.date()
    connector_blockers = _connector_blockers(connectors, month, now, tz)
    blockers = [
        *connector_blockers,
        *_item_blockers(items, buckets),
        *_obligation_blockers(open_obls, today),
    ]
    if now < month.end(tz):
        blockers.append(Blocker(BlockerKind.MONTH_NOT_OVER, f"{month.label(today)} isn't over yet."))
    blockers.sort(key=lambda b: _BLOCKER_ORDER[b.kind])

    closed = not blockers
    unresolved = (
        counts.total - counts.done
        + sum(1 for o in open_obls if o.id not in tracked_subjects)
        + len(connector_blockers)
    )  # fmt: skip
    needs_you = (
        counts.needs_owner
        + counts.conflict
        + sum(1 for b in connector_blockers if b.needs_owner)
        + sum(1 for o in open_obls if _owner_overdue(o, today) and o.id not in tracked_subjects)
    )
    if closed:
        state = MonthState.CLOSED
    elif needs_you:
        state = MonthState.NEEDS_OWNER
    else:
        state = MonthState.ON_TRACK
    done_weight, total_weight = _weighed(items, buckets, weights, default_weight)

    return MonthStatus(
        entity_id=entity_id,
        month=month,
        as_of=local_now,
        state=state,
        percent_closed=_percent(done_weight, total_weight, closed),
        counts=counts,
        needs_you=needs_you,
        missing_documents=len(buckets.missing),
        open_obligations=tuple(o.id for o in sorted(open_obls, key=lambda o: (o.due_on, o.id))),
        blockers=tuple(blockers),
        summary=_summary(
            entity_id, month, tz, items, buckets, own, activities, interactions, unresolved
        ),
        connectors_ok=not connector_blockers,
        weighted_done=done_weight,
        weighted_total=total_weight,
    )


# --------------------------------------------------------------------------- §2 rendering


@dataclass(frozen=True, slots=True)
class ClosedSummaryText:
    """§2 text in parts so the UI can set the headline and footer in bold."""

    headline: str  # "September is closed."
    facts: tuple[str, ...]  # "218 transactions checked", ...
    footer: str  # "You spent 4 minutes."

    def render(self) -> str:
        return f"{self.headline} {MIDDLE_DOT.join(self.facts)}. {self.footer}"


def closed_summary(status: MonthStatus) -> ClosedSummaryText:
    """The §2 north-star summary. Refuses to speak for a month that is not closed (§3)."""
    if not status.closed:
        raise MonthNotClosed(f"{status.month} is not closed for {status.entity_id}")
    s = status.summary
    facts = (
        f"{count_phrase(s.transactions_checked, 'transaction')} checked",
        f"{count_phrase(s.documents_collected, 'document')} collected",
        f"{count_phrase(s.missing_documents_retrieved, 'missing document')} retrieved automatically",
        f"{count_phrase(s.suppliers_chased, 'supplier')} chased",
        f"{count_phrase(s.accountant_questions_resolved, 'accountant question')} resolved",
        f"{count_phrase(s.tax_obligations_verified, 'tax obligation')} verified",
        f"{count_phrase(s.unresolved_issues, 'unresolved issue')}",
    )
    footer = f"You spent {count_phrase(s.owner_minutes, 'minute')}."
    return ClosedSummaryText(headline=status.headline, facts=facts, footer=footer)


def render_closed_summary(status: MonthStatus) -> str:
    """'September is closed. 218 transactions checked · … · 0 unresolved issues. You spent 4 minutes.'"""
    return closed_summary(status).render()
