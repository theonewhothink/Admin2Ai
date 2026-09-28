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
from .gmail import GMAIL_API, GMAIL_READONLY_SCOPE, GmailConfig, GmailConnector, GmailPush
from .http import AuthorizedHttp
from .imap import IMAPAuth, IMAPClient, IMAPConfig, IMAPConnector, encode_mailbox_name, imap_date
from .microsoft import (
    GRAPH_API,
    GRAPH_MAIL_SCOPES,
    GraphAttachment,
    GraphMailConfig,
    GraphSubscription,
    MicrosoftMailConnector,
)
from .oauth import (
    GOOGLE_TOKEN_URL,
    OAuthClientConfig,
    OAuthRefresher,
    OAuthToken,
    RefreshingTokenProvider,
    TokenProvider,
    microsoft_token_url,
)
from .open_banking import (
    GOCARDLESS_API,
    BankAccessDenied,
    BankAccountInfo,
    BankAggregator,
    BankConsent,
    BankLink,
    BankSink,
    BankSyncConfig,
    BookedTransaction,
    ConsentStatus,
    GoCardlessBankAccountData,
    OpenBankingConnector,
    parse_gocardless_transaction,
    to_transactions,
)

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
