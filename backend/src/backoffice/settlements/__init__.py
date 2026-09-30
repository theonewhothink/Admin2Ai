"""Payout settlements: card terminals and payment / sales platforms (§7, §18-21, §54, §57).

A payout into the bank from SIBS, Stripe, PayPal, Booking.com, Glovo, ... is
the provider's net settlement of many sales: gross sales minus its fees or
commission, minus refunds and disputed card payments, plus or minus
adjustments. The bank line alone is never customer revenue; the provider's
payout report is its evidence.

Public API
----------

Reading a report (structured CSV / JSON only, never OCR)::

    reports = parse_settlement_reports(data, source=evidence_id, filename="payouts.csv", hint="stripe.com")
    report.provider.label, report.payout_id, report.payout_date, report.currency
    report.gross_sales, report.fees, report.refunds, report.chargebacks, report.adjustments, report.net
    report.lines                      # per order / booking / card payment, when the report has them
    report.observations               # field provenance (FieldObservation), like every other value (§18)
    report.adds_up                    # gross − fees − refunds − chargebacks ± adjustments == net, to the cent
    report.breakdown()                # plain "Why?" lines (§54)

Pairing reports with bank payouts::

    decisions = match_payouts([PayoutCandidate(tx, payout_provider(tx)), ...], {doc_id: report, ...})
    decision.outcome                  # SETTLED (GREEN) | AMOUNT_DIFFERS (RED, ask) | AMBIGUOUS (AMBER)

Bank-line recognition lives with the other bank wording in
:mod:`backoffice.reconciliation.payouts` (``payout_provider``,
``provider_named``); the expected-evidence engine gives such a line
``EvidenceExpectation.PAYOUT_REPORT``. The orchestrator's settlement agent
wires all of this into the live pipeline.
"""

from .match import (
    PayoutCandidate,
    PayoutConfig,
    PayoutDecision,
    PayoutOutcome,
    likely_payouts,
    match_payouts,
    provider_label,
)
from .parse import SettlementReportError, looks_like_settlement_report, parse_settlement_reports
from .report import REPORT_FIELDS, LineKind, SettlementLine, SettlementReport

__all__ = [
    "REPORT_FIELDS",
    "LineKind",
    "PayoutCandidate",
    "PayoutConfig",
    "PayoutDecision",
    "PayoutOutcome",
    "SettlementLine",
    "SettlementReport",
    "SettlementReportError",
    "likely_payouts",
    "looks_like_settlement_report",
    "match_payouts",
    "parse_settlement_reports",
    "provider_label",
]
