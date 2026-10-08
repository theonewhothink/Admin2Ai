"""Searching the supplier's website before asking (QA K5) and learning where each supplier's invoices are (L6).

* K5, on the production server: a payment without its invoice is searched for in every connected place before
  any supplier is asked; the mailbox and Google Drive have nothing, EDP's website (the EDP adapter, signed in
  with the owner's saved session, against recorded pages, tests/_edp_site.py) has it: it is fetched, read before
  the event is recorded, verified, matched, and the payment closes; nobody is written to. When the website does
  not have it either, the supplier is asked.
* L6: the engine remembers which website holds a supplier's invoices (fetched there by the daily sign-in or by
  a search, an invoice email that only links there, or the owner adding it under the supplier's name), shows it
  on the supplier ("Invoices: fetched from edp.pt"), keeps it through replay, and searches that website first
  the next time one of its invoices is missing.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from _edp_site import AUGUST, EMAIL, GAS, PASSWORD, SEPTEMBER, FakeEdpSite, SiteReader
from _server_support import FakeClock
from test_acceptance_search_connectors import (
    MAILBOX,
    FakeGoogleWorkspace,
    _attempt,
    _business,
    _ok,
)

from backoffice.connectors.cloud_storage import DRIVE_READONLY_SCOPE
from backoffice.domain.models import Quality
from backoffice.language import find_jargon, find_off_tone
from backoffice.missing import (
    EvidenceCandidate,
    FoundFile,
    MissingEvidenceAutopilot,
    SearchRequest,
    SearchSource,
    run_recorded,
    run_searches,
)
from backoffice.server.events import state_digest
from backoffice.server.portals import PortalWorker
from backoffice.server.runtime import TenantManager
from backoffice.server.sync import SyncWorker

WEBSITE = "portal-edp"
DRIVE = "files-google-ana-padaria-pt"
ON_EDP = "your account on EDP's website"


def _plain(*texts: str) -> None:
    for text in texts:
        assert find_jargon(text) == [] and find_off_tone(text) == [], text


# =========================================================================== on the production server


class Business:
    """Padaria Lda on the production server: Gmail (and Google Drive), a bank, EDP as a supplier, EDP's website
    added with the owner's sign-in there, and the sync worker that reads it all."""

    def __init__(self, tmp_path: Path, *, drive: bool = True) -> None:
        from test_acceptance_search_connectors import ConsentPages, client
        from test_server_sync import GOOGLE, _gmail_owner, _setup

        self.clock = FakeClock()
        self.site = FakeEdpSite(clock=lambda: self.clock.now_)
        self.reader = SiteReader(SEPTEMBER, AUGUST, GAS)
        # The server builds the registered EDP adapter by its key; here it talks to the fake website.
        self.factory = lambda key: self.site.adapter(clock=lambda: self.clock.now_) if key == "edp_pt" else None
        self.h, self.vault, self.expo = _setup(tmp_path, authorizer=ConsentPages(), reader=self.reader,
                                       portal_factory=self.factory, now=self.clock)
        self.tenant, self.H = _gmail_owner(self.h, self.vault)
        if drive:
            self.post("/api/sources", {"kind": "files", "provider": "google", "address": "ana@padaria.pt"})
            self.vault.store(self.tenant, DRIVE, "google", {"refresh_token": "rt-drive", "scope": DRIVE_READONLY_SCOPE})
            assert self.h.manager.finish_sign_in(self.tenant, DRIVE, "google", "ana@padaria.pt")[0] == 200
        self.bank = self.post("/api/sources", {"kind": "bank", "bank": "Millennium BCP", "companyId": "padaria-lda",
                                               "iban": "PT50000201231234567890154"})["id"]
        self.post("/api/sources", {"kind": "supplier", "name": "EDP", "taxId": "501000100", "email": "faturas@edp.pt"})
        self.post("/api/settings/automation", {"supplierRequests": True})
        added = self.post("/api/sources", {"kind": "portal", "supplier": "EDP", "username": EMAIL,
                                           "password": PASSWORD})
        assert added["id"] == WEBSITE
        assert added["message"] == "Done. I will sign in to EDP and fetch your invoices from there."
        assert self.vault.open(self.tenant, WEBSITE)["portal"] == "edp_pt"  # the EDP adapter, chosen by the name
        self.google = FakeGoogleWorkspace()
        self.google.drive.files = []  # nothing from EDP in Google Drive either
        self.worker = SyncWorker(self.h.manager, vault=self.vault, oauth_apps={"google": GOOGLE},
                                 http_client=client(self.google),
                                 portals=PortalWorker(self.h.manager, vault=self.vault, factory=self.factory))

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        res = self.h.client.post(path, json=body, headers=self.H)
        assert res.status_code == 200, res.text
        return res.json()

    def get(self, path: str) -> Any:
        res = self.h.client.get(path, headers=self.H)
        assert res.status_code == 200, res.text
        return res.json()

    def pay(self, day: str, amount: str) -> str:
        rows = (f"date,amount,counterparty,account,description,kind\n"
                f"{day},-{amount},EDP,{self.bank},DD EDP,direct_debit\n").encode()
        res = self.h.client.post("/api/evidence", files={"file": ("extrato.csv", rows, "text/csv")}, headers=self.H)
        assert res.status_code == 200, res.text
        return res.json()["transactions"][0]

    def events(self, kind: str) -> list[Any]:
        from test_server_sync import _events

        return _events(self.h, self.tenant, kind)

    def read(self, fn: Any) -> Any:
        return self.h.manager.read(self.tenant, fn)

    def supplier_line(self) -> str:
        groups = self.get("/api/sources")["groups"]
        return next(i for g in groups if g["id"] == "suppliers" for i in g["items"] if i["name"] == "EDP")["detail"]

    def same_after_replay(self) -> None:
        """Another process rebuilds the same business from the log alone: no website, reader or network."""
        step, self.clock.step = self.clock.step, self.clock.step * 0
        with self.h.manager.open(self.tenant) as rt:
            live = state_digest(rt.service)
            learned = dict(rt.service.repo.supplier_websites)
        calls, broken = len(self.site.requests), SiteReader(fail=True)
        with TenantManager(self.h.store, self.h.objects, now=self.clock, strict_reads=True,
                           reader=broken).open(self.tenant) as rt:
            assert state_digest(rt.service) == live
            assert rt.service.repo.supplier_websites == learned
        assert broken.calls == [] and len(self.site.requests) == calls  # never signed in, fetched or read again
        self.clock.step = step


def test_the_worker_searches_edps_website_before_asking_and_closes_the_payment(tmp_path: Path) -> None:
    b = Business(tmp_path)
    # The daily sign-in to EDP's website: nothing there yet; the session is kept in the vault.
    first = b.worker.run_once()
    assert f"{b.tenant}/{WEBSITE}" in first.synced and b.site.signed_in() == 1
    assert b.vault.open(b.tenant, WEBSITE)["session"]["state"]["cookies"]
    assert b.supplier_line() == "0 documents · 0 payments · Invoices: on edp.pt"  # added under EDP's name

    tx = b.pay("2026-09-19", "64.10")
    b.site.invoices.append(SEPTEMBER)  # EDP publishes the September invoice after this morning's sign-in
    plan = b.get(f"/api/transactions/{tx}")["nextStep"]
    assert plan == ("I'm looking for the invoice for the €64.10 payment to EDP on 19 September in your email, your "
                    f"Google Drive and {ON_EDP}.")
    b.clock.advance(minutes=16)
    report = b.worker.run_once()
    assert (report.searches, report.found) == (1, 1)
    assert f"{b.tenant}/{WEBSITE}" not in report.synced  # the daily sync was not due: the search went there

    event = b.events("search.recorded")[-1].data
    assert [(a["source"], a["place"], a["outcome"], a["found"]) for a in event["attempts"]] == [
        ("current_email", "your email", "nothing", 0), ("historical_email", "your email", "nothing", 0),
        ("files", "your Google Drive", "nothing", 0), ("supplier_portal", ON_EDP, "found", 1)]
    assert not any(a.get("first") for a in event["attempts"])  # not yet known to hold EDP's invoices: in order
    [found] = event["files"]
    assert found["filename"] == "Fatura_FT_EDP2026_558120.pdf" and found["place"] == ON_EDP
    assert found["provenance"] == {
        "source": "portal", "provider": "edp_pt", "portalId": "9001558120", "number": "FT EDP2026/558120",
        "date": "2026-09-18", "connectionId": WEBSITE,
        "webUrl": "https://www.edp.pt/area-cliente/faturas/9001558120/pdf"}
    assert len(event["reads"]) == 1 and len(b.reader.calls) == 1  # read once, before the event was recorded
    assert b.site.signed_in() == 1 and b.site.downloads() == ["9001558120"]  # the saved session; only that PDF
    assert "9001558120" in b.vault.open(b.tenant, WEBSITE)["known"]  # the daily sync will not fetch it again

    detail = b.get(f"/api/transactions/{tx}")
    assert detail["status"] == "closed" and detail["headline"] == "Matched to the invoice."
    search = detail["search"]
    assert (search["summary"], search["foundIn"]) == (f"Found it in {ON_EDP}.", ON_EDP)
    assert [(p["place"], p["result"]) for p in search["places"]] == [
        ("your email", "Nothing there."), ("your Google Drive", "Nothing there."), (ON_EDP, "Found it.")]
    activity = json.dumps(b.get("/api/activity"), ensure_ascii=False)
    assert f"Found the invoice for the €64.10 payment to EDP in {ON_EDP}." in activity
    with b.h.manager.open(b.tenant) as rt:
        repo = rt.service.repo
        assert repo.chases == {} and not repo.outbox  # found before anyone was asked: no email to EDP
        doc = repo.documents[repo.transactions[tx].document_ids[0]]
        assert doc.document.invoice_number == "FT EDP2026/558120" and doc.document.quality is Quality.GREEN
        sighting = repo.registry.sightings(b.tenant, doc.evidence_ids[0])[0].context
        assert (sighting["found_by"], sighting["portalId"], sighting["source"]) == (
            "missing_document_search", "9001558120", "portal")
        learned = repo.supplier_websites["sup-edp"]
        assert (learned.how, learned.connection_id, learned.host) == ("found", WEBSITE, "edp.pt")
    assert b.supplier_line() == "1 document · 1 payment · Invoices: fetched from edp.pt"
    _plain(plan, search["summary"], b.supplier_line())
    b.same_after_replay()


def test_the_website_learned_to_hold_a_suppliers_invoices_is_searched_first_next_time(tmp_path: Path) -> None:
    b = Business(tmp_path, drive=False)
    b.site.invoices.append(AUGUST)
    b.worker.run_once()  # the daily sign-in fetches August's invoice: EDP's invoices are on EDP's website
    retrieved = b.events("portal.retrieved")[-1].data
    assert [d["portalId"] for d in retrieved["documents"]] == ["9001544002"]
    with b.h.manager.open(b.tenant) as rt:
        learned = rt.service.repo.supplier_websites["sup-edp"]
        assert (learned.how, learned.connection_id, learned.host) == ("retrieved", WEBSITE, "edp.pt")
        audit = [r.data() for r in rt.service.repo.audit_store.records(b.tenant)]
        assert [(e["subject_id"], e["extracted_values"]["how"]) for e in audit if e["action"] == "website_learned"] \
            == [("sup-edp", "named"), ("sup-edp", "retrieved")]
    assert b.supplier_line() == "1 document · 0 payments · Invoices: fetched from edp.pt"
    b.same_after_replay()  # what was learned comes back from the log alone

    tx = b.pay("2026-09-28", "31.98")
    b.site.invoices.append(GAS)
    assert b.get(f"/api/transactions/{tx}")["nextStep"] == (
        f"I'm looking for the invoice for the €31.98 payment to EDP on 28 September in {ON_EDP} and your email.")
    requests = b.read(lambda svc: svc.search_requests())
    assert [(r["connections"], r["first"]) for r in requests] == [([WEBSITE, MAILBOX], [WEBSITE])]
    b.clock.advance(minutes=16)
    report = b.worker.run_once()
    assert (report.searches, report.found) == (1, 1)
    attempts = b.events("search.recorded")[-1].data["attempts"]
    assert [(a["source"], a.get("first", False), a["outcome"]) for a in attempts] == [
        ("supplier_portal", True, "found"), ("current_email", False, "nothing"),
        ("historical_email", False, "nothing")]
    search = b.get(f"/api/transactions/{tx}")["search"]
    assert search["foundIn"] == ON_EDP and search["places"] == [
        {"place": ON_EDP, "result": "Found it.", "at": search["places"][0]["at"]},
        {"place": "your email", "result": "Not needed: found it earlier.", "at": search["places"][1]["at"]}]
    assert b.get(f"/api/transactions/{tx}")["status"] == "closed"
    with b.h.manager.open(b.tenant) as rt:
        assert rt.service.repo.chases == {}
    b.same_after_replay()


def test_when_the_website_does_not_have_it_the_supplier_is_asked(tmp_path: Path) -> None:
    b = Business(tmp_path, drive=False)
    b.site.invoices.append(AUGUST)
    b.worker.run_once()
    tx = b.pay("2026-09-19", "64.10")  # September's invoice is not on the website (nor in the mailbox)
    b.clock.advance(minutes=16)
    report = b.worker.run_once()
    assert (report.searches, report.found) == (1, 0)
    attempts = b.events("search.recorded")[-1].data["attempts"]
    assert [(a["source"], a["outcome"]) for a in attempts] == [
        ("supplier_portal", "nothing"), ("current_email", "nothing"), ("historical_email", "nothing")]
    assert b.site.downloads() == ["9001544002"]  # the August invoice was listed, not downloaded again
    detail = b.get(f"/api/transactions/{tx}")
    assert detail["search"]["summary"] == f"I searched {ON_EDP} and your email: it is not there."
    assert detail["nextStep"].startswith(f"I searched {ON_EDP} and your email: it is not there. I wrote to EDP "
                                         "asking for the invoice for the €64.10 payment on 19 September.")
    with b.h.manager.open(b.tenant) as rt:
        chase = rt.service.repo.chases[tx]
        assert chase.message.to == "faturas@edp.pt"
        assert ("Looked for the invoice for the €64.10 payment to EDP in your account on EDP's website and your "
                "email: it is not there.") in [a.text for a in rt.service.repo.activity]
    _plain(detail["nextStep"], detail["search"]["summary"])
    b.same_after_replay()


def test_a_search_never_sends_the_owner_a_code_nobody_asked_them_for(tmp_path: Path) -> None:
    b = Business(tmp_path, drive=False)
    b.worker.run_once()
    b.vault.update(b.tenant, WEBSITE, {"session": {}})  # no saved session left
    b.site.code = True  # the website now sends an SMS code at every sign-in
    tx = b.pay("2026-09-19", "64.10")
    b.site.invoices.append(SEPTEMBER)
    b.clock.advance(minutes=16)
    assert b.worker.run_once().searches == 1
    website = next(a for a in b.events("search.recorded")[-1].data["attempts"] if a["source"] == "supplier_portal")
    assert website["outcome"] == "failed" and website["failure"] == "TransientError:portal_mfa_required"
    # The code the website sent is asked for once, as the daily sign-in would: one push, one Needs-you item.
    assert b.site.codes == ["000001"] and b.events("portal.code_needed")[-1].data["channel"] == "sms"
    assert [(m["title"], m["body"]) for m in b.expo.sent] == [("Sign-in code needed", "EDP needs a sign-in code.")]
    (item,) = [i for i in b.get("/api/needs-you")["items"] if i["kind"] == "code"]
    assert item["code"]["submitPath"] == f"/api/portals/{WEBSITE}/code"
    assert b.get(f"/api/transactions/{tx}")["nextStep"] == (
        f"I'm looking for the invoice for the €64.10 payment to EDP on 19 September in your email and {ON_EDP}. "
        f"I couldn't reach {ON_EDP} yet, so I will look again shortly.")
    with b.h.manager.open(b.tenant) as rt:
        assert rt.service.repo.chases == {}  # not asked while a place could not be searched

    for _ in range(2):  # looked at again later: a website that sends codes is only read with a saved session
        b.clock.advance(hours=6, minutes=30)
        assert b.worker.run_once().searches == 1
        website = next(a for a in b.events("search.recorded")[-1].data["attempts"]
                       if a["source"] == "supplier_portal")
        assert website["failure"] == "TransientError:portal_waiting_for_code"
    assert b.site.codes == ["000001"] and len(b.expo.sent) == 1  # no second code, no second push
    with b.h.manager.open(b.tenant) as rt:  # after the third round the supplier is asked all the same
        assert rt.service.repo.chases[tx].message.to == "faturas@edp.pt"
    b.same_after_replay()


# =========================================================================== learning in the engine (L6)


def _link_email(url: str, *, attachment: bytes | None = None, sender: str = "EDP Comercial <faturas@edp.pt>",
                mid: str = "<fatura-0918@edp.pt>") -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "ana@padaria.pt", "A sua fatura EDP de setembro"
    m["Message-ID"], m["Date"] = mid, "Fri, 18 Sep 2026 10:00:00 +0100"
    m.set_content(f"A sua fatura de setembro está disponível.\nVer fatura: {url}\n")
    m.add_alternative("<p>A sua fatura de setembro está disponível.</p>"
                      f'<p><a class="botao" href="{url}">Ver fatura</a></p>'
                      '<p><a href="https://www.edp.pt/privacidade">Privacidade</a></p>', subtype="html")
    if attachment is not None:
        m.add_attachment(attachment, maintype="application", subtype="pdf", filename="fatura.pdf")
    return m.as_bytes()


def _supplier_line(svc: Any, name: str = "EDP") -> str:
    groups = _ok(svc, "GET", "/api/sources")["groups"]
    return next(i for g in groups if g["id"] == "suppliers" for i in g["items"] if i["name"] == name)["detail"]


def test_an_invoice_email_that_only_links_to_edps_website_teaches_where_edps_invoices_are() -> None:
    svc, _, tx = _business(drive=False)
    svc.sync_mail(MAILBOX, [_link_email("https://www.edp.pt/area-cliente/faturas/9001558120/pdf")], None)
    learned = svc.repo.supplier_websites["sup-edp"]
    assert (learned.how, learned.host, learned.connection_id) == ("email_link", "edp.pt", None)
    assert _supplier_line(svc) == "0 documents · 1 payment · Invoices: on edp.pt"
    # Not connected yet: the mailbox is all there is to search.
    assert [(r["connections"], r["first"]) for r in svc.search_requests()] == [([MAILBOX], [])]

    # The owner adds EDP's website: the email showed EDP's invoices are there, so it is searched first.
    added = _ok(svc, "POST", "/api/sources", {"kind": "portal", "supplier": "EDP", "username": EMAIL,
                                              "password": PASSWORD})
    assert added["id"] == WEBSITE and svc.repo.connectors[WEBSITE].hosts == ("edp.pt",)
    assert svc.sign_in[WEBSITE]["portal"] == "edp_pt"
    learned = svc.repo.supplier_websites["sup-edp"]
    assert (learned.how, learned.host, learned.connection_id) == ("email_link", "edp.pt", WEBSITE)
    assert [(r["connections"], r["first"]) for r in svc.search_requests()] == [([WEBSITE, MAILBOX], [WEBSITE])]
    assert svc.transaction(tx)["nextStep"] == ("I'm looking for the invoice for the €64.10 payment to EDP on 19 "
                                               f"September in {ON_EDP} and your email.")
    _plain(_supplier_line(svc), svc.transaction(tx)["nextStep"])


def test_only_a_link_to_the_suppliers_own_known_website_teaches_anything() -> None:
    attached, _, _ = _business(drive=False)  # the invoice came attached: the link is not where they live
    attached.sync_mail(MAILBOX, [_link_email("https://www.edp.pt/area-cliente/faturas/1/pdf",
                                             attachment=b"%PDF-1.7 fatura")], None)
    unknown, _, _ = _business(drive=False)  # a link to a website the back office does not know
    unknown.sync_mail(MAILBOX, [_link_email("https://faturas.example.com/edp/558120")], None)
    stranger, _, _ = _business(drive=False)  # someone else's email pointing at EDP's website
    stranger.sync_mail(MAILBOX, [_link_email("https://www.edp.pt/area-cliente/faturas/1/pdf",
                                             sender="Newsletter <news@promo.example>", mid="<n1@promo.example>")],
                       None)
    for svc in (attached, unknown, stranger):
        assert svc.repo.supplier_websites == {}
        assert "Invoices:" not in _supplier_line(svc)


def test_a_website_added_under_a_suppliers_name_is_searched_in_order_until_its_invoices_are_found_there() -> None:
    svc, _, tx = _business(drive=False)
    _ok(svc, "POST", "/api/sources", {"kind": "portal", "supplier": "Vodafone", "username": EMAIL,
                                      "password": "other"})
    _ok(svc, "POST", "/api/sources", {"kind": "portal", "supplier": "EDP", "username": EMAIL, "password": PASSWORD})
    learned = svc.repo.supplier_websites["sup-edp"]
    assert (learned.how, learned.connection_id) == ("named", WEBSITE)
    assert _supplier_line(svc) == "0 documents · 1 payment · Invoices: on edp.pt"
    # EDP's website is searched (Vodafone's is not: it holds Vodafone's), after the mailbox: nothing seen there yet.
    assert [(r["connections"], r["first"]) for r in svc.search_requests()] == [([MAILBOX, WEBSITE], [])]
    found = {"data": SEPTEMBER.text.encode(), "source": "supplier_portal", "place": ON_EDP,
             "filename": "Fatura_FT_EDP2026_558120.txt", "contentType": "text/plain",
             "provenance": {"source": "portal", "provider": "edp_pt", "portalId": "9001558120",
                            "number": "FT EDP2026/558120", "date": "2026-09-18", "connectionId": WEBSITE}}
    result = svc.record_search(tx, 1, [_attempt("current_email", "your email", "nothing"),
                                       _attempt("historical_email", "your email", "nothing"),
                                       _attempt("supplier_portal", ON_EDP, "found", 1)], [found])
    assert result["status"] == "found" and svc.transaction(tx)["status"] == "closed"
    learned = svc.repo.supplier_websites["sup-edp"]
    assert (learned.how, learned.connection_id, learned.host) == ("found", WEBSITE, "edp.pt")
    assert _supplier_line(svc) == "1 document · 1 payment · Invoices: fetched from edp.pt"
    # The next EDP payment without its invoice: EDP's website first.
    csv_rows = (f"date,amount,counterparty,account,description,kind\n"
                f"2026-09-28,-31.98,EDP,{svc.repo.transactions[tx].tx.account_id},DD EDP,direct_debit\n").encode()
    _ok(svc, "POST", "/api/evidence", {"filename": "extrato2.csv", "contentType": "text/csv",
                                       "dataBase64": base64.b64encode(csv_rows).decode()})
    assert [(r["connections"], r["first"]) for r in svc.search_requests()] == [([WEBSITE, MAILBOX], [WEBSITE])]
    # A website that stopped working is not searched (nor waited for).
    svc.repo.connectors[WEBSITE].healthy = False
    assert [(r["connections"], r["first"]) for r in svc.search_requests()] == [([MAILBOX], [])]


def test_a_learned_place_is_asked_first_live_and_when_the_record_is_applied() -> None:
    class Place:
        def __init__(self, source: SearchSource, place: str, first: bool = False) -> None:
            self.source, self.place, self.first, self.asked = source, place, first, 0

        def find(self, request: SearchRequest) -> list[FoundFile]:
            self.asked += 1
            return []

    request = SearchRequest.from_json({"subjectId": "tx_1", "kind": "payment", "round": 1, "amount": "64.10",
                                       "date": "2026-09-19", "windowStart": "2026-08-05", "windowEnd": "2026-10-04",
                                       "counterparty": "EDP", "first": [WEBSITE]})
    assert request.first == (WEBSITE,) and SearchRequest.from_json(request.to_json()) == request
    places = [Place(SearchSource.FILES, "your Google Drive"), Place(SearchSource.CURRENT_EMAIL, "your email"),
              Place(SearchSource.SUPPLIER_PORTAL, ON_EDP, first=True)]
    ticks = iter(datetime(2026, 10, 2, 8, 0, s, tzinfo=timezone.utc) for s in range(30))
    run = run_searches(request, places, clock=lambda: next(ticks))
    assert [(a["source"], a.get("first")) for a in run.attempts] == [
        ("supplier_portal", True), ("current_email", None), ("files", None)]

    class Recorded:
        def __init__(self, source: SearchSource, hit: bool) -> None:
            self.source, self.hit = source, hit

        async def search(self, query: Any) -> list[EvidenceCandidate]:
            return [EvidenceCandidate(evidence_id=f"ev_{self.source.value}", document_id=f"doc_{self.source.value}",
                                      invoice_number="FT 1", quality=Quality.GREEN)] if self.hit else []

    from backoffice.domain.models import LegalEntity

    company = LegalEntity(id="c1", tenant_id="t1", name="Padaria Lda", tax_id="516123459", country="PT")
    query = request.evidence_query("t1")
    searches = [Recorded(SearchSource.CURRENT_EMAIL, True), Recorded(SearchSource.SUPPLIER_PORTAL, True)]
    spec = run_recorded(MissingEvidenceAutopilot(searches, authorize=lambda r: False, timeout_seconds=None).run(
        query, company=company, supplier=None, today=date(2026, 10, 2)))
    learned = run_recorded(MissingEvidenceAutopilot(searches, authorize=lambda r: False, timeout_seconds=None,
                                                    first=(SearchSource.SUPPLIER_PORTAL,)).run(
        query, company=company, supplier=None, today=date(2026, 10, 2)))
    assert spec.found.source is SearchSource.CURRENT_EMAIL  # the spec's order when nothing is learned
    assert learned.found.source is SearchSource.SUPPLIER_PORTAL and len(learned.attempts) == 1
