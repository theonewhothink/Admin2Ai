"""Accountant workspace data (§28).

Home table, one row per client and month::

    Client | Month | Complete | Missing | Needs accountant

"Complete" is the month's ``percent_closed`` (never 100 unless closed),
"Missing" counts transactions whose expected document has not been found
(§21), and "Needs accountant" counts what only the accountant can settle:
open questions addressed to them and unresolved tax flags.

The client drill-down gathers reconciliations (items with their matched
documents and "Why?" lines), supporting evidence, anomalies, tax flags,
missing documents, questions and the export state of the month's package.
Accountant-facing text may use accounting words; owner-facing text never does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from backoffice.domain.models import LegalEntity, Quality

from ._text import require_aware
from .month import EvidenceDecision, MonthStatus
from .package import DeliveryState, PackageDelivery, PackageEntry, require_same_company
from .period import Month

__all__ = [
    "AccountantQuestion",
    "Anomaly",
    "ClientMonth",
    "ClientRow",
    "ClientView",
    "EvidenceRow",
    "ExportState",
    "ExportStatus",
    "MissingDocument",
    "QuestionDirection",
    "QuestionStatus",
    "TaxFlag",
    "build_client_view",
    "build_home_table",
    "client_row",
    "needs_accountant",
]


class QuestionDirection(str, Enum):
    FROM_ACCOUNTANT = "from_accountant"  # the accountant asked; the system or owner answers
    TO_ACCOUNTANT = "to_accountant"  # only the accountant can decide


class QuestionStatus(str, Enum):
    OPEN = "open"
    WAITING_FOR_OWNER = "waiting_for_owner"
    ANSWERED = "answered"


@dataclass(frozen=True, slots=True)
class AccountantQuestion:
    question_id: str
    text: str
    direction: QuestionDirection
    status: QuestionStatus = QuestionStatus.OPEN
    item_id: str | None = None
    asked_at: datetime | None = None
    answered_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("asked_at", "answered_at"):
            value = getattr(self, name)
            if value is not None:
                require_aware(value, name)

    @property
    def open(self) -> bool:
        return self.status is not QuestionStatus.ANSWERED


@dataclass(frozen=True, slots=True)
class TaxFlag:
    """A tax treatment question on an item (e.g. VAT deductibility unclear)."""

    flag_id: str
    item_id: str
    description: str
    resolved: bool = False


@dataclass(frozen=True, slots=True)
class Anomaly:
    """Something unusual on an item. RED is real risk (§26); AMBER is worth a look."""

    anomaly_id: str
    item_id: str
    description: str
    quality: Quality = Quality.AMBER
    resolved: bool = False


@dataclass(frozen=True, slots=True)
class MissingDocument:
    item_id: str
    transaction_id: str
    booked_on: date
    description: str
    amount: Decimal | None
    currency: str


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    evidence_id: str
    sha256: str
    filename: str | None
    format: str
    source: str
    retrieved_at: datetime
    item_ids: tuple[str, ...]


class ExportState(str, Enum):
    NOT_PREPARED = "not_prepared"
    PREPARED = "prepared"
    DELIVERED = "delivered"
    CONFIRMED = "confirmed"


@dataclass(frozen=True, slots=True)
class ExportStatus:
    state: ExportState
    package_sha256: str | None = None
    prepared_at: datetime | None = None
    delivered_at: datetime | None = None
    confirmed_at: datetime | None = None

    @classmethod
    def of(cls, delivery: PackageDelivery | None) -> ExportStatus:
        if delivery is None:
            return cls(ExportState.NOT_PREPARED)
        state = {
            DeliveryState.PREPARED: ExportState.PREPARED,
            DeliveryState.DELIVERED: ExportState.DELIVERED,
            DeliveryState.CONFIRMED: ExportState.CONFIRMED,
        }[delivery.state]
        return cls(
            state=state,
            package_sha256=delivery.package_sha256,
            prepared_at=delivery.prepared_at,
            delivered_at=delivery.delivered_at,
            confirmed_at=delivery.confirmed_at,
        )


# --------------------------------------------------------------------------- home table


@dataclass(frozen=True, slots=True)
class ClientMonth:
    """Inputs for one client's row."""

    entity: LegalEntity
    status: MonthStatus
    questions: Sequence[AccountantQuestion] = ()
    tax_flags: Sequence[TaxFlag] = ()

    def __post_init__(self) -> None:
        if self.status.entity_id != self.entity.id:
            raise ValueError("month status belongs to another company")


@dataclass(frozen=True, slots=True)
class ClientRow:
    entity_id: str
    client: str
    month: Month
    complete_percent: int
    missing: int
    needs_accountant: int
    closed: bool


def needs_accountant(questions: Sequence[AccountantQuestion], tax_flags: Sequence[TaxFlag]) -> int:
    """Open questions addressed to the accountant plus unresolved tax flags."""
    asked = sum(1 for q in questions if q.open and q.direction is QuestionDirection.TO_ACCOUNTANT)
    return asked + sum(1 for f in tax_flags if not f.resolved)


def client_row(client: ClientMonth) -> ClientRow:
    s = client.status
    return ClientRow(
        entity_id=client.entity.id,
        client=client.entity.name,
        month=s.month,
        complete_percent=s.percent_closed,
        missing=s.missing_documents,
        needs_accountant=needs_accountant(client.questions, client.tax_flags),
        closed=s.closed,
    )


def build_home_table(clients: Sequence[ClientMonth]) -> list[ClientRow]:
    """Rows, most in need of the accountant first, then least complete, then by name."""
    rows = [client_row(c) for c in clients]
    return sorted(rows, key=lambda r: (-r.needs_accountant, r.complete_percent, r.client.casefold(), r.entity_id, r.month))


# --------------------------------------------------------------------------- client drill-down


@dataclass(frozen=True, slots=True)
class ClientView:
    entity_id: str
    client: str
    month: Month
    complete_percent: int
    closed: bool
    open_reasons: tuple[str, ...]
    reconciliations: tuple[PackageEntry, ...]
    evidence: tuple[EvidenceRow, ...]
    anomalies: tuple[Anomaly, ...]
    tax_flags: tuple[TaxFlag, ...]
    missing: tuple[MissingDocument, ...]
    questions: tuple[AccountantQuestion, ...]
    export: ExportStatus
    needs_accountant: int


def _missing(entries: Sequence[PackageEntry], decisions: Sequence[EvidenceDecision]) -> list[MissingDocument]:
    """Transactions that need a document (§21), are not done and have none matched."""
    needs = {d.transaction_id for d in decisions if d.requires_document}
    return [
        MissingDocument(e.item_id, e.subject_id, e.booked_on, e.description, e.amount, e.currency)
        for e in entries
        if e.subject_type == "transaction" and e.subject_id in needs and not e.done and not e.documents
    ]


def _evidence_rows(entries: Sequence[PackageEntry]) -> list[EvidenceRow]:
    by_id: dict[str, EvidenceRow] = {}
    for e in entries:
        for ev in e.evidence:
            row = by_id.get(ev.id)
            items = (*row.item_ids, e.item_id) if row else (e.item_id,)
            by_id[ev.id] = EvidenceRow(
                evidence_id=ev.id,
                sha256=ev.sha256,
                filename=ev.filename,
                format=ev.format.value,
                source=ev.source_kind.value,
                retrieved_at=ev.retrieved_at,
                item_ids=tuple(sorted(set(items))),
            )
    return [by_id[k] for k in sorted(by_id)]


_QUALITY_RANK = {Quality.RED: 0, Quality.AMBER: 1, Quality.GREEN: 2}


def build_client_view(
    entity: LegalEntity,
    status: MonthStatus,
    *,
    entries: Sequence[PackageEntry] = (),
    decisions: Sequence[EvidenceDecision] = (),
    anomalies: Sequence[Anomaly] = (),
    tax_flags: Sequence[TaxFlag] = (),
    questions: Sequence[AccountantQuestion] = (),
    delivery: PackageDelivery | None = None,
) -> ClientView:
    """Everything the accountant needs for one client and month, open things first."""
    if status.entity_id != entity.id:
        raise ValueError("month status belongs to another company")
    if delivery is not None and (delivery.entity_id != entity.id or delivery.month != status.month):
        raise ValueError("package delivery belongs to another company or month")
    require_same_company(entries, entity)
    ordered = sorted(entries, key=lambda e: (e.booked_on, e.item_id))
    return ClientView(
        entity_id=entity.id,
        client=entity.name,
        month=status.month,
        complete_percent=status.percent_closed,
        closed=status.closed,
        open_reasons=tuple(status.reasons()),
        reconciliations=tuple(e for e in ordered if e.subject_type == "transaction"),
        evidence=tuple(_evidence_rows(ordered)),
        anomalies=tuple(
            sorted(anomalies, key=lambda a: (a.resolved, _QUALITY_RANK[a.quality], a.anomaly_id))
        ),
        tax_flags=tuple(sorted(tax_flags, key=lambda f: (f.resolved, f.flag_id))),
        missing=tuple(_missing(ordered, decisions)),
        questions=tuple(
            sorted(
                questions,
                key=lambda q: (not q.open, q.asked_at is None, q.asked_at or datetime.min, q.question_id),
            )
        ),
        export=ExportStatus.of(delivery),
        needs_accountant=needs_accountant(questions, tax_flags),
    )
