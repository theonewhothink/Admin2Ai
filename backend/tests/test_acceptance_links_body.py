"""Links followed in production, expired links, invoices in the email body, earlier thread attachments.

QA checks C1-C4 (links followed safely, redirects, files preserved, HTML receipts captured), C7 (an expired
link triggers another way to get the invoice), B4 (an invoice written in the email body), B6 (an attachment
sent earlier in the thread) and D14 on the server (a link shared from the phone's browser is followed).

The server opens links with ``LinkFetcher`` before an event is recorded; the event keeps what came back
(the bytes in the object store) and every apply, live or replayed, reads through that recording. A fake
HTTP transport and a fake DNS resolver stand in for the internet; nothing here touches the network.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
import pytest
from _server_support import b64, bearer, harness, signup
from test_server_push import TOKEN_A, FakeExpo

from backoffice.demo import evidence as E
from backoffice.domain.models import EvidenceFormat, ExtractionMethod, SourceKind, Supplier, TransactionKind
from backoffice.evidence.email import parse_eml
from backoffice.evidence.links import LinkFetcher, LinkOutcome, UrlSafety, UrlSafetyConfig
from backoffice.evidence.retrieval import PortalLinks, refers_to_earlier_invoice
from backoffice.orchestrator import TZ, BankRow, Orchestrator
from backoffice.policy import ActionKind
from backoffice.reading import ReadOutcome
from backoffice.reading.stage0 import ReadStep, StepState
from backoffice.server.events import Event, state_digest
from backoffice.server.notify import ExpoPushClient, PushNotifier
from backoffice.server.runtime import TenantManager
from backoffice.server.store import MemoryStore
from backoffice.service import BackOfficeService

SRC = Path(__file__).resolve().parents[1] / "src"
PDF = b"%PDF-1.4\n% EDP invoice FT EDP2026/558120\n1 0 obj<<>>endobj\n%%EOF\n"
CLICK = "https://click.edp-mail.example/c?id=9"
PDF_URL = "https://faturas.edp.pt/f/558120.pdf"
DNS = {
    "click.edp-mail.example": ["93.184.216.34"],
    "faturas.edp.pt": ["93.184.216.35"],
    "www.edp.pt": ["93.184.216.36"],
    "minha.edp.pt": ["93.184.216.37"],
    "cliente.edp.pt": ["93.184.216.38"],
}


# --------------------------------------------------------------------------- the fake internet


class FakeResolver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve(self, host: str, port: int) -> list[str]:
        self.calls.append(host)
        return DNS.get(host, [])


class Site:
    """Answers pinned requests by their Host header; counts every request that reached it."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get((request.headers["host"], request.url.path))
        if handler is None:
            return httpx.Response(404, text="not found")
        return handler(request) if callable(handler) else handler


def fetcher(routes: dict[tuple[str, str], Any]) -> tuple[LinkFetcher, Site, FakeResolver]:
    site, resolver = Site(routes), FakeResolver()
    safety = UrlSafety(UrlSafetyConfig(known_domains=("edp.pt",)), resolver)
    return LinkFetcher(safety, transport=httpx.MockTransport(site)), site, resolver


def pdf_response() -> httpx.Response:
    return httpx.Response(200, content=PDF, headers={
        "content-type": "application/pdf", "content-disposition": "attachment; filename=\"FT EDP2026-558120.pdf\""})


def html_response(html: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=html.encode("utf-8"), headers={"content-type": "text/html; charset=utf-8"})


TRACKED_PDF = {
    ("click.edp-mail.example", "/c"): httpx.Response(302, headers={"location": "https://faturas.edp.pt/f/558120"}),
    ("faturas.edp.pt", "/f/558120"): httpx.Response(303, headers={"location": "/f/558120.pdf"}),
    ("faturas.edp.pt", "/f/558120.pdf"): pdf_response(),
}


class FakeReader:
    """Stands in for the PDF text layer / OCR: the EDP invoice's text. Counts every call."""

    external_ai = False

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def engines(self) -> tuple[str, ...]:
        return ()

    def read(self, request: Any) -> ReadOutcome:
        self.calls.append(request)
        return ReadOutcome(text=E.EDP_INVOICE.decode("utf-8"), text_method=ExtractionMethod.EMBEDDED_TEXT, page_count=1,
                           steps=(ReadStep("pdf_text", StepState.DONE, "1 page with text"),))


class Refuses:
    """A link fetcher that must never be called (a replay)."""

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, url: str, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError(f"a replay opened {url}")


# --------------------------------------------------------------------------- emails and tenants


def email(*, subject: str, text: str | None = None, html: str | None = None,
          attachments: tuple[tuple[str, str, bytes], ...] = (), sender: str = "EDP Comercial <faturas@edp.pt>",
          message_id: str = "<fatura-0918@edp.pt>", date_header: str = "Fri, 18 Sep 2026 10:00:00 +0100",
          headers: dict[str, str] | None = None) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "ana@padaria.pt", subject
    m["Message-ID"], m["Date"] = message_id, date_header
    for name, value in (headers or {}).items():
        m[name] = value
    if text is not None:
        m.set_content(text)
        if html is not None:
            m.add_alternative(html, subtype="html")
    elif html is not None:
        m.set_content(html, subtype="html")
    for filename, mime, data in attachments:
        maintype, subtype = mime.split("/", 1)
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return m.as_bytes()


def link_email(url: str = CLICK) -> bytes:
    html = ("<html><body><p>Olá, a sua fatura de setembro está disponível.</p>"
            f'<p><a class="button" href="{url}">Ver fatura</a></p>'
            '<p><a href="https://www.edp.pt/privacidade">Privacidade</a></p></body></html>')
    return email(subject="A sua fatura EDP", text=f"A sua fatura de setembro está disponível.\nVer fatura: {url}\n",
                 html=html)


def owner(h: Any) -> tuple[str, dict[str, str]]:
    account = signup(h.client)
    tenant, H = account["tenant"]["id"], bearer(account["token"])
    res = h.client.post("/api/sources", json={"kind": "supplier", "name": "EDP", "taxId": "501000100",
                                              "email": "faturas@edp.pt"}, headers=H)
    assert res.status_code == 200, res.text
    return tenant, H


def upload(h: Any, H: dict[str, str], raw: bytes, name: str = "fatura.eml") -> dict[str, Any]:
    res = h.client.post("/api/evidence", json={"filename": name, "contentType": "message/rfc822",
                                               "dataBase64": b64(raw)}, headers=H)
    assert res.status_code == 200, res.text
    return res.json()


def events(h: Any, tenant: str, kind: str | None = None) -> list[Event]:
    return [e for e in (Event.parse(r) for r in h.store.events(tenant)) if kind is None or e.kind == kind]


def replayed_digest(h: Any, tenant: str, **services: Any) -> str:
    fresh = TenantManager(h.store, h.objects, now=h.clock, strict_reads=True, **services)
    with fresh.open(tenant) as rt:
        return state_digest(rt.service)


def live_digest(h: Any, tenant: str) -> str:
    h.clock.step = h.clock.step * 0
    with h.manager.open(tenant) as rt:
        return state_digest(rt.service)


def activity(h: Any, H: dict[str, str]) -> list[str]:
    return [a["text"] for a in h.client.get("/api/activity", headers=H).json()["items"]]


def read(h: Any, tenant: str, reader: Any) -> Any:
    return h.manager.read(tenant, reader, what="test")


# --------------------------------------------------------------------------- C1, C3: followed, with full provenance


def test_invoice_link_in_an_email_is_followed_and_ingested_with_full_provenance(tmp_path: Path) -> None:
    links, site, _ = fetcher(TRACKED_PDF)
    reader = FakeReader()
    h = harness(tmp_path, link_fetcher=links, reader=reader)
    tenant, H = owner(h)
    out = upload(h, H, link_email())

    # Opened once, before the event was recorded; the event keeps the outcome and provenance, not the bytes.
    assert len(site.requests) == 3
    event = events(h, tenant, "request")[-1]
    recorded = event.data["links"][CLICK]
    assert recorded["outcome"] == "downloaded" and recorded["format"] == "pdf"
    assert recorded["record"]["original_url"] == CLICK and recorded["record"]["final_url"] == PDF_URL
    assert [hop["status"] for hop in recorded["record"]["redirect_chain"]] == [302, 303, 200]
    assert recorded["content"]["$object"]["sha256"] == recorded["record"]["sha256"]
    assert PDF.decode("latin-1") not in json.dumps(event.data)
    assert [c.data for c in reader.calls] == [PDF]  # the downloaded PDF was read before recording too

    # The invoice is a document, recovered from the link; its evidence is the downloaded original.
    [doc] = out["documents"]
    assert doc["supplier"] == "EDP" and doc["label"].startswith("Invoice FT EDP2026/558120")
    pdf_id = next(e for e in doc["evidenceIds"])
    evidence = read(h, tenant, lambda svc: svc.repo.evidence(pdf_id))
    assert evidence.format is EvidenceFormat.PDF and evidence.source_kind is SourceKind.EMAIL
    assert evidence.original_url == CLICK and evidence.filename == "FT EDP2026-558120.pdf"
    fetch = evidence.metadata["fetch"]
    assert fetch["final_url"] == PDF_URL and fetch["retrieved_at"] and fetch["sha256"] == evidence.sha256
    assert [(hop["url"], hop["status"], hop["kind"]) for hop in fetch["redirect_chain"]] == [
        (CLICK, 302, "request"), ("https://faturas.edp.pt/f/558120", 303, "http"), (PDF_URL, 200, "http")]
    email_id = out["evidenceIds"][0]
    sighting = read(h, tenant, lambda svc: svc.repo.registry.sightings(tenant, pdf_id))[0]
    assert sighting.context["email_evidence_id"] == email_id
    assert "Recovered the EDP invoice from a link in your email." in activity(h, H)
    assert read(h, tenant, lambda svc: svc.links_waiting()) == []
    h.clock.step = h.clock.step * 0
    assert replayed_digest(h, tenant) == live_digest(h, tenant)


# --------------------------------------------------------------------------- C2 redirects, D14 shared from a browser


def test_redirect_chain_is_followed_hop_by_hop_for_a_link_shared_from_the_browser(tmp_path: Path) -> None:
    routes = {
        ("click.edp-mail.example", "/c"): httpx.Response(301, headers={"location": "http://www.edp.pt/go"}),
        ("www.edp.pt", "/go"): httpx.Response(302, headers={"location": "https://minha.edp.pt/doc"}),
        ("minha.edp.pt", "/doc"): html_response(
            '<html><head><meta http-equiv="refresh" content="0; url=https://faturas.edp.pt/f/558120.pdf">'
            "</head><body>A abrir…</body></html>"),
        ("faturas.edp.pt", "/f/558120.pdf"): pdf_response(),
    }
    links, site, _ = fetcher(routes)
    h = harness(tmp_path, link_fetcher=links, reader=FakeReader())
    tenant, H = owner(h)
    res = h.client.post("/api/share", json={"kind": "url", "url": CLICK, "sourceApp": "com.android.chrome"},
                        headers=H)
    assert res.status_code == 200, res.text
    out = res.json()
    assert [(r.headers["host"], r.url.path) for r in site.requests] == [
        ("click.edp-mail.example", "/c"), ("www.edp.pt", "/go"), ("minha.edp.pt", "/doc"),
        ("faturas.edp.pt", "/f/558120.pdf")]
    recorded = events(h, tenant, "request")[-1].data["links"][CLICK]["record"]
    assert [(hop["status"], hop["kind"]) for hop in recorded["redirect_chain"]] == [
        (301, "request"), (302, "http"), (200, "http"), (200, "meta_refresh")]
    [doc] = out["documents"]
    pdf = read(h, tenant, lambda svc: svc.repo.evidence(doc["evidenceIds"][0]))
    assert pdf.source_kind is SourceKind.MOBILE_SHARE and pdf.original_url == CLICK
    assert pdf.metadata["fetch"]["final_url"] == PDF_URL
    assert "Recovered the EDP invoice from the link you shared." in activity(h, H)


# --------------------------------------------------------------------------- C4: an HTML receipt


def test_page_without_a_file_is_preserved_as_rendered_evidence_and_read(tmp_path: Path) -> None:
    page = "<html><head><title>Fatura</title></head><body>" + "".join(
        f"<p>{line}</p>" for line in E.EDP_INVOICE.decode("utf-8").splitlines() if line) + "</body></html>"
    links, site, _ = fetcher({("cliente.edp.pt", "/recibo/558120"): html_response(page)})
    h = harness(tmp_path, link_fetcher=links)  # no reader: the page is text, nothing to OCR
    tenant, H = owner(h)
    out = upload(h, H, link_email("https://cliente.edp.pt/recibo/558120"))
    assert len(site.requests) == 1
    [doc] = out["documents"]
    assert doc["label"] == "Invoice FT EDP2026/558120 · €64.10" and doc["verified"]
    page_ev = read(h, tenant, lambda svc: svc.repo.evidence(doc["evidenceIds"][0]))
    assert page_ev.format is EvidenceFormat.HTML and page_ev.original_url == "https://cliente.edp.pt/recibo/558120"
    assert page_ev.metadata["outcome"] == LinkOutcome.RENDERED_PAGE.value
    stored = read(h, tenant, lambda svc: svc.repo.registry.open(tenant, page_ev.id))
    assert stored == page.encode("utf-8")  # the page itself, exactly as it was served


# --------------------------------------------------------------------------- C10: unsafe links are never opened


def test_an_unsafe_link_is_never_opened(tmp_path: Path) -> None:
    links, site, resolver = fetcher({})
    h = harness(tmp_path, link_fetcher=links)
    tenant, H = owner(h)
    metadata = "http://169.254.169.254/latest/fatura.pdf"
    upload(h, H, link_email(metadata))
    shared = h.client.post("/api/share", json={"kind": "url", "url": "https://edp.pt.secure-billing.com/fatura"},
                           headers=H).json()
    assert site.requests == [] and resolver.calls == []  # no connection, not even a DNS lookup
    assert shared["message"] == "This link didn't look safe, so I didn't open it."
    assert events(h, tenant, "request")[-2].data["links"][metadata]["outcome"] == "blocked_unsafe"
    status = read(h, tenant, lambda svc: {u: r.status for u, r in svc.repo.links_seen.items()})
    assert status == {metadata: "blocked", "https://edp.pt.secure-billing.com/fatura": "blocked"}
    stored = read(h, tenant, lambda svc: (len(svc.repo.store), [r.evidence_ids for r in svc.repo.links_seen.values()]))
    assert stored == (2, [(), ()])  # the email and the shared link itself; nothing came from either link
    assert read(h, tenant, lambda svc: svc.links_waiting()) == []  # and neither is tried again


# --------------------------------------------------------------------------- C5: a sign-in wall


def test_a_sign_in_page_becomes_the_owner_sign_in_state(tmp_path: Path) -> None:
    login = html_response('<html><body><form action="/login"><input type="email" name="email">'
                          '<input type="password" name="password"><button>Entrar</button></form></body></html>')
    code = html_response('<html><body><form action="/mfa"><p>Introduza o código que enviámos por SMS.</p>'
                         '<input name="otp" autocomplete="one-time-code"></form></body></html>')
    links, _, _ = fetcher({("minha.edp.pt", "/faturas/558120"): login, ("minha.edp.pt", "/codigo"): code})
    expo = FakeExpo()
    store = MemoryStore()
    notifier = PushNotifier(store, ExpoPushClient(transport=httpx.MockTransport(expo)))
    h = harness(tmp_path, store=store, link_fetcher=links, notifier=notifier)
    tenant, H = owner(h)
    h.client.post("/api/devices", json={"expoPushToken": TOKEN_A, "platform": "ios"}, headers=H)
    upload(h, H, link_email("https://minha.edp.pt/faturas/558120"))
    assert ("EDP needs you to sign in. The invoice link in its email opens a sign-in page, so I could not get the "
            "invoice from it yet.") in activity(h, H)
    assert [(m["title"], m["body"]) for m in expo.sent] == [("Sign-in needed", "EDP needs you to sign in.")]
    link = read(h, tenant, lambda svc: svc.repo.links_seen["https://minha.edp.pt/faturas/558120"])
    assert link.status == "sign_in" and link.message == "EDP needs you to sign in."
    assert read(h, tenant, lambda svc: svc.links_waiting()) == []  # only the owner can get past it
    # A link shared from the phone that asks for a code: the owner hears it at once.
    shared = h.client.post("/api/share", json={"kind": "url", "url": "https://minha.edp.pt/codigo"}, headers=H)
    assert shared.json()["message"] == "EDP needs authentication."
    h.clock.step = h.clock.step * 0
    assert replayed_digest(h, tenant) == live_digest(h, tenant)


# --------------------------------------------------------------------------- replay safety


def test_replay_never_opens_a_link_again(tmp_path: Path) -> None:
    links, site, _ = fetcher(TRACKED_PDF)
    reader = FakeReader()
    h = harness(tmp_path, link_fetcher=links, reader=reader)
    tenant, H = owner(h)
    upload(h, H, link_email())
    h.client.post("/api/share", json={"kind": "text", "text": f"A fatura: {CLICK} obrigado"}, headers=H)
    opened = len(site.requests)
    assert opened == 3  # the shared copy of a link already followed is not opened again
    live = live_digest(h, tenant)
    h.manager.evict(tenant)
    with h.manager.open(tenant) as rt:  # rebuilt in-process from the log
        assert state_digest(rt.service) == live
    refuses = Refuses()
    assert replayed_digest(h, tenant, link_fetcher=refuses, reader=FakeReader()) == live  # another process
    assert replayed_digest(h, tenant) == live  # and one with no fetcher at all
    assert refuses.calls == 0 and len(site.requests) == opened and len(reader.calls) == 1


def test_waiting_links_are_opened_by_the_sync_worker_and_recorded(tmp_path: Path) -> None:
    from backoffice.server.sync import SyncWorker

    busy = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        busy["n"] += 1
        return httpx.Response(503) if busy["n"] == 1 else pdf_response()

    links, site, _ = fetcher({("faturas.edp.pt", "/f/558120.pdf"): flaky})
    h = harness(tmp_path, link_fetcher=links, reader=FakeReader())
    tenant, H = owner(h)
    out = upload(h, H, link_email(PDF_URL))
    assert out["documents"] == [] and out["pendingLinks"] == [PDF_URL]  # the site was down: it waits
    worker = SyncWorker(h.manager)
    report = worker.run_once()
    assert report.links == 1 and len(site.requests) == 2
    assert [e.data["urls"] for e in events(h, tenant, "links.fetched")] == [[PDF_URL]]
    assert read(h, tenant, lambda svc: [d.document.invoice_number for d in svc.repo.documents.values()]) == [
        "FT EDP2026/558120"]
    assert worker.run_once().links == 0 and len(site.requests) == 2  # settled: never opened again
    h.clock.step = h.clock.step * 0
    assert replayed_digest(h, tenant, link_fetcher=Refuses()) == live_digest(h, tenant)


# --------------------------------------------------------------------------- C7: an expired link


def test_expired_link_is_recorded_and_starts_the_missing_evidence_path(tmp_path: Path) -> None:
    links, site, _ = fetcher({("faturas.edp.pt", "/f/558120.pdf"): httpx.Response(410, text="gone")})
    h = harness(tmp_path, link_fetcher=links)
    tenant, H = owner(h)
    upload(h, H, link_email(PDF_URL))
    recorded = events(h, tenant, "request")[-1].data["links"][PDF_URL]
    assert recorded["outcome"] == "unavailable" and recorded["reason"] == "http_410" and not recorded["retryable"]
    broken = read(h, tenant, lambda svc: list(svc.repo.broken_links.values()))
    assert [(b.url, b.supplier_name, b.status) for b in broken] == [(PDF_URL, "EDP", "missing")]
    assert broken[0].searched and broken[0].note  # looked for it; asking is not switched on for this business
    assert read(h, tenant, lambda svc: svc.orchestrator.waiting_messages()) == []  # nothing claimed as asked
    assert read(h, tenant, lambda svc: svc.links_waiting()) == []  # an expired link is never retried
    h.clock.step = h.clock.step * 0
    assert replayed_digest(h, tenant) == live_digest(h, tenant)


class RefusingMailer:
    def __init__(self) -> None:
        self.tries = 0

    def send(self, *args: Any, **kwargs: Any) -> None:
        self.tries += 1
        raise ConnectionError("smtp down")


class CapturingMailer:
    def __init__(self) -> None:
        self.sent: list[tuple[list[str], str, str]] = []

    def send(self, to: list[str], subject: str, body: str, files: Any, headers: Any = None) -> None:
        self.sent.append((list(to), subject, body))


class LiveLinks:
    """The link source protocol over a real LinkFetcher (the server's recorded source in miniature)."""

    def __init__(self, fetcher: LinkFetcher) -> None:
        self.fetcher = fetcher

    def fetch(self, url: str, *, supplier_name: str | None = None) -> Any:
        return self.fetcher.fetch(url, supplier_name=supplier_name)


def _business(links: LinkFetcher, *, payment: bool) -> BackOfficeService:
    """A real (non-demo) engine with one company, its bank, EDP, and supplier requests allowed (§25)."""
    svc = BackOfficeService.new_tenant("t-links", owner_name="Ana Silva", owner_email="ana@padaria.pt",
                                       now=datetime(2026, 9, 21, 9, 0, tzinfo=TZ))
    svc.add_company("Padaria Lda", "516123459")
    company = next(iter(svc.repo.companies))
    bank = svc.dispatch("POST", "/api/sources", {"kind": "bank", "bank": "Millennium BCP", "companyId": company,
                                                 "iban": "PT50000201231234567890154"})[1]
    svc.repo.add_supplier(Supplier(id="sup-edp", tenant_id="t-links", name="EDP", aliases=["EDP COMERCIAL"],
                                   tax_id="501000100", email_domains=["edp.pt"], countries=["PT"],
                                   contact_email="faturas@edp.pt"))
    svc.repo.policy = svc.repo.policy.with_grant(ActionKind.SUPPLIER_INVOICE_REQUEST, granted_by="ana@padaria.pt",
                                                 at=datetime(2026, 9, 1, tzinfo=TZ))
    svc.repo.links = LiveLinks(links)
    if payment:
        svc.orchestrator.ingest_bank([BankRow(bank_id="b1", account_id=bank["id"], booked_on=date(2026, 9, 19),
                                              amount=Decimal("-64.10"), counterparty="EDP COMERCIAL",
                                              description="DD EDP COMERCIAL", kind=TransactionKind.DIRECT_DEBIT)])
    return svc


def test_expired_link_asks_the_supplier_and_counts_only_what_a_transport_accepted() -> None:
    links, _, _ = fetcher({("faturas.edp.pt", "/f/558120.pdf"): httpx.Response(404, text="expired")})
    svc = _business(links, payment=True)
    o: Orchestrator = svc.orchestrator
    refusing = RefusingMailer()
    o.transport = refusing
    o.ingest_file(link_email(PDF_URL), filename="fatura.eml", content_type="message/rfc822",
                  source_kind=SourceKind.EMAIL, origin="email")
    [link] = o.repo.broken_links.values()
    rec = next(iter(o.repo.transactions.values()))
    assert link.status == "requested" and link.tx_id == rec.id  # asked at once, about the payment it is for
    assert refusing.tries >= 1 and o.repo.chases[rec.id].sent_at is None and link.sent_at is None
    texts = [a.text for a in o.repo.activity]
    waiting = ("The link in EDP's email no longer works, so I wrote to EDP asking for the invoice. "
               "It is waiting to be sent.")
    assert waiting in texts and not any(a.kind == "chased" for a in o.repo.activity)
    assert o.missing.plan(rec) == waiting

    mailer = CapturingMailer()
    o.transport = mailer
    o.run()
    [(to, subject, body)] = mailer.sent
    assert to == ["faturas@edp.pt"] and subject.startswith("Fatura do pagamento de 64,10 € de 19 de setembro")
    asked = "The link in EDP's email no longer works, so I asked EDP for the invoice."
    assert [a.text for a in o.repo.activity if a.kind == "chased"] == [asked]
    assert o.missing.plan(rec) == asked and o.repo.chases[rec.id].sent_at is not None

    # The supplier answers with the invoice: it closes the payment and the link's case (never the request).
    o.ingest_file(email(subject="Re: Fatura", text="Segue a fatura.", message_id="<resend@edp.pt>",
                        attachments=(("FT_EDP2026_558120.txt", "text/plain", E.EDP_INVOICE),)),
                  filename="resend.eml", content_type="message/rfc822", source_kind=SourceKind.EMAIL, origin="email")
    assert link.status == "received" and o.repo.items[rec.item_id].is_done
    assert len(mailer.sent) == 1  # asked once


def test_expired_link_without_a_payment_asks_for_the_invoice_from_that_email() -> None:
    links, _, _ = fetcher({})  # every path answers 404
    svc = _business(links, payment=False)
    o = svc.orchestrator
    mailer = CapturingMailer()
    o.transport = mailer
    o.ingest_file(link_email(PDF_URL), filename="fatura.eml", content_type="message/rfc822",
                  source_kind=SourceKind.EMAIL, origin="email")
    [(to, subject, body)] = mailer.sent
    assert to == ["faturas@edp.pt"] and subject.startswith("Fatura do vosso email de 18 de setembro")
    assert "já não funciona" in body and PDF_URL not in body  # the dead link itself never reaches the supplier
    assert [a.text for a in o.repo.activity if a.kind == "chased"] == [
        "The link in EDP's email no longer works, so I asked EDP for the invoice."]
    # A payment that arrives later is not asked about a second time.
    bank = next(a for a in o.repo.accounts.values())
    o.ingest_bank([BankRow(bank_id="b2", account_id=bank.id, booked_on=date(2026, 9, 19), amount=Decimal("-64.10"),
                           counterparty="EDP COMERCIAL", description="DD EDP COMERCIAL",
                           kind=TransactionKind.DIRECT_DEBIT)])
    o.run(datetime(2026, 9, 28, 9, 0, tzinfo=TZ))
    assert len(mailer.sent) == 1


def test_expired_link_asks_nobody_when_the_invoice_is_already_on_file() -> None:
    links, _, _ = fetcher({})  # every path answers 404
    svc = _business(links, payment=True)
    o = svc.orchestrator
    mailer = CapturingMailer()
    o.transport = mailer
    html = f'<p>A sua fatura segue em anexo.</p><p><a class="button" href="{PDF_URL}">Ver fatura</a></p>'
    o.ingest_file(email(subject="A sua fatura EDP", text=f"A sua fatura segue em anexo. Ver fatura: {PDF_URL}",
                        html=html, attachments=(("FT_EDP2026_558120.txt", "text/plain", E.EDP_INVOICE),)),
                  filename="fatura.eml", content_type="message/rfc822", source_kind=SourceKind.EMAIL, origin="email")
    [link] = o.repo.broken_links.values()
    assert link.status == "received" and link.document_id in o.repo.documents  # searched first: it is here
    assert mailer.sent == []


# --------------------------------------------------------------------------- B4: the invoice in the email body


INVOICE_BODY = (
    "EDP Comercial - Comercialização de Energia, S.A.\n"
    "NIF: 501000100\n"
    "Fatura n.º FT EDP2026/558120\n"
    "Data de emissão: 18/09/2026\n"
    "Cliente: Padaria Lda\n"
    "NIF: 516123459\n"
    "Base tributável (23%): 52,11\n"
    "IVA 23%: 11,99\n"
    "Total: 64,10 €\n"
)


@pytest.mark.parametrize("html_only", [False, True], ids=["plain-text", "html-only"])
def test_invoice_written_only_in_the_email_body_becomes_a_document(tmp_path: Path, html_only: bool) -> None:
    h = harness(tmp_path)
    tenant, H = owner(h)
    if html_only:
        html = ("<html><head><style>p{color:#333}</style><script>track()</script></head><body><table>"
                + "".join(f"<tr><td>{line}</td></tr>" for line in INVOICE_BODY.splitlines()) + "</table></body></html>")
        raw = email(subject="Fatura EDP de setembro", html=html)
        assert parse_eml(raw).text_body == ""  # nothing but HTML
    else:
        raw = email(subject="Fatura EDP de setembro", text="Olá,\n\n" + INVOICE_BODY + "\nObrigado.\n")
    out = upload(h, H, raw)
    email_id = out["evidenceIds"][0]
    [doc] = out["documents"]
    assert doc["supplier"] == "EDP" and doc["evidenceIds"] == [email_id]  # the email is its evidence
    record = read(h, tenant, lambda svc: svc.repo.documents[doc["id"]])
    d = record.document
    assert (d.invoice_number, d.issue_date, d.gross_amount, d.supplier_tax_id) == (
        "FT EDP2026/558120", date(2026, 9, 18), Decimal("64.10"), "501000100")
    assert "track()" not in record.text and "color" not in record.text  # scripts and styles never read
    assert "Collected the EDP invoice from your email." in activity(h, H)


def test_invoice_in_the_body_and_behind_its_link_is_one_document(tmp_path: Path) -> None:
    from backoffice.server.sync import SyncWorker

    tries = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        tries["n"] += 1
        return httpx.Response(503) if tries["n"] == 1 else pdf_response()

    links, _, _ = fetcher({("faturas.edp.pt", "/f/558120.pdf"): flaky})
    h = harness(tmp_path, link_fetcher=links, reader=FakeReader())
    tenant, H = owner(h)
    body = ("A sua fatura de setembro já está disponível.\nFatura n.º FT EDP2026/558120\n"
            f"Data de emissão: 18/09/2026\nTotal: 64,10 €\nVer fatura: {PDF_URL}\n")
    out = upload(h, H, email(subject="A sua fatura EDP", text=body))
    [doc] = out["documents"]  # the body names the invoice, its date and total; EDP is known by its address
    assert out["pendingLinks"] == [PDF_URL] and doc["supplier"] == "EDP"
    SyncWorker(h.manager).run_once()  # the site answers now: the PDF joins the same document
    docs = read(h, tenant, lambda svc: [(d.id, len(d.evidence_ids), d.document.supplier_tax_id)
                                        for d in svc.repo.documents.values()])
    assert docs == [(doc["id"], 2, "501000100")]


def test_foreign_e_receipt_in_the_body_is_read_with_the_international_labels(tmp_path: Path) -> None:
    body = ("Thanks for your order!\n\nCloudHost Europe Ltd\nVAT number: IE9692928F\nInvoice number: CH-2026-0042\n"
            "Invoice date: 12 September 2026\nBilled to: Padaria Lda, VAT PT516123459\n"
            "Subtotal: €20.00\nVAT (23%): €4.60\nTotal: €24.60\n")
    h = harness(tmp_path)
    tenant, H = owner(h)
    out = upload(h, H, email(subject="Your CloudHost invoice", text=body,
                             sender="CloudHost Billing <billing@cloudhost.example>"))
    [doc] = out["documents"]
    record = read(h, tenant, lambda svc: svc.repo.documents[doc["id"]])
    d = record.document
    assert (d.invoice_number, d.issue_date, d.gross_amount, d.currency) == (
        "CH-2026-0042", date(2026, 9, 12), Decimal("24.60"), "EUR")
    assert record.issuer is not None and record.issuer.country == "IE"  # read as a document from abroad (P7)
    assert d.supplier_name == "CloudHost Billing" and record.evidence_ids == [out["evidenceIds"][0]]


def test_json_ld_receipt_in_an_html_email_is_read_as_structured_data(tmp_path: Path) -> None:
    jsonld = {
        "@context": "https://schema.org", "@type": "Invoice", "identifier": "EDP-ONLINE-7731",
        "paymentStatus": "https://schema.org/PaymentDue", "paymentDueDate": "2026-10-02",
        "provider": {"@type": "Organization", "name": "EDP Comercial", "vatID": "PT501000100"},
        "customer": {"@type": "Organization", "name": "Padaria Lda", "vatID": "PT516123459"},
        "totalPaymentDue": {"@type": "PriceSpecification", "price": 23.5, "priceCurrency": "EUR"},
    }
    html = (f'<html><head><script type="application/ld+json">{json.dumps(jsonld)}</script></head><body>'
            "<h1>Recibo EDP-ONLINE-7731</h1><p>Obrigado pela sua compra online.</p>"
            "<p>Total: 23,50 €</p><p>Pague até 02/10/2026.</p></body></html>")
    h = harness(tmp_path)
    tenant, H = owner(h)
    out = upload(h, H, email(subject="O seu recibo", html=html, sender="EDP Online <loja@edp.pt>"))
    [doc] = out["documents"]
    record = read(h, tenant, lambda svc: svc.repo.documents[doc["id"]])
    d = record.document
    assert (d.invoice_number, d.gross_amount, d.currency, d.due_date) == (
        "EDP-ONLINE-7731", Decimal("23.50"), "EUR", date(2026, 10, 2))
    assert record.supplier_id is not None and d.supplier_name == "EDP"
    methods = {o.method for o in record.observations["gross_amount"]}
    assert ExtractionMethod.HTML_STRUCTURED in methods  # the schema.org data, next to the visible text
    assert record.observations["invoice_number"][0].location == "json-ld[0].identifier"
    assert record.evidence_ids == [out["evidenceIds"][0]]  # the email is its evidence


def test_a_body_that_only_mentions_the_invoice_makes_no_second_document(tmp_path: Path) -> None:
    h = harness(tmp_path)
    tenant, H = owner(h)
    mention = ("Olá,\nSegue em anexo a fatura FT EDP2026/558120 de 18/09/2026, no valor de 64,10 €.\n"
               "NIF 501000100\nCumprimentos,\nEDP\n")
    out = upload(h, H, email(subject="Fatura EDP", text=mention,
                             attachments=(("FT_EDP2026_558120.txt", "text/plain", E.EDP_INVOICE),)))
    assert len(out["documents"]) == 1
    # A body that only says where the invoice is gives no document at all.
    out = upload(h, H, email(subject="A sua fatura", message_id="<aviso@edp.pt>",
                             text="A sua fatura de setembro, no valor de 64,10 €, já está disponível na sua "
                                  "área de cliente.\n"))
    assert out["documents"] == []
    assert read(h, tenant, lambda svc: len(svc.repo.documents)) == 1


# --------------------------------------------------------------------------- B6: an earlier attachment in the thread


class ThreadGmail:
    """Google's token endpoint and a Gmail API with threads: the reply is in the sync window, the message
    with the invoice it points at is older (never listed by the sync)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        old = email(subject="Fatura setembro", text="Segue a fatura em anexo.", message_id="<old@edp.pt>",
                    date_header="Thu, 3 Sep 2026 10:00:00 +0100",
                    attachments=(("FT_EDP2026_558120.txt", "text/plain", E.EDP_INVOICE),))
        reply = email(subject="Re: Fatura setembro", message_id="<reply@edp.pt>",
                      date_header="Mon, 28 Sep 2026 10:00:00 +0100",
                      headers={"In-Reply-To": "<old@edp.pt>", "References": "<old@edp.pt>"},
                      text="Bom dia,\nPlease see the invoice I sent on the 3rd.\n\n> Can you send the invoice?\n")
        self.messages = {"m-old": (old, "1757000000000"), "m-reply": (reply, "1759050000000")}
        self.listed = ["m-reply"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        path = request.url.path.replace("/gmail/v1/users/me", "")
        if path == "/profile":
            return httpx.Response(200, json={"emailAddress": "ana@padaria.pt", "historyId": "100"})
        if path == "/messages":
            return httpx.Response(200, json={"messages": [{"id": i} for i in self.listed]})
        if path == "/threads/t-1":
            return httpx.Response(200, json={"id": "t-1", "messages": [
                {"id": "m-old", "labelIds": ["INBOX"]}, {"id": "m-reply", "labelIds": ["INBOX"]}]})
        if path.startswith("/messages/"):
            mid = path.rsplit("/", 1)[1]
            raw, when = self.messages[mid]
            return httpx.Response(200, json={"id": mid, "threadId": "t-1", "labelIds": ["INBOX"],
                                             "internalDate": when,
                                             "raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")})
        return httpx.Response(404)

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(path)]


def test_earlier_thread_attachment_is_fetched_through_the_connector(tmp_path: Path) -> None:
    from backoffice.connectors.authorize import PROVIDERS, OAuthApp
    from backoffice.connectors.vault import LocalKeyProvider, TokenVault
    from backoffice.server.sync import SyncWorker

    vault = TokenVault(LocalKeyProvider(os.urandom(32)))
    h = harness(tmp_path, vault=vault)
    tenant, H = owner(h)
    vault.store(tenant, "mail-google-pending", "google", {"refresh_token": "rt-1"})
    status, body = h.manager.finish_sign_in(tenant, "mail-google-pending", "google", "ana@padaria.pt")
    assert status == 200
    gmail = ThreadGmail()
    worker = SyncWorker(h.manager, vault=vault, http_client=httpx.Client(transport=httpx.MockTransport(gmail)),
                        oauth_apps={"google": OAuthApp("google", "id", "secret", **PROVIDERS["google"])})
    assert refers_to_earlier_invoice(parse_eml(gmail.messages["m-reply"][0]))
    report = worker.run_once()
    assert report.thread_messages == 1 and len(gmail.calls("/threads/t-1")) == 1
    # The earlier message was fetched before recording and stored with the reply, earlier first.
    [synced] = events(h, tenant, "sync.mail")
    refs = [m["$object"]["sha256"] for m in synced.data["messages"]]
    assert refs == [hashlib.sha256(gmail.messages[m][0]).hexdigest() for m in ("m-old", "m-reply")]
    docs = read(h, tenant, lambda svc: [(d.document.invoice_number, d.document.gross_amount)
                                        for d in svc.repo.documents.values()])
    assert docs == [("FT EDP2026/558120", Decimal("64.10"))]
    # The next reply in the same thread does not bring the same earlier message again.
    h.clock.advance(minutes=20)
    gmail.messages["m-again"] = (email(subject="Re: Fatura setembro", message_id="<again@edp.pt>",
                                       headers={"In-Reply-To": "<reply@edp.pt>"},
                                       text="See the invoice I sent before, please.\n"), "1759060000000")
    gmail.listed = ["m-reply", "m-again"]
    worker.run_once()
    assert all(len(e.data["messages"]) <= 2 for e in events(h, tenant, "sync.mail"))
    assert read(h, tenant, lambda svc: len(svc.repo.documents)) == 1
    h.clock.step = h.clock.step * 0
    assert replayed_digest(h, tenant) == live_digest(h, tenant)


def test_microsoft_conversation_gives_the_earlier_messages_oldest_first() -> None:
    from backoffice.connectors.microsoft import MicrosoftMailConnector

    def graph(request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/v1.0", "")
        if path == "/me/messages":
            assert request.url.params["$filter"] == "conversationId eq 'AAQk''x'"  # quotes escaped
            return httpx.Response(200, json={"value": [
                {"id": "r1", "receivedDateTime": "2026-09-28T10:00:00Z", "conversationId": "AAQk'x"},
                {"id": "o1", "receivedDateTime": "2026-09-03T10:00:00Z", "conversationId": "AAQk'x"},
                {"id": "d1", "isDraft": True}]})
        if path.startswith("/me/messages/") and path.endswith("/$value"):
            return httpx.Response(200, content=f"Subject: {path.split('/')[3]}\r\n\r\nbody".encode())
        return httpx.Response(404)

    class Tokens:
        def access_token(self) -> str:
            return "at"

        def invalidate(self) -> None:
            pass

    connector = MicrosoftMailConnector(Tokens(), client=httpx.Client(transport=httpx.MockTransport(graph)))
    items = connector.thread_messages("AAQk'x")
    assert [(i.provider_id, i.thread_id) for i in items] == [("o1", "AAQk'x"), ("r1", "AAQk'x")]  # no draft


def test_only_a_reply_pointing_at_an_earlier_invoice_fetches_its_thread() -> None:
    def parsed(text: str, **headers: str) -> Any:
        return parse_eml(email(subject=headers.pop("subject", "Re: Fatura"), text=text, headers=headers))

    assert refers_to_earlier_invoice(parsed("Please see the invoice I sent on the 3rd.", **{"In-Reply-To": "<a@b>"}))
    assert refers_to_earlier_invoice(parsed("Bom dia, a fatura foi enviada na semana passada.",
                                            **{"References": "<a@b>"}))
    assert not refers_to_earlier_invoice(parsed("Please see the invoice I sent on the 3rd.",
                                                subject="Fatura"))  # not a reply
    assert not refers_to_earlier_invoice(parsed("Thanks, all good.", **{"In-Reply-To": "<a@b>"}))
    assert not refers_to_earlier_invoice(parsed("Ok.\n\nOn Tue, Ana wrote:\n> the invoice I sent earlier",
                                                **{"In-Reply-To": "<a@b>"}))  # only quoted history


# --------------------------------------------------------------------------- the demo and the browser build


def test_demo_follows_its_portal_link_through_the_same_interface() -> None:
    svc = BackOfficeService.demo()
    repo = svc.repo
    assert isinstance(repo.links, PortalLinks)  # the demo's simulated adapters, behind the one link source
    link = repo.links_seen[E.ADOBE_INVOICE_URL]
    assert link.status == "retrieved" and link.email_evidence_id
    [evidence_id] = link.evidence_ids
    ev = repo.evidence(evidence_id)
    assert ev.source_kind is SourceKind.SUPPLIER_PORTAL and ev.metadata["portal"] == "Adobe account"
    assert ev.original_url == E.ADOBE_INVOICE_URL
    assert any(a.text == "Recovered the Adobe invoice from a link in your email." for a in repo.activity)
    status, out = svc.dispatch("POST", "/api/share", {"kind": "url", "url": "https://example.org/invoice/1"})
    assert status == 200 and out["pendingLinks"] == ["https://example.org/invoice/1"]  # no network: it waits


def test_browser_build_imports_and_reads_bodies_without_network_libraries() -> None:
    raw = email(subject="Fatura EDP de setembro", text="Olá,\n\n" + INVOICE_BODY)
    code = (
        "import sys, base64\n"
        "for m in ('fastapi','httpx','temporalio','starlette','uvicorn','playwright'): sys.modules[m] = None\n"
        "import backoffice.service as s, backoffice.evidence.retrieval, backoffice.evidence.links\n"
        "svc = s.BackOfficeService.demo()\n"
        f"raw = base64.b64decode({base64.b64encode(raw).decode()!r})\n"
        "st, out = svc.dispatch('POST', '/api/evidence', {'filename': 'f.eml', 'contentType': 'message/rfc822',"
        " 'dataBase64': base64.b64encode(raw).decode()})\n"
        "print(st, len(out['documents']))\n"
        "print([m for m in ('fastapi','httpx','temporalio','starlette','uvicorn') if sys.modules.get(m)])\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=SRC, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\n")[:2] == ["200 1", "[]"]
