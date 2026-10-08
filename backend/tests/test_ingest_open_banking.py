"""Open banking: GoCardless-style adapter, consent expiry, Decimal transactions (§4, §20, §47)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from backoffice.connectors.base import ConnectorKind, ConnectorState, Health, ProviderError, TransientError, evaluate_health
from backoffice.connectors.open_banking import (
    BankAccessDenied,
    BankAggregator,
    BankConsent,
    BookedTransaction,
    ConsentStatus,
    GoCardlessBankAccountData,
    OpenBankingConnector,
    parse_gocardless_transaction,
    to_transactions,
)
from backoffice.domain.models import TransactionKind

NOW = datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc)
BASE = "https://bankaccountdata.gocardless.com/api/v2"

BOOKED = [
    {"transactionId": "T1", "bookingDate": "2026-09-18", "valueDate": "2026-09-18",
     "transactionAmount": {"amount": "-117.20", "currency": "EUR"}, "creditorName": "HAZEL TREE LDA",
     "creditorAccount": {"iban": "PT50 0002 0123 1234 5678 9015 4"}, "endToEndId": "FT2026-183",
     "remittanceInformationUnstructured": "TRF FT 2026/183", "bankTransactionCode": "PMNT-ICDT-ESCT"},
    {"internalTransactionId": "i-2", "bookingDate": "2026-09-19", "transactionAmount": {"amount": -92.40, "currency": "eur"},
     "creditorName": "VODAFONE PORTUGAL", "remittanceInformationUnstructuredArray": ["DD VODAFONE", "SEPA"],
     "bankTransactionCode": "PMNT-RDDT-ESDD", "endToEndId": "NOTPROVIDED"},
    {"bookingDate": "2026-09-20", "transactionAmount": {"amount": "-4.50", "currency": "EUR"},
     "remittanceInformationUnstructured": "COMPRA CARTAO 4817 PASTELARIA"},
    {"bookingDate": "2026-09-20", "transactionAmount": {"amount": "-4.50", "currency": "EUR"},
     "remittanceInformationUnstructured": "COMPRA CARTAO 4817 PASTELARIA"},
    {"transactionId": "T5", "bookingDate": "2026-09-21", "transactionAmount": {"amount": "1500.00", "currency": "EUR"},
     "debtorName": "CLIENTE SA", "debtorAccount": {"iban": "PT50000201231234567890154"}},
    {"transactionId": "T6", "bookingDateTime": "2026-09-22T10:00:00Z",
     "transactionAmount": {"amount": "-2.08", "currency": "EUR"}, "proprietaryBankTransactionCode": "CHRG",
     "remittanceInformationUnstructured": "COMISSAO MANUTENCAO"},
]

# What the adapter sees after decoding the wire JSON (numbers become Decimal, never float).
WIRE = json.loads(json.dumps(BOOKED), parse_float=Decimal)


class FakeGoCardless:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.requisition_status = "LN"
        self.accepted = "2026-07-01T10:00:00.000Z"
        self.transactions_status = 200
        self.token_expired_once = False
        self.bad_secret = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.replace("/api/v2", "")
        if path == "/token/new/":
            if self.bad_secret:
                return httpx.Response(401, json={"summary": "Authentication failed"})
            return httpx.Response(200, json={"access": "acc-1", "access_expires": 86400, "refresh": "ref-1",
                                             "refresh_expires": 2592000})
        if path == "/token/refresh/":
            return httpx.Response(200, json={"access": "acc-2", "access_expires": 86400})
        if self.token_expired_once and request.headers.get("authorization") == "Bearer acc-1":
            self.token_expired_once = False
            return httpx.Response(401, json={"summary": "Invalid token"})
        if path == "/requisitions/req-1/":
            return httpx.Response(200, json={"id": "req-1", "status": self.requisition_status, "agreement": "agr-1",
                                             "accounts": ["acc-A"], "institution_id": "MILLENNIUMBCP_BCOMPTPL"})
        if path == "/agreements/enduser/agr-1/":
            return httpx.Response(200, json={"id": "agr-1", "accepted": self.accepted, "access_valid_for_days": 90,
                                             "max_historical_days": 180})
        if path == "/accounts/acc-A/transactions/":
            if self.transactions_status != 200:
                return httpx.Response(self.transactions_status, json={"summary": "x"},
                                      headers={"HTTP_X_RATELIMIT_ACCOUNT_SUCCESS_RESET": "3600"})
            return httpx.Response(200, json={"transactions": {"booked": BOOKED, "pending": [
                {"transactionAmount": {"amount": "-9.99", "currency": "EUR"}, "creditorName": "PENDING"}]}})
        if path == "/accounts/acc-A/":
            return httpx.Response(200, json={"id": "acc-A", "iban": "PT50000201231234567890154", "status": "READY"})
        if path == "/accounts/acc-A/details/":
            return httpx.Response(200, json={"account": {"iban": "PT50000201231234567890154", "currency": "EUR",
                                                         "ownerName": "PADARIA LDA", "product": "Conta Empresa"}})
        if path == "/agreements/enduser/" and request.method == "POST":
            return httpx.Response(201, json={"id": "agr-9"})
        if path == "/requisitions/" and request.method == "POST":
            return httpx.Response(201, json={"id": "req-9", "link": "https://ob.gocardless.com/psd2/start/req-9",
                                             "status": "CR"})
        return httpx.Response(404, json={"summary": "Not found"})


def adapter(api: FakeGoCardless, clock=lambda: NOW) -> GoCardlessBankAccountData:
    return GoCardlessBankAccountData("sid", "skey", client=httpx.Client(transport=httpx.MockTransport(api)),
                                     clock=clock)


def new_state(**kw) -> ConnectorState:
    return ConnectorState(tenant_id="t1", kind=ConnectorKind.OPEN_BANKING, account="Millennium ••••0154",
                          display_name="Millennium BCP", **kw)


# --------------------------------------------------------------------------- adapter


def test_adapter_satisfies_the_protocol_and_hides_secrets():
    gc = adapter(FakeGoCardless())
    assert isinstance(gc, BankAggregator)
    assert "skey" not in repr(gc)


def test_consent_maps_status_and_expiry():
    api = FakeGoCardless()
    consent = adapter(api).consent("req-1")
    assert consent.status is ConsentStatus.ACTIVE and consent.account_ids == ("acc-A",)
    assert consent.expires_at == datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
    assert consent.max_historical_days == 180
    token_call = api.requests[0]
    assert token_call.url.path.endswith("/token/new/")
    assert json.loads(token_call.content) == {"secret_id": "sid", "secret_key": "skey"}
    assert api.requests[1].headers["authorization"] == "Bearer acc-1"
    for code, status in (("EX", ConsentStatus.EXPIRED), ("RJ", ConsentStatus.REJECTED), ("GC", ConsentStatus.PENDING)):
        api.requisition_status = code
        assert adapter(api).consent("req-1").status is status


def test_expired_access_token_is_renewed_once():
    api = FakeGoCardless()
    api.token_expired_once = True
    gc = adapter(api)
    assert gc.consent("req-1").status is ConsentStatus.ACTIVE
    auths = [r.headers.get("authorization") for r in api.requests if "requisitions" in r.url.path]
    assert auths == ["Bearer acc-1", "Bearer acc-2"]  # rejected once, renewed via the refresh token
    assert any(r.url.path.endswith("/token/refresh/") for r in api.requests)


def test_token_refresh_is_used_before_new_token():
    api = FakeGoCardless()
    clock_now = [NOW]
    gc = adapter(api, clock=lambda: clock_now[0])
    gc.consent("req-1")
    clock_now[0] = NOW + timedelta(days=2)  # access expired, refresh still valid
    gc.consent("req-1")
    paths = [r.url.path.rsplit("/api/v2", 1)[1] for r in api.requests if "/token/" in r.url.path]
    assert paths == ["/token/new/", "/token/refresh/"]


def test_bad_secrets_are_our_problem_not_the_owners():
    api = FakeGoCardless()
    api.bad_secret = True
    with pytest.raises(ProviderError) as info:
        adapter(api).consent("req-1")
    assert info.value.code == "gocardless_credentials" and not info.value.needs_reconnect


def test_transactions_are_decimal_and_booked_only():
    booked = adapter(FakeGoCardless()).booked_transactions("acc-A", date(2026, 9, 1), date(2026, 9, 25))
    assert len(booked) == 6
    first, second = booked[0], booked[1]
    assert first.amount == Decimal("-117.20") and isinstance(first.amount, Decimal)
    assert first.creditor_iban == "PT50000201231234567890154"
    assert second.amount == Decimal("-92.40")  # JSON number parsed exactly, never through float
    assert second.currency == "EUR" and second.end_to_end_id is None
    assert second.remittance == "DD VODAFONE SEPA"
    assert booked[5].booked_on == date(2026, 9, 22)


def test_rate_limit_and_access_denied():
    api = FakeGoCardless()
    api.transactions_status = 429
    with pytest.raises(TransientError) as info:
        adapter(api).booked_transactions("acc-A", date(2026, 9, 1), date(2026, 9, 25))
    assert info.value.retry_after == 3600
    api.transactions_status = 409
    with pytest.raises(BankAccessDenied):
        adapter(api).booked_transactions("acc-A", date(2026, 9, 1), date(2026, 9, 25))


def test_account_details_and_link_creation():
    api = FakeGoCardless()
    gc = adapter(api)
    info = gc.account("acc-A")
    assert info.iban == "PT50000201231234567890154" and info.owner_name == "PADARIA LDA" and info.status == "READY"
    link = gc.create_link(institution_id="MILLENNIUMBCP_BCOMPTPL", redirect_url="https://app.example/bank/done",
                          reference="t1-conn-1", history_days=90, access_days=90)
    assert link.requisition_id == "req-9" and link.link.startswith("https://ob.gocardless.com/")
    agreement = json.loads(next(r for r in api.requests if r.url.path.endswith("/agreements/enduser/")).content)
    assert agreement["access_scope"] == ["balances", "details", "transactions"]
    requisition = json.loads(next(r for r in api.requests if r.url.path.endswith("/requisitions/")).content)
    assert requisition["agreement"] == "agr-9" and requisition["user_language"] == "PT"


@pytest.mark.parametrize(
    "raw",
    [
        {"bookingDate": "2026-09-01", "transactionAmount": {"amount": "abc", "currency": "EUR"}},
        {"bookingDate": "2026-09-01", "transactionAmount": {"amount": "1.00", "currency": "EURO"}},
        {"transactionAmount": {"amount": "1.00", "currency": "EUR"}},
        {"bookingDate": "2026-09-01", "transactionAmount": {"amount": 1.5, "currency": "EUR"}},
        {"bookingDate": "2026-09-01", "transactionAmount": {"amount": "NaN", "currency": "EUR"}},
    ],
)
def test_malformed_transactions_fail_loudly(raw):
    with pytest.raises(ProviderError):
        parse_gocardless_transaction(raw)


# --------------------------------------------------------------------------- mapping


def test_mapping_to_domain_transactions():
    booked = [parse_gocardless_transaction(b) for b in WIRE]
    txs = to_transactions(booked, tenant_id="t1", account_id="acct_1")
    hazel, vodafone, coffee1, coffee2, client, fee = txs
    assert hazel.amount == Decimal("-117.20") and hazel.counterparty == "HAZEL TREE LDA"
    assert hazel.kind is TransactionKind.TRANSFER_OUT and hazel.reference == "FT2026-183"
    assert hazel.counterparty_iban == "PT50000201231234567890154"
    assert vodafone.kind is TransactionKind.DIRECT_DEBIT and vodafone.reference is None
    assert coffee1.kind is TransactionKind.CARD and coffee1.card_last4 == "4817"
    assert coffee1.id != coffee2.id  # identical same-day rows stay distinct
    assert client.kind is TransactionKind.TRANSFER_IN and client.counterparty == "CLIENTE SA"
    assert fee.kind is TransactionKind.FEE
    assert all(t.tenant_id == "t1" and t.account_id == "acct_1" for t in txs)


def test_transaction_ids_are_stable_across_syncs():
    booked = [parse_gocardless_transaction(b) for b in WIRE]
    a = [t.id for t in to_transactions(booked, tenant_id="t1", account_id="acct_1")]
    b = [t.id for t in to_transactions(booked, tenant_id="t1", account_id="acct_1")]
    other = [t.id for t in to_transactions(booked, tenant_id="t2", account_id="acct_1")]
    assert a == b and not set(a) & set(other)


# --------------------------------------------------------------------------- connector


class FakeAggregator:
    def __init__(self, consent: BankConsent, booked=None, deny=False):
        self._consent, self._booked, self._deny = consent, booked or [], deny
        self.windows = []

    def create_link(self, **kw):  # pragma: no cover - not used here
        raise NotImplementedError

    def consent(self, requisition_id):
        return self._consent

    def account(self, account_id):  # pragma: no cover - not used here
        raise NotImplementedError

    def booked_transactions(self, account_id, date_from, date_to):
        self.windows.append((account_id, date_from, date_to))
        if self._deny:
            raise BankAccessDenied("gocardless_http_401")
        return self._booked


def consent(status=ConsentStatus.ACTIVE, expires=NOW + timedelta(days=60), max_days=None) -> BankConsent:
    return BankConsent("req-1", status, ("acc-A",), "BANK", expires, max_days)


def test_first_sync_imports_the_history_window():
    agg = FakeAggregator(consent(), [parse_gocardless_transaction(b) for b in WIRE])
    got = []
    outcome = OpenBankingConnector(agg, "req-1", account_ids={"acc-A": "acct_1"}, clock=lambda: NOW).sync(
        new_state(), got.append)
    assert outcome.ok and outcome.full_sync and outcome.delivered == 6
    assert agg.windows == [("acc-A", date(2026, 6, 27), date(2026, 9, 25))]
    assert got[0].account_id == "acct_1"
    s = outcome.state
    assert s.cursor == "2026-09-25" and s.auth_expires_at == NOW + timedelta(days=60)
    assert s.coverage_start == datetime(2026, 6, 27, tzinfo=timezone.utc)


def test_next_sync_overlaps_and_respects_the_consent_history_limit():
    agg = FakeAggregator(consent(max_days=30))
    OpenBankingConnector(agg, "req-1", clock=lambda: NOW).sync(new_state(cursor="2026-09-24"), lambda t: None)
    first = OpenBankingConnector(agg, "req-1", clock=lambda: NOW).sync(new_state(), lambda t: None)
    assert agg.windows == [("acc-A", date(2026, 9, 19), date(2026, 9, 25)), ("acc-A", date(2026, 8, 26), date(2026, 9, 25))]
    assert first.state.known_gaps == ()  # a first sync limited by consent starts coverage later instead
    assert first.state.coverage_start == datetime(2026, 8, 26, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("bank_consent", "code"),
    [
        (consent(expires=NOW - timedelta(minutes=1)), "bank_consent_expired"),
        (consent(status=ConsentStatus.EXPIRED), "bank_consent_expired"),
        (consent(status=ConsentStatus.REJECTED), "bank_consent_rejected"),
        (consent(status=ConsentStatus.PENDING), "bank_consent_pending"),
    ],
)
def test_consent_problems_need_the_owner(bank_consent, code):
    last = datetime(2026, 9, 24, 14, 42, tzinfo=timezone.utc)
    outcome = OpenBankingConnector(FakeAggregator(bank_consent), "req-1", clock=lambda: NOW).sync(
        new_state(last_successful_sync=last, cursor="2026-09-24"), lambda t: None)
    assert outcome.error.code == code and outcome.state.reconnect_required
    report = evaluate_health(outcome.state, NOW)
    assert report.health is Health.BROKEN and report.title == "Millennium BCP needs reconnecting."
    assert report.detail == "Your bank account has not synced since 14:42 yesterday."
    assert report.notify


def test_access_denied_mid_sync_means_reconnect():
    outcome = OpenBankingConnector(FakeAggregator(consent(), deny=True), "req-1", clock=lambda: NOW).sync(
        new_state(), lambda t: None)
    assert outcome.error.code == "bank_access_denied" and outcome.state.reconnect_required


def test_consent_expiring_soon_warns_without_breaking():
    outcome = OpenBankingConnector(FakeAggregator(consent(expires=NOW + timedelta(days=3))), "req-1",
                                   clock=lambda: NOW).sync(new_state(), lambda t: None)
    report = evaluate_health(outcome.state, NOW)
    assert outcome.ok and report.health is Health.DEGRADED
    assert report.title == "Millennium BCP needs reconnecting soon." and report.action.label == "Reconnect"


def test_booked_transaction_rejects_float_money():
    bt = BookedTransaction("x", date(2026, 9, 1), Decimal("1.00"), "EUR")
    assert to_transactions([bt], tenant_id="t1", account_id="a")[0].amount == Decimal("1.00")
    with pytest.raises(TypeError):
        BookedTransaction("x", date(2026, 9, 1), 1.0, "EUR")
