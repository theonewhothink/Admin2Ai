"""A real, deterministic supplier-website adapter (QA C5): EDP's customer area through the generic invoice-list
adapter (connectors/portals/invoice_pages.py, configured in backoffice.invoice_sites).

Tested against recorded pages that imitate the customer area's structure (tests/fixtures/portals/edp), served by a
fake website (tests/_edp_site.py) through httpx's mock transport: no real website is ever fetched. The pages and
selectors still need confirming with a real EDP account (the configuration says ``verified=False``).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from _edp_site import AUGUST, EMAIL, GAS, PASSWORD, SEPTEMBER, FakeEdpSite, Invoice
from pydantic import SecretStr

from backoffice.connectors.base import ConnectorKind, ConnectorState
from backoffice.connectors.portals import (
    AuthStatus,
    EdpPortal,
    PortalChanged,
    PortalCredentials,
    PortalError,
    PortalSession,
    PortalSync,
    RetrievalStrategy,
    SessionExpired,
    default_registry,
    parse_amount,
    plan_retrieval,
)
from backoffice.connectors.portals.pages import parse_html
from backoffice.invoice_sites import EDP, site_for_host, site_for_key, site_for_name

NOW = datetime(2026, 10, 2, 9, 30, tzinfo=timezone.utc)
CREDS = PortalCredentials(EMAIL, SecretStr(PASSWORD))


def _site(**kwargs) -> FakeEdpSite:
    site = FakeEdpSite(clock=lambda: NOW, **kwargs)
    site.invoices = [SEPTEMBER, AUGUST, GAS]
    return site


def _signed_in(site: FakeEdpSite) -> tuple[EdpPortal, PortalSession]:
    portal = site.adapter(clock=lambda: NOW)
    result = portal.authenticate(CREDS)
    assert result.status is AuthStatus.AUTHENTICATED and result.session is not None
    return portal, result.session


def _state() -> ConnectorState:
    return ConnectorState(tenant_id="t1", kind=ConnectorKind.SUPPLIER_PORTAL, account=EMAIL, display_name="EDP")


# --------------------------------------------------------------------------- signing in, listing, downloading


def test_the_edp_adapter_signs_in_lists_every_year_and_downloads_the_original_pdfs() -> None:
    site = _site(page_size=2)
    site.invoices.append(Invoice("9001400001", "FT EDP2025/400001", date(2025, 12, 18), "71,30", "", ""))
    portal, session = _signed_in(site)
    sign_in = [r for r in site.requests if r.method == "POST"][0]
    form = dict(p.split("=", 1) for p in sign_in.content.decode().split("&"))
    assert form["_csrf"] and form["origem"] == "area-cliente" and form["email"] == "ana%40padaria.pt"
    assert "lembrar" not in form  # an unchecked box is not sent, as in a browser
    assert session.state == {"cookies": {"edp_sessao": session.state["cookies"]["edp_sessao"]}}
    assert session.expires_at == NOW + timedelta(minutes=EDP.session_minutes)

    refs = portal.list_invoices(session, date(2025, 11, 1), date(2026, 9, 30))
    assert [(r.portal_id, r.invoice_number, r.issue_date, r.gross_amount) for r in refs] == [
        ("9001400001", "FT EDP2025/400001", date(2025, 12, 18), Decimal("71.30")),
        ("9001544002", "FT EDP2026/544002", date(2026, 8, 18), Decimal("50.00")),
        ("9001558120", "FT EDP2026/558120", date(2026, 9, 18), Decimal("64.10")),
        ("9001560077", "FT EDP2026/560077", date(2026, 9, 25), Decimal("31.98"))]
    assert (refs[2].period_start, refs[2].period_end) == (date(2026, 8, 18), date(2026, 9, 17))
    assert refs[2].url == "https://www.edp.pt/area-cliente/faturas/9001558120/pdf"
    listed = [str(r.url) for r in site.requests if r.url.path == "/area-cliente/faturas"]
    assert listed == ["https://www.edp.pt/area-cliente/faturas?ano=2025",  # one page per year, then its next pages
                      "https://www.edp.pt/area-cliente/faturas?ano=2026",
                      "https://www.edp.pt/area-cliente/faturas?ano=2026&pagina=2"]

    doc = portal.retrieve_invoice(session, refs[2])
    assert doc.data == SEPTEMBER.pdf and doc.content_type == "application/pdf"
    assert doc.filename == "Fatura_FT_EDP2026_558120.pdf" and doc.source_url == refs[2].url
    assert doc.ref is refs[2] and doc.retrieved_at == NOW
    assert portal.retrieve_statement(session, date(2026, 9, 1), date(2026, 9, 30)) is None
    # The daily sync fetches only what it has not fetched before.
    got = []
    outcome = PortalSync(portal, clock=lambda: NOW).sync(_state(), got.append, session=session,
                                                         known_ids={"9001544002"})
    assert outcome.outcome.ok and outcome.retrieved_ids == ("9001558120", "9001560077")
    assert [d.data for d in got] == [SEPTEMBER.pdf, GAS.pdf]


def test_a_year_without_invoices_is_not_a_changed_website() -> None:
    site = _site()
    site.invoices = []
    portal, session = _signed_in(site)
    assert portal.list_invoices(session, date(2026, 1, 1), date(2026, 9, 30)) == []


def test_a_sign_in_code_a_wrong_code_and_an_expired_code_on_the_edp_website() -> None:
    clock = {"now": NOW}
    site = _site(code=True)
    site.clock = lambda: clock["now"]
    portal = site.adapter(clock=lambda: clock["now"])
    asked = portal.authenticate(CREDS)
    assert asked.status is AuthStatus.MFA_REQUIRED and asked.owner_message == "EDP needs authentication."
    challenge = asked.challenge
    assert challenge.channel == "sms" and challenge.expires_at == NOW + timedelta(minutes=10)
    assert challenge.resume_state["action"] == "https://www.edp.pt/area-cliente/codigo"
    assert challenge.resume_state["fields"]["pedido"] and challenge.resume_state["cookies"]
    assert PASSWORD not in repr(challenge.resume_state)  # what the vault keeps to resume: never the password

    # Each step in a fresh adapter (the server resumes in another process), from the challenge alone.
    wrong = site.adapter(clock=lambda: clock["now"]).complete_mfa(challenge, "999999")
    assert wrong.status is AuthStatus.CODE_REJECTED
    right = site.adapter(clock=lambda: clock["now"]).complete_mfa(challenge, site.codes[-1])
    assert right.status is AuthStatus.AUTHENTICATED and right.session is not None
    refs = site.adapter().list_invoices(right.session, date(2026, 9, 1), date(2026, 9, 30))
    assert [r.invoice_number for r in refs] == ["FT EDP2026/558120", "FT EDP2026/560077"]

    later = site.adapter(clock=lambda: clock["now"]).authenticate(CREDS).challenge
    clock["now"] = NOW + timedelta(minutes=11)
    expired = site.adapter(clock=lambda: clock["now"]).complete_mfa(later, site.codes[-1])
    assert expired.status is AuthStatus.CODE_EXPIRED
    # The library resumes the same way (PortalSync.resume): a good code fetches the invoices.
    clock["now"] = NOW
    fresh = site.adapter(clock=lambda: NOW).authenticate(CREDS).challenge
    got = []
    done = PortalSync(site.adapter(clock=lambda: NOW), clock=lambda: NOW).resume(
        _state(), fresh, site.codes[-1], got.append, known_ids={"9001544002", "9001560077"})
    assert done.outcome.ok and done.retrieved_ids == ("9001558120",) and got[0].data == SEPTEMBER.pdf


def test_a_refused_password_an_outage_and_a_changed_website_are_told_apart() -> None:
    site = _site()
    refused = site.adapter().authenticate(PortalCredentials(EMAIL, SecretStr("old-password")))
    assert refused.status is AuthStatus.LOGIN_REQUIRED
    outcome = PortalSync(site.adapter(), clock=lambda: NOW).sync(
        _state(), lambda d: None, credentials=PortalCredentials(EMAIL, SecretStr("old-password")))
    assert outcome.outcome.state.reconnect_required  # only the owner can fix it: "EDP needs reconnecting."

    site.down = True
    with pytest.raises(PortalError) as down:
        site.adapter().authenticate(CREDS)
    assert down.value.code == "http_503" and not isinstance(down.value, PortalChanged)
    outage = PortalSync(site.adapter(), clock=lambda: NOW).sync(_state(), lambda d: None, credentials=CREDS)
    assert outage.outcome.error.retryable and not outage.adapter_broken  # tried again later

    site.down = False
    site.layout = "cards"  # the customer area was redesigned: no invoice table any more
    changed = PortalSync(site.adapter(), clock=lambda: NOW).sync(_state(), lambda d: None, credentials=CREDS)
    assert changed.adapter_broken and changed.outcome.error.code == "portal_changed:invoice_list_missing"
    plan = plan_retrieval(registry=default_registry, supplier_key="edp_pt", adapter_broken=True,
                          allow_ai_fallback=True)
    assert plan.strategy is RetrievalStrategy.AI_BROWSER  # the fallback, never a guess


def test_a_session_the_website_signed_out_is_replaced_by_one_new_sign_in() -> None:
    site = _site()
    portal, session = _signed_in(site)
    ref = portal.list_invoices(session, date(2026, 9, 1), date(2026, 9, 30))[0]
    site.expire_sessions()  # the website signed the session out: a list or a download sends to the sign-in
    with pytest.raises(SessionExpired):
        portal.list_invoices(session, date(2026, 9, 1), date(2026, 9, 30))
    with pytest.raises(SessionExpired):
        portal.retrieve_invoice(session, ref)
    before = site.signed_in()
    got = []
    outcome = PortalSync(site.adapter(), clock=lambda: NOW).sync(_state(), got.append, credentials=CREDS,
                                                                 session=session)
    assert outcome.outcome.ok and site.signed_in() == before + 1 and len(got) == 3
    assert outcome.session is not None and outcome.session.state != session.state  # the new one, for the vault


def test_the_adapter_never_leaves_the_suppliers_website() -> None:
    site = _site()
    portal, session = _signed_in(site)
    ref = portal.list_invoices(session, date(2026, 9, 1), date(2026, 9, 30))[0]
    site.download_redirect = "https://faturas-edp.example/ft/558120.pdf"
    with pytest.raises(PortalChanged) as off:
        portal.retrieve_invoice(session, ref)
    assert off.value.code == "link_off_site"
    assert {r.url.host for r in site.requests} == {"www.edp.pt"}  # never followed, the cookie never sent there
    assert {host for host, _ in site.passwords_seen} == {"www.edp.pt"}
    for bad in ("http://www.edp.pt/area-cliente/entrar", "https://www.edp.pt.evil.example/x",
                "https://user:pw@www.edp.pt/x", "javascript:alert(1)"):
        with pytest.raises(PortalChanged):
            portal._url(bad, "https://www.edp.pt/area-cliente/faturas")
    site.download_redirect = None
    page_not_pdf = replace(ref, url="https://www.edp.pt/area-cliente/inicio")  # a link that answers a web page
    with pytest.raises(PortalChanged) as not_pdf:
        portal.retrieve_invoice(session, page_not_pdf)
    assert not_pdf.value.code == "download_not_a_pdf"


def test_the_edp_adapter_is_registered_and_known_by_its_host_and_name() -> None:
    assert default_registry.get("edp_pt") is EdpPortal and default_registry.for_host("www.edp.pt") is EdpPortal
    assert EdpPortal.display_name == "EDP" and EdpPortal.domains == ("edp.pt",) and EdpPortal.deterministic
    plan = plan_retrieval(registry=default_registry, host="www.edp.pt", allow_ai_fallback=True)
    assert plan.strategy is RetrievalStrategy.DETERMINISTIC and plan.connector is EdpPortal
    assert site_for_key("edp_pt") is EDP and site_for_host("faturas.edp.pt") is EDP
    assert site_for_name(" edp  comercial ") is EDP and site_for_name("Vodafone") is None
    assert site_for_host("edp.pt.evil.example") is None and not EDP.verified  # not yet checked on a real account


# --------------------------------------------------------------------------- reading the pages


def test_rows_cells_forms_and_amounts_are_read_like_a_browser_would() -> None:
    doc = parse_html("<table class='lista-faturas'><tbody><tr data-fatura-id=1><td class=numero>FT 1"
                     "<td class=valor>1.234,56&nbsp;€<tr data-fatura-id=2><td class=numero>FT 2</table>"
                     "<form id=f><input type=hidden name=t value=x><input type=password name=p value=s>"
                     "<select name=s><option value=a>A<option value=b selected>B</select>"
                     "<input type=checkbox name=c><input type=submit name=go value=Go></form>")
    rows = doc.select("table.lista-faturas > tbody > tr")
    assert [r.attr("data-fatura-id") for r in rows] == ["1", "2"]  # unclosed rows and cells, closed for it
    assert rows[0].select_one("td.valor").text() == "1.234,56 €"
    assert doc.select_one("form#f").form_fields() == {"t": "x", "s": "b"}  # never a password, box or button
    assert len(doc.select("tr, form")) == 3 and doc.select_one("[data-fatura-id='2'] td.numero").text() == "FT 2"
    for unsupported in ("tr:first-child", "td + td", "> tr", "tr >", "", "a,"):
        with pytest.raises(ValueError):
            doc.select(unsupported)
    assert [parse_amount(t) for t in ("64,10 €", "€ 1.234,56", "1,234.56 EUR", "1.234", "-12,00", "—")] == [
        Decimal("64.10"), Decimal("1234.56"), Decimal("1234.56"), Decimal("1234"), Decimal("-12.00"), None]
