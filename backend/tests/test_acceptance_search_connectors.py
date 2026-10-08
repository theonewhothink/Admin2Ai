"""Searching every connected place before asking a supplier, and the supplier's reply (QA K2-K4, K6, K8, R3).

* Cloud storage (K3): Google Drive (Drive API v3 ``files.list`` with ``q``, ``alt=media``, Google Docs exported to
  PDF) and OneDrive / SharePoint (Graph ``/drive/root/search``, ``/content`` redirect fetched without the token),
  each able to search for a missing document and to watch a chosen folder, files kept with their provenance.
* Missing-document search wired (K2, K6): mailboxes (Gmail ``q``, Graph ``$search`` over every folder, IMAP
  ``SEARCH``), cloud storage, accounting software, portal and the supplier's usual way of sending, in the spec's
  order, before any supplier request; each search recorded; read before the event is recorded on the server.
* Accounting software (K4, R3): InvoiceXpress (the customer's API key), Moloni (OAuth, developer app), TOConline
  (the company's API data) behind one interface: documents, PDFs, the month's export, sync health.
* Supplier replies (K8): matched by thread, the reply's invoice closes the request once it verifies and matches;
  reminders through the send path; then one plain owner line; never a reply that did not come.

Fake transports answer with the shapes each provider's public API documentation gives.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from backoffice.connectors.accounting import (
    DocumentLookup,
    InvoiceXpressConnector,
    MoloniConnector,
    MoloniRefresher,
    TOConlineConnector,
    TOConlineCredentials,
    TOConlineTokens,
)
from backoffice.connectors.base import (
    ConnectorKind,
    ConnectorState,
    ReconnectRequired,
    TransientError,
)
from backoffice.connectors.cloud_storage import (
    DRIVE_READONLY_SCOPE,
    GRAPH_FILES_SCOPE,
    CloudStorageConfig,
    FileQuery,
    GoogleDriveConnector,
    OneDriveConnector,
)
from backoffice.connectors.gmail import GmailConnector
from backoffice.connectors.imap import IMAPAuth, IMAPConfig, IMAPConnector
from backoffice.connectors.mail_search import MailQuery, MailTerm, TermKind
from backoffice.connectors.microsoft import MicrosoftMailConnector
from backoffice.demo import evidence as E
from backoffice.mailer import SimulatedOutbox
from backoffice.missing import (
    SEARCH_ORDER,
    AccountingSearch,
    CloudStorageSearch,
    FoundFile,
    MailboxSearch,
    RecurringMailSearch,
    SearchPattern,
    SearchRequest,
    SearchSource,
    run_searches,
)
from backoffice.orchestrator import TZ
from backoffice.service import BackOfficeService

NIF = "516123459"  # Padaria Lda (the demo's Hazel Tree number: the demo's EDP invoice is made out to it)
PDF = b"%PDF-1.7\n% Fatura EDP setembro\n%%EOF\n"
DOCX = b"PK\x03\x04 a Word file"


class Tokens:
    """A token provider that counts invalidations (a 401 asks for a new token once)."""

    def __init__(self) -> None:
        self.n = 0
        self.invalidated = 0

    def access_token(self) -> str:
        self.n += 1
        return f"at-{self.n}"

    def invalidate(self) -> None:
        self.invalidated += 1


def client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def plain(*texts: str) -> None:
    """Owner-facing words: no internal codes, ids or provider errors."""
    for text in texts:
        assert text and not any(bad in text for bad in ("_", "Error", "http", "None", "{", "tx_", "ev_")), text


def edp_email(*, subject: str = "Fatura EDP setembro", message_id: str = "<fatura-0918@edp.pt>",
              in_reply_to: str | None = None, attachment: bytes | None = E.EDP_INVOICE, sender: str = "faturas@edp.pt",
              body: str = "Segue em anexo a sua fatura de 64,10 EUR.") -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "ana@padaria.pt", subject
    m["Message-ID"] = message_id
    m["Date"] = "Fri, 18 Sep 2026 10:00:00 +0100"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    m.set_content(body)
    if attachment is not None:
        m.add_attachment(attachment, maintype="text", subtype="plain", filename="fatura.txt")
    return m.as_bytes()


# =========================================================================== mailbox search (K2)


def _query() -> MailQuery:
    return MailQuery(date(2026, 8, 5), date(2026, 10, 4),
                     any_of=(MailTerm(TermKind.FROM, "edp.pt"), MailTerm(TermKind.TEXT, "FT EDP2026/558120"),
                             MailTerm(TermKind.TEXT, "64,10")), limit=5)


def test_gmail_searches_every_label_with_one_query_and_fetches_raw_messages() -> None:
    raw = edp_email()
    seen: list[httpx.Request] = []

    def gmail(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path.replace("/gmail/v1/users/me", "")
        if path == "/messages":  # users.messages.list
            return httpx.Response(200, json={"messages": [{"id": "18f1", "threadId": "18f1"},
                                                          {"id": "18f2", "threadId": "18f2"}],
                                             "resultSizeEstimate": 2})
        if path == "/messages/18f1":  # users.messages.get?format=raw
            return httpx.Response(200, json={"id": "18f1", "threadId": "18f1", "labelIds": ["CATEGORY_UPDATES"],
                                             "internalDate": "1758186000000", "historyId": "4801",
                                             "raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")})
        if path == "/messages/18f2":
            return httpx.Response(200, json={"id": "18f2", "threadId": "18f2", "labelIds": ["SPAM"],
                                             "internalDate": "1758186000000", "raw": "eA"})
        return httpx.Response(404)

    found = GmailConnector(Tokens(), client=client(gmail)).search_messages(_query())
    assert [m.provider_id for m in found] == ["18f1"] and found[0].raw == raw  # spam is never searched
    q = seen[0].url.params["q"]
    assert q.startswith("after:2026/08/05 before:2026/10/05 ")
    assert '(from:edp.pt OR "FT EDP2026/558120" OR "64,10")' in q and "-in:drafts" in q and "-in:chats" in q
    assert "in:inbox" not in q and "label:" not in q  # archived mail too (§8: no folder needed)
    assert seen[0].url.params["includeSpamTrash"] == "false" and seen[1].url.params["format"] == "raw"


def test_graph_searches_every_folder_with_kql_and_fetches_mime() -> None:
    raw = edp_email()
    seen: list[httpx.Request] = []

    def graph(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1.0/me/messages":
            return httpx.Response(200, json={"@odata.context": "https://graph.microsoft.com/v1.0/$metadata#messages",
                                             "value": [{"@odata.etag": 'W/"1"', "id": "AAMkAD1", "isDraft": False,
                                                        "receivedDateTime": "2026-09-18T09:00:00Z",
                                                        "conversationId": "AAQkAD1",
                                                        "internetMessageId": "<fatura-0918@edp.pt>"}]})
        if request.url.path == "/v1.0/me/messages/AAMkAD1/$value":
            return httpx.Response(200, content=raw, headers={"content-type": "text/plain"})
        return httpx.Response(404)

    found = MicrosoftMailConnector(Tokens(), client=client(graph)).search_messages(_query())
    assert [m.raw for m in found] == [raw] and found[0].thread_id == "AAQkAD1"
    search = seen[0].url.params["$search"]
    assert search.startswith('"received>=08/05/2026 AND received<=10/04/2026 AND (from:edp.pt OR ')
    assert '\\"64,10\\"' in search and "$filter" not in seen[0].url.params  # $search cannot take a $filter
    assert "mailFolders" not in seen[0].url.path  # every folder, the archive included


def test_imap_search_is_read_only_and_uses_search_criteria() -> None:
    from test_ingest_imap import FakeIMAP

    fake = FakeIMAP({'"INBOX"': {7: edp_email(), 9: b"Subject: other\r\n\r\nx"}})
    connector = IMAPConnector(IMAPConfig(host="imap.padaria.pt"), IMAPAuth("ana@padaria.pt", password="pw"),
                              client_factory=lambda cfg: fake)
    found = connector.search_messages(_query())
    assert {m.provider_id for m in found} == {"INBOX:7", "INBOX:9"} and fake.logged_out
    search = next(c for c in fake.commands if c[:2] == ("UID", "SEARCH"))
    assert search[2:] == ("SINCE", "5-Aug-2026", "BEFORE", "5-Oct-2026", "OR", "FROM", '"edp.pt"', "OR", "TEXT",
                          '"FT EDP2026/558120"', "TEXT", '"64,10"')
    assert ("SELECT", '"INBOX"', True) in fake.commands  # EXAMINE: never marks anything read


# =========================================================================== cloud storage (K3)


class FakeDrive:
    """Drive API v3: files.list (q), files.get (metadata, alt=media), files.export."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.files = [
            {"id": "1EdpPdf", "name": "EDP setembro.pdf", "mimeType": "application/pdf",
             "modifiedTime": "2026-09-20T10:15:00.000Z", "size": str(len(PDF)), "parents": ["0Faturas"],
             "webViewLink": "https://drive.google.com/file/d/1EdpPdf/view?usp=drivesdk"},
            {"id": "1EdpDoc", "name": "Notas EDP", "mimeType": "application/vnd.google-apps.document",
             "modifiedTime": "2026-09-21T08:00:00.000Z", "parents": ["0Faturas"],
             "webViewLink": "https://docs.google.com/document/d/1EdpDoc/edit"},
            {"id": "1Folder", "name": "2026", "mimeType": "application/vnd.google-apps.folder",
             "modifiedTime": "2026-09-01T08:00:00.000Z", "parents": ["0Faturas"]},
        ]
        self.status: tuple[int, dict[str, Any]] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"].startswith("Bearer at-")
        if self.status is not None:
            return httpx.Response(self.status[0], json=self.status[1])
        path = request.url.path.removeprefix("/drive/v3")
        if path == "/files":
            return httpx.Response(200, json={"kind": "drive#fileList", "incompleteSearch": False,
                                             "files": self.files})
        if path == "/files/0Faturas":
            return httpx.Response(200, json={"id": "0Faturas", "name": "Faturas", "parents": ["0Root"]})
        if path == "/files/0Root":
            return httpx.Response(200, json={"id": "0Root", "name": "My Drive"})
        if path == "/files/1EdpPdf" and request.url.params.get("alt") == "media":
            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
        if path == "/files/1EdpDoc/export":
            assert request.url.params["mimeType"] == "application/pdf"
            return httpx.Response(200, content=b"%PDF-1.4 exported", headers={"content-type": "application/pdf"})
        return httpx.Response(404, json={"error": {"code": 404, "message": "File not found"}})


def test_google_drive_searches_downloads_exports_and_keeps_provenance() -> None:
    drive = FakeDrive()
    connector = GoogleDriveConnector(Tokens(), client=client(drive))
    since = datetime(2026, 8, 5, tzinfo=timezone.utc)
    files = connector.search(FileQuery(("FT EDP2026/558120", "64,10", "EDP"), since=since, limit=5))
    assert [f.file_id for f in files] == ["1EdpPdf", "1EdpDoc"]  # a folder is never a document
    q = drive.requests[0].url.params["q"]
    assert "trashed = false" in q and "modifiedTime > '2026-08-05T00:00:00Z'" in q
    assert "fullText contains 'FT EDP2026/558120'" in q and "name contains '64,10'" in q
    assert "orderBy" not in drive.requests[0].url.params  # Drive refuses sorting with full-text terms
    assert drive.requests[0].url.params["supportsAllDrives"] == "true"
    pdf = connector.download(files[0])
    assert pdf is not None and pdf.data == PDF and pdf.content_type == "application/pdf"
    assert pdf.provenance() == {"source": "drive", "provider": "google_drive", "fileId": "1EdpPdf",
                                "name": "EDP setembro.pdf", "path": "/My Drive/Faturas/EDP setembro.pdf",
                                "modifiedAt": "2026-09-20T10:15:00+00:00",
                                "webUrl": "https://drive.google.com/file/d/1EdpPdf/view?usp=drivesdk"}
    doc = connector.download(files[1])
    assert doc is not None and doc.filename == "Notas EDP.pdf" and doc.data.startswith(b"%PDF")
    # Throttling waits; a grant without Drive access asks the owner to sign in again.
    drive.status = (403, {"error": {"code": 403, "errors": [{"reason": "userRateLimitExceeded"}]}})
    with pytest.raises(TransientError):
        connector.search(FileQuery(("EDP",)))
    drive.status = (403, {"error": {"code": 403, "errors": [{"reason": "insufficientPermissions"}]}})
    with pytest.raises(ReconnectRequired):
        connector.search(FileQuery(("EDP",)))


class FakeOneDrive:
    """Graph: /me/drive/root/search(q='...') with paging, /items/{id}/content answering 302."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if request.url.host == "padaria-my.sharepoint.com":
            assert "authorization" not in request.headers  # pre-authenticated: the token never leaves Graph
            return httpx.Response(200, content=PDF)
        assert request.headers["authorization"].startswith("Bearer at-")
        if "/root/search(q=" in url and "skiptoken" not in url:
            return httpx.Response(200, json={
                "value": [{"id": "01EDPPDF", "name": "Fatura EDP.pdf", "size": len(PDF),
                           "file": {"mimeType": "application/pdf"}, "lastModifiedDateTime": "2026-09-20T10:15:00Z",
                           "parentReference": {"driveId": "b!padaria", "driveType": "business", "id": "01FATURAS",
                                               "path": "/drive/root:/Faturas/2026"},
                           "webUrl": "https://padaria-my.sharepoint.com/personal/ana/Documents/Fatura%20EDP.pdf"},
                          {"id": "01FOLDER", "name": "EDP", "folder": {"childCount": 3},
                           "lastModifiedDateTime": "2026-09-20T10:15:00Z"}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/drive/root/search(q='EDP')?$skiptoken=X1"})
        if "skiptoken" in url:
            return httpx.Response(200, json={"value": [{
                "id": "01EDPDOC", "name": "Contrato EDP.docx", "size": 9000,
                "file": {"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
                "lastModifiedDateTime": "2026-09-21T10:15:00Z",
                "parentReference": {"driveId": "b!padaria", "path": "/drive/root:/Contratos"}}]})
        if request.url.path.endswith("/content"):
            return httpx.Response(302, headers={
                "location": "https://padaria-my.sharepoint.com/_layouts/15/download.aspx?UniqueId=1&tempauth=x"})
        return httpx.Response(404)


def test_onedrive_searches_and_downloads_through_the_preauthenticated_address() -> None:
    graph = FakeOneDrive()
    connector = OneDriveConnector(Tokens(), client=client(graph))
    files = connector.search(FileQuery(("EDP",), since=datetime(2026, 8, 5, tzinfo=timezone.utc)))
    assert [f.file_id for f in files] == ["01EDPPDF", "01EDPDOC"]  # paged; the folder is not a file
    assert files[0].path == "/Faturas/2026/Fatura EDP.pdf"
    got = connector.download(files[0])
    assert got is not None and got.data == PDF and got.provenance()["source"] == "onedrive"
    assert got.provenance()["fileId"] == "01EDPPDF" and got.provenance()["modifiedAt"] == "2026-09-20T10:15:00+00:00"
    content = next(r for r in graph.requests if r.url.path.endswith("/content"))
    assert content.url.path == "/v1.0/drives/b!padaria/items/01EDPPDF/content"
    word = connector.download(files[1])
    assert word is not None and word.filename == "Contrato EDP.pdf"  # an Office file comes as PDF
    assert [r.url.params.get("format") for r in graph.requests if r.url.path.endswith("/content")][-1] == "pdf"
    with pytest.raises(ValueError):
        OneDriveConnector(Tokens(), config=CloudStorageConfig(drive="../users/boss/drive"))


def test_a_watched_folder_reads_new_files_once_and_keeps_its_sync_state() -> None:
    drive = FakeDrive()
    connector = GoogleDriveConnector(Tokens(), client=client(drive), config=CloudStorageConfig(
        folder="https://drive.google.com/drive/folders/0Faturas?usp=sharing"))
    state = ConnectorState(tenant_id="t1", connector_id="files-1", kind=ConnectorKind.CLOUD_STORAGE,
                           account="ana@padaria.pt", display_name="Google Drive")
    now = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    got: list[Any] = []
    first = connector.watch(state, got.append, now=now)
    assert first.ok and first.full_sync and [d.file.file_id for d in got] == ["1EdpPdf", "1EdpDoc"]
    listing = drive.requests[0].url.params
    assert listing["q"].startswith("'0Faturas' in parents and trashed = false and modifiedTime > '2026-07-04")
    assert listing["orderBy"] == "modifiedTime"
    assert first.state.coverage_start == now - timedelta(days=90) and first.state.last_successful_sync == now
    assert json.loads(first.state.cursor)["modifiedAfter"].startswith("2026-09-21T08:00:00")
    drive.requests.clear()
    second = connector.watch(first.state, got.append, now=now + timedelta(hours=1))
    assert "modifiedTime > '2026-09-21T08:00:00Z'" in drive.requests[0].url.params["q"] and second.ok
    drive.status = (401, {"error": {"code": 401}})
    failed = connector.watch(second.state, got.append, now=now + timedelta(hours=2))
    assert not failed.ok and failed.state.reconnect_required and failed.state.consecutive_failures == 1
    assert failed.state.health(now + timedelta(hours=2)).title == "Google Drive needs reconnecting."


# =========================================================================== accounting software (K4, R3)


def _ixp_invoice(i: int, kind: str, total: str, day: str, status: str = "final") -> dict[str, Any]:
    return {"id": 541790 + i, "status": status, "archived": False, "type": kind, "sequence_number": f"A/{i}",
            "inverted_sequence_number": f"{i}/A", "atcud": f"ABCD1234-{i}", "date": day, "due_date": day,
            "reference": None, "observations": None, "retention": None,
            "permalink": f"https://web.invoicexpress.com/documents/{541790 + i}", "sum": total,
            "discount": 0, "before_taxes": str(Decimal(total) / Decimal("1.23")), "taxes": "0.00", "total": total,
            "currency": "Euro", "client": {"name": "Café Central", "fiscal_id": "508025338", "country": "Portugal"}}


class FakeInvoiceXpress:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.pdf_tries = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "invoicexpress-pdfs.s3.amazonaws.com":
            assert "api_key" not in str(request.url)
            return httpx.Response(200, content=PDF)
        if request.url.params.get("api_key") != "ixp-key-123":
            return httpx.Response(401, json={"errors": [{"error": "Authentication Error"}]})
        if request.url.path == "/invoices.json":
            page = int(request.url.params["page"])
            items = [_ixp_invoice(1, "Invoice", "123.00", "18/09/2026"),
                     _ixp_invoice(2, "CreditNote", "24.60", "20/09/2026"),
                     _ixp_invoice(3, "Invoice", "50.00", "21/09/2026", status="draft")] if page == 1 else \
                [_ixp_invoice(4, "InvoiceReceipt", "64.10", "25/09/2026")]
            return httpx.Response(200, json={"invoices": items, "pagination": {
                "total_entries": 4, "per_page": 3, "current_page": page, "total_pages": 2}})
        if request.url.path.startswith("/api/pdf/"):
            self.pdf_tries += 1
            if self.pdf_tries == 1:
                return httpx.Response(202)  # still being made
            return httpx.Response(200, json={"output": {
                "pdfUrl": "https://invoicexpress-pdfs.s3.amazonaws.com/541791/fatura.pdf?X-Amz-Signature=abc"}})
        return httpx.Response(404)


def test_invoicexpress_lists_documents_with_the_customers_key_and_downloads_pdfs() -> None:
    fake = FakeInvoiceXpress()
    ixp = InvoiceXpressConnector("padaria", "ixp-key-123", client=client(fake))
    docs = ixp.list_documents(date(2026, 9, 1), date(2026, 9, 30), "sales")
    assert [(d.doc_type, d.number, d.gross, d.final) for d in docs] == [
        ("FT", "1/A", Decimal("123.00"), True), ("NC", "2/A", Decimal("24.60"), True),
        ("FT", "3/A", Decimal("50.00"), False), ("FR", "4/A", Decimal("64.10"), True)]
    first = fake.requests[0]
    assert first.url.host == "padaria.app.invoicexpress.com" and first.url.path == "/invoices.json"
    params = first.url.params
    assert params.get_list("type[]") == ["Invoice", "InvoiceReceipt", "SimplifiedInvoice", "CreditNote", "DebitNote"]
    assert (params["date[from]"], params["date[to]"], params["page"]) == ("01/09/2026", "30/09/2026", "1")
    assert docs[0].counterparty_tax_id == "508025338" and docs[0].currency == "EUR"
    assert ixp.list_documents(date(2026, 9, 1), date(2026, 9, 30), "purchases") == []  # no purchases there
    found = ixp.search(DocumentLookup(since=date(2026, 9, 1), until=date(2026, 9, 30), amount=Decimal("123.00")))
    assert [d.provider_id for d in found] == ["541791"]
    got = ixp.fetch(found[0])  # 202 first, then the PDF's address, fetched without the key
    assert got is not None and got.data == PDF and fake.pdf_tries == 2
    assert got.provenance()["source"] == "accounting" and got.provenance()["provider"] == "invoicexpress"
    with pytest.raises(ReconnectRequired):  # the owner regenerated the key: they add it again
        InvoiceXpressConnector("padaria", "old-key", client=client(fake)).list_documents(
            date(2026, 9, 1), date(2026, 9, 30), "sales")
    with pytest.raises(ValueError):
        InvoiceXpressConnector("padaria.evil.example/x", "k")
    assert "ixp-key-123" not in repr(ixp)


def _moloni_doc(i: int, saft: str, gross: str, day: str, status: int = 1, entity: str = "Café Central",
                vat: str = "508025338") -> dict[str, Any]:
    return {"document_id": 9100 + i, "document_type_id": 1, "document_set_id": 7, "number": i,
            "date": f"{day}T00:00:00+0100", "expiration_date": None, "your_reference": "", "our_reference": "",
            "entity_number": "C1", "entity_name": entity, "entity_vat": vat, "entity_address": "Porto",
            "gross_value": gross, "comercial_discount_value": 0, "financial_discount_value": 0,
            "taxes_value": "11.99", "deduction_value": 0, "net_value": "52.11", "status": status,
            "transport_code": "", "transport_code_set_by": 0, "exchange_currency_id": 0, "exchange_total_value": 0,
            "exchange_rate": 0, "document_type": {"document_type_id": 1, "saft_code": saft},
            "document_set": {"document_set_id": 7, "name": "2026"}}


class FakeMoloni:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "www.moloni.pt":
            return httpx.Response(200, content=PDF)
        if request.url.path == "/v1/grant/":
            q = request.url.params
            assert (q["client_id"], q["client_secret"]) == ("dev-id", "dev-secret")
            if q["grant_type"] == "refresh_token" and q["refresh_token"] == "expired":
                return httpx.Response(400, json={"error": "invalid_grant",
                                                 "error_description": "Invalid refresh token"})
            return httpx.Response(200, json={"access_token": "mol-at-1", "expires_in": 3600, "token_type": "bearer",
                                             "scope": None, "refresh_token": "mol-rt-2"})
        assert request.method == "POST" and request.url.params["access_token"].startswith("at-")
        form = parse_qs(request.content.decode())
        if request.url.path == "/v1/companies/getAll/":
            return httpx.Response(200, json=[{"company_id": 4, "name": "Outra Lda", "vat": "PT501000100"},
                                             {"company_id": 5, "name": "Padaria Lda", "vat": f"PT{NIF}"}])
        if request.url.path == "/v1/documents/getAll/":
            assert form["company_id"] == ["5"] and form["year"] == ["2026"] and form["qty"] == ["50"]
            if form["offset"] == ["0"]:
                docs = [_moloni_doc(i, "FT", "10.00", "2026-09-02") for i in range(1, 50)]
                return httpx.Response(200, json=[_moloni_doc(0, "OR", "99.00", "2026-09-01"), *docs])
            return httpx.Response(200, json=[_moloni_doc(60, "FR", "64.10", "2026-09-18"),
                                             _moloni_doc(61, "FT", "80.00", "2026-09-19", status=0)])
        if request.url.path == "/v1/supplierInvoices/getAll/":
            return httpx.Response(200, json=[_moloni_doc(70, "FT", "64.10", "2026-09-18", entity="EDP Comercial",
                                                         vat="501000100")])
        if request.url.path == "/v1/supplierCreditNotes/getAll/":
            return httpx.Response(200, json=[])
        if request.url.path == "/v1/documents/getPDFLink/":
            return httpx.Response(200, json={"url": "https://www.moloni.pt/downloads/index.php?action=getDownload"
                                                    f"&h=abc&d={form['document_id'][0]}"})
        return httpx.Response(404)


def test_moloni_signs_in_with_the_developer_app_and_reads_sales_and_purchases() -> None:
    fake = FakeMoloni()
    refresher = MoloniRefresher("dev-id", "dev-secret", client=client(fake))
    token = refresher.refresh("mol-rt-1")
    assert (token.access_token, token.refresh_token) == ("mol-at-1", "mol-rt-2")
    grant = next(r for r in fake.requests if r.url.path == "/v1/grant/")
    assert grant.method == "GET" and grant.url.params["grant_type"] == "refresh_token"  # Moloni's documented GET
    with pytest.raises(ReconnectRequired):  # older than 14 days: the owner signs in to Moloni again
        refresher.refresh("expired")
    moloni = MoloniConnector(Tokens(), company_tax_id=NIF, client=client(fake))
    sales = moloni.list_documents(date(2026, 9, 1), date(2026, 9, 30), "sales")
    assert len(sales) == 51 and all(d.doc_type in ("FT", "FR") for d in sales)  # an estimate (OR) is not booked
    receipt = next(d for d in sales if d.doc_type == "FR")
    assert (receipt.number, receipt.gross, receipt.issue_date) == ("FR 2026/60", Decimal("64.10"), date(2026, 9, 18))
    assert not next(d for d in sales if d.provider_id == "9161").final  # a draft
    purchases = moloni.list_documents(date(2026, 9, 1), date(2026, 9, 30), "purchases")
    assert [(d.counterparty, d.counterparty_tax_id, d.direction) for d in purchases] == [
        ("EDP Comercial", "501000100", "purchases")]
    found = moloni.search(DocumentLookup(since=date(2026, 8, 5), until=date(2026, 10, 4), amount=Decimal("64.10"),
                                         tax_id="501000100", direction="purchases"))
    assert [d.provider_id for d in found] == ["9170"]
    got = moloni.fetch(found[0])
    assert got is not None and got.data == PDF and got.filename == "FT_2026_70.pdf"
    company_calls = [r for r in fake.requests if r.url.path == "/v1/companies/getAll/"]
    assert len(company_calls) == 1  # found once by the company's tax number, then remembered


class FakeTOConline:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.refresh_ok = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/oauth/auth":
            q = request.url.params
            assert (q["client_id"], q["response_type"], q["scope"]) == ("pt516123459_c1", "code", "commercial")
            return httpx.Response(302, headers={"location": "https://oauth.pstmn.io/v1/callback?code=AUTHCODE1"})
        if path == "/oauth/token":
            assert request.headers["authorization"] == "Basic " + base64.b64encode(b"pt516123459_c1:s3cret").decode()
            form = parse_qs(request.content.decode())
            assert form["scope"] == ["commercial"]
            if form["grant_type"] == ["refresh_token"] and not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": f"toc-at-{len(self.requests)}", "expires_in": 14400,
                                             "refresh_token": "toc-rt-1", "token_type": "Bearer"})
        if request.url.host == "app10.toconline.pt" and path.startswith("/public-file/"):
            assert "authorization" not in request.headers
            return httpx.Response(200, content=PDF)
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["content-type"] == "application/vnd.api+json"
        if path in ("/api/v1/commercial_sales_documents", "/api/v1/commercial_purchases_documents"):
            sales = path.endswith("sales_documents")
            party = "customer" if sales else "supplier"
            rows = [("11", "FT 2026/12", "2026-09-28", "123.00", 1), ("10", "NC 2026/3", "2026-09-10", "24.60", 1),
                    ("9", "FT 2026/9", "2026-08-01", "40.00", 1)] if sales else \
                [("21", "FC 2026/41", "2026-09-18", "64.10", 1)]
            return httpx.Response(200, json={"data": [{
                "type": "commercial_sales_documents" if sales else "commercial_purchases_documents", "id": i,
                "attributes": {"document_no": no, "date": day, "document_type": no.split()[0], "gross_total": total,
                               "net_total": "52.11", "tax_payable": "11.99", "currency_iso_code": "EUR",
                               "status": status, f"{party}_business_name": "EDP Comercial" if not sales else "Café",
                               f"{party}_tax_registration_number": "501000100" if not sales else "508025338"}}
                for i, no, day, total, status in rows]})
        if path.startswith("/api/url_for_print/"):
            assert request.url.params["filter[type]"] in ("Document", "PurchasesDocument")
            return httpx.Response(200, json={"data": {"attributes": {"url": {
                "host": "app10.toconline.pt", "path": "/public-file/eyJ0eXAiOiJKV1Qi.sig", "port": 443,
                "scheme": "https"}}, "id": path.rsplit("/", 1)[1], "type": "url_for_print"}})
        return httpx.Response(404)


def test_toconline_authorizes_with_the_companys_api_data_and_reads_documents() -> None:
    fake = FakeTOConline()
    creds = TOConlineCredentials("pt516123459_c1", "s3cret", "https://app10.toconline.pt/oauth",
                                 "https://app10.toconline.pt/api")
    assert creds.api_url == "https://app10.toconline.pt" and "s3cret" not in repr(creds)
    rotated: list[str] = []
    tokens = TOConlineTokens(creds, client=client(fake), on_rotate=lambda t: rotated.append(t.refresh_token or ""))
    toc = TOConlineConnector(tokens, creds.api_url, client=client(fake))
    sales = toc.list_documents(date(2026, 9, 1), date(2026, 9, 30), "sales")
    assert [(d.number, d.doc_type, d.gross) for d in sales] == [("FT 2026/12", "FT", Decimal("123.00")),
                                                                 ("NC 2026/3", "NC", Decimal("24.60"))]
    listing = next(r for r in fake.requests if r.url.path.endswith("commercial_sales_documents"))
    assert listing.url.params["sort"] == "-date" and listing.url.params["page[size]"] == "100"
    assert rotated == ["toc-rt-1"]
    purchase = toc.search(DocumentLookup(since=date(2026, 8, 5), until=date(2026, 10, 4), amount=Decimal("64.10"),
                                         tax_id="501000100", direction="purchases"))
    assert [d.number for d in purchase] == ["FC 2026/41"]
    got = toc.fetch(purchase[0])
    assert got is not None and got.data == PDF
    # A lapsed refresh token (8 hours): a new code from the company's API data, no owner needed.
    fake.refresh_ok = False
    tokens.invalidate()
    assert tokens.access_token().startswith("toc-at-")
    assert [r.url.path for r in fake.requests].count("/oauth/auth") == 2
    for bad in ("http://app10.toconline.pt/oauth", "https://toconline.pt.evil.example/oauth", "https://10.0.0.1/x"):
        with pytest.raises(ValueError):
            TOConlineCredentials("id", "secret", bad, "https://app10.toconline.pt")


def test_the_month_export_has_every_booked_document_its_pdf_and_a_ledger_csv() -> None:
    fake = FakeTOConline()
    creds = TOConlineCredentials("pt516123459_c1", "s3cret", "https://app10.toconline.pt/oauth",
                                 "https://app10.toconline.pt")
    toc = TOConlineConnector(TOConlineTokens(creds, client=client(fake)), creds.api_url, client=client(fake))
    export = toc.export_month(2026, 9)
    assert [d.number for d in export.documents] == ["FT 2026/12", "NC 2026/3", "FC 2026/41"]
    rows = list(csv.reader(io.StringIO(export.ledger_csv.decode())))
    assert rows[0] == ["date", "direction", "type", "number", "counterparty", "tax_number", "net", "vat", "gross",
                       "currency", "software", "document_id"]
    assert ["2026-09-10", "sales", "NC", "NC 2026/3", "Café", "508025338", "-52.11", "-11.99", "-24.60", "EUR",
            "toconline", "10"] in rows  # a credit note takes off
    names = zipfile.ZipFile(io.BytesIO(export.zip_bytes())).namelist()
    assert "2026-09/ledger.csv" in names and "2026-09/purchases/FC_2026_41.pdf" in names
    assert len([n for n in names if n.endswith(".pdf")]) == 3


def test_an_accounting_sync_reads_new_sales_documents_once_and_reports_its_health() -> None:
    fake = FakeInvoiceXpress()
    ixp = InvoiceXpressConnector("padaria", "ixp-key-123", client=client(fake))
    state = ConnectorState(tenant_id="t1", connector_id="acc-1", kind=ConnectorKind.ACCOUNTING,
                           account="Padaria Lda", display_name="InvoiceXpress")
    now = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    got: list[Any] = []
    first = ixp.sync(state, got.append, now=now)
    assert first.ok and first.full_sync and len(got) == 3  # the draft is not booked
    assert first.state.coverage_start == now - timedelta(days=90)
    assert json.loads(first.state.cursor) == {"ids": ["541794"], "through": "2026-09-25", "v": 1}
    again = ixp.sync(first.state, got.append, now=now + timedelta(hours=1))
    assert again.ok and len(got) == 3  # nothing twice
    broken = InvoiceXpressConnector("padaria", "revoked", client=client(fake)).sync(again.state, got.append,
                                                                                    now=now + timedelta(hours=2))
    assert not broken.ok and broken.state.reconnect_required
    report = broken.state.health(now + timedelta(hours=2))
    assert report.title == "InvoiceXpress needs reconnecting." and report.action is not None
    plain(report.title, report.detail)


# =========================================================================== the searches themselves (K2, K6)


def _request(**changes: Any) -> SearchRequest:
    base: dict[str, Any] = {
        "subject_id": "tx_1", "subject_kind": "payment", "round": 1, "company_id": "padaria-lda",
        "amount": Decimal("64.10"), "currency": "EUR", "paid_on": date(2026, 9, 19),
        "window_start": date(2026, 8, 5), "window_end": date(2026, 10, 4), "counterparty": "EDP",
        "supplier_name": "EDP", "supplier_tax_id": "501000100", "supplier_domains": ("edp.pt",)}
    base.update(changes)
    return SearchRequest(**base)


class _Place:
    """One connected place as run_searches sees it: gives files, nothing, or fails."""

    def __init__(self, source: SearchSource, place: str, files: Any = (), error: Exception | None = None) -> None:
        self.source, self.place, self.files, self.error = source, place, list(files), error
        self.asked = 0

    def find(self, request: SearchRequest) -> list[FoundFile]:
        self.asked += 1
        if self.error is not None:
            raise self.error
        return self.files


def test_every_place_is_searched_in_the_specs_order_and_each_attempt_is_noted() -> None:
    drive_file = FoundFile(SearchSource.FILES, "your Google Drive", PDF, "EDP setembro.pdf", "application/pdf",
                           {"source": "drive", "fileId": "1EdpPdf"})
    places = [_Place(SearchSource.RECURRING_SEQUENCE, "EDP's usual invoice emails"),
              _Place(SearchSource.ACCOUNTING_PLATFORM, "Moloni", error=TransientError("moloni_http_503")),
              _Place(SearchSource.FILES, "your Google Drive", [drive_file] * 7),
              _Place(SearchSource.HISTORICAL_EMAIL, "your email"),
              _Place(SearchSource.CURRENT_EMAIL, "your email", error=TimeoutError())]
    ticks = iter(datetime(2026, 10, 2, 8, 0, s, tzinfo=timezone.utc) for s in range(60))
    run = run_searches(_request(), places, clock=lambda: next(ticks))
    assert [a["source"] for a in run.attempts] == [s.value for s in SEARCH_ORDER
                                                   if s is not SearchSource.SUPPLIER_PORTAL]
    assert [(a["outcome"], a["found"]) for a in run.attempts] == [
        ("timed_out", 0), ("nothing", 0), ("found", 5), ("failed", 0), ("nothing", 0)]
    assert run.attempts[3]["failure"] == "TransientError:moloni_http_503"  # for the audit log, never shown
    assert (run.attempts[0]["startedAt"], run.attempts[0]["finishedAt"]) == ("2026-10-02T08:00:00+00:00",
                                                                              "2026-10-02T08:00:01+00:00")
    assert all(p.asked == 1 for p in places) and len(run.files) == 5  # one broken place never stops the others


class _Mailbox:
    def __init__(self, found: Any = ()) -> None:
        self.queries: list[MailQuery] = []
        self.found = list(found)

    def search_messages(self, query: MailQuery) -> list[Any]:
        self.queries.append(query)
        return self.found


def test_mail_is_searched_now_in_the_months_before_and_the_suppliers_usual_way() -> None:
    from types import SimpleNamespace

    box = _Mailbox([SimpleNamespace(provider_id="18f1", thread_id="t1", folder="INBOX", raw=edp_email(),
                                    received_at=datetime(2026, 9, 18, 9, tzinfo=timezone.utc))])
    request = _request(invoice_number="FT EDP2026/558120", pattern=SearchPattern(
        senders=("faturas@edp.pt",), subject_words=("fatura", "edp"), filename_words=("fatura",)))
    current = MailboxSearch(box, provider="gmail").find(request)
    earlier = MailboxSearch(box, provider="gmail", historical=True).find(request)
    usual = RecurringMailSearch(box, place="EDP's usual invoice emails", provider="gmail").find(request)
    now_q, before_q, usual_q = box.queries
    assert (now_q.since, now_q.until) == (date(2026, 8, 5), date(2026, 10, 4))
    assert {t.value for t in now_q.any_of} == {"edp.pt", "FT EDP2026/558120", "64,10", "64.10", "EDP"}
    assert before_q.until == date(2026, 8, 4) and before_q.since == date(2026, 8, 4) - timedelta(days=400)
    assert {t.value for t in before_q.any_of} == {"FT EDP2026/558120", "64,10", "64.10"}
    assert [t.value for t in before_q.all_of] == ["edp.pt"]  # only this supplier's mail, months back
    assert usual_q.attachment and [t.value for t in usual_q.any_of] == ["faturas@edp.pt"]
    assert [(t.kind, t.value) for t in usual_q.all_of] == [(TermKind.SUBJECT, "fatura"), (TermKind.SUBJECT, "edp"),
                                                         (TermKind.FILENAME, "fatura")]
    assert current[0].provenance == {"source": "email", "provider": "gmail", "messageId": "18f1", "threadId": "t1",
                                     "folder": "INBOX", "receivedAt": "2026-09-18T09:00:00+00:00"}
    assert current[0].is_email and earlier[0].source is SearchSource.HISTORICAL_EMAIL
    assert usual[0].source is SearchSource.RECURRING_SEQUENCE
    assert RecurringMailSearch(box, place="x").find(_request()) == []  # nothing learned yet: not searched


def test_cloud_storage_is_searched_with_the_mail_sign_in_and_one_more_read_only_scope() -> None:
    drive = FakeDrive()
    drive.files.append({"id": "1Locked", "name": "EDP 64,10 (shared).pdf", "mimeType": "application/pdf",
                        "modifiedTime": "2026-09-20T10:15:00.000Z", "parents": ["0Faturas"]})

    def locked(request: httpx.Request) -> httpx.Response:  # the owner may view it, not download it
        if request.url.path.endswith("/files/1Locked"):
            return httpx.Response(403, json={"error": {"code": 403, "errors": [{"reason": "cannotDownloadFile"}]}})
        return drive(request)

    found = CloudStorageSearch(GoogleDriveConnector(Tokens(), client=client(locked)),
                               place="your Google Drive").find(_request())
    assert [f.provenance["fileId"] for f in found] == ["1EdpPdf", "1EdpDoc"]  # a file it may not download: skipped
    assert found[0].source is SearchSource.FILES and found[0].place == "your Google Drive"
    q = drive.requests[0].url.params["q"]
    assert "fullText contains '64,10'" in q and "fullText contains 'EDP'" in q
    assert "modifiedTime > '2026-08-05T00:00:00Z'" in q
    svc = BackOfficeService.new_tenant("t-files", owner_name="Ana Silva", owner_email="ana@padaria.pt",
                                       now=ASKED_ON, authorizer=ConsentPages())
    svc.add_company("Padaria Lda", NIF)
    added = _ok(svc, "POST", "/api/sources", {"kind": "files", "provider": "microsoft", "address": "ana@padaria.pt",
                                              "drive": "sites/padaria.sharepoint.com,1,2/drive"})
    assert added["message"] == "Almost done. Sign in so I can search your OneDrive for missing invoices."
    assert GRAPH_FILES_SCOPE in svc.authorizer.begun[-1][3]
    assert svc.sign_in[added["id"]]["drive"] == "sites/padaria.sharepoint.com,1,2/drive"
    status, _ = svc.dispatch("POST", "/api/sources", {"kind": "files", "provider": "microsoft",
                                                      "address": "bob@padaria.pt", "drive": "../../users/boss"})
    assert status == 400


def test_the_accounting_software_is_searched_for_the_suppliers_document() -> None:
    moloni = MoloniConnector(Tokens(), company_tax_id=NIF, client=client(FakeMoloni()))
    found = AccountingSearch(moloni, place="Moloni").find(_request())
    assert [(f.filename, f.provenance["number"], f.provenance["direction"]) for f in found] == [
        ("FT_2026_70.pdf", "FT 2026/70", "purchases")]
    assert found[0].data == PDF and found[0].source is SearchSource.ACCOUNTING_PLATFORM
    ixp = InvoiceXpressConnector("padaria", "ixp-key-123", client=client(FakeInvoiceXpress()))
    assert AccountingSearch(ixp, place="InvoiceXpress").find(_request()) == []  # it only holds sales
    sales = AccountingSearch(ixp, place="InvoiceXpress").find(_request(direction="sales", amount=Decimal("123.00"),
                                                                       supplier_tax_id=None))
    assert [f.provenance["documentId"] for f in sales] == ["541791"]  # a refund given: the company's own document


# =========================================================================== searching before asking, in the engine


class ConsentPages:
    """The providers' consent pages (connectors.authorize.OAuthAuthorizer's shape)."""

    providers = ("google", "microsoft", "moloni")

    def __init__(self) -> None:
        self.begun: list[tuple[str, str, str | None, tuple[str, ...]]] = []

    def begin(self, provider: str, tenant_id: str, connection_id: str, login_hint: str | None = None,
              scopes: tuple[str, ...] = ()) -> str:
        self.begun.append((provider, connection_id, login_hint, tuple(scopes)))
        return f"https://accounts.example/{provider}/consent?state=s{len(self.begun)}"


ASKED_ON = datetime(2026, 10, 2, 9, 30, tzinfo=TZ)
MAILBOX = "mail-ana-padaria-pt"
DRIVE = "files-google-ana-padaria-pt"


def _ok(svc: BackOfficeService, method: str, path: str, body: Any = None) -> Any:
    status, out = svc.dispatch(method, path, body)
    assert status == 200, (path, status, out)
    return out


def _synced(cid: str, kind: ConnectorKind, now: datetime, *, account: str = "ana@padaria.pt") -> dict[str, Any]:
    return ConnectorState(tenant_id="t-search", connector_id=cid, kind=kind, account=account, display_name=cid,
                          last_successful_sync=now, coverage_start=now - timedelta(days=90),
                          coverage_end=now).model_dump(mode="json")


def _business(*, drive: bool = True, mail: bool = True, requests: bool = True, transport: bool = True,
              now: datetime = ASKED_ON) -> tuple[BackOfficeService, SimulatedOutbox, str]:
    """Padaria Lda: a bank, EDP as a supplier, Gmail and Google Drive signed in, and a €64.10 EDP payment on
    19 September whose invoice is nowhere yet."""
    svc = BackOfficeService.new_tenant("t-search", owner_name="Ana Silva", owner_email="ana@padaria.pt", now=now,
                                       authorizer=ConsentPages())
    svc.add_company("Padaria Lda", NIF)
    outbox = SimulatedOutbox()
    if transport:
        svc.orchestrator.transport = outbox
    bank = _ok(svc, "POST", "/api/sources", {"kind": "bank", "bank": "Millennium BCP", "companyId": "padaria-lda",
                                             "iban": "PT50000201231234567890154"})
    _ok(svc, "POST", "/api/sources", {"kind": "supplier", "name": "EDP", "taxId": "501000100",
                                      "email": "faturas@edp.pt"})
    if mail:
        assert _ok(svc, "POST", "/api/sources", {"kind": "email", "provider": "google",
                                                 "address": "ana@padaria.pt"})["id"] == MAILBOX
        svc.finish_sign_in(MAILBOX)
        svc.sync_mail(MAILBOX, [], _synced(MAILBOX, ConnectorKind.GMAIL, now))
    if drive:
        added = _ok(svc, "POST", "/api/sources", {"kind": "files", "provider": "google", "address": "ana@padaria.pt"})
        assert added["id"] == DRIVE and added["authorizeUrl"].startswith("https://accounts.example/google/")
        assert svc.authorizer.begun[-1] == ("google", DRIVE, "ana@padaria.pt", (DRIVE_READONLY_SCOPE,))
        svc.finish_sign_in(DRIVE)
    if requests:
        _ok(svc, "POST", "/api/settings/automation", {"supplierRequests": True})
    csv_rows = (f"date,amount,counterparty,account,description,kind\n"
                f"2026-09-19,-64.10,EDP,{bank['id']},DD EDP,direct_debit\n").encode()
    tx = _ok(svc, "POST", "/api/evidence", {"filename": "extrato.csv", "contentType": "text/csv",
                                            "dataBase64": base64.b64encode(csv_rows).decode()})["transactions"][0]
    return svc, outbox, tx


def _attempt(source: str, place: str, outcome: str, found: int = 0, failure: str | None = None) -> dict[str, Any]:
    at = (ASKED_ON + timedelta(minutes=1)).isoformat()
    out: dict[str, Any] = {"source": source, "place": place, "startedAt": at, "finishedAt": at, "outcome": outcome,
                           "found": found}
    if failure:
        out["failure"] = failure
    return out


def edp_invoice(seq: int, day: date, net: str, vat: str, total: str) -> bytes:
    """An EDP invoice like the demo's, for another month (its QR code agrees with the printed totals)."""
    def dot(v: str) -> str:
        return v.replace(",", ".")

    return (f"EDP Comercial - Comercialização de Energia, S.A.\nNIF: 501000100\nFatura n.º FT EDP2026/{seq}\n"
            f"ATCUD: EDPQ7K2M-{seq}\nData de emissão: {day:%d/%m/%Y}\n"
            f"Data de vencimento: {day + timedelta(days=1):%d/%m/%Y}\nCliente: Padaria Lda\nNIF: {NIF}\n"
            f"Eletricidade - loja\nBase tributável (23%): {net}\nIVA 23%: {vat}\nTotal: {total} €\n"
            f"Código QR: A:501000100*B:{NIF}*C:PT*D:FT*E:N*F:{day:%Y%m%d}*G:FT EDP2026/{seq}*H:EDPQ7K2M-{seq}*"
            f"I1:PT*I7:{dot(net)}*I8:{dot(vat)}*N:{dot(vat)}*O:{dot(total)}*Q:e1Dk*R:1422\n").encode()


DRIVE_FILE = {"data": E.EDP_INVOICE, "source": "files", "place": "your Google Drive", "filename": "EDP setembro.txt",
              "contentType": "text/plain",
              "provenance": {"source": "drive", "provider": "google_drive", "fileId": "1EdpPdf",
                             "name": "EDP setembro.txt", "path": "/My Drive/Faturas/EDP setembro.txt",
                             "modifiedAt": "2026-09-20T10:15:00+00:00"}}


def test_an_invoice_found_in_drive_closes_the_payment_before_any_supplier_is_asked() -> None:
    svc, outbox, tx = _business()
    requests = svc.search_requests()
    assert [(r["subjectId"], r["round"], r["amount"], r["connections"]) for r in requests] == [
        (tx, 1, "64.10", [MAILBOX, DRIVE])]
    assert requests[0]["supplierDomains"] == ["edp.pt"] and requests[0]["windowStart"] == "2026-08-05"
    plan = svc.transaction(tx)["nextStep"]
    assert plan == ("I'm looking for the invoice for the €64.10 payment to EDP on 19 September in your email and "
                    "your Google Drive.")
    svc.orchestrator.run(ASKED_ON + timedelta(days=4))
    assert svc.repo.chases == {} and outbox.accepted == []  # never asked before the search, even days later

    result = svc.record_search(tx, 1, [_attempt("current_email", "your email", "nothing"),
                                       _attempt("historical_email", "your email", "nothing"),
                                       _attempt("files", "your Google Drive", "found", 1)], [DRIVE_FILE])
    assert result["status"] == "found" and result["nextStep"] == "match_found"
    detail = svc.transaction(tx)
    assert detail["status"] == "closed" and detail["headline"] == "Matched to the invoice."
    search = detail["search"]
    assert (search["summary"], search["searched"], search["foundIn"]) == (
        "Found it in your Google Drive.", ["your email", "your Google Drive"], "your Google Drive")
    assert [(p["place"], p["result"]) for p in search["places"]] == [("your email", "Nothing there."),
                                                                     ("your Google Drive", "Found it.")]
    rec = svc.repo.transactions[tx]
    doc = svc.repo.documents[rec.document_ids[0]]
    sightings = svc.repo.registry.sightings("t-search", doc.evidence_ids[0])
    assert {k: sightings[0].context[k] for k in ("found_by", "source", "fileId", "path", "modifiedAt")} == {
        "found_by": "missing_document_search", "source": "drive", "fileId": "1EdpPdf",
        "path": "/My Drive/Faturas/EDP setembro.txt", "modifiedAt": "2026-09-20T10:15:00+00:00"}
    lines = [a.text for a in svc.repo.activity]
    assert "Found the invoice for the €64.10 payment to EDP in your Google Drive." in lines
    plain(*lines[-3:])
    accountant = json.dumps(_ok(svc, "GET", "/api/accountant/clients/padaria-lda"), ensure_ascii=False)
    assert "Found in your Google Drive on 6 October at 09:30, before asking the supplier." in accountant
    svc.orchestrator.run(ASKED_ON + timedelta(days=8))
    assert svc.search_requests() == [] and svc.repo.chases == {} and outbox.accepted == []
    assert svc.record_search(tx, 1, [], []) == {"ok": True, "ignored": True}  # a round already applied


def test_nothing_found_then_the_supplier_is_asked_and_the_plan_says_where_i_searched() -> None:
    svc, outbox, tx = _business()
    august = dict(DRIVE_FILE, data=edp_invoice(544002, date(2026, 8, 18), "40,65", "9,35", "50,00"),
                  filename="EDP agosto.txt")
    result = svc.record_search(tx, 1, [_attempt("current_email", "your email", "nothing"),
                                       _attempt("historical_email", "your email", "nothing"),
                                       _attempt("files", "your Google Drive", "found", 1)], [august])
    assert result["status"] == "nothing" and result["nextStep"] == "chase_supplier"
    places = svc.transaction(tx)["search"]["places"]
    assert places[1] == {"place": "your Google Drive", "result": "Found documents, but none of them is this one.",
                         "at": places[1]["at"]}
    assert [m.subject for m in outbox.accepted] == [svc.repo.chases[tx].message.subject]  # asked only now
    plan = svc.transaction(tx)["nextStep"]
    assert plan.startswith("I searched your email and your Google Drive: it is not there. I asked EDP for the "
                           "invoice for the €64.10 payment on 19 September.")
    assert ("Looked for the invoice for the €64.10 payment to EDP in your email and your Google Drive: it is not "
            "there.") in [a.text for a in svc.repo.activity]
    accountant = json.dumps(_ok(svc, "GET", "/api/accountant/clients/padaria-lda"), ensure_ascii=False)
    assert "I searched your email and your Google Drive: it is not there." in accountant

    # Asking suppliers switched off: searched all the same, nobody is written to.
    quiet, quiet_outbox, quiet_tx = _business(requests=False)
    result = quiet.record_search(quiet_tx, 1, [_attempt("current_email", "your email", "nothing"),
                                               _attempt("files", "your Google Drive", "nothing")], [])
    assert result["status"] == "nothing" and quiet.repo.chases == {} and quiet_outbox.accepted == []
    assert quiet.transaction(quiet_tx)["nextStep"].startswith(
        "I searched your email and your Google Drive: it is not there.")


def test_a_place_that_could_not_be_searched_is_tried_again_then_the_supplier_is_asked() -> None:
    svc, outbox, tx = _business()
    o = svc.orchestrator
    first = svc.record_search(tx, 1, [_attempt("current_email", "your email", "nothing"),
                                      _attempt("files", "your Google Drive", "failed",
                                               failure="TransientError:drive_http_503")], [])
    assert first["status"] == "retry" and svc.repo.chases == {}
    assert svc.transaction(tx)["nextStep"] == (
        "I'm looking for the invoice for the €64.10 payment to EDP on 19 September in your email and your Google "
        "Drive. I couldn't reach your Google Drive yet, so I will look again shortly.")
    assert svc.search_requests() == []  # not before six hours
    o.run(ASKED_ON + timedelta(hours=6, minutes=30))
    assert [r["round"] for r in svc.search_requests()] == [2] and svc.repo.chases == {}
    assert svc.record_search(tx, 1, [], []) == {"ok": True, "ignored": True}  # a stale round changes nothing
    assert svc.record_search(tx, 2, [_attempt("current_email", "your email", "nothing"),
                                     _attempt("files", "your Google Drive", "timed_out", failure="TimeoutError")],
                             [])["status"] == "retry"
    o.run(ASKED_ON + timedelta(hours=13))
    third = svc.record_search(tx, 3, [_attempt("current_email", "your email", "nothing"),
                                      _attempt("files", "your Google Drive", "failed",
                                               failure="ReconnectRequired:drive_http_401")], [])
    assert third["status"] == "nothing" and third["nextStep"] == "chase_supplier"  # three rounds: ask anyway
    assert len(outbox.accepted) == 1 and svc.repo.chases[tx].sent
    search = svc.transaction(tx)["search"]
    assert search["summary"] == "I searched your email: it is not there." and len(search["earlier"]) == 2
    assert [p["result"] for p in search["places"]] == ["Nothing there.", "I couldn't search it this time."]
    assert all("Error" not in json.dumps(p) for p in search["places"])  # the internal reason stays in the audit


def edp_mail(subject: str, mid: str, day: date, attachment: bytes, filename: str) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "EDP Comercial <faturas@edp.pt>", "ana@padaria.pt", subject
    m["Message-ID"], m["Date"] = mid, f"{day:%a, %d %b %Y} 10:00:00 +0100"
    m.set_content("Segue em anexo a sua fatura.")
    m.add_attachment(attachment, maintype="text", subtype="plain", filename=filename)
    return m.as_bytes()


def test_the_suppliers_usual_way_of_sending_is_learned_for_the_search() -> None:
    svc, _, tx = _business(drive=False)
    svc.sync_mail(MAILBOX, [
        edp_mail("Fatura EDP julho 2026", "<f07@edp.pt>", date(2026, 7, 18),
                 edp_invoice(530001, date(2026, 7, 18), "48,78", "11,22", "60,00"), "fatura-edp-julho.txt"),
        edp_mail("Fatura EDP agosto 2026", "<f08@edp.pt>", date(2026, 8, 18),
                 edp_invoice(544002, date(2026, 8, 18), "40,65", "9,35", "50,00"), "fatura-edp-agosto.txt")], None)
    pattern = svc.orchestrator.search.pattern("sup-edp")
    assert pattern == SearchPattern(("faturas@edp.pt",), ("fatura", "edp"), ("fatura",))
    request = SearchRequest.from_json(svc.search_requests()[0])
    assert request.subject_id == tx and request.pattern == pattern and request.connections == (MAILBOX,)
    assert SearchRequest.from_json(request.to_json()) == request  # what the worker reads back


def test_a_usual_invoice_that_is_late_is_searched_for_before_it_is_chased() -> None:
    svc, outbox, tx = _business()
    history = [(5, "48,78", "11,22", "60,00"), (6, "40,65", "9,35", "50,00"), (7, "52,85", "12,15", "65,00"),
               (8, "44,72", "10,28", "55,00")]
    svc.sync_mail(MAILBOX, [edp_mail(f"Fatura EDP {m}/2026", f"<f{m}@edp.pt>", date(2026, m, 18),
                                     edp_invoice(500000 + m, date(2026, m, 18), net, vat, total), f"fatura-{m}.txt")
                            for m, net, vat, total in history], _synced(MAILBOX, ConnectorKind.GMAIL, ASKED_ON))
    svc.orchestrator.run(ASKED_ON + timedelta(minutes=5))
    late = [r for r in svc.search_requests() if r["kind"] == "expected_invoice"]
    assert [(r["amount"], r["windowStart"], r["connections"]) for r in late] == [(None, "2026-09-12",
                                                                                  [MAILBOX, DRIVE])]
    rid = late[0]["subjectId"]
    assert svc.repo.expected_invoices[rid].status == "missing" and outbox.accepted == []
    found = svc.record_search(rid, 1, [_attempt("current_email", "your email", "nothing"),
                                       _attempt("files", "your Google Drive", "found", 1)], [DRIVE_FILE])
    assert found["status"] == "found" and svc.repo.expected_invoices[rid].status == "received"
    assert svc.transaction(tx)["status"] == "closed"  # the same invoice proves the payment
    assert "The EDP invoice for September arrived." in [a.text for a in svc.repo.activity]
    assert svc.search_requests() == [] and outbox.accepted == []


def test_a_stopped_accounting_sync_keeps_the_month_from_closing() -> None:
    svc, _, _ = _business(drive=False)
    added = _ok(svc, "POST", "/api/sources", {"kind": "accounting", "provider": "invoicexpress", "account": "padaria",
                                              "apiKey": "ixp-key-123"})
    cid = added["id"]
    c = svc.repo.connectors[cid]
    assert (c.kind, c.name, c.searchable) == ("accounting", "InvoiceXpress", True)
    assert svc.vault.open("t-search", cid) == {"account": "padaria", "api_key": "ixp-key-123"}
    sources = _ok(svc, "GET", "/api/sources")
    assert "ixp-key-123" not in json.dumps(sources)
    assert [i["id"] for g in sources["groups"] if g["id"] == "accounting" for i in g["items"]] == [cid]
    assert [r["connections"] for r in svc.search_requests()] == [[MAILBOX, cid]]

    def waiting() -> list[str]:
        remaining = _ok(svc, "GET", "/api/months/padaria-lda/2026-09")["remaining"]
        return [r["text"] for r in remaining if r["id"].startswith("r_connector") and "InvoiceXpress" in r["text"]]

    assert waiting() == ["InvoiceXpress has not synced yet."]
    later = ASKED_ON + timedelta(hours=1)
    svc.sync_accounting(cid, [{"data": PDF, "filename": "FR_4_A.pdf", "contentType": "application/pdf",
                               "provenance": {"source": "accounting", "provider": "invoicexpress",
                                              "documentId": "541794", "direction": "sales", "type": "FR",
                                              "number": "4/A", "date": "2026-09-25"}}],
                        _synced(cid, ConnectorKind.ACCOUNTING, later, account="Padaria Lda"))
    assert waiting() == []
    failed = ConnectorState.model_validate(svc.sync_states[cid]).model_copy(
        update={"reconnect_required": True, "consecutive_failures": 1})
    svc.sync_failed(cid, failed.model_dump(mode="json"), reconnect=True)
    assert waiting() and waiting()[0].startswith("InvoiceXpress has not synced since")  # never green while stopped
    assert cid not in [p.id for p in svc.orchestrator.search.places("padaria-lda")]  # nor searched


def test_a_business_without_searchable_places_asks_as_before_and_the_demo_is_unchanged() -> None:
    svc, outbox, tx = _business(drive=False, mail=False, now=datetime(2026, 9, 22, 9, 30, tzinfo=TZ))
    assert svc.search_requests() == []
    svc.orchestrator.run(datetime(2026, 9, 23, 10, tzinfo=TZ))
    assert svc.repo.chases[tx].sent and len(outbox.accepted) == 1
    assert "searched" not in svc.transaction(tx)["nextStep"]
    demo = BackOfficeService.demo()
    assert demo.search_requests() == [] and demo.repo.evidence_searches == {}
    kinds = {m.kind for m in demo.repo.outbox.values()} if isinstance(demo.repo.outbox, dict) else \
        {m.kind for m in demo.repo.outbox}
    assert kinds <= {"supplier_request", "accountant_answer"}  # no reminder in the demo's month


# =========================================================================== the supplier's reply (K8)


ASK_DAY = datetime(2026, 9, 22, 9, 30, tzinfo=TZ)


def _asked() -> tuple[BackOfficeService, SimulatedOutbox, str]:
    """No searchable place: EDP is asked on 23 September, as the policy allows."""
    svc, outbox, tx = _business(drive=False, mail=False, now=ASK_DAY)
    _day(svc, 23)
    assert svc.repo.chases[tx].sent
    return svc, outbox, tx


def _day(svc: BackOfficeService, day: int, month: int = 9) -> datetime:
    at = datetime(2026, month, day, 10, tzinfo=TZ)
    svc.orchestrator.run(at)
    return at


def _days(svc: BackOfficeService, start: datetime, end: datetime) -> None:
    day = start
    while day <= end:
        svc.orchestrator.run(day)
        day += timedelta(days=1)


def _reply(svc: BackOfficeService, tx: str, at: datetime, *, sender: str = "faturas@edp.pt",
           in_thread: bool = True, invoice: bool = True, body: str = "Segue a fatura em anexo.",
           mid: str = "<reply-1@edp.pt>") -> None:
    chase = svc.repo.chases[tx]
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "ana@padaria.pt", "Re: " + chase.message.subject
    m["Message-ID"], m["Date"] = mid, at.strftime("%a, %d %b %Y %H:%M:%S +0100")
    if in_thread:
        m["In-Reply-To"] = chase.message.message_id
        m["References"] = chase.message.message_id
    m.set_content(body)
    if invoice:
        m.add_attachment(E.EDP_INVOICE, maintype="text", subtype="plain", filename="fatura.txt")
    svc.orchestrator.ingest_file(m.as_bytes(), filename="reply.eml", content_type="message/rfc822", at=at,
                                 origin="email")


def test_a_reply_in_the_thread_with_the_invoice_closes_the_request() -> None:
    svc, outbox, tx = _asked()
    chase = svc.repo.chases[tx]
    first = outbox.accepted[0]
    assert dict(first.headers)["Message-ID"] == chase.message.message_id
    assert svc.transaction(tx)["nextStep"] == ("I asked EDP for the invoice for the €64.10 payment on 19 September. "
                                               "Suppliers usually reply within a few days.")
    _days(svc, datetime(2026, 9, 24, 10, tzinfo=TZ), datetime(2026, 9, 28, 10, tzinfo=TZ))
    reminder = outbox.accepted[1]  # six days on (a working day): through the same send path
    headers = dict(reminder.headers)
    assert reminder.subject == "Re: " + chase.message.subject and reminder.to == ("faturas@edp.pt",)
    assert headers["In-Reply-To"] == chase.message.message_id == headers["References"]
    assert "Relembramos o nosso pedido" in reminder.body
    assert svc.transaction(tx)["nextStep"] == ("I asked EDP for the invoice for the €64.10 payment on 19 September "
                                               "and sent one reminder, the last on 28 September.")
    _reply(svc, tx, datetime(2026, 9, 29, 11, tzinfo=TZ))
    assert svc.repo.items[svc.repo.transactions[tx].item_id].stage.value == "closed"
    _day(svc, 30)
    assert chase.status == "received" and len(outbox.accepted) == 2  # no reminder after the invoice came
    assert "EDP sent the invoice for the €64.10 payment in reply to my request." in [a.text for a in
                                                                                    svc.repo.activity]


def test_a_reply_without_the_invoice_is_noted_reminders_go_on_then_one_owner_line() -> None:
    svc, outbox, tx = _asked()
    chase = svc.repo.chases[tx]
    _reply(svc, tx, datetime(2026, 9, 24, 10, tzinfo=TZ), invoice=False, body="Vamos verificar e enviamos em breve.")
    assert len(chase.replies) == 1 and chase.status == "asking"
    assert svc.transaction(tx)["nextStep"] == ("EDP replied on 24 September, but there was no invoice in the reply. "
                                               "I will remind them if it does not come.")
    assert "EDP replied about the €64.10 payment, but there was no invoice in the reply." in [
        a.text for a in svc.repo.activity]
    _days(svc, datetime(2026, 9, 25, 10, tzinfo=TZ), datetime(2026, 10, 7, 10, tzinfo=TZ))
    reminders = outbox.accepted[1:]
    assert len(reminders) == 2  # the policy's two reminders, each in the thread
    assert dict(reminders[1].headers)["References"] == (f"{chase.message.message_id} "
                                                        f"{dict(reminders[0].headers)['Message-ID']}")
    needs = [i for i in _ok(svc, "GET", "/api/needs-you")["items"] if i["id"] == chase.needs_id]
    assert len(needs) == 1 and chase.status == "escalated"
    item = needs[0]
    assert item["question"] == "EDP replied, but I still don't have the invoice for €64.10 on 19 September."
    assert [o["label"] for o in item["options"]] == ["Ask EDP again", "I'll upload it"]
    plain(item["question"], *item["why"])
    _ok(svc, "POST", f"/api/needs-you/{chase.needs_id}/answer", {"optionId": "ask_again"})
    _day(svc, 8, 10)
    assert len(outbox.accepted) == 4 and chase.status == "asking"  # asked again, still in the thread
    _reply(svc, tx, datetime(2026, 10, 9, 10, tzinfo=TZ), mid="<reply-2@edp.pt>")
    _day(svc, 10, 10)
    assert chase.status == "received" and svc.repo.items[svc.repo.transactions[tx].item_id].stage.value == "closed"
    assert svc.repo.needs[chase.needs_id].status == "answered" and len(outbox.accepted) == 4


def test_a_strangers_message_is_not_a_reply_and_nothing_is_claimed_without_a_transport() -> None:
    svc, outbox, tx = _asked()
    chase = svc.repo.chases[tx]
    _reply(svc, tx, datetime(2026, 9, 24, 10, tzinfo=TZ), sender="someone@gmail.com", in_thread=False,
           invoice=False, mid="<x1@gmail.com>")  # the subject's reference alone, from another domain
    assert chase.replies == [] and "replied" not in svc.transaction(tx)["nextStep"]
    # Asking switched off after the request: no reminder goes, and none is claimed.
    _ok(svc, "POST", "/api/settings/automation", {"supplierRequests": False})
    _days(svc, datetime(2026, 9, 25, 10, tzinfo=TZ), datetime(2026, 10, 3, 10, tzinfo=TZ))
    assert len(outbox.accepted) == 1 and chase.reminders == []
    assert "reminder" not in svc.transaction(tx)["nextStep"]

    # No way to send email yet: the request waits, and nothing claims it went or that anyone answered.
    unsent, _, unsent_tx = _business(drive=False, mail=False, transport=False, now=ASK_DAY)
    _days(unsent, datetime(2026, 9, 23, 10, tzinfo=TZ), datetime(2026, 10, 14, 10, tzinfo=TZ))
    waiting = unsent.repo.chases[unsent_tx]
    assert not waiting.sent and waiting.reminders == [] and waiting.needs_id is None
    assert unsent.transaction(unsent_tx)["nextStep"] == ("I wrote to EDP asking for the invoice for the €64.10 "
                                                         "payment on 19 September. It is waiting to be sent.")


# =========================================================================== on the production server


def _edp_reading() -> Any:
    """What OCR reads on the scanned EDP invoice in Drive (the reader stands in for PP-OCR)."""
    from backoffice.domain.models import BoundingBox, DocumentType, ExtractionMethod, FieldObservation
    from backoffice.reading import ReadOutcome
    from backoffice.reading.stage0 import ReadStep, StepState

    text = E.EDP_INVOICE.decode()
    total = FieldObservation(value=Decimal("64.10"), source="ev_scan@fake-ocr", method=ExtractionMethod.OCR,
                             confidence=0.93, location=BoundingBox(page=1, x0=0.1, y0=0.8, x1=0.3, y1=0.85))
    issued = FieldObservation(value=date(2026, 9, 18), source="ev_scan@fake-ocr", method=ExtractionMethod.OCR,
                              confidence=0.9, location="line 5")
    return ReadOutcome(text=text, text_method=ExtractionMethod.OCR,
                       readings={"gross_amount": (total,), "issue_date": (issued,)}, reading_text=text,
                       supplier_name="EDP Comercial", doc_type=DocumentType.INVOICE, page_count=1,
                       steps=(ReadStep("pdf_text", StepState.NOTHING, "no text layer"),
                              ReadStep("ocr", StepState.DONE, "1 page", engine="fake-ocr", cost=Decimal("0.0021"))),
                       cost=Decimal("0.0021"))


class FakeGoogleWorkspace:
    """Google's token endpoint, Gmail (a mailbox with nothing from EDP) and Drive (the EDP invoice as a PDF)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.drive = FakeDrive()
        self.drive.files = self.drive.files[:1]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": f"at-{len(self.requests)}", "expires_in": 3600})
        if request.url.path.startswith("/drive/v3"):
            return self.drive(request)
        assert request.headers["authorization"].startswith("Bearer at-")
        path = request.url.path.removeprefix("/gmail/v1/users/me")
        if path == "/profile":
            return httpx.Response(200, json={"emailAddress": "ana@padaria.pt", "historyId": "100"})
        if path == "/messages":
            return httpx.Response(200, json={"resultSizeEstimate": 0})
        return httpx.Response(404)

    def searched(self) -> list[str]:
        return [r.url.params["q"] for r in self.requests if r.url.path.endswith("/messages") and
                "from:edp.pt" in r.url.params.get("q", "")]


def test_the_worker_searches_gmail_and_drive_before_asking_and_the_search_replays(tmp_path: Path) -> None:
    from _server_support import sha
    from test_server_replay_safety import FakeReader
    from test_server_sync import GOOGLE, _events, _gmail_owner, _same_after_replay, _setup

    from backoffice.server.events import OBJECT, state_digest
    from backoffice.server.runtime import TenantManager
    from backoffice.server.sync import SyncWorker

    reader = FakeReader(_edp_reading())
    pages = ConsentPages()
    h, vault, _ = _setup(tmp_path, authorizer=pages, reader=reader)
    tenant, H = _gmail_owner(h, vault)
    res = h.client.post("/api/sources", json={"kind": "files", "provider": "google", "address": "ana@padaria.pt"},
                        headers=H)
    assert res.status_code == 200 and res.json()["authorizeUrl"].startswith("https://accounts.example/google/")
    assert pages.begun[-1] == ("google", DRIVE, "ana@padaria.pt", (DRIVE_READONLY_SCOPE,))
    vault.store(tenant, DRIVE, "google", {"refresh_token": "rt-drive", "scope": DRIVE_READONLY_SCOPE})
    assert h.manager.finish_sign_in(tenant, DRIVE, "google", "ana@padaria.pt")[0] == 200
    bank = h.client.post("/api/sources", json={"kind": "bank", "bank": "Millennium BCP", "companyId": "padaria-lda",
                                               "iban": "PT50000201231234567890154"}, headers=H).json()
    h.client.post("/api/sources", json={"kind": "supplier", "name": "EDP", "taxId": "501000100",
                                        "email": "faturas@edp.pt"}, headers=H)
    h.client.post("/api/settings/automation", json={"supplierRequests": True}, headers=H)
    rows = (f"date,amount,counterparty,account,description,kind\n"
            f"2026-09-19,-64.10,EDP,{bank['id']},DD EDP,direct_debit\n").encode()
    tx = h.client.post("/api/evidence", files={"file": ("extrato.csv", rows, "text/csv")},
                       headers=H).json()["transactions"][0]

    google = FakeGoogleWorkspace()
    worker = SyncWorker(h.manager, vault=vault, oauth_apps={"google": GOOGLE}, http_client=client(google))
    report = worker.run_once()
    assert (report.searches, report.found) == (1, 1)
    assert len(google.searched()) == 2  # this month's mail, then the months before (every label, one query each)
    event = _events(h, tenant, "search.recorded")[-1].data
    assert (event["subjectId"], event["round"]) == (tx, 1)
    assert [(a["source"], a["place"], a["outcome"], a["found"]) for a in event["attempts"]] == [
        ("current_email", "your email", "nothing", 0), ("historical_email", "your email", "nothing", 0),
        ("files", "your Google Drive", "found", 1)]
    [found] = event["files"]
    assert OBJECT in found and found["filename"] == "EDP setembro.pdf" and found["place"] == "your Google Drive"
    assert found["provenance"]["fileId"] == "1EdpPdf" and found["provenance"]["path"] == "/My Drive/Faturas/EDP setembro.pdf"
    assert set(event["reads"]) == {sha(PDF)} and len(reader.calls) == 1  # read once, before the event
    assert PDF.decode("latin-1") not in json.dumps(event)  # the file itself is in the object store
    detail = h.client.get(f"/api/transactions/{tx}", headers=H).json()
    assert detail["status"] == "closed" and detail["search"]["foundIn"] == "your Google Drive"
    with h.manager.open(tenant) as rt:
        assert rt.service.repo.chases == {}  # found before anyone was asked
    _same_after_replay(h, tenant)
    broken = FakeReader(fail=True)
    with h.manager.open(tenant) as rt:
        live = state_digest(rt.service)
    with TenantManager(h.store, h.objects, now=h.clock, strict_reads=True, reader=broken).open(tenant) as rt:
        assert state_digest(rt.service) == live and broken.calls == []  # a replay never searches nor reads
    h.clock.advance(minutes=16)
    assert worker.run_once().searches == 0  # nothing left to look for


def test_a_refused_drive_sign_in_during_a_search_asks_the_owner_and_the_payment_waits(tmp_path: Path) -> None:
    from test_server_sync import GOOGLE, _events, _gmail_owner, _setup

    from backoffice.server.sync import SyncWorker

    h, vault, _ = _setup(tmp_path, authorizer=ConsentPages())
    tenant, H = _gmail_owner(h, vault)
    h.client.post("/api/sources", json={"kind": "files", "provider": "google", "address": "ana@padaria.pt"}, headers=H)
    vault.store(tenant, DRIVE, "google", {"refresh_token": "rt-drive", "scope": DRIVE_READONLY_SCOPE})
    h.manager.finish_sign_in(tenant, DRIVE, "google", "ana@padaria.pt")
    bank = h.client.post("/api/sources", json={"kind": "bank", "bank": "Millennium BCP", "companyId": "padaria-lda",
                                               "iban": "PT50000201231234567890154"}, headers=H).json()
    h.client.post("/api/sources", json={"kind": "supplier", "name": "EDP", "taxId": "501000100",
                                        "email": "faturas@edp.pt"}, headers=H)
    rows = (f"date,amount,counterparty,account,description,kind\n"
            f"2026-09-19,-64.10,EDP,{bank['id']},DD EDP,direct_debit\n").encode()
    tx = h.client.post("/api/evidence", files={"file": ("extrato.csv", rows, "text/csv")},
                       headers=H).json()["transactions"][0]
    google = FakeGoogleWorkspace()
    google.drive.status = (403, {"error": {"code": 403, "errors": [{"reason": "insufficientPermissions"}]}})
    worker = SyncWorker(h.manager, vault=vault, oauth_apps={"google": GOOGLE}, http_client=client(google))
    report = worker.run_once()
    assert report.searches == 1 and report.found == 0
    attempts = _events(h, tenant, "search.recorded")[-1].data["attempts"]
    assert attempts[-1]["outcome"] == "failed" and attempts[-1]["failure"].startswith("ReconnectRequired")
    failed = [e.data for e in _events(h, tenant, "sync.failed") if e.data["connectionId"] == DRIVE]
    assert failed and failed[-1]["reconnect"] is True
    detail = h.client.get(f"/api/transactions/{tx}", headers=H).json()
    assert detail["nextStep"] == ("I'm looking for the invoice for the €64.10 payment to EDP on 19 September in your "
                                  "email. I couldn't reach your Google Drive yet, so I will look again shortly.")
    drive = next(c for c in h.client.get("/api/connections", headers=H).json()["connections"] if c["id"] == DRIVE)
    assert drive["status"] != "healthy"


def test_accounting_software_keeps_its_key_in_the_vault_and_is_synced_and_exported(tmp_path: Path) -> None:
    from _server_support import bearer, signup
    from test_server_sync import _events, _same_after_replay, _setup

    from backoffice.server.sync import SyncWorker

    fake = FakeInvoiceXpress()
    h, vault, _ = _setup(tmp_path, http_client=client(fake))
    account = signup(h.client)
    tenant, H = account["tenant"]["id"], bearer(account["token"])
    res = h.client.post("/api/sources", json={"kind": "accounting", "provider": "invoicexpress", "account": "padaria",
                                              "apiKey": "ixp-key-123"}, headers=H)
    assert res.status_code == 200, res.text
    cid = res.json()["id"]
    added = [e for e in _events(h, tenant, "request") if e.data.get("path") == "/api/sources"][-1]
    assert added.data["body"]["apiKey"] == "$redacted"
    assert "ixp-key-123" not in "\n".join(r.body for r in h.store.events(tenant))
    assert vault.open(tenant, cid) == {"account": "padaria", "api_key": "ixp-key-123"}

    report = SyncWorker(h.manager, vault=vault, http_client=client(fake)).run_once()
    assert report.synced == [f"{tenant}/{cid}"] and report.documents == 3  # the draft is not booked
    synced = _events(h, tenant, "sync.accounting")
    files = [f for e in synced for f in e.data["files"]]
    assert [f["provenance"]["documentId"] for f in files] == ["541791", "541792", "541794"]
    assert synced[-1].data["state"]["last_successful_sync"] is not None
    with h.manager.open(tenant) as rt:
        c = rt.service.repo.connectors[cid]
        assert c.healthy and c.covered_from is not None and c.searchable
    _same_after_replay(h, tenant)

    export = h.client.get(f"/api/accounting/{cid}/export?month=2026-09", headers=H)
    assert export.status_code == 200 and export.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(export.content)).namelist()
    assert "2026-09/ledger.csv" in names and "2026-09/sales/FT_1_A.pdf" in names
    assert h.client.get(f"/api/accounting/{cid}/export?month=2026-13", headers=H).status_code == 400
    assert h.client.get("/api/accounting/nope/export?month=2026-09", headers=H).status_code == 404
    assert h.client.get(f"/api/accounting/{cid}/export?month=2026-09").status_code == 401
