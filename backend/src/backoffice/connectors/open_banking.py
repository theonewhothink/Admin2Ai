"""Bank accounts through a PSD2 aggregator (§4 step 4, §20, §47).

:class:`BankAggregator` is the seam; :class:`GoCardlessBankAccountData` is an
httpx adapter for the GoCardless Bank Account Data API (formerly Nordigen):
end-user agreements and requisitions (the consent), accounts, and booked
transactions. Endpoint shapes follow GoCardless's public v2 documentation as
known to the author (verified_as_of 2026-09, not re-checked against the live
API; confirm field names and error statuses, and the provider's availability
for new customers, before production use). Another aggregator only needs a
new adapter.

Consent expiry becomes ``auth_expires_at``; an expired, rejected or revoked
consent becomes :class:`ReconnectRequired` (§47: "Bank consent expired →
mobile notification"). Only *booked* transactions are imported: pending ones
change and are never evidence. Amounts are parsed as ``Decimal`` from the
JSON text; a binary float is refused. Any unexpected payload shape is a
:class:`ProviderError`, never a raw ``KeyError`` or ``ValueError``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Protocol, runtime_checkable

import httpx

from backoffice.domain.models import Transaction, TransactionKind, utcnow

from .base import (
    ConnectorError,
    ConnectorKind,
    ConnectorState,
    CountingSink,
    ProviderError,
    ReconnectRequired,
    SyncOutcome,
    TimeRange,
    TransientError,
    record_backfill,
    record_failure,
    record_gap,
    record_success,
)
from .http import object_list, required_str, retry_after_seconds

__all__ = [
    "BankAccessDenied",
    "BankAccountInfo",
    "BankAggregator",
    "BankConsent",
    "BankLink",
    "BankSink",
    "BankSyncConfig",
    "BookedTransaction",
    "ConsentStatus",
    "GOCARDLESS_API",
    "GoCardlessBankAccountData",
    "OpenBankingConnector",
    "parse_gocardless_transaction",
    "to_transactions",
]

GOCARDLESS_API = "https://bankaccountdata.gocardless.com/api/v2"


class BankAccessDenied(ConnectorError):
    """The bank or aggregator refused account data (consent revoked or expired)."""

    retryable = False


class ConsentStatus(str, Enum):
    PENDING = "pending"  # the owner has not finished linking
    ACTIVE = "active"
    EXPIRED = "expired"
    REJECTED = "rejected"
    SUSPENDED = "suspended"


# GoCardless requisition status codes (public docs; see module docstring).
_REQUISITION_STATUS = {
    "LN": ConsentStatus.ACTIVE,
    "EX": ConsentStatus.EXPIRED,
    "RJ": ConsentStatus.REJECTED,
    "SU": ConsentStatus.SUSPENDED,
}


@dataclass(frozen=True)
class BankLink:
    requisition_id: str
    link: str  # where the owner authorises access at their bank
    agreement_id: str | None = None


@dataclass(frozen=True)
class BankConsent:
    requisition_id: str
    status: ConsentStatus
    account_ids: tuple[str, ...]
    institution_id: str | None = None
    expires_at: datetime | None = None
    max_historical_days: int | None = None


@dataclass(frozen=True)
class BankAccountInfo:
    account_id: str
    iban: str | None = None
    currency: str | None = None
    name: str | None = None
    owner_name: str | None = None
    status: str | None = None


@dataclass(frozen=True)
class BookedTransaction:
    """Aggregator-neutral booked transaction. Negative amount = money out."""

    provider_id: str | None
    booked_on: date
    amount: Decimal
    currency: str
    value_date: date | None = None
    creditor_name: str | None = None
    creditor_iban: str | None = None
    debtor_name: str | None = None
    debtor_iban: str | None = None
    remittance: str = ""
    end_to_end_id: str | None = None
    structured_reference: str | None = None
    bank_code: str | None = None  # ISO 20022 domain-family-subfamily, e.g. PMNT-CCRD-POSD
    proprietary_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal) or not self.amount.is_finite():
            raise TypeError("money must be a finite Decimal, never float or text")


@runtime_checkable
class BankAggregator(Protocol):
    def create_link(self, *, institution_id: str, redirect_url: str, reference: str, history_days: int,
                    access_days: int, language: str = "PT") -> BankLink: ...

    def consent(self, requisition_id: str) -> BankConsent: ...

    def account(self, account_id: str) -> BankAccountInfo: ...

    def booked_transactions(self, account_id: str, date_from: date, date_to: date) -> list[BookedTransaction]: ...


# --------------------------------------------------------------------------- GoCardless adapter


def _money(value: Any) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ProviderError("bank_amount_not_decimal")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ProviderError("bank_bad_amount") from None
    if not amount.is_finite():
        raise ProviderError("bank_bad_amount")
    return amount


def _day(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _iban(value: Any) -> str | None:
    if not isinstance(value, Mapping) or not value.get("iban"):
        return None
    return re.sub(r"\s+", "", str(value["iban"])).upper() or None


def _text(value: Any) -> str | None:
    text = " ".join(str(value).split()) if value not in (None, "") else ""
    return text or None


def _int(value: Any, code: str) -> int:
    if isinstance(value, bool):
        raise ProviderError(code)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        raise ProviderError(code) from None


def _timestamp(value: Any, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ProviderError(code) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_gocardless_transaction(raw: Mapping[str, Any]) -> BookedTransaction:
    if not isinstance(raw, Mapping):
        raise ProviderError("bank_unexpected_transaction")
    amount_info = raw.get("transactionAmount") or {}
    if not isinstance(amount_info, Mapping):
        raise ProviderError("bank_bad_amount")
    currency = str(amount_info.get("currency") or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ProviderError("bank_bad_currency")
    booked_on = _day(raw.get("bookingDate")) or _day(raw.get("bookingDateTime")) or _day(raw.get("valueDate"))
    if booked_on is None:
        raise ProviderError("bank_bad_date")
    parts = raw.get("remittanceInformationUnstructuredArray") or ()
    unstructured = raw.get("remittanceInformationUnstructured") or " ".join(
        str(x) for x in (parts if isinstance(parts, (list, tuple)) else (parts,))
    )
    remittance = _text(unstructured) or _text(raw.get("additionalInformation")) or ""
    end_to_end = _text(raw.get("endToEndId"))
    return BookedTransaction(
        provider_id=_text(raw.get("transactionId")) or _text(raw.get("internalTransactionId")),
        booked_on=booked_on,
        amount=_money(amount_info.get("amount")),
        currency=currency,
        value_date=_day(raw.get("valueDate")),
        creditor_name=_text(raw.get("creditorName")),
        creditor_iban=_iban(raw.get("creditorAccount")),
        debtor_name=_text(raw.get("debtorName")),
        debtor_iban=_iban(raw.get("debtorAccount")),
        remittance=remittance,
        end_to_end_id=None if (end_to_end or "").upper() == "NOTPROVIDED" else end_to_end,
        structured_reference=_text(raw.get("remittanceInformationStructured")),
        bank_code=_text(raw.get("bankTransactionCode")),
        proprietary_code=_text(raw.get("proprietaryBankTransactionCode")),
    )


class GoCardlessBankAccountData:
    """httpx client for GoCardless Bank Account Data (v2). Secrets come from the vault."""

    def __init__(
        self,
        secret_id: str,
        secret_key: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = GOCARDLESS_API,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._secret_id = secret_id
        self._secret_key = secret_key
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0))
        self.base_url = base_url.rstrip("/")
        self._clock = clock
        self._access: tuple[str, datetime] | None = None
        self._refresh: tuple[str, datetime] | None = None

    def __repr__(self) -> str:
        return f"GoCardlessBankAccountData(base_url={self.base_url!r})"

    # ----------------------------------------------------------------- tokens

    def _access_token(self) -> str:
        now = self._clock()
        margin = timedelta(seconds=60)
        if self._access and self._access[1] - margin > now:
            return self._access[0]
        if self._refresh and self._refresh[1] - margin > now:
            response = self._send("POST", "/token/refresh/", json_payload={"refresh": self._refresh[0]}, auth=False,
                                  allow=(401,))
            if response.status_code == 200:
                self._access = self._token(self._object(response), "access", now)
                return self._access[0]
        response = self._send("POST", "/token/new/", auth=False, allow=(401,),
                              json_payload={"secret_id": self._secret_id, "secret_key": self._secret_key})
        if response.status_code == 401:
            raise ProviderError("gocardless_credentials")  # our secrets, not the owner's consent
        data = self._object(response)
        self._access = self._token(data, "access", now)
        self._refresh = self._token(data, "refresh", now)
        return self._access[0]

    @staticmethod
    def _token(data: Mapping[str, Any], name: str, now: datetime) -> tuple[str, datetime]:
        lifetime = _int(data.get(f"{name}_expires", 0), "gocardless_token_malformed")
        return required_str(data, name, "gocardless"), now + timedelta(seconds=max(lifetime, 0))

    # ----------------------------------------------------------------- transport

    def _send(self, method: str, path: str, *, params: Mapping[str, Any] | None = None, json_payload: Any = None,
              auth: bool = True, allow: Sequence[int] = ()) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in (1, 2):
            headers = {"Accept": "application/json"}
            if auth:
                headers["Authorization"] = f"Bearer {self._access_token()}"
            try:
                response = self._client.request(method, f"{self.base_url}{path}", params=params,
                                                json=json_payload, headers=headers)
            except httpx.TimeoutException:
                raise TransientError("gocardless_timeout") from None
            except httpx.HTTPError:
                raise TransientError("gocardless_network") from None
            if response.status_code == 401 and auth and attempt == 1:
                self._access = None  # our access token expired early: renew once
                continue
            break
        assert response is not None
        status = response.status_code
        if status < 400 or status in allow:
            return response
        if status == 429 or status >= 500:
            reset = response.headers.get("http_x_ratelimit_account_success_reset")
            retry = float(reset) if reset and reset.isdigit() else retry_after_seconds(response)
            raise TransientError(f"gocardless_http_{status}", retry_after=retry)
        if status in (401, 403, 409):
            raise BankAccessDenied(f"gocardless_http_{status}")
        raise ProviderError(f"gocardless_http_{status}")

    @staticmethod
    def _object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = json.loads(response.content or b"null", parse_float=Decimal)
        except ValueError:
            raise ProviderError("gocardless_bad_json") from None
        if not isinstance(payload, dict):
            raise ProviderError("gocardless_unexpected_json")
        return payload

    def _get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return self._object(self._send("GET", path, **kwargs))

    # ----------------------------------------------------------------- API

    def create_link(self, *, institution_id: str, redirect_url: str, reference: str, history_days: int,
                    access_days: int, language: str = "PT") -> BankLink:
        agreement = self._object(self._send("POST", "/agreements/enduser/", json_payload={
            "institution_id": institution_id, "max_historical_days": history_days,
            "access_valid_for_days": access_days, "access_scope": ["balances", "details", "transactions"],
        }))
        agreement_id = required_str(agreement, "id", "gocardless")
        requisition = self._object(self._send("POST", "/requisitions/", json_payload={
            "redirect": redirect_url, "institution_id": institution_id, "reference": reference,
            "agreement": agreement_id, "user_language": language,
        }))
        return BankLink(required_str(requisition, "id", "gocardless"), required_str(requisition, "link", "gocardless"),
                        agreement_id)

    def consent(self, requisition_id: str) -> BankConsent:
        requisition = self._get(f"/requisitions/{requisition_id}/")
        status = _REQUISITION_STATUS.get(str(requisition.get("status", "")).upper(), ConsentStatus.PENDING)
        expires_at, max_days = None, None
        if requisition.get("agreement"):
            agreement = self._get(f"/agreements/enduser/{required_str(requisition, 'agreement', 'gocardless')}/")
            accepted = agreement.get("accepted")
            days = agreement.get("access_valid_for_days")
            if agreement.get("max_historical_days"):
                max_days = _int(agreement["max_historical_days"], "gocardless_bad_agreement")
            if accepted and days:
                start = _timestamp(accepted, "gocardless_bad_agreement")
                expires_at = start + timedelta(days=_int(days, "gocardless_bad_agreement"))
        accounts = requisition.get("accounts") or []
        if not isinstance(accounts, list):
            raise ProviderError("gocardless_unexpected_accounts")
        institution = requisition.get("institution_id")
        return BankConsent(str(requisition_id), status, tuple(str(a) for a in accounts),
                           str(institution) if institution else None, expires_at, max_days)

    def account(self, account_id: str) -> BankAccountInfo:
        meta = self._get(f"/accounts/{account_id}/")
        details = self._get(f"/accounts/{account_id}/details/").get("account") or {}
        if not isinstance(details, dict):
            raise ProviderError("gocardless_unexpected_account")
        return BankAccountInfo(
            account_id=str(account_id),
            iban=_iban(details) or _iban(meta),
            currency=details.get("currency"),
            name=details.get("name") or details.get("product"),
            owner_name=details.get("ownerName") or meta.get("owner_name"),
            status=meta.get("status"),
        )

    def booked_transactions(self, account_id: str, date_from: date, date_to: date) -> list[BookedTransaction]:
        payload = self._get(f"/accounts/{account_id}/transactions/",
                            params={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()})
        transactions = payload.get("transactions") or {}
        if not isinstance(transactions, dict):
            raise ProviderError("gocardless_unexpected_transactions")
        return [parse_gocardless_transaction(item) for item in object_list(transactions, "booked", "gocardless")]


# --------------------------------------------------------------------------- domain mapping

_CARD_LAST4 = re.compile(
    r"(?:\*{2,}|x{2,}|•{2,}|\bcart[ãa]o\s*(?:n[º°.]?\s*)?|\bcard\s*(?:no\.?\s*)?(?:ending\s*(?:in\s*)?)?)(\d{4})\b",
    re.IGNORECASE,
)
_DIRECT_DEBIT_TEXT = re.compile(r"direct debit|d[ée]bito dire(c)?to|deb\.? dire(c)?to|\bSDD\b", re.IGNORECASE)


def _kind(bt: BookedTransaction, card_last4: str | None) -> TransactionKind:
    """Best-effort type from ISO 20022 codes and wording; reconciliation decides for real."""
    codes = f"{bt.bank_code or ''} {bt.proprietary_code or ''}".upper()
    if "CHRG" in codes:
        return TransactionKind.FEE
    if "CCRD" in codes or "POSD" in codes or card_last4:
        return TransactionKind.CARD
    if "RDDT" in codes or "IDDT" in codes or _DIRECT_DEBIT_TEXT.search(bt.remittance):
        return TransactionKind.DIRECT_DEBIT
    return TransactionKind.TRANSFER_OUT if bt.amount < 0 else TransactionKind.TRANSFER_IN


def _fingerprint(bt: BookedTransaction) -> str:
    return "|".join([bt.booked_on.isoformat(), str(bt.amount), bt.currency, bt.remittance,
                     bt.creditor_name or "", bt.debtor_name or ""])


def to_transactions(
    booked: Sequence[BookedTransaction], *, tenant_id: str, account_id: str
) -> list[Transaction]:
    """Domain transactions with ids stable across syncs (re-imports never duplicate).

    Without a provider id, identical same-day rows (two equal coffees) are told
    apart by their order within the day, which banks keep stable.
    """
    seen: dict[str, int] = {}
    out = []
    for bt in booked:
        key = bt.provider_id or f"fp:{_fingerprint(bt)}"
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        digest = hashlib.sha256(f"{tenant_id}|{account_id}|{key}|{occurrence}".encode()).hexdigest()[:16]
        out.append(_transaction(bt, f"tx_{digest}", tenant_id, account_id))
    return out


def _transaction(bt: BookedTransaction, tx_id: str, tenant_id: str, account_id: str) -> Transaction:
    outgoing = bt.amount < 0
    card = _CARD_LAST4.search(bt.remittance)
    card_last4 = card.group(1) if card else None
    counterparty = (bt.creditor_name if outgoing else bt.debtor_name) or ""
    return Transaction(
        id=tx_id,
        tenant_id=tenant_id,
        account_id=account_id,
        booked_on=bt.booked_on,
        amount=bt.amount,
        currency=bt.currency,
        counterparty=counterparty,
        description=bt.remittance,
        kind=_kind(bt, card_last4),
        card_last4=card_last4,
        counterparty_iban=bt.creditor_iban if outgoing else bt.debtor_iban,
        reference=bt.end_to_end_id or bt.structured_reference,
    )


# --------------------------------------------------------------------------- connector

BankSink = Callable[[Transaction], None]


@dataclass(frozen=True)
class BankSyncConfig:
    history_window: timedelta = timedelta(days=90)  # §6
    overlap: timedelta = timedelta(days=5)  # banks book late and backdate; ids dedupe the overlap


class OpenBankingConnector:
    kind = ConnectorKind.OPEN_BANKING

    def __init__(
        self,
        aggregator: BankAggregator,
        requisition_id: str,
        *,
        account_ids: Mapping[str, str] | None = None,  # aggregator account id -> our account id
        config: BankSyncConfig | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.aggregator = aggregator
        self.requisition_id = requisition_id
        self._account_ids = dict(account_ids or {})
        self.config = config or BankSyncConfig()
        self._clock = clock

    def sync(self, state: ConnectorState, sink: BankSink, *, now: datetime | None = None) -> SyncOutcome:
        """Deliver booked transactions; consent expiry is tracked in ``auth_expires_at``."""
        now = now or self._clock()
        counted = CountingSink(sink)
        try:
            consent = self.aggregator.consent(self.requisition_id)
            state = state.model_copy(update={"auth_expires_at": consent.expires_at})
            self._require_active(consent, now)
            wanted_from, date_from, date_to = self._window(state, consent, now)
            if state.cursor and date_from > wanted_from:
                # The bank no longer serves that far back: the hole is known, never green (§47).
                state = record_gap(state, TimeRange(start=_midnight(wanted_from), end=_midnight(date_from)))
            for provider_account in consent.account_ids:
                account_id = self._account_ids.get(provider_account, provider_account)
                booked = self.aggregator.booked_transactions(provider_account, date_from, date_to)
                for tx in to_transactions(booked, tenant_id=state.tenant_id, account_id=account_id):
                    counted(tx)
        except ConnectorError as exc:
            if isinstance(exc, BankAccessDenied):  # consent revoked or expired at the bank: only the owner can fix it
                exc = ReconnectRequired("bank_access_denied")
            return SyncOutcome(record_failure(state, at=now, error=exc), counted.count, error=exc)
        first = state.cursor is None
        coverage_start = _midnight(date_from) if first else None
        return SyncOutcome(record_success(state, at=now, cursor=date_to.isoformat(), coverage_start=coverage_start),
                           counted.count, full_sync=first)

    def backfill(self, state: ConnectorState, gap: TimeRange, sink: BankSink, *,
                 now: datetime | None = None) -> SyncOutcome:
        """Re-read ``gap`` as far back as the consent reaches (§47).

        Whatever the bank no longer serves stays a known gap: that period can
        only be completed from statements, never assumed complete.
        """
        now = now or self._clock()
        counted = CountingSink(sink)
        first_day = gap.start.astimezone(timezone.utc).date()
        last_day = (gap.end - timedelta(microseconds=1)).astimezone(timezone.utc).date()
        try:
            consent = self.aggregator.consent(self.requisition_id)
            self._require_active(consent, now)
            today = now.astimezone(timezone.utc).date()
            reachable = first_day
            if consent.max_historical_days:
                reachable = max(first_day, today - timedelta(days=consent.max_historical_days))
            if reachable > last_day:
                return SyncOutcome(state, 0)  # nothing the bank still serves
            for provider_account in consent.account_ids:
                account_id = self._account_ids.get(provider_account, provider_account)
                booked = self.aggregator.booked_transactions(provider_account, reachable, last_day)
                for tx in to_transactions(booked, tenant_id=state.tenant_id, account_id=account_id):
                    counted(tx)
        except ConnectorError as exc:
            if isinstance(exc, BankAccessDenied):
                exc = ReconnectRequired("bank_access_denied")
            return SyncOutcome(record_failure(state, at=now, error=exc), counted.count, error=exc)
        state = record_backfill(state, gap)
        if reachable > first_day:
            state = record_gap(state, TimeRange(start=gap.start, end=_midnight(reachable)))
        return SyncOutcome(state, counted.count)

    @staticmethod
    def _require_active(consent: BankConsent, now: datetime) -> None:
        if consent.status is ConsentStatus.PENDING:
            raise ReconnectRequired("bank_consent_pending")
        if consent.status is not ConsentStatus.ACTIVE:
            raise ReconnectRequired(f"bank_consent_{consent.status.value}")
        if consent.expires_at is not None and consent.expires_at <= now:
            raise ReconnectRequired("bank_consent_expired")

    def _window(self, state: ConnectorState, consent: BankConsent, now: datetime) -> tuple[date, date, date]:
        """``(wanted start, start the consent allows, end)`` in bank booking days."""
        today = now.astimezone(timezone.utc).date()
        wanted = today - self.config.history_window
        if state.cursor:
            try:
                wanted = date.fromisoformat(state.cursor) - self.config.overlap
            except ValueError:
                pass
        start = wanted
        if consent.max_historical_days:
            start = max(start, today - timedelta(days=consent.max_historical_days))
        return wanted, start, today


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)
