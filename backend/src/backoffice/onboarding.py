"""The first run of a new business (§4, §5, §6, §58, §59): what onboarding asks, and how long it took.

Two things are kept here, both as plain state on the repository and both written
to the audit log as they happen (every milestone is an audit record with its time):

**First-run learning (§5, checklist A10).** While the historical scan of a new
business is being imported (``learning``), no "which company?" question is put in
Needs You: the answers would be asked in the order the payments happen to arrive,
not by how much they matter. When the scan is in (no connection is still reading
its first 90 days, see ``waiting_for``), the uncertain payments are grouped by
series and ranked by value (:func:`backoffice.learning.select_questions`: amount x
occurrences x uncertainty); at most ``question_limit`` questions are asked (4 by
default, never 10 or more). One answer with "Always" teaches the whole series, so
many of the rest are answered by learning. What is left is *deferred*: it stays
open (never closed on a guess) and is asked later, one series at a time and at most
one a day, only once nothing from the first run is waiting for the owner. Needs You
is never flooded. A connection added later reads its own 90 days the same way, and
the cap counts the first-run questions still open.

**Activation and onboarding time (§58, §59, checklist A11, U1, U2).** The moments
the six activation conditions were first met are recorded as milestones, from the
account's creation (``started_at``): email connected, bank connected, historical
scan complete, first document found, first transaction matched by the system, and
the owner seeing the time saved (reported by the app). ``activation_signals`` hands
them to :func:`backoffice.closure.evaluate_activation`, which gives time to first
value. Owner active onboarding time is the sum of the owner's interaction spans
while onboarding is open (kind ``ONBOARDING``): each set-up step and each answer to
a first-run question is one span. The engine records a fixed span per step (the
same estimate it uses for every answer) until the apps report measured time, so
the minutes are marked as an estimate. Onboarding closes once first-run learning is
over and none of its questions is still open, or seven days after the start.

A business that never went through onboarding (the demo, set up by hand) has no
``started_at``: nothing here is measured for it, and every figure says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from backoffice.closure import ActivationSignals
from backoffice.learning import DEFAULT_QUESTION_LIMIT

__all__ = [
    "LEARNING_MAX",
    "MILESTONES",
    "ONBOARDING_WINDOW",
    "OnboardingState",
]

LEARNING_MAX = timedelta(days=2)  # never wait longer than this for a connection's first read
ONBOARDING_WINDOW = timedelta(days=7)  # onboarding time is counted for a week at most

# Milestone names, in the order a business normally reaches them.
ACCOUNT_CREATED = "account_created"
COMPANY_ADDED = "company_added"
EMAIL_CONNECTED = "email_connected"
BANK_CONNECTED = "bank_connected"
HISTORICAL_SCAN_COMPLETE = "historical_scan_complete"
FIRST_DOCUMENT_FOUND = "first_document_found"
FIRST_AUTO_MATCH = "first_auto_match"
TIME_SAVED_SEEN = "time_saved_seen"
LEARNING_FINISHED = "learning_finished"
ONBOARDING_FINISHED = "onboarding_finished"
MILESTONES = (ACCOUNT_CREATED, COMPANY_ADDED, EMAIL_CONNECTED, BANK_CONNECTED, HISTORICAL_SCAN_COMPLETE,
              FIRST_DOCUMENT_FOUND, FIRST_AUTO_MATCH, TIME_SAVED_SEEN, LEARNING_FINISHED, ONBOARDING_FINISHED)

MILESTONE_WORDS = {
    ACCOUNT_CREATED: "Account created",
    COMPANY_ADDED: "Company added",
    EMAIL_CONNECTED: "Email connected",
    BANK_CONNECTED: "Bank connected",
    HISTORICAL_SCAN_COMPLETE: "Last 90 days read",
    FIRST_DOCUMENT_FOUND: "First document found",
    FIRST_AUTO_MATCH: "First payment matched by itself",
    TIME_SAVED_SEEN: "Owner saw the time saved",
    LEARNING_FINISHED: "First-run learning finished",
    ONBOARDING_FINISHED: "Onboarding finished",
}


@dataclass
class OnboardingState:
    """One business's onboarding (module docstring). Empty for a business set up by hand."""

    started_at: datetime | None = None
    learning: bool = False
    question_limit: int = DEFAULT_QUESTION_LIMIT
    waiting_for: set[str] = field(default_factory=set)  # connections still reading their first 90 days
    historical: set[str] = field(default_factory=set)  # payments imported while learning
    asked: list[str] = field(default_factory=list)  # Needs-You ids of the first-run questions
    deferred: set[str] = field(default_factory=set)  # payments whose question waits
    released: list[str] = field(default_factory=list)  # Needs-You ids of deferred questions asked later
    released_on: date | None = None
    milestones: dict[str, datetime] = field(default_factory=dict)
    finished_at: datetime | None = None
    estimated_spans: int = 0  # owner spans recorded with the fixed estimate (not measured by an app)

    @property
    def started(self) -> bool:
        return self.started_at is not None

    def open_at(self, at: datetime) -> bool:
        """Onboarding is open at ``at``: started, not finished, and within its window."""
        if self.started_at is None or self.finished_at is not None:
            return False
        return at - self.started_at <= ONBOARDING_WINDOW

    def holds(self, tx_id: str) -> bool:
        """This payment's "which company?" question must not be asked now (learning, or deferred)."""
        return self.learning or tx_id in self.deferred

    def activation_signals(self) -> ActivationSignals | None:
        """The six §58 conditions as :class:`ActivationSignals`, or None when onboarding was never started."""
        if self.started_at is None:
            return None
        start = self.started_at

        def at(name: str) -> datetime | None:
            value = self.milestones.get(name)
            return None if value is None else max(value, start)

        return ActivationSignals(
            signed_up_at=start,
            email_connected_at=at(EMAIL_CONNECTED),
            bank_connected_at=at(BANK_CONNECTED),
            historical_scan_completed_at=at(HISTORICAL_SCAN_COMPLETE),
            first_document_found_at=at(FIRST_DOCUMENT_FOUND),
            first_auto_match_at=at(FIRST_AUTO_MATCH),
            time_saved_seen_at=at(TIME_SAVED_SEEN),
        )
