"""Supplier portal connectors (§10): reusable adapters per supplier.

Each adapter (VodafoneConnector, AmazonConnector, ...) implements
:class:`SupplierPortalConnector`: authenticate, list invoices, retrieve an
invoice, retrieve a statement, retrieve history, detect new documents.

Deterministic connectors come first; AI browser navigation is a fallback
only (§10). :func:`plan_retrieval` makes that choice explicit and auditable:
a registered deterministic adapter is always preferred, and the AI browser is
used only when policy allows it and no adapter exists or the adapter reported
that the portal changed under it.

When a portal wants a one-time code the adapter returns ``MFA_REQUIRED`` with
the §9 prompt "Supplier X needs authentication." and a challenge to resume
with once the owner answers on mobile. :meth:`PortalSync.resume` continues
from that challenge with the code the owner entered: the adapter's
:meth:`~SupplierPortalConnector.complete_mfa` finishes the sign-in and the
retrieval goes on exactly as a normal sync. A wrong code
(``CODE_REJECTED``) leaves the challenge waiting for another try; an expired
one (``CODE_EXPIRED``, or past the challenge's ``expires_at``) needs a new
code from the portal.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar

from pydantic import SecretStr

from backoffice.domain.models import utcnow

from ..base import (
    ConnectorError,
    ConnectorState,
    CountingSink,
    ProviderError,
    ReconnectRequired,
    SyncOutcome,
    TransientError,
    record_failure,
    record_success,
)

__all__ = [
    "AuthResult",
    "AuthStatus",
    "MfaChallenge",
    "PortalChanged",
    "PortalCredentials",
    "PortalDocument",
    "PortalError",
    "PortalInvoiceRef",
    "PortalRegistry",
    "PortalSession",
    "PortalSync",
    "PortalSyncOutcome",
    "RetrievalPlan",
    "RetrievalStrategy",
    "SupplierPortalConnector",
    "authentication_prompt",
    "code_prompt",
    "default_registry",
    "plan_retrieval",
    "register_portal",
]


def authentication_prompt(supplier: str) -> str:
    """§9 mobile prompt, word for word."""
    return f"{supplier} needs authentication."


def code_prompt(supplier: str) -> str:
    """What the owner reads when a supplier's site sent them a one-time code to enter (push and Needs you)."""
    return f"{supplier} needs a sign-in code."


class PortalError(Exception):
    """Internal portal failure; ``code`` is for logs, never owner copy."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PortalChanged(PortalError):
    """The portal's pages or API no longer match the adapter (triggers AI fallback)."""


class AuthStatus(str, Enum):
    AUTHENTICATED = "authenticated"
    MFA_REQUIRED = "mfa_required"
    LOGIN_REQUIRED = "login_required"  # stored credentials no longer work: owner reconnects
    FAILED = "failed"  # portal down or refused for another reason: retry later
    CODE_REJECTED = "code_rejected"  # the one-time code was wrong: the same challenge waits for another try
    CODE_EXPIRED = "code_expired"  # the one-time code is no longer valid: the portal must send a new one


@dataclass(frozen=True)
class PortalCredentials:
    username: str
    password: SecretStr  # from the secrets vault; never logged (§52)
    extra: Mapping[str, str] = field(default_factory=dict)  # e.g. customer/account number


@dataclass(frozen=True)
class PortalSession:
    """An authorised session. ``state`` is opaque (cookies, tokens) and vault-stored."""

    supplier_key: str
    account: str
    authenticated_at: datetime
    expires_at: datetime | None = None
    state: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class MfaChallenge:
    supplier_key: str
    account: str
    channel: str | None = None  # "sms", "email", "app" — shown to the owner as-is
    resume_state: Mapping[str, Any] = field(default_factory=dict, repr=False)
    issued_at: datetime | None = None
    expires_at: datetime | None = None  # the code's validity, when the portal says it

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at


@dataclass(frozen=True)
class AuthResult:
    status: AuthStatus
    session: PortalSession | None = None
    challenge: MfaChallenge | None = None
    owner_message: str | None = None


@dataclass(frozen=True)
class PortalInvoiceRef:
    supplier_key: str
    portal_id: str  # the portal's own identifier; stable across listings
    invoice_number: str | None = None
    issue_date: date | None = None
    gross_amount: Decimal | None = None
    currency: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        amount = self.gross_amount
        if amount is not None and (not isinstance(amount, Decimal) or not amount.is_finite()):
            raise TypeError("money must be a finite Decimal, never float or text")


@dataclass(frozen=True)
class PortalDocument:
    ref: PortalInvoiceRef | None  # None for statements
    data: bytes = field(repr=False)
    content_type: str = "application/pdf"
    filename: str | None = None
    retrieved_at: datetime | None = None
    source_url: str | None = None


class SupplierPortalConnector(ABC):
    """One supplier portal. Subclasses set the class attributes and the methods."""

    supplier_key: ClassVar[str]  # "vodafone_pt"
    display_name: ClassVar[str]  # "Vodafone" (owner-facing)
    domains: ClassVar[tuple[str, ...]] = ()  # portal hosts, for link routing (§9 login required)
    deterministic: ClassVar[bool] = True  # False only for AI-navigated adapters

    @abstractmethod
    def authenticate(self, credentials: PortalCredentials) -> AuthResult:
        """Log in. Returns MFA_REQUIRED with a challenge instead of blocking on a code."""

    def complete_mfa(self, challenge: MfaChallenge, code: str) -> AuthResult:
        """Resume after the owner answered. Portals without codes never get here."""
        return AuthResult(AuthStatus.FAILED)

    @abstractmethod
    def list_invoices(self, session: PortalSession, since: date, until: date) -> list[PortalInvoiceRef]:
        """Invoices issued in ``[since, until]``."""

    @abstractmethod
    def retrieve_invoice(self, session: PortalSession, ref: PortalInvoiceRef) -> PortalDocument:
        """The original file for ``ref`` (never a re-rendered copy when an original exists)."""

    @abstractmethod
    def retrieve_statement(self, session: PortalSession, period_start: date, period_end: date) -> PortalDocument | None:
        """Account statement for the period, if the portal offers one."""

    def retrieve_history(self, session: PortalSession, since: date, until: date) -> Iterator[PortalDocument]:
        for ref in self.list_invoices(session, since, until):
            yield self.retrieve_invoice(session, ref)

    def detect_new(self, session: PortalSession, known_ids: Iterable[str], since: date,
                   until: date) -> list[PortalInvoiceRef]:
        known = set(known_ids)
        return [ref for ref in self.list_invoices(session, since, until) if ref.portal_id not in known]

    @classmethod
    def mfa_required(cls, account: str, *, channel: str | None = None,
                     resume_state: Mapping[str, Any] | None = None, issued_at: datetime | None = None,
                     expires_at: datetime | None = None) -> AuthResult:
        """Helper for adapters: the standard MFA answer with the §9 owner prompt."""
        challenge = MfaChallenge(cls.supplier_key, account, channel, dict(resume_state or {}), issued_at, expires_at)
        return AuthResult(AuthStatus.MFA_REQUIRED, challenge=challenge,
                          owner_message=authentication_prompt(cls.display_name))


# --------------------------------------------------------------------------- registry


class PortalRegistry:
    def __init__(self) -> None:
        self._by_key: dict[str, type[SupplierPortalConnector]] = {}
        self._lock = threading.Lock()

    def register(self, connector: type[SupplierPortalConnector]) -> type[SupplierPortalConnector]:
        key = getattr(connector, "supplier_key", None)
        if not key or not getattr(connector, "display_name", None):
            raise ValueError("portal connectors need supplier_key and display_name")
        with self._lock:
            existing = self._by_key.get(key)
            if existing is not None and existing is not connector:
                raise ValueError(f"portal {key!r} is already registered")
            self._by_key[key] = connector
        return connector

    def get(self, supplier_key: str) -> type[SupplierPortalConnector] | None:
        return self._by_key.get(supplier_key)

    def for_host(self, host: str) -> type[SupplierPortalConnector] | None:
        """Adapter whose domain is ``host`` or a parent of it (``my.vodafone.pt``)."""
        host = host.lower().rstrip(".")
        matches = [
            (len(domain), key)
            for key, cls in self._by_key.items()
            for domain in cls.domains
            if host == domain.lower() or host.endswith("." + domain.lower())
        ]
        return self._by_key[max(matches)[1]] if matches else None

    def keys(self) -> list[str]:
        return sorted(self._by_key)


default_registry = PortalRegistry()


def register_portal(connector: type[SupplierPortalConnector]) -> type[SupplierPortalConnector]:
    """Class decorator adding an adapter to the default registry."""
    return default_registry.register(connector)


class RetrievalStrategy(str, Enum):
    DETERMINISTIC = "deterministic"
    AI_BROWSER = "ai_browser"  # fallback only (§10)
    NONE = "none"  # no adapter and fallback not allowed: chase the supplier instead (§22)


@dataclass(frozen=True)
class RetrievalPlan:
    strategy: RetrievalStrategy
    connector: type[SupplierPortalConnector] | None
    reason: str  # internal, for the audit trail (§55)


def plan_retrieval(
    *,
    registry: PortalRegistry,
    supplier_key: str | None = None,
    host: str | None = None,
    allow_ai_fallback: bool = False,
    adapter_broken: bool = False,
) -> RetrievalPlan:
    """Deterministic first; the AI browser only as an allowed fallback (§10)."""
    connector = registry.get(supplier_key) if supplier_key else None
    if connector is None and host:
        connector = registry.for_host(host)
    if connector is not None and connector.deterministic and not adapter_broken:
        return RetrievalPlan(RetrievalStrategy.DETERMINISTIC, connector, "adapter_available")
    if allow_ai_fallback:
        return RetrievalPlan(RetrievalStrategy.AI_BROWSER, connector, _fallback_reason(connector, adapter_broken))
    return RetrievalPlan(RetrievalStrategy.NONE, connector, "fallback_not_allowed")


def _fallback_reason(connector: type[SupplierPortalConnector] | None, adapter_broken: bool) -> str:
    if connector is None:
        return "no_adapter"
    return "adapter_broken" if adapter_broken else "adapter_not_deterministic"


# --------------------------------------------------------------------------- sync runner


class _MfaPending(ConnectorError):
    """The portal asked for a code; the owner is prompted, nothing is broken."""

    retryable = False


@dataclass(frozen=True)
class PortalSyncOutcome:
    outcome: SyncOutcome  # state to persist, delivered count, error
    session: PortalSession | None = None  # reuse next time (vault-stored)
    challenge: MfaChallenge | None = None  # set when the owner must enter a code
    owner_message: str | None = None  # "Vodafone needs authentication."
    adapter_broken: bool = False  # the portal changed: plan_retrieval(adapter_broken=True)
    retrieved_ids: tuple[str, ...] = ()
    code_rejected: bool = False  # resume: the code was wrong; ``challenge`` still waits for another try
    code_expired: bool = False  # resume: the code expired; the portal must send a new one


class PortalSync:
    """Runs one portal adapter under the §47 connector state machine.

    The cursor is the last day listed (ISO date); the next run lists again
    from a week before the last good sync, so late-published invoices are
    caught and ``known_ids`` keeps repeats out.
    """

    def __init__(self, connector: SupplierPortalConnector, *, history_window: timedelta = timedelta(days=90),
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.connector = connector
        self.history_window = history_window
        self._clock = clock

    def sync(
        self,
        state: ConnectorState,
        sink: Callable[[PortalDocument], None],
        *,
        credentials: PortalCredentials | None = None,
        session: PortalSession | None = None,
        known_ids: Iterable[str] = (),
        now: datetime | None = None,
    ) -> PortalSyncOutcome:
        """Authenticate if needed, fetch documents not seen before, deliver them."""
        now = now or self._clock()
        if session is None or (session.expires_at is not None and session.expires_at <= now):
            try:
                session, early = self._authenticate(state, credentials, now)
            except PortalError as exc:
                return self._portal_failed(state, now, exc, None, CountingSink(sink), [])
            if early is not None:
                return early
        assert session is not None
        return self._retrieve(state, session, sink, known_ids, now)

    def resume(
        self,
        state: ConnectorState,
        challenge: MfaChallenge,
        code: str,
        sink: Callable[[PortalDocument], None],
        *,
        known_ids: Iterable[str] = (),
        now: datetime | None = None,
    ) -> PortalSyncOutcome:
        """The owner entered the code ``challenge`` waited for: finish the sign-in, then retrieve as :meth:`sync`.

        A wrong code keeps the challenge (``code_rejected``); an expired one needs a new code (``code_expired``).
        Neither touches the connection's state: nothing is broken, the owner simply tries again.
        """
        now = now or self._clock()
        if challenge.expired(now):
            return PortalSyncOutcome(SyncOutcome(state, 0), code_expired=True)
        try:
            result = self.connector.complete_mfa(challenge, code)
        except PortalError as exc:
            return self._portal_failed(state, now, exc, None, CountingSink(sink), [])
        if result.status is AuthStatus.AUTHENTICATED and result.session is not None:
            return self._retrieve(state, result.session, sink, known_ids, now)
        if result.status is AuthStatus.CODE_REJECTED:
            return PortalSyncOutcome(SyncOutcome(state, 0), challenge=challenge, code_rejected=True)
        if result.status is AuthStatus.CODE_EXPIRED:
            return PortalSyncOutcome(SyncOutcome(state, 0), code_expired=True)
        if result.status is AuthStatus.MFA_REQUIRED:  # one more step (another code)
            failed = self._failed(state, now, _MfaPending("portal_mfa_required"))
            message = result.owner_message or authentication_prompt(self.connector.display_name)
            return PortalSyncOutcome(failed.outcome, challenge=result.challenge, owner_message=message)
        if result.status is AuthStatus.LOGIN_REQUIRED:
            return self._failed(state, now, ReconnectRequired("portal_login_rejected"))
        return self._failed(state, now, TransientError("portal_auth_failed"))

    def _retrieve(self, state: ConnectorState, session: PortalSession, sink: Callable[[PortalDocument], None],
                  known_ids: Iterable[str], now: datetime) -> PortalSyncOutcome:
        counted = CountingSink(sink)
        retrieved: list[str] = []
        try:
            since = (state.last_successful_sync or now - self.history_window).date() - timedelta(days=7)
            for ref in self.connector.detect_new(session, known_ids, since, now.date()):
                counted(self.connector.retrieve_invoice(session, ref))
                retrieved.append(ref.portal_id)
        except PortalError as exc:
            return self._portal_failed(state, now, exc, session, counted, retrieved)
        except ConnectorError as exc:  # adapters built on the shared HTTP/OAuth helpers raise these
            return PortalSyncOutcome(SyncOutcome(record_failure(state, at=now, error=exc), counted.count,
                                                 error=exc), session, retrieved_ids=tuple(retrieved))
        first = state.last_successful_sync is None
        new_state = record_success(state, at=now, cursor=now.date().isoformat(),
                                   coverage_start=now - self.history_window if first else None)
        return PortalSyncOutcome(SyncOutcome(new_state, counted.count, full_sync=first), session,
                                 retrieved_ids=tuple(retrieved))

    @staticmethod
    def _portal_failed(state: ConnectorState, now: datetime, exc: PortalError, session: PortalSession | None,
                       counted: CountingSink, retrieved: list[str]) -> PortalSyncOutcome:
        if isinstance(exc, PortalChanged):
            error: ConnectorError = ProviderError(f"portal_changed:{exc.code}")
            return PortalSyncOutcome(SyncOutcome(record_failure(state, at=now, error=error), counted.count,
                                                 error=error), session, adapter_broken=True,
                                     retrieved_ids=tuple(retrieved))
        error = TransientError(f"portal:{exc.code}")
        return PortalSyncOutcome(SyncOutcome(record_failure(state, at=now, error=error), counted.count,
                                             error=error), session, retrieved_ids=tuple(retrieved))

    def _authenticate(
        self, state: ConnectorState, credentials: PortalCredentials | None, now: datetime
    ) -> tuple[PortalSession | None, PortalSyncOutcome | None]:
        if credentials is None:
            return None, self._failed(state, now, ReconnectRequired("portal_no_credentials"))
        result = self.connector.authenticate(credentials)
        if result.status is AuthStatus.AUTHENTICATED and result.session is not None:
            return result.session, None
        if result.status is AuthStatus.MFA_REQUIRED:
            failed = self._failed(state, now, _MfaPending("portal_mfa_required"))
            message = result.owner_message or authentication_prompt(self.connector.display_name)
            return None, PortalSyncOutcome(failed.outcome, challenge=result.challenge, owner_message=message)
        if result.status is AuthStatus.LOGIN_REQUIRED:
            return None, self._failed(state, now, ReconnectRequired("portal_login_rejected"))
        return None, self._failed(state, now, TransientError("portal_auth_failed"))

    @staticmethod
    def _failed(state: ConnectorState, now: datetime, error: ConnectorError) -> PortalSyncOutcome:
        return PortalSyncOutcome(SyncOutcome(record_failure(state, at=now, error=error), 0, error=error))
