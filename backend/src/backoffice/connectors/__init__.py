"""Self-healing connectors (§8, §10, §47-48).

State and health (``base``)
    ConnectorState                  immutable, persisted after every run
    evaluate_health(state, now, tz=) -> HealthReport(health, title, detail, action, notify)
                                    HEALTHY / DEGRADED / BROKEN with §48 owner copy + Reconnect
    needs_backfill(state, now) -> BackfillPlan | None   missed/silent webhooks, lost cursors, gaps
    covers(state, start, end, tz=, settle=) -> Coverage  month close: never green without it
                                    (settle defaults per kind: banks wait 5 days for late bookings)
    record_success / record_failure / record_event / record_webhook / record_gap /
    record_backfill / record_reconnected                  pure state transitions
    ConnectorError > ReconnectRequired | TransientError | CursorExpired | ProviderError
Mail (each ``sync(state, sink, now=) -> SyncOutcome`` and ``backfill(state, gap, sink)``;
the sink receives ``MailItem(provider_id, raw RFC 822 bytes, ...)``)
    GmailConnector(tokens, client=)             history.list + full-sync fallback, format=raw, watch
    MicrosoftMailConnector(tokens, client=)     Graph delta per folder, MIME, attachments, subscriptions
    IMAPConnector(config, auth, client_factory=) UIDVALIDITY/UID cursor, read-only, BODY.PEEK
Bank
    BankAggregator Protocol; GoCardlessBankAccountData(secret_id, secret_key, client=)
    OpenBankingConnector(aggregator, requisition_id).sync(state, sink) -> domain Transactions
                                    .backfill(state, gap, sink); history the consent no longer
                                    reaches stays a known gap (never assumed complete)
OAuth
    OAuthRefresher, RefreshingTokenProvider, TokenProvider  (refused refresh -> ReconnectRequired)
Portals (``portals``)
    SupplierPortalConnector ABC, PortalRegistry / register_portal, plan_retrieval, PortalSync

Every sync returns a SyncOutcome with the state to persist; provider errors,
malformed payloads and refused tokens are typed ConnectorErrors, never raw.
"""

from .base import (
    SOURCE_KIND,
    ActionKind,
    BackfillPlan,
    BackfillReason,
    ConnectorError,
    ConnectorKind,
    ConnectorState,
    CountingSink,
    Coverage,
    CoverageReason,
    CursorExpired,
    DEFAULT_POLICIES,
    Health,
    HealthPolicy,
    HealthReason,
    HealthReport,
    MailItem,
    MailSink,
    OwnerAction,
    ProviderError,
    ReconnectRequired,
    SyncOutcome,
    TimeRange,
    TransientError,
    WebhookState,
    covers,
    evaluate_health,
    gap_after_cursor_loss,
    needs_backfill,
    policy_for,
    record_backfill,
    record_event,
    record_failure,
    record_gap,
    record_reconnected,
    record_success,
    record_webhook,
    since_phrase,
)
# The HTTP connectors (they need httpx) load on first use, so the engine (also in the browser, pydantic only) can
# import the pure parts of this package (base, choices, mail_search) without them.
_LAZY = {
    "GMAIL_API": "gmail",
    "GMAIL_READONLY_SCOPE": "gmail",
    "GmailConfig": "gmail",
    "GmailConnector": "gmail",
    "GmailPush": "gmail",
    "AuthorizedHttp": "http",
    "IMAPAuth": "imap",
    "IMAPClient": "imap",
    "IMAPConfig": "imap",
    "IMAPConnector": "imap",
    "encode_mailbox_name": "imap",
    "imap_date": "imap",
    "GRAPH_API": "microsoft",
    "GRAPH_MAIL_SCOPES": "microsoft",
    "GraphAttachment": "microsoft",
    "GraphMailConfig": "microsoft",
    "GraphSubscription": "microsoft",
    "MicrosoftMailConnector": "microsoft",
    "GOOGLE_TOKEN_URL": "oauth",
    "OAuthClientConfig": "oauth",
    "OAuthRefresher": "oauth",
    "OAuthToken": "oauth",
    "RefreshingTokenProvider": "oauth",
    "TokenProvider": "oauth",
    "microsoft_token_url": "oauth",
    "GOCARDLESS_API": "open_banking",
    "BankAccessDenied": "open_banking",
    "BankAccountInfo": "open_banking",
    "BankAggregator": "open_banking",
    "BankConsent": "open_banking",
    "BankLink": "open_banking",
    "BankSink": "open_banking",
    "BankSyncConfig": "open_banking",
    "BookedTransaction": "open_banking",
    "ConsentStatus": "open_banking",
    "GoCardlessBankAccountData": "open_banking",
    "OpenBankingConnector": "open_banking",
    "parse_gocardless_transaction": "open_banking",
    "to_transactions": "open_banking",
}


def __getattr__(name: str) -> object:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "ActionKind",
    "AuthorizedHttp",
    "BackfillPlan",
    "BackfillReason",
    "BankAccessDenied",
    "BankAccountInfo",
    "BankAggregator",
    "BankConsent",
    "BankLink",
    "BankSink",
    "BankSyncConfig",
    "BookedTransaction",
    "ConnectorError",
    "ConnectorKind",
    "ConnectorState",
    "ConsentStatus",
    "CountingSink",
    "Coverage",
    "CoverageReason",
    "CursorExpired",
    "DEFAULT_POLICIES",
    "GMAIL_API",
    "GMAIL_READONLY_SCOPE",
    "GOCARDLESS_API",
    "GOOGLE_TOKEN_URL",
    "GRAPH_API",
    "GRAPH_MAIL_SCOPES",
    "GmailConfig",
    "GmailConnector",
    "GmailPush",
    "GoCardlessBankAccountData",
    "GraphAttachment",
    "GraphMailConfig",
    "GraphSubscription",
    "Health",
    "HealthPolicy",
    "HealthReason",
    "HealthReport",
    "IMAPAuth",
    "IMAPClient",
    "IMAPConfig",
    "IMAPConnector",
    "MailItem",
    "MailSink",
    "MicrosoftMailConnector",
    "OAuthClientConfig",
    "OAuthRefresher",
    "OAuthToken",
    "OpenBankingConnector",
    "OwnerAction",
    "ProviderError",
    "ReconnectRequired",
    "RefreshingTokenProvider",
    "SOURCE_KIND",
    "SyncOutcome",
    "TimeRange",
    "TokenProvider",
    "TransientError",
    "WebhookState",
    "covers",
    "encode_mailbox_name",
    "evaluate_health",
    "gap_after_cursor_loss",
    "imap_date",
    "microsoft_token_url",
    "needs_backfill",
    "parse_gocardless_transaction",
    "policy_for",
    "record_backfill",
    "record_event",
    "record_failure",
    "record_gap",
    "record_reconnected",
    "record_success",
    "record_webhook",
    "since_phrase",
    "to_transactions",
]
