"""Regressions found in adversarial review of the evidence and connector modules.

Each test names the defect it pins down. Grouped by module.
"""

from __future__ import annotations

import hashlib
import io
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from backoffice.connectors import (
    ConnectorKind,
    ConnectorState,
    GmailConnector,
    GoCardlessBankAccountData,
    MicrosoftMailConnector,
    OpenBankingConnector,
    ProviderError,
    ReconnectRequired,
    TransientError,
    needs_backfill,
)
from backoffice.connectors.open_banking import BankAccessDenied, BankConsent, BookedTransaction, ConsentStatus
from backoffice.connectors.portals import (
    AuthResult,
    AuthStatus,
    PortalDocument,
    PortalInvoiceRef,
    PortalRegistry,
    PortalSession,
    PortalSync,
    RetrievalStrategy,
    SupplierPortalConnector,
    plan_retrieval,
)
from backoffice.domain.models import EvidenceFormat, SourceKind
from backoffice.evidence import (
    EmailIngestor,
    EvidenceRegistry,
    FetchPolicy,
    LinkFetcher,
    LinkOutcome,
    LocalObjectStore,
    S3ObjectStore,
    SessionCookie,
    ShareIntake,
    SharePayload,
    StorageConfigError,
    UploadRequest,
    UploadService,
    UploadStatus,
    UrlSafety,
    UrlSafetyConfig,
    analyze_html,
    expand_zip,
    extract_links,
    parse_eml,
)

NOW = datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc)
PDF = b"%PDF-1.4\n% invoice\n%%EOF\n"
FIXTURES = Path(__file__).parent / "fixtures" / "email"


def _zip(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return out.getvalue()


def _xlsx() -> bytes:
    return _zip({"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<workbook/>",
                 "xl/worksheets/sheet1.xml": b"<worksheet/>"})


# --------------------------------------------------------------------------- html / email: linear time


def test_vml_buttons_in_one_huge_comment_are_read_in_linear_time():
    """Defect: each VML match copied the rest of the comment (O(n^2)): 20k buttons took ~25 s."""
    many = "<!--[if mso]>" + '<v:roundrect href="https://s.example/i.pdf">Ver fatura</v:roundrect>' * 20_000 + "<![endif]-->"
    start = time.perf_counter()
    signals = analyze_html(many)
    assert time.perf_counter() - start < 3
    assert signals.anchors and signals.anchors[0].text == "Ver fatura" and signals.anchors[0].source == "vml"


def test_unclosed_tags_in_a_comment_cannot_stall_the_link_scanner():
    """Defect: `[^>]*` scanned to the end of the comment for every `<a` start (ReDoS)."""
    start = time.perf_counter()
    analyze_html("<!--" + "<a " * 100_000 + "-->")
    assert time.perf_counter() - start < 3


def test_anchor_count_is_bounded():
    signals = analyze_html('<a href="https://s.example/x">x</a>' * 20_000)
    assert len(signals.anchors) <= 5_000 and signals.anchors_truncated


def test_many_plain_text_links_are_extracted_in_linear_time():
    """Defect: the context lookup scanned back to the start of the text for every URL."""
    start = time.perf_counter()
    links = extract_links("", "https://s.example/x) " * 100_000)
    assert time.perf_counter() - start < 3
    assert [link.url for link in links] == ["https://s.example/x"]


# --------------------------------------------------------------------------- email: invoice context


def test_download_button_in_an_invoice_email_is_invoice_likely():
    """Defect: §9 'Your invoice is ready' + a bare 'Download' button scored 25 and was never fetched."""
    html = '<td bgcolor="#e60000"><a href="https://s.example/d/1">Download</a></td>'
    (plain,) = extract_links(html, "")
    assert not plain.invoice_likely
    (link,) = extract_links(html, "", context="Your invoice is ready")
    assert link.invoice_likely and "context:invoice" in link.reasons
    (pt,) = extract_links(html.replace("Download", "Descarregar"), "", context="A sua fatura de setembro")
    assert pt.invoice_likely


def test_invoice_context_never_rescues_negative_or_app_links():
    html = ('<a class="btn" href="https://s.example/u">Unsubscribe</a>'
            '<a class="btn" href="https://s.example/app">Download our app</a>')
    links = extract_links(html, "", context="Your invoice is ready")
    assert not any(link.invoice_likely for link in links)


def test_parsed_email_uses_its_subject_as_invoice_context():
    raw = (b"From: Billing <billing@s.example>\r\nTo: ana@padaria.pt\r\nSubject: Your invoice is ready\r\n"
           b"Message-ID: <1@s.example>\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
           b'<table><tr><td bgcolor="#0055ff"><a href="https://s.example/d/77">Download</a></td></tr></table>')
    parsed = parse_eml(raw)
    assert [link.url for link in parsed.invoice_links] == ["https://s.example/d/77"]


# --------------------------------------------------------------------------- archives


def test_office_files_inside_a_zip_are_members_not_nested_archives():
    """Defect: an .xlsx (itself a ZIP) was exploded into XML parts and never kept as a spreadsheet."""
    docx = _zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w/>"})
    outer = _zip({"statement.xlsx": _xlsx(), "letter.docx": docx, "inner.zip": _zip({"a.pdf": PDF})})
    expansion = expand_zip(outer)
    assert sorted(m.path for m in expansion.members) == ["inner.zip/a.pdf", "letter.docx", "statement.xlsx"]
    assert expansion.complete


def test_shared_zip_with_a_spreadsheet_registers_the_spreadsheet(tmp_path):
    registry = EvidenceRegistry(LocalObjectStore(tmp_path))
    outcome = ShareIntake(registry, clock=lambda: NOW).accept(
        "t1", SharePayload.for_file(_zip({"extrato.xlsx": _xlsx()}), "export.zip"))
    formats = [r.evidence.format for r in outcome.registrations]
    assert formats == [EvidenceFormat.ZIP, EvidenceFormat.XLSX]


# --------------------------------------------------------------------------- links


class _Resolver:
    def resolve(self, host, port):
        return ["93.184.216.34"]


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _fetcher(handler, **kw) -> LinkFetcher:
    safety = UrlSafety(UrlSafetyConfig(known_domains=("acme-cloud.com",)), _Resolver())
    return LinkFetcher(safety, transport=httpx.MockTransport(handler), clock=lambda: NOW, **kw)


def _html(body: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=body.encode())


def test_invoice_page_with_newsletter_field_and_sign_in_link_still_downloads_the_invoice():
    """Defect: an email field + 'Sign in' nav link was taken for a login wall; the PDF link was never followed."""
    landing = ('<html><head><title>Your invoice</title></head><body><nav><a href="/login">Sign in</a></nav>'
               '<p>Invoice FT 2026/183 is ready.</p><a class="btn" href="/files/FT2026-183.pdf">Download PDF</a>'
               '<footer><form action="/newsletter"><input type="email" name="email"></form></footer></body></html>')

    def site(request):
        if request.url.path == "/i/183":
            return _html(landing)
        if request.url.path == "/files/FT2026-183.pdf":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=PDF)
        return httpx.Response(404)

    result = _fetcher(site).fetch("https://billing.acme-cloud.com/i/183")
    assert result.outcome is LinkOutcome.DOWNLOADED and result.content == PDF


def test_password_form_is_still_a_login_wall_even_with_a_document_link():
    page = ('<form action="/login"><input type="email" name="email"><input type="password" name="pw"></form>'
            '<a class="btn" href="/files/1.pdf">Download invoice PDF</a>')
    result = _fetcher(lambda r: _html(page)).fetch("https://billing.acme-cloud.com/i/1")
    assert result.outcome is LinkOutcome.LOGIN_REQUIRED


def test_weak_login_signals_without_a_document_still_ask_for_sign_in():
    page = '<html><title>Sign in</title><form action="/s"><input type="email" name="email"></form></html>'
    result = _fetcher(lambda r: _html(page)).fetch("https://billing.acme-cloud.com/i/1")
    assert result.outcome is LinkOutcome.LOGIN_REQUIRED


def test_slow_meta_refresh_is_a_session_timeout_not_a_redirect():
    """Defect: a 30-minute session-timeout refresh was followed, preserving 'Session expired' instead of the invoice."""
    invoice = ('<html><head><meta http-equiv="refresh" content="1800;url=/timeout"></head><body>'
               + "<p>Invoice 123, total 10,00 EUR.</p>" * 20 + "</body></html>")

    def site(request):
        if request.url.path == "/page":
            return _html(invoice)
        return _html("<html><body><p>Session expired</p></body></html>")

    result = _fetcher(site).fetch("https://billing.acme-cloud.com/page")
    assert result.outcome is LinkOutcome.RENDERED_PAGE
    assert result.record.final_url == "https://billing.acme-cloud.com/page" and b"Invoice 123" in result.content


def test_slow_drip_download_is_abandoned_at_the_total_deadline():
    """Defect: the timeout was per read, so a server dripping bytes could hold a worker for hours."""
    clock = _Clock()

    def site(request):
        def body():
            yield b"%PDF-1.4\n"
            for _ in range(10_000):
                clock.t += 1.0
                yield b"x" * 16

        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=body())

    fetcher = _fetcher(site, policy=FetchPolicy(total_timeout_s=60.0), monotonic=clock)
    result = fetcher.fetch("https://billing.acme-cloud.com/slow.pdf")
    assert result.outcome is LinkOutcome.UNAVAILABLE and result.reason == "timeout" and result.retryable
    assert clock.t < 100


def test_stored_session_cookies_open_a_login_protected_link_and_stay_on_their_host():
    """§9: 'Login required → stored authorized session'. The fetcher had no way to use one."""
    seen: list[tuple[str, str | None]] = []

    def site(request):
        seen.append((request.headers["host"], request.headers.get("cookie")))
        if request.headers["host"] == "billing.acme-cloud.com":
            if request.headers.get("cookie") != "sid=abc":
                return _html('<form action="/login"><input type="password"></form>')
            return httpx.Response(302, headers={"location": "https://files.acme-cloud.com/1.pdf"})
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=PDF)

    fetcher = _fetcher(site)
    assert fetcher.fetch("https://billing.acme-cloud.com/i/1").outcome is LinkOutcome.LOGIN_REQUIRED
    seen.clear()
    result = fetcher.fetch("https://billing.acme-cloud.com/i/1",
                           session_cookies=[SessionCookie("billing.acme-cloud.com", "sid", "abc")])
    assert result.outcome is LinkOutcome.DOWNLOADED
    assert seen == [("billing.acme-cloud.com", "sid=abc"), ("files.acme-cloud.com", None)]


# --------------------------------------------------------------------------- storage


class _FakeS3Location:
    def __init__(self, location):
        self.location = location

    def get_bucket_versioning(self, Bucket):
        return {"Status": "Enabled"}

    def get_bucket_location(self, Bucket):
        return {"LocationConstraint": self.location}


@pytest.mark.parametrize("region", ["eu-west-2", "eu-central-2", "us-east-1", "il-central-1"])
def test_s3_regions_outside_the_eu_are_refused_even_with_an_eu_prefix(region):
    """Defect: `startswith("eu-")` accepted London (eu-west-2) and Zurich (eu-central-2), outside the EU (§52)."""
    with pytest.raises(StorageConfigError):
        S3ObjectStore("b", region=region, client=_FakeS3Location("eu-west-1"))


@pytest.mark.parametrize("region", ["eu-west-1", "eu-west-3", "eu-central-1", "eu-north-1", "eu-south-1", "eu-south-2"])
def test_s3_regions_in_the_eu_are_accepted(region):
    S3ObjectStore("b", region=region, client=_FakeS3Location(region)).verify_bucket()


def test_s3_bucket_in_us_east_1_is_not_mistaken_for_eu():
    """Defect: S3 reports us-east-1 as LocationConstraint None, which passed the EU check."""
    with pytest.raises(StorageConfigError):
        S3ObjectStore("b", client=_FakeS3Location(None)).verify_bucket()
    S3ObjectStore("b", client=_FakeS3Location("EU")).verify_bucket()  # legacy name of eu-west-1
    with pytest.raises(StorageConfigError):
        S3ObjectStore("b", client=_FakeS3Location("eu-west-2")).verify_bucket()
    S3ObjectStore("b", region="us-east-1", allow_non_eu_region=True, client=_FakeS3Location(None)).verify_bucket()


def test_s3_compatible_eu_provider_regions_can_be_declared():
    store = S3ObjectStore("b", region="fr-par", eu_regions={"fr-par"}, endpoint_url="https://s3.fr-par.example",
                          client=_FakeS3Location("fr-par"))
    store.verify_bucket()


# --------------------------------------------------------------------------- mail connectors: malformed payloads


class _Tokens:
    def access_token(self):
        return "at"

    def invalidate(self):
        pass


def _gmail(handler) -> GmailConnector:
    return GmailConnector(_Tokens(), client=httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: NOW)


def _mail_state(kind=ConnectorKind.GMAIL, **kw) -> ConnectorState:
    return ConnectorState(tenant_id="t1", kind=kind, account="ana@padaria.pt", **kw)


@pytest.mark.parametrize("profile, page", [
    ({"historyId": "5"}, {"messages": [{"threadId": "x"}]}),
    (["not", "an", "object"], {}),
    ({"historyId": "5"}, {"messages": "nope"}),
])
def test_malformed_gmail_payloads_are_typed_failures_not_crashes(profile, page):
    """Defect: KeyError / AttributeError escaped sync(), so no failure was recorded."""
    def site(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json=page)
        return httpx.Response(404)

    outcome = _gmail(site).sync(_mail_state(), lambda item: None)
    assert isinstance(outcome.error, ProviderError) and outcome.state.consecutive_failures == 1


def test_malformed_gmail_message_date_is_a_typed_failure():
    def site(request):
        path = request.url.path
        if path.endswith("/profile"):
            return httpx.Response(200, json={"historyId": "5"})
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        return httpx.Response(200, json={"id": "m1", "raw": "RnJvbTogYQ", "internalDate": "soon"})

    outcome = _gmail(site).sync(_mail_state(), lambda item: None)
    assert isinstance(outcome.error, ProviderError)


def test_malformed_graph_payloads_are_typed_failures_not_crashes():
    def site(request):
        if request.url.path.endswith("/mailFolders/inbox"):
            return httpx.Response(200, json={"displayName": "Inbox"})
        return httpx.Response(404)

    connector = MicrosoftMailConnector(_Tokens(), client=httpx.Client(transport=httpx.MockTransport(site)),
                                       clock=lambda: NOW)
    outcome = connector.sync(_mail_state(ConnectorKind.MICROSOFT), lambda item: None)
    assert isinstance(outcome.error, ProviderError) and outcome.state.consecutive_failures == 1


def test_lost_gmail_cursor_after_a_long_outage_records_the_hole():
    """Defect: a missing cursor with an old last sync re-read only the window and silently skipped the gap."""
    def site(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"historyId": "900"})
        return httpx.Response(200, json={})

    old = NOW - timedelta(days=200)
    state = _mail_state(last_successful_sync=old, coverage_start=old - timedelta(days=90), coverage_end=old)
    outcome = _gmail(site).sync(state, lambda item: None)
    assert outcome.ok and outcome.state.known_gaps
    assert outcome.state.known_gaps[0].start == old


# --------------------------------------------------------------------------- open banking


class _Bank:
    def __init__(self, handler):
        self.handler = handler

    def __call__(self, request):
        path = request.url.path
        if path.endswith("/token/new/"):
            return httpx.Response(200, json={"access": "a", "access_expires": 86400, "refresh": "r",
                                             "refresh_expires": 2592000})
        return self.handler(request)


def _bank_state() -> ConnectorState:
    return ConnectorState(tenant_id="t1", kind=ConnectorKind.OPEN_BANKING, account="PT50 ... 154")


def test_malformed_consent_dates_are_typed_failures_not_crashes():
    """Defect: a bad 'accepted' timestamp raised a raw ValueError out of sync()."""
    def api(request):
        if "/requisitions/" in request.url.path:
            return httpx.Response(200, json={"id": "req", "status": "LN", "agreement": "ag", "accounts": ["acc"]})
        return httpx.Response(200, json={"id": "ag", "accepted": "not-a-date", "access_valid_for_days": 90})

    aggregator = GoCardlessBankAccountData("id", "key", client=httpx.Client(transport=httpx.MockTransport(_Bank(api))),
                                           clock=lambda: NOW)
    outcome = OpenBankingConnector(aggregator, "req", clock=lambda: NOW).sync(_bank_state(), lambda tx: None)
    assert isinstance(outcome.error, ProviderError)


def test_malformed_token_response_is_a_typed_failure():
    def api(request):
        return httpx.Response(200, json={"unexpected": True})

    aggregator = GoCardlessBankAccountData("id", "key", client=httpx.Client(transport=httpx.MockTransport(api)),
                                           clock=lambda: NOW)
    with pytest.raises(ProviderError):
        aggregator.consent("req")


def test_revoked_consent_on_the_requisition_itself_means_reconnect():
    """Defect: access denied while reading the consent was a plain failure, not a reconnect."""
    class Denied:
        def consent(self, requisition_id):
            raise BankAccessDenied("gocardless_http_403")

    outcome = OpenBankingConnector(Denied(), "req", clock=lambda: NOW).sync(_bank_state(), lambda tx: None)
    assert isinstance(outcome.error, ReconnectRequired) and outcome.state.reconnect_required


@pytest.mark.parametrize("amount", [1.1, "1.10", Decimal("NaN"), Decimal("Infinity"), True])
def test_booked_transaction_refuses_anything_but_finite_decimal_money(amount):
    """Defect: the test of this name asserted nothing; a float amount was accepted."""
    with pytest.raises(TypeError):
        BookedTransaction("x", date(2026, 9, 1), amount, "EUR")


# --------------------------------------------------------------------------- portals


class _Portal(SupplierPortalConnector):
    supplier_key = "acme"
    display_name = "Acme"

    def __init__(self, error=None):
        self.error = error

    def authenticate(self, credentials):
        return AuthResult(AuthStatus.AUTHENTICATED, PortalSession("acme", "ana", NOW))

    def list_invoices(self, session, since, until):
        if self.error:
            raise self.error
        return [PortalInvoiceRef("acme", "inv-1", gross_amount=Decimal("10.00"))]

    def retrieve_invoice(self, session, ref):
        return PortalDocument(ref, PDF)

    def retrieve_statement(self, session, period_start, period_end):
        return None


def _portal_state(**kw) -> ConnectorState:
    return ConnectorState(tenant_id="t1", kind=ConnectorKind.SUPPLIER_PORTAL, account="ana", **kw)


def test_successful_portal_sync_does_not_ask_for_a_backfill_forever():
    """Defect: portal syncs stored cursor=None, so needs_backfill() always said 'cursor lost'."""
    out = PortalSync(_Portal(), clock=lambda: NOW).sync(_portal_state(), lambda d: None,
                                                        session=PortalSession("acme", "ana", NOW))
    assert out.outcome.ok and out.outcome.state.cursor
    assert needs_backfill(out.outcome.state, NOW + timedelta(minutes=5)) is None


@pytest.mark.parametrize("error, reconnect", [(TransientError("acme_http_503"), False),
                                              (ReconnectRequired("acme_session_revoked"), True)])
def test_connector_errors_raised_by_an_adapter_are_recorded(error, reconnect):
    """Defect: only PortalError was caught; a ConnectorError from an adapter escaped sync()."""
    out = PortalSync(_Portal(error), clock=lambda: NOW).sync(_portal_state(), lambda d: None,
                                                             session=PortalSession("acme", "ana", NOW))
    assert out.outcome.error is error and out.outcome.state.consecutive_failures == 1
    assert out.outcome.state.reconnect_required is reconnect


@pytest.mark.parametrize("amount", [92.4, "92.40", Decimal("NaN")])
def test_portal_invoice_refs_accept_only_finite_decimal_money(amount):
    with pytest.raises(TypeError):
        PortalInvoiceRef("acme", "inv-1", gross_amount=amount)


def test_non_deterministic_adapter_fallback_reason_is_not_reported_as_broken():
    class AiPortal(_Portal):
        supplier_key = "ai_acme"
        deterministic = False

    registry = PortalRegistry()
    registry.register(AiPortal)
    plan = plan_retrieval(registry=registry, supplier_key="ai_acme", allow_ai_fallback=True)
    assert plan.strategy is RetrievalStrategy.AI_BROWSER and plan.reason == "adapter_not_deterministic"


# --------------------------------------------------------------------------- offline upload


def _upload(data: bytes, upload_id: str, **kw) -> UploadRequest:
    return UploadRequest(tenant_id="t1", client_upload_id=upload_id, sha256=hashlib.sha256(data).hexdigest(),
                         data=data, source_kind=SourceKind.MOBILE_SHARE, **kw)


def test_queued_email_export_is_stored_and_its_attachments_ingested(tmp_path):
    """Defect: an .eml shared while offline was refused forever with 'I can't read this kind of file yet.'"""
    registry = EvidenceRegistry(LocalObjectStore(tmp_path))
    service = UploadService(registry, clock=lambda: NOW)
    eml = (FIXTURES / "vodafone_invoice.eml").read_bytes()
    receipt = service.receive(_upload(eml, "share-eml-0001", filename="fatura.eml"))
    assert receipt.status is UploadStatus.STORED and receipt.delete_local and receipt.owner_message is None
    message = registry.get("t1", receipt.evidence_id)
    assert message.format is EvidenceFormat.EML and message.source_kind is SourceKind.MOBILE_SHARE
    pdfs = [ev for ev in _all_evidence(registry, "t1") if ev.format is EvidenceFormat.PDF]
    assert pdfs, "the invoice attached to the shared email must become evidence"
    again = service.receive(_upload(eml, "share-eml-0001", filename="fatura.eml"))
    assert again == receipt


def test_queued_zip_is_stored_and_expanded(tmp_path):
    registry = EvidenceRegistry(LocalObjectStore(tmp_path))
    service = UploadService(registry, clock=lambda: NOW)
    archive = _zip({"a.pdf": PDF, "b.xlsx": _xlsx()})
    receipt = service.receive(_upload(archive, "share-zip-0001", filename="docs.zip"))
    assert receipt.status is UploadStatus.STORED and receipt.delete_local
    assert registry.get("t1", receipt.evidence_id).format is EvidenceFormat.ZIP
    formats = sorted(ev.format.value for ev in _all_evidence(registry, "t1"))
    assert formats == ["pdf", "xlsx", "zip"]


def _all_evidence(registry: EvidenceRegistry, tenant: str):
    index = registry.index
    return [ev for (t, _), ev in index._by_id.items() if t == tenant]  # test-only peek at the in-memory index


# --------------------------------------------------------------------------- imap


def test_duplicate_fetch_items_for_one_uid_do_not_crash_or_double_deliver():
    """Defect: sorting (uid, date|None, raw) tuples raised TypeError when a UID appeared twice."""
    from backoffice.connectors.imap import _parse_fetch

    data = [(b'1 (UID 5 BODY[] {3}', b"abc"), b' INTERNALDATE "01-Sep-2026 10:00:00 +0000")',
            (b"2 (UID 5 BODY[] {3}", b"abc"), b")"]
    items = _parse_fetch(data)
    assert [uid for uid, _, _ in items] == [5]


# --------------------------------------------------------------------------- coverage, copy, quotas


def test_bank_coverage_waits_for_late_bookings_by_default():
    """Defect: settle defaulted to 0, so a bank synced at 00:05 on the 1st 'covered' a month whose
    last card payments book days later; month close could go green without them."""
    from backoffice.connectors import covers

    just_after = datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc)
    bank = ConnectorState(tenant_id="t1", kind=ConnectorKind.OPEN_BANKING, account="acc",
                          last_successful_sync=just_after, coverage_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
                          coverage_end=just_after, cursor="2026-10-01")
    assert not covers(bank, date(2026, 9, 1), date(2026, 9, 30))
    settled = bank.model_copy(update={"coverage_end": datetime(2026, 10, 6, 6, tzinfo=timezone.utc)})
    assert covers(settled, date(2026, 9, 1), date(2026, 9, 30))
    assert covers(bank, date(2026, 9, 1), date(2026, 9, 30), settle=timedelta(0))  # explicit override
    mail = ConnectorState(tenant_id="t1", kind=ConnectorKind.GMAIL, account="a", last_successful_sync=just_after,
                          coverage_start=datetime(2026, 6, 1, tzinfo=timezone.utc), coverage_end=just_after)
    assert covers(mail, date(2026, 9, 1), date(2026, 9, 30))


@pytest.mark.parametrize("host", ["93.184.216.34", "2a00:1450:4003:80e::2003", "", "123.45"])
def test_owner_copy_never_names_a_supplier_after_an_ip_address(host):
    """Defect: an IP-literal link produced 'needs you to sign in' copy naming '216'."""
    from backoffice.evidence import display_name_for_host

    assert display_name_for_host(host) == "The supplier"


def test_gmail_daily_quota_is_ours_to_wait_out_not_a_reconnect():
    def site(request):
        return httpx.Response(403, json={"error": {"errors": [{"reason": "dailyLimitExceeded"}]}})

    outcome = _gmail(site).sync(_mail_state(), lambda item: None)
    assert isinstance(outcome.error, TransientError) and not outcome.state.reconnect_required


def test_forged_graph_webhook_bodies_are_ignored_not_crashes():
    state = _mail_state(ConnectorKind.MICROSOFT)
    for body in ({"value": ["x", 1, None]}, {"value": "x"}, ["x"], {}):
        new_state, accepted = MicrosoftMailConnector.accept_notifications(state, body, "secret", now=NOW)
        assert accepted == 0 and new_state == state
        assert MicrosoftMailConnector.accept_lifecycle(state, body, "secret") == state


# --------------------------------------------------------------------------- zip directory bombs


def _many_entries(count: int) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
        for i in range(count):
            zf.writestr(f"{i}", b"")
    return out.getvalue()


def test_zip_with_a_huge_directory_is_refused_before_parsing_it():
    """Defect: the whole central directory was parsed first; a 64 MB attachment could hold ~1.4M entries."""
    from backoffice.evidence import SkipReason, ZipLimits, sniff

    data = _many_entries(30_000)
    start = time.perf_counter()
    assert sniff(data).format is EvidenceFormat.ZIP
    result = expand_zip(data, ZipLimits(max_entries=10_000))
    assert time.perf_counter() - start < 1.0
    assert result.members == () and [s.reason for s in result.skipped] == [SkipReason.TOO_MANY]


def test_zip_that_lies_about_its_entry_count_is_still_bounded_by_directory_size():
    from backoffice.evidence import SkipReason, ZipLimits

    data = bytearray(_many_entries(30_000))
    eocd = data.rfind(b"PK\x05\x06")
    data[eocd + 8 : eocd + 12] = (5).to_bytes(2, "little") * 2  # claim 5 entries
    result = expand_zip(bytes(data), ZipLimits(max_entries=10_000, max_directory_bytes=1_000_000))
    assert [s.reason for s in result.skipped] == [SkipReason.TOO_MANY]


def test_ordinary_zip_directory_reading_is_unchanged():
    from backoffice.evidence.sniff import zip_directory_shape

    data = _zip({"a.pdf": PDF, "b.pdf": PDF + b"b"})
    entries, size = zip_directory_shape(data)
    assert entries == 2 and 0 < size < 200
    assert zip_directory_shape(b"not a zip") is None


def test_zip64_directory_shape_is_read_and_unreadable_zip64_counts_as_huge():
    from backoffice.evidence.sniff import zip_directory_shape

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        with zf.open("a.pdf", "w", force_zip64=True) as fh:
            fh.write(PDF)
    data = out.getvalue()
    # Python writes a ZIP64 end record only when needed; build one by hand from the classic record.
    end = data.rfind(b"PK\x05\x06")
    entries, size = int.from_bytes(data[end + 10 : end + 12], "little"), int.from_bytes(data[end + 12 : end + 16], "little")
    cd_offset = data[end + 16 : end + 20]
    record = (b"PK\x06\x06" + (44).to_bytes(8, "little") + b"\x2d\x00\x2d\x00" + b"\x00" * 8
              + entries.to_bytes(8, "little") * 2 + size.to_bytes(8, "little")
              + int.from_bytes(cd_offset, "little").to_bytes(8, "little"))
    locator = b"PK\x06\x07" + b"\x00" * 4 + end.to_bytes(8, "little") + (1).to_bytes(4, "little")
    classic = data[end : end + 10] + b"\xff\xff" + b"\xff\xff\xff\xff" + b"\xff\xff\xff\xff" + data[end + 20 :]
    zip64 = data[:end] + record + locator + classic
    assert zip_directory_shape(zip64) == (1, size)
    broken = data[:end] + classic  # markers but no ZIP64 record
    assert zip_directory_shape(broken) == (0xFFFF, 0xFFFFFFFF)


def test_untrusted_capture_metadata_is_bounded(tmp_path):
    """Defect: device-sent capture JSON could nest until RecursionError (a raw 500) or be megabytes."""
    from backoffice.evidence import RejectReason, check_json_metadata

    deep: dict = {}
    node = deep
    for _ in range(5_000):
        node["x"] = {}
        node = node["x"]
    with pytest.raises(TypeError):
        check_json_metadata(deep)
    service = UploadService(EvidenceRegistry(LocalObjectStore(tmp_path)), clock=lambda: NOW)
    jpeg = b"\xff\xd8\xff\xe0" + b"photo" * 50
    for capture in (deep, {"notes": "x" * 100_000}):
        receipt = service.receive(_upload(jpeg, "cap-deep-0001", capture=capture))
        assert receipt.status is UploadStatus.REJECTED and receipt.reason is RejectReason.BAD_REQUEST
    ok = service.receive(_upload(jpeg, "cap-fine-0001", capture={"pages": 1, "glare": False}))
    assert ok.status is UploadStatus.STORED


def test_bank_history_limit_after_a_long_outage_is_recorded_as_a_gap():
    """Defect: after an outage longer than the consent's history limit, the resync silently started
    later than the cursor, yet coverage advanced: months with missing transactions could go green."""
    from backoffice.connectors import covers

    class Aggregator:
        def __init__(self):
            self.windows = []

        def consent(self, requisition_id):
            return BankConsent("req", ConsentStatus.ACTIVE, ("acc",), expires_at=NOW + timedelta(days=60),
                               max_historical_days=90)

        def booked_transactions(self, account_id, date_from, date_to):
            self.windows.append((date_from, date_to))
            return []

    aggregator = Aggregator()
    last = datetime(2026, 5, 1, 6, tzinfo=timezone.utc)
    state = ConnectorState(tenant_id="t1", kind=ConnectorKind.OPEN_BANKING, account="acc", cursor="2026-05-01",
                           last_successful_sync=last, coverage_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                           coverage_end=last)
    outcome = OpenBankingConnector(aggregator, "req", clock=lambda: NOW).sync(state, lambda tx: None)
    assert outcome.ok and aggregator.windows == [(date(2026, 6, 27), date(2026, 9, 25))]
    (gap,) = outcome.state.known_gaps
    assert gap.start == datetime(2026, 4, 26, tzinfo=timezone.utc) and gap.end == datetime(2026, 6, 27, tzinfo=timezone.utc)
    assert not covers(outcome.state, date(2026, 6, 1), date(2026, 6, 30))
    assert covers(outcome.state, date(2026, 7, 1), date(2026, 7, 31))


def test_bank_backfill_closes_what_the_consent_still_reaches_and_keeps_the_rest():
    from backoffice.connectors import TimeRange

    class Aggregator:
        def __init__(self, max_days):
            self.max_days, self.windows = max_days, []

        def consent(self, requisition_id):
            return BankConsent("req", ConsentStatus.ACTIVE, ("acc",), expires_at=NOW + timedelta(days=60),
                               max_historical_days=self.max_days)

        def booked_transactions(self, account_id, date_from, date_to):
            self.windows.append((date_from, date_to))
            return [BookedTransaction("T1", date_from, Decimal("-9.99"), "EUR")]

    gap = TimeRange(start=datetime(2026, 4, 26, tzinfo=timezone.utc), end=datetime(2026, 6, 27, tzinfo=timezone.utc))
    state = ConnectorState(tenant_id="t1", kind=ConnectorKind.OPEN_BANKING, account="acc", cursor="2026-09-25",
                           last_successful_sync=NOW, coverage_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                           coverage_end=NOW, known_gaps=(gap,))
    unlimited = Aggregator(None)
    got: list = []
    out = OpenBankingConnector(unlimited, "req", clock=lambda: NOW).backfill(state, gap, got.append)
    assert out.ok and out.state.known_gaps == () and len(got) == 1
    assert unlimited.windows == [(date(2026, 4, 26), date(2026, 6, 26))]

    partial = Aggregator(120)  # reaches back to 2026-05-28 only
    out = OpenBankingConnector(partial, "req", clock=lambda: NOW).backfill(state, gap, lambda tx: None)
    assert partial.windows == [(date(2026, 5, 28), date(2026, 6, 26))]
    assert out.state.known_gaps == (TimeRange(start=gap.start, end=datetime(2026, 5, 28, tzinfo=timezone.utc)),)

    unreachable = Aggregator(90)
    out = OpenBankingConnector(unreachable, "req", clock=lambda: NOW).backfill(state, gap, lambda tx: None)
    assert unreachable.windows == [] and out.state.known_gaps == (gap,) and out.ok


def test_downloaded_zip_bundle_members_become_evidence_with_provenance(tmp_path):
    """Defect: a link that downloads a ZIP (PDF + XML invoice bundle) stored only the ZIP."""
    from backoffice.evidence import register_fetch

    ubl = (b'<?xml version="1.0"?><Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2">'
           b"<ID>FT 1</ID></Invoice>")
    bundle = _zip({"FT1.pdf": PDF, "FT1.xml": ubl})

    def site(request):
        return httpx.Response(200, headers={"content-type": "application/zip"}, content=bundle)

    result = _fetcher(site).fetch("https://billing.acme-cloud.com/bundle/1")
    assert result.outcome is LinkOutcome.DOWNLOADED and result.format is EvidenceFormat.ZIP
    registry = EvidenceRegistry(LocalObjectStore(tmp_path))
    regs = register_fetch(result, registry, tenant_id="t1")
    assert [r.evidence.format for r in regs] == [EvidenceFormat.ZIP, EvidenceFormat.PDF, EvidenceFormat.UBL]
    archive_id = regs[0].evidence.id
    assert regs[0].evidence.original_url == "https://billing.acme-cloud.com/bundle/1"
    assert all(r.sighting.context["archive_evidence_id"] == archive_id for r in regs[1:])
    assert all(r.evidence.original_url == "https://billing.acme-cloud.com/bundle/1" for r in regs)


def test_downloaded_email_file_is_ingested_as_an_email(tmp_path):
    from backoffice.evidence import register_fetch

    eml = (FIXTURES / "vodafone_invoice.eml").read_bytes()

    def site(request):
        return httpx.Response(200, headers={"content-type": "message/rfc822"}, content=eml)

    result = _fetcher(site).fetch("https://billing.acme-cloud.com/mail/1.eml")
    registry = EvidenceRegistry(LocalObjectStore(tmp_path))
    regs = register_fetch(result, registry, tenant_id="t1")
    assert regs[0].evidence.format is EvidenceFormat.EML
    assert EvidenceFormat.PDF in [r.evidence.format for r in regs[1:]]


@pytest.mark.parametrize("location", ["//[bad", "https://[::1"])
def test_malformed_redirect_target_is_an_outcome_not_an_exception(location):
    """Defect: urljoin raised ValueError on a hostile Location header, escaping fetch()."""
    result = _fetcher(lambda r: httpx.Response(302, headers={"location": location})).fetch(
        "https://billing.acme-cloud.com/x")
    assert result.outcome in (LinkOutcome.UNAVAILABLE, LinkOutcome.BLOCKED_UNSAFE)
    assert result.owner_message in (None, "This link didn't look safe, so I didn't open it.")


def test_malformed_refresh_and_document_links_are_ignored():
    page = ('<html><head><meta http-equiv="refresh" content="0; url=//[x"></head><body>'
            '<a class="btn" href="//[y">Download invoice PDF</a>' + "<p>Invoice 9 total 5,00 EUR.</p>" * 20
            + "</body></html>")
    result = _fetcher(lambda r: _html(page)).fetch("https://billing.acme-cloud.com/x")
    assert result.outcome is LinkOutcome.RENDERED_PAGE


# --------------------------------------------------------------------------- email: poison messages


def _forward(inner: bytes) -> bytes:
    return (b'From: a@b.pt\r\nSubject: fwd\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="B"\r\n\r\n'
            b"--B\r\nContent-Type: text/plain\r\n\r\nsee attached\r\n"
            b"--B\r\nContent-Type: message/rfc822\r\n\r\n" + inner + b"\r\n--B--\r\n")


POISON = {
    "nul_charset": b'From: a@b.pt\r\nSubject: x\r\nContent-Type: text/plain; charset="utf\x00-8"\r\n\r\nhello\r\n',
    "nested_without_boundary": _forward(b"From: c@d.pt\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; "
                                        b'bound.ry="X"\r\n\r\n--X\r\nContent-Type: text/plain\r\n\r\nol\xc3\xa1\r\n--X--'),
    "nested_vertical_tab_header": _forward(b"From: c@d.pt\r\nMessage-ID: <o\x0big@d.pt>\r\nSubject: y\r\n\r\nbody"),
}


@pytest.mark.parametrize("name", sorted(POISON))
def test_malformed_emails_never_crash_ingestion(name, tmp_path):
    """Defect (found by fuzzing): a bad charset name or an unserialisable forwarded message raised
    ValueError / UnicodeEncodeError / HeaderWriteError. In a mailbox sync the sink raises, the cursor
    never moves, and that one message blocks the mailbox forever."""
    raw = POISON[name]
    parsed = parse_eml(raw)
    assert parsed.subject in ("x", "fwd")
    result = EmailIngestor(EvidenceRegistry(LocalObjectStore(tmp_path))).ingest(raw, tenant_id="t1")
    assert result.message.evidence.format is EvidenceFormat.EMAIL
    if name == "nul_charset":
        assert "hello" in parsed.text_body
    else:  # the forwarded message is either read or reported, never silently dropped
        assert result.attached_emails or any(s.reason == "unreadable_email" for s in result.skipped)


def test_seeded_fuzz_of_fixture_emails_raises_only_typed_errors(tmp_path):
    import random

    from backoffice.evidence import EmailParseError

    fixtures = [p.read_bytes() for p in sorted(FIXTURES.glob("*.eml"))]
    tokens = [b"\r\n", b"\n", b"=?utf-8?q?", b"?=", b"; filename*=utf-8''%ff%fe", b"\x00", b"\xff", b"\x0b",
              b"Content-Type: message/rfc822\r\n", b"Content-Transfer-Encoding: base64\r\n", b"--", b"bound.ry",
              b'Content-Type: text/html; charset="x\x00"\r\n', b"Date: Mon, 99 Foo 2026 99:99:99 +9999\r\n"]
    rng = random.Random(20260928)
    ingestor = EmailIngestor(EvidenceRegistry(LocalObjectStore(tmp_path)))
    for _ in range(400):
        data = bytearray(rng.choice(fixtures))
        for _ in range(rng.randint(1, 8)):
            pos = rng.randrange(len(data) + 1)
            op = rng.random()
            if op < 0.5:
                data[pos:pos] = rng.choice(tokens)
            elif op < 0.75:
                del data[pos : pos + rng.randint(1, 40)]
            else:
                data[pos : pos + 1] = bytes([rng.randrange(256)])
        try:
            ingestor.ingest(bytes(data), tenant_id="t1")
        except EmailParseError:
            pass


def _future_version_zip() -> bytes:
    data = bytearray(_zip({"a.pdf": PDF}))
    central = data.find(b"PK\x01\x02")
    data[central + 6 : central + 8] = (92).to_bytes(2, "little")  # "version needed to extract" 9.2
    return bytes(data)


def _corrupt_lzma_zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_LZMA) as zf:
        zf.writestr("a.txt", b"abc" * 2000)
    data = bytearray(out.getvalue())
    start = data.find(b"a.txt") + len(b"a.txt") + 12  # inside the compressed stream
    data[start : start + 8] = b"\xff" * 8
    return bytes(data)


@pytest.mark.parametrize("make", [_future_version_zip, _corrupt_lzma_zip])
def test_hostile_zip_attachments_are_reported_not_raised(make, tmp_path):
    """Defect (found by fuzzing): zipfile's NotImplementedError / LZMAError escaped sniff() and
    expand_zip(); sniff runs on every attachment, so one crafted ZIP crashed the whole email."""
    from email.message import EmailMessage

    from backoffice.evidence import sniff

    data = make()
    assert sniff(data).format in (EvidenceFormat.ZIP, None)
    expansion = expand_zip(data)
    assert expansion.members == () and expansion.skipped and not expansion.complete
    msg = EmailMessage()
    msg["From"], msg["Subject"] = "a@b.pt", "zip"
    msg.set_content("see attached")
    msg.add_attachment(data, maintype="application", subtype="zip", filename="docs.zip")
    result = EmailIngestor(EvidenceRegistry(LocalObjectStore(tmp_path))).ingest(bytes(msg), tenant_id="t1")
    assert result.skipped


def test_browser_rendering_stays_within_the_total_deadline():
    from backoffice.evidence import RenderedPage

    script_page = "<html><body><script>app()</script><noscript>Enable JavaScript</noscript></body></html>"

    class Browser:
        def __init__(self):
            self.timeouts = []

        def render(self, url, *, timeout_s):
            self.timeouts.append(timeout_s)
            return RenderedPage(url, url, "<html><body>" + "<p>Invoice 1, 10,00 EUR.</p>" * 20 + "</body></html>")

    clock = _Clock()

    def site(request):
        clock.t += 25.0  # the plain fetch was slow
        return _html(script_page)

    browser = Browser()
    policy = FetchPolicy(timeout_s=20.0, total_timeout_s=60.0)
    result = _fetcher(site, browser=browser, policy=policy, monotonic=clock).fetch("https://billing.acme-cloud.com/a")
    assert result.outcome is LinkOutcome.RENDERED_PAGE and result.via_browser
    assert browser.timeouts == [35.0]  # 60 s total - 25 s spent, not 2 x 20 s

    clock.t, browser.timeouts = 0.0, []
    slow = FetchPolicy(timeout_s=20.0, total_timeout_s=25.0)  # nothing left once the page is read
    late = _fetcher(site, browser=browser, policy=slow, monotonic=clock).fetch("https://billing.acme-cloud.com/a")
    assert browser.timeouts == [] and late.outcome is LinkOutcome.RENDERED_PAGE and not late.via_browser
