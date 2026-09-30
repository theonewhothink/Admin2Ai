"""Missing document autopilot (§22) and supplier chasing.

Public API
----------
``MissingEvidenceAutopilot(searches, authorize=..., verifier=None).run(query, company=, supplier=, today=)``
    -> ``MissingEvidenceOutcome`` with every ``SearchAttempt`` and a ``NextStep``:
    MATCH_FOUND, CONFIRM_WITH_OWNER, RESOLVE_CONFLICT, RETRY_LATER,
    CHASE_SUPPLIER (with the composed ``ChaseMessage``) or ASK_OWNER (with a
    Needs-You ``Question``). Searches implement the ``EvidenceSearch``
    protocol (``source`` + ``async search(query)``); ``authorize`` is any
    callable (sync or async) returning True when supplier invoice requests
    are pre-authorized (§25).

``EvidenceQuery.from_transaction(tx, supplier=None, invoice_number=None)``

Chasing — :mod:`.chase`
    ``compose_request(facts, token=, today=, message_id_domain=) -> ChaseMessage`` (PT/EN)
    ``compose_reminder(facts, thread, today=, message_id_domain=) -> ChaseMessage``
    ``compose_correction_request(facts, token=, today=, message_id_domain=) -> ChaseMessage`` (an invoice on hold)
    ``compose_statement_request(supplier_name=, ..., items=[StatementItem], corrections=False) -> ChaseMessage``
    (documents a supplier's account statement lists that were never received, or that differ)
    ``next_reminder(thread, ReminderPolicy(), today) -> ReminderDecision``
    ``match_reply(threads, InboundEmail(...)) -> ReplyMatch | None``
    ``thread_token(tenant_id, subject_id) -> str``
    ``clean_invoice_number(raw) -> str | None`` (one printable line, header-safe)

Safety: ``run`` refuses (``ValueError``) a company or supplier from another
tenant, or a company other than the payment's own; a payment with no company
yet is never chased automatically. Every email header is a single line.
"""

from __future__ import annotations

from .autopilot import (
    MISSING_INVOICE_PROMPT,
    SEARCH_ORDER,
    AttemptOutcome,
    Authorize,
    ChaseAuthorizationRequest,
    EvidenceCandidate,
    EvidenceQuery,
    EvidenceSearch,
    FoundEvidence,
    MissingEvidenceAutopilot,
    MissingEvidenceOutcome,
    NextStep,
    SearchAttempt,
    SearchSource,
    Verifier,
)
from .chase import (
    ChaseFacts,
    ChaseMessage,
    ChaseThread,
    InboundEmail,
    Language,
    MatchMethod,
    ReminderDecision,
    ReminderPolicy,
    ReminderStep,
    ReplyMatch,
    SentMessage,
    StatementItem,
    activity_line,
    choose_language,
    clean_invoice_number,
    compose_correction_request,
    compose_reminder,
    compose_request,
    compose_statement_request,
    day_month_pt,
    format_money_pt,
    match_reply,
    next_reminder,
    thread_token,
    valid_mail_domain,
)

__all__ = [
    "MISSING_INVOICE_PROMPT",
    "SEARCH_ORDER",
    "AttemptOutcome",
    "Authorize",
    "ChaseAuthorizationRequest",
    "ChaseFacts",
    "ChaseMessage",
    "ChaseThread",
    "EvidenceCandidate",
    "EvidenceQuery",
    "EvidenceSearch",
    "FoundEvidence",
    "InboundEmail",
    "Language",
    "MatchMethod",
    "MissingEvidenceAutopilot",
    "MissingEvidenceOutcome",
    "NextStep",
    "ReminderDecision",
    "ReminderPolicy",
    "ReminderStep",
    "ReplyMatch",
    "SearchAttempt",
    "SearchSource",
    "SentMessage",
    "StatementItem",
    "Verifier",
    "activity_line",
    "choose_language",
    "clean_invoice_number",
    "compose_correction_request",
    "compose_reminder",
    "compose_request",
    "compose_statement_request",
    "day_month_pt",
    "format_money_pt",
    "match_reply",
    "next_reminder",
    "thread_token",
    "valid_mail_domain",
]
