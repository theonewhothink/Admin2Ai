"""Supplier portal adapters: registry, deterministic-first planning, sync runner (§9, §10, §47)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import SecretStr

from backoffice.connectors.base import ConnectorKind, ConnectorState, Health, evaluate_health
from backoffice.connectors.portals import (
    AuthResult,
    AuthStatus,
    PortalChanged,
    PortalCredentials,
    PortalDocument,
    PortalError,
    PortalInvoiceRef,
    PortalRegistry,
    PortalSession,
    PortalSync,
    RetrievalStrategy,
    SupplierPortalConnector,
    authentication_prompt,
    plan_retrieval,
)

NOW = datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc)
CREDS = PortalCredentials("ana@padaria.pt", SecretStr("hunter2"))


class TestPortal(SupplierPortalConnector):
    """Test double standing in for a real adapter such as VodafoneConnector."""

    __test__ = False  # not a pytest class
    supplier_key = "vodafone_pt"
    display_name = "Vodafone"
    domains = ("vodafone.pt",)

    def __init__(self, *, auth=AuthStatus.AUTHENTICATED, fail_list=None):
        self.auth = auth
        self.fail_list = fail_list
        self.retrieved: list[str] = []
        self.listed_since: list[date] = []

    def authenticate(self, credentials):
        if self.auth is AuthStatus.MFA_REQUIRED:
            return self.mfa_required(credentials.username, channel="sms", resume_state={"flow": "abc"})
        if self.auth is AuthStatus.AUTHENTICATED:
            return AuthResult(AuthStatus.AUTHENTICATED, PortalSession(self.supplier_key, credentials.username, NOW))
        return AuthResult(self.auth)

    def list_invoices(self, session, since, until):
        self.listed_since.append(since)
        if self.fail_list:
            raise self.fail_list
        return [PortalInvoiceRef(self.supplier_key, f"inv-{i}", f"FT 2026/{180 + i}", date(2026, 9, 20 + i),
                                 Decimal("92.40"), "EUR") for i in range(3)]

    def retrieve_invoice(self, session, ref):
        self.retrieved.append(ref.portal_id)
        return PortalDocument(ref, b"%PDF-" + ref.portal_id.encode(), filename=f"{ref.portal_id}.pdf",
                              retrieved_at=NOW)

    def retrieve_statement(self, session, period_start, period_end):
        return None


class OtherPortal(TestPortal):
    __test__ = False
    supplier_key = "vodafone_business"
    display_name = "Vodafone Business"
    domains = ("business.vodafone.pt",)


def new_state(**kw) -> ConnectorState:
    return ConnectorState(tenant_id="t1", kind=ConnectorKind.SUPPLIER_PORTAL, account="ana@padaria.pt",
                          display_name="Vodafone", **kw)


# --------------------------------------------------------------------------- registry and planning


def test_registry_lookup_by_key_and_most_specific_host():
    registry = PortalRegistry()
    registry.register(TestPortal)
    registry.register(OtherPortal)
    registry.register(TestPortal)  # idempotent for the same class
    assert registry.get("vodafone_pt") is TestPortal
    assert registry.for_host("my.vodafone.pt") is TestPortal
    assert registry.for_host("portal.business.vodafone.pt") is OtherPortal
    assert registry.for_host("vodafone.pt.evil.example") is None
    assert registry.keys() == ["vodafone_business", "vodafone_pt"]


def test_registry_refuses_conflicts_and_incomplete_adapters():
    registry = PortalRegistry()
    registry.register(TestPortal)

    class Impostor(TestPortal):
        __test__ = False

    with pytest.raises(ValueError):
        registry.register(Impostor)

    class Nameless(TestPortal):
        __test__ = False
        display_name = ""

    with pytest.raises(ValueError):
        registry.register(Nameless)


def test_deterministic_adapters_come_first():
    registry = PortalRegistry()
    registry.register(TestPortal)
    plan = plan_retrieval(registry=registry, host="minha.vodafone.pt", allow_ai_fallback=True)
    assert plan.strategy is RetrievalStrategy.DETERMINISTIC and plan.connector is TestPortal


def test_ai_browser_is_only_a_permitted_fallback():
    registry = PortalRegistry()
    registry.register(TestPortal)
    broken = plan_retrieval(registry=registry, supplier_key="vodafone_pt", adapter_broken=True, allow_ai_fallback=True)
    assert broken.strategy is RetrievalStrategy.AI_BROWSER and broken.reason == "adapter_broken"
    unknown = plan_retrieval(registry=registry, host="portal.unknown.example", allow_ai_fallback=True)
    assert unknown.strategy is RetrievalStrategy.AI_BROWSER and unknown.connector is None
    forbidden = plan_retrieval(registry=registry, host="portal.unknown.example")
    assert forbidden.strategy is RetrievalStrategy.NONE


def test_non_deterministic_adapter_is_not_treated_as_deterministic():
    class AiNavigated(TestPortal):
        __test__ = False
        supplier_key = "ai_generic"
        deterministic = False

    registry = PortalRegistry()
    registry.register(AiNavigated)
    assert plan_retrieval(registry=registry, supplier_key="ai_generic").strategy is RetrievalStrategy.NONE


# --------------------------------------------------------------------------- adapter contract


def test_detect_new_and_history_defaults():
    portal = TestPortal()
    session = PortalSession("vodafone_pt", "ana", NOW)
    fresh = portal.detect_new(session, {"inv-0"}, date(2026, 9, 1), date(2026, 9, 30))
    assert [r.portal_id for r in fresh] == ["inv-1", "inv-2"]
    docs = list(portal.retrieve_history(session, date(2026, 9, 1), date(2026, 9, 30)))
    assert [d.filename for d in docs] == ["inv-0.pdf", "inv-1.pdf", "inv-2.pdf"]
    assert portal.complete_mfa(None, "123456").status is AuthStatus.FAILED  # type: ignore[arg-type]


def test_invoice_refs_refuse_float_money_and_credentials_hide_passwords():
    with pytest.raises(TypeError):
        PortalInvoiceRef("vodafone_pt", "x", gross_amount=92.40)  # type: ignore[arg-type]
    assert "hunter2" not in repr(CREDS)
    assert authentication_prompt("Vodafone") == "Vodafone needs authentication."


# --------------------------------------------------------------------------- sync runner


def test_sync_retrieves_only_new_documents():
    portal, got = TestPortal(), []
    result = PortalSync(portal, clock=lambda: NOW).sync(new_state(), got.append, credentials=CREDS,
                                                          known_ids={"inv-0"})
    assert result.outcome.ok and result.outcome.delivered == 2
    assert result.retrieved_ids == ("inv-1", "inv-2") and [d.filename for d in got] == ["inv-1.pdf", "inv-2.pdf"]
    assert result.session.account == "ana@padaria.pt"
    assert result.outcome.state.last_successful_sync == NOW
    assert portal.listed_since == [(NOW - timedelta(days=90)).date() - timedelta(days=7)]


def test_valid_session_skips_login():
    portal = TestPortal(auth=AuthStatus.FAILED)  # would fail if called
    session = PortalSession("vodafone_pt", "ana", NOW, expires_at=NOW + timedelta(hours=1))
    result = PortalSync(portal, clock=lambda: NOW).sync(new_state(), lambda d: None, session=session)
    assert result.outcome.ok


def test_mfa_prompts_the_owner_without_breaking_the_connection():
    result = PortalSync(TestPortal(auth=AuthStatus.MFA_REQUIRED), clock=lambda: NOW).sync(
        new_state(), lambda d: None, credentials=CREDS)
    assert result.owner_message == "Vodafone needs authentication."
    assert result.challenge.channel == "sms" and result.challenge.resume_state == {"flow": "abc"}
    state = result.outcome.state
    assert not state.reconnect_required and state.last_error_code == "portal_mfa_required"


def test_rejected_login_needs_reconnect_with_plain_copy():
    result = PortalSync(TestPortal(auth=AuthStatus.LOGIN_REQUIRED), clock=lambda: NOW).sync(
        new_state(), lambda d: None, credentials=CREDS)
    report = evaluate_health(result.outcome.state, NOW)
    assert report.health is Health.BROKEN and report.title == "Vodafone needs reconnecting."
    missing = PortalSync(TestPortal(), clock=lambda: NOW).sync(new_state(), lambda d: None)
    assert missing.outcome.state.reconnect_required


def test_portal_layout_change_flags_the_adapter_for_fallback():
    result = PortalSync(TestPortal(fail_list=PortalChanged("invoice_table_missing")), clock=lambda: NOW).sync(
        new_state(), lambda d: None, credentials=CREDS)
    assert result.adapter_broken and not result.outcome.ok
    assert result.outcome.error.code == "portal_changed:invoice_table_missing"


def test_portal_outage_is_transient():
    result = PortalSync(TestPortal(fail_list=PortalError("http_503")), clock=lambda: NOW).sync(
        new_state(), lambda d: None, credentials=CREDS)
    assert result.outcome.error.retryable and not result.adapter_broken
    failed_auth = PortalSync(TestPortal(auth=AuthStatus.FAILED), clock=lambda: NOW).sync(
        new_state(), lambda d: None, credentials=CREDS)
    assert failed_auth.outcome.error.code == "portal_auth_failed" and failed_auth.outcome.error.retryable
