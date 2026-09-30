"""Accountants per company, clients invited by their accountant, and the accountant's client view.

Checklist O5 (case 50): each company can have its own accountant, the business's accountant being the
default, and everything accountant-facing follows it; in production an accountant membership can be
limited to some companies and every read is filtered to them.
Checklist X36 (case 22, §29): an accountant invites a client business by email; the invited owner
accepts and the accountant gets a (company-limited) membership.
Checklist N1, N4, N5: the accountant's home and client view (evidence links, reconciliation, open items,
"needs accountant" counting what only the accountant decides).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from _server_support import NIF_A, NIF_B, NIF_C, PASSWORD, b64, bearer, build_business, harness, signup

from backoffice.demo import evidence as E
from backoffice.invitations import HEADLINE
from backoffice.service import BackOfficeService

CARLA = "carla@contascarla.pt"


def get(s: BackOfficeService, path: str) -> dict[str, Any]:
    status, body = s.dispatch("GET", path, None)
    assert status == 200, (path, body)
    json.dumps(body)
    return body


def post(s: BackOfficeService, path: str, body: Any) -> tuple[int, dict[str, Any]]:
    return s.dispatch("POST", path, body)


@pytest.fixture
def demo() -> BackOfficeService:
    return BackOfficeService.demo()


class FakeMailer:
    """A transport: accepts (records) or refuses every message."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[list[str], str, str]] = []

    def send(self, to: list[str], subject: str, body: str, files: list[Any]) -> None:
        if self.fail:
            raise OSError("the mail server refused the connection")
        self.sent.append((list(to), subject, body))


# =========================================================================== one accountant per company (O5)


def test_each_company_can_have_its_own_accountant_and_the_business_one_is_the_default(demo: BackOfficeService) -> None:
    status, out = post(demo, "/api/settings/accountant", {"companyId": "company-c", "email": "Carla@ContasCarla.pt",
                                                          "name": "Carla Reis", "firm": "Contas Carla",
                                                          "software": "Moloni"})
    assert status == 200 and out["message"] == f"Done. I will send Company C’s closed months to {CARLA}."
    settings = get(demo, "/api/settings/accountant")
    assert settings["default"]["email"] == E.ACCOUNTANT_EMAIL
    by = {c["companyId"]: c for c in settings["companies"]}
    assert by["company-c"]["own"] and by["company-c"]["accountant"] == {
        "email": CARLA, "name": "Carla Reis", "firm": "Contas Carla", "software": "Moloni"}
    assert not by["hazel-tree"]["own"] and by["hazel-tree"]["accountant"]["email"] == E.ACCOUNTANT_EMAIL
    # One accountant source each, covering the companies they look after.
    sources = {c.account: c.company_ids for c in demo.repo.connectors.values() if c.kind == "accountant"}
    assert sources == {E.ACCOUNTANT_EMAIL: ("hazel-tree", "company-b"), CARLA: ("company-c",)}
    group = next(g for g in get(demo, "/api/sources")["groups"] if g["id"] == "accountant")
    assert {i["detail"] for i in group["items"]} == {E.ACCOUNTANT_EMAIL, CARLA}
    # The accountant view, the month and the monthly package follow each company's accountant.
    c = get(demo, "/api/accountant/clients/company-c")
    assert (c["accountant"]["firm"], c["software"]) == ("Contas Carla", "Moloni")
    assert "Moloni" in c["exportState"]["note"]
    assert get(demo, "/api/accountant/clients/hazel-tree")["software"] == "TOConline"
    remaining = get(demo, "/api/months/company-c/2026-09")["remaining"]
    assert remaining[-1]["text"] == "Once those are done, I will send the month to Contas Carla."
    recipients = {r["email"]: r for r in get(demo, "/api/settings/report")["recipients"]}
    assert recipients[CARLA]["companies"] == ["company-c"]
    assert recipients[E.ACCOUNTANT_EMAIL]["companies"] == ["hazel-tree", "company-b"]
    # Back to the business's accountant.
    status, out = post(demo, "/api/settings/accountant", {"companyId": "company-c", "useDefault": True})
    assert status == 200 and out["message"] == f"Done. Company C uses {E.ACCOUNTANT_EMAIL} again."
    assert get(demo, "/api/accountant/clients/company-c")["software"] == "TOConline"
    assert [c.id for c in demo.repo.connectors.values() if c.kind == "accountant"] == ["accountant"]
    assert post(demo, "/api/settings/accountant", {"companyId": "nope", "email": CARLA})[0] == 400
    assert post(demo, "/api/settings/accountant", {"companyId": "company-c", "email": "not an email"})[0] == 400


def test_a_new_company_joins_the_business_accountant_not_another_companys() -> None:
    from datetime import datetime

    from backoffice.orchestrator import TZ

    svc = BackOfficeService.new_tenant("t-acct", owner_name="Ana Silva", owner_email="ana@example.pt",
                                       now=datetime(2026, 10, 2, 9, 30, tzinfo=TZ))
    svc.add_company("Padaria", NIF_A)
    svc.set_accountant("marc@vidal.pt", "Marc Vidal")
    svc.set_accountant(CARLA, "Carla Reis", company_id="padaria")
    svc.add_company("Studio Two", NIF_B)
    assert svc.repo.accountant_for("studio-two").email == "marc@vidal.pt"
    assert svc.repo.accountant_for("padaria").email == CARLA
    sources = {c.account: c.company_ids for c in svc.repo.connectors.values() if c.kind == "accountant"}
    assert sources == {"marc@vidal.pt": ("studio-two",), CARLA: ("padaria",)}


def test_accountant_questions_are_filed_under_the_asking_accountants_company(demo: BackOfficeService) -> None:
    post(demo, "/api/settings/accountant", {"companyId": "company-c", "email": CARLA, "name": "Carla Reis"})
    # Carla asks about Hazel Tree's €1,200 rent: she only looks after Company C, so the question is hers,
    # under Company C, and it is not answered with Hazel Tree's evidence.
    raw = E.email(sender=f"Carla Reis <{CARLA}>", subject="September", at=demo._now(), message_id="<q1@carla.pt>",
                  text="Hi Laura,\nIs the €1,200 transfer to Marta Gonçalves on 1 September the studio rent?\n")
    report = demo.orchestrator.ingest_file(raw, filename="q.eml", content_type="message/rfc822", origin="email")
    [qid] = report.question_ids
    q = demo.repo.accountant_questions[qid]
    assert (q.company_id, q.status, q.accountant_id) == ("company-c", "waiting", "acct-carla-contascarla-pt")
    assert demo.orchestrator.accountant.asker(q).email == CARLA  # an answer would go back to Carla, not Marc
    assert qid in {x["id"] for x in get(demo, "/api/accountant/clients/company-c")["questions"]}
    assert qid not in {x["id"] for x in get(demo, "/api/accountant/clients/hazel-tree")["questions"]}
    # Marc's own questions stay with his companies (the rent one answered from Hazel Tree's evidence).
    marc = [x for x in demo.repo.accountant_questions.values() if x.accountant_id == "acct-vidal"]
    assert {x.company_id for x in marc} <= {"hazel-tree", "company-b", "company-c"}
    # An email from someone who is nobody's accountant is not an accountant question.
    stranger = E.email(sender="someone@else.pt", subject="?", at=demo._now(), message_id="<q2@else.pt>",
                       text="Is the €1,200 transfer to Marta Gonçalves the studio rent?\n")
    assert demo.orchestrator.ingest_file(stranger, filename="s.eml", content_type="message/rfc822",
                                         origin="email").question_ids == []


def test_accountant_rules_stay_with_the_company_and_its_accountant(demo: BackOfficeService) -> None:
    post(demo, "/api/settings/accountant", {"companyId": "company-b", "email": CARLA, "firm": "Contas Carla"})
    status, out = post(demo, "/api/accountant/clients/company-b/rules", {"text": "Treat all Uber as Marketing"})
    assert status == 200 and out["affected"] == 1 and out["rule"]["companyIds"] == ["company-b"]
    assert out["message"] == "Done. Treat all Uber costs as Marketing for Company B. It applies to 1 payment so far."
    # Marc's rule "for all his clients" covers his companies only: Company B is Carla's now.
    status, out = post(demo, "/api/accountant/rules", {"text": "Treat all Uber as Staff training", "scope": "all"})
    assert status == 200 and out["affected"] == 1
    found = demo.chat_tool({"name": "find_payments", "input": {"supplier": "Uber"}})["result"]["payments"]
    assert {(p["company"], p["category"]) for p in found} == {("Hazel Tree", "Staff training"),
                                                               ("Company B", "Marketing")}
    assert [r["label"] for r in get(demo, "/api/accountant/clients/company-b")["rules"]] == \
        ["Treat all Uber costs as Marketing"]
    assert [r["label"] for r in get(demo, "/api/accountant/clients/hazel-tree")["rules"]] == \
        ["Treat all Uber costs as Staff training"]
    assert post(demo, "/api/accountant/clients/nope/rules", {"text": "Treat all Uber as Travel"})[0] == 404


def test_scoped_reads_never_show_another_companys_documents_payments_evidence_or_answers(
        demo: BackOfficeService) -> None:
    """The engine's filter for an accountant of Company B only, over the demo's three companies."""
    repo = demo.repo
    index = demo._evidence_index()
    a_docs = [d.id for d in repo.documents.values() if demo._document_company(d) == "hazel-tree"]
    a_txs = [r.id for r in repo.transactions.values() if r.company_id == "hazel-tree"]
    a_evidence = [e for e, owners in index.items() if "hazel-tree" in owners]
    answers = [q.answer for q in repo.accountant_questions.values() if q.company_id == "hazel-tree" and q.answer]
    assert a_docs and a_txs and a_evidence and answers
    marks = {"hazel-tree", *a_docs, *a_txs, *a_evidence, *answers,
             *(q.id for q in repo.accountant_questions.values() if q.company_id == "hazel-tree")}
    report = demo.chat_tool({"name": "period_report", "input": {"date_from": "2026-09-01",
                                                                "date_to": "2026-09-30"}})["result"]
    a = "hazel-tree"
    paths = ["/healthz", "/api/home", "/api/needs-you", "/api/activity", "/api/companies", f"/api/companies/{a}",
             f"/api/months/{a}/2026-09", "/api/sources", "/api/chat/tools", "/api/tasks",
             f"/api/reports/{report['id']}/file", "/api/documents", f"/api/documents?company={a}",
             *(f"/api/documents/{d}/file" for d in a_docs), *(f"/api/documents/{d}" for d in a_docs),
             *(f"/api/transactions/{t}" for t in a_txs), "/api/obligations", f"/api/companies/{a}/cost-centers",
             "/api/cost-centers/cc_hazel", "/api/cost-centers/cc_hazel/statement",
             "/api/settings/report", "/api/settings/accountant", "/api/settings/mailboxes", "/api/onboarding",
             "/api/settings/automation",
             "/api/accountant/api-keys", "/api/connections", "/api/accountant/clients", f"/api/accountant/clients/{a}",
             f"/api/accountant/clients/{a}/export", *(f"/api/accountant/clients/{a}/evidence/{e}/file" for e in a_evidence),
             *(f"/api/accountant/clients/company-b/evidence/{e}/file" for e in a_evidence),
             *(f"/api/evidence/{e}/file" for e in a_evidence), "/api/accountant/invitations", "/api/audit",
             "/api/pipeline", "/api/internal/overview", "/api/internal/operations", "/api/internal/readiness",
             # Employee cards and staff expenses (backoffice.staff): never an accountant's.
             "/api/employees", "/api/expense-claims", "/api/employee/card-payments",
             # Who opened sensitive documents (backoffice.sensitivity): the owner's only.
             "/api/documents/access-log",
             # The plan (backoffice.billing): the owner's only.
             "/api/billing"]
    allowed = {"company-b"}
    for p in paths:
        status, body = demo.dispatch_scoped("GET", p, None, allowed)
        assert status in (200, 403, 404), (p, status, body)
        if status == 200:
            text = json.dumps(body, ensure_ascii=False)
            assert not [m for m in marks if m in text], p
    for verb, pattern, _ in demo._routes():
        if verb == "GET":
            assert any(pattern.fullmatch(p.split("?", 1)[0]) for p in paths), pattern.pattern
    # Company B's own data is all there.
    status, body = demo.dispatch_scoped("GET", "/api/accountant/clients/company-b", None, allowed)
    assert status == 200 and body["reconciliation"] and body["evidenceLinks"]
    for link in body["evidenceLinks"]:
        assert demo.dispatch_scoped("GET", link["href"], None, allowed)[0] == 200


# =========================================================================== the accountant's view (N1, N4, N5)


def test_needs_accountant_counts_what_only_the_accountant_decides(demo: BackOfficeService) -> None:
    rows = {r["id"]: r for r in get(demo, "/api/accountant/clients")["clients"]}
    assert rows["hazel-tree"]["needsAccountant"] == 1  # the rent tax flag: the accountant's call
    assert rows["company-c"]["needsAccountant"] == 0  # the accountant's own IKEA question waits for the owner
    assert rows["company-b"]["needsAccountant"] == 0
    detail = get(demo, "/api/accountant/clients/hazel-tree")
    assert detail["needsAccountant"] == len(detail["taxFlags"]) == 1
    waiting = [q for q in get(demo, "/api/accountant/clients/company-c")["questions"] if q["status"] == "waiting"]
    assert waiting and rows["company-c"]["needsAccountant"] == 0


def test_client_view_shows_evidence_links_and_the_reconciliation(demo: BackOfficeService) -> None:
    detail = get(demo, "/api/accountant/clients/hazel-tree")
    recon = {r["payee"]: r for r in detail["reconciliation"]}
    vodafone = recon["Vodafone"]
    assert vodafone["status"] == "closed" and vodafone["statusLabel"] == "Matched" and vodafone["tone"] == "good"
    assert "Invoice total €92.40" in vodafone["why"] and "Bank charge €92.40" in vodafone["why"]
    [doc] = vodafone["documents"]
    assert doc["label"].startswith("Invoice FT VF2026/1183") and doc["href"].startswith(
        "/api/accountant/clients/hazel-tree/evidence/ev_")
    # The open item: EDP's invoice is missing, with what I'm doing about it.
    edp = recon["EDP"]
    assert (edp["status"], edp["statusLabel"], edp["tone"]) == ("open", "Document missing", "attention")
    assert edp["why"] and edp["why"][0].startswith("I asked EDP for the invoice")
    assert [m["payee"] for m in detail["missingDocuments"]] == ["EDP"]
    assert detail["openReasons"] and recon["Autoridade Tributaria"]["statusLabel"] == "Matched to the tax letter"
    # Every evidence link opens the original, byte for byte.
    links = detail["evidenceLinks"]
    assert links and all(x["href"] == f"/api/accountant/clients/hazel-tree/evidence/{x['id']}/file" for x in links)
    for x in links:
        f = get(demo, x["href"])
        ev = demo.repo.registry.get(demo.repo.tenant_id, x["id"])
        assert hashlib.sha256(base64.b64decode(f["data"])).hexdigest() == ev.sha256
    assert {x["kind"] for x in links} >= {"payment", "document"}
    # Answered questions link the accountant's email and the proof used in the answer.
    answered = next(q for q in detail["questions"] if q["status"] == "answered")
    assert answered["evidence"][0]["label"].startswith("Your email") and len(answered["evidence"]) == 3
    # Another company's evidence is not this client's.
    other = get(demo, "/api/accountant/clients/company-b")["evidenceLinks"][0]["id"]
    assert demo.dispatch("GET", f"/api/accountant/clients/hazel-tree/evidence/{other}/file", None)[0] == 404
    assert demo.dispatch("GET", "/api/accountant/clients/hazel-tree/evidence/ev_nope/file", None)[0] == 404
    assert get(demo, f"/api/evidence/{other}/file")["data"]  # the owner opens any of their originals
    # The month's documents for the accountant's software, as a ZIP.
    export = get(demo, detail["links"]["export"])
    assert export["contentType"] == "application/zip" and export["count"] >= 3


# =========================================================================== invitations in the demo (X36)


def test_demo_invitation_is_recorded_and_sent_through_the_simulated_outbox(demo: BackOfficeService) -> None:
    status, out = post(demo, "/api/accountant/invitations", {"email": "Rui@Oficina.pt", "clientName": "Oficina Rui",
                                                             "taxIds": ["501 234 560"]})
    assert status == 200 and out["message"] == \
        "Done. The invitation to rui@oficina.pt is in the outbox. This demo does not send real email."
    inv = out["invitation"]
    assert (inv["email"], inv["taxIds"], inv["status"]) == ("rui@oficina.pt", ["501234560"], "demo_outbox")
    assert "token" not in json.dumps(out).lower().replace("tokenhash", "")
    msg = demo.assistant.outbox[inv["messageId"]]
    assert msg.to == ["rui@oficina.pt"] and msg.subject == HEADLINE and HEADLINE in msg.body
    assert msg.status == "sent" and "not connected in this demo" in msg.delivery
    assert [i["id"] for i in get(demo, "/api/accountant/invitations")["invitations"]] == [inv["id"]]
    stored = demo._invitations()[inv["id"]]
    assert re.fullmatch(r"[0-9a-f]{64}", stored["tokenHash"])  # only the token's hash is kept
    assert post(demo, "/api/accountant/invitations", {"email": "nope"})[0] == 400
    assert post(demo, "/api/accountant/invitations", {"email": "a@b.pt", "taxIds": ["123"]})[0] == 400


def test_an_invitation_counts_as_sent_only_when_the_transport_accepted_it(demo: BackOfficeService) -> None:
    demo.mailer = FakeMailer()
    status, out = post(demo, "/api/accountant/invitations", {"email": "rui@oficina.pt"})
    assert status == 200 and out["invitation"]["status"] == "sent" and out["message"].startswith("Done. I sent")
    assert demo.mailer.sent[0][0] == ["rui@oficina.pt"] and HEADLINE in demo.mailer.sent[0][2]
    demo.mailer = FakeMailer(fail=True)
    before = (len(demo._invitations()), len(demo.assistant.outbox))
    status, out = post(demo, "/api/accountant/invitations", {"email": "joana@loja.pt"})
    assert status == 502 and out["message"].startswith("I couldn't send the invitation email")
    assert (len(demo._invitations()), len(demo.assistant.outbox)) == before  # nothing claims it was sent


def test_without_a_transport_an_invitation_is_written_and_waits(demo: BackOfficeService) -> None:
    demo.mailer = None
    status, out = post(demo, "/api/accountant/invitations", {"email": "rui@oficina.pt"})
    assert status == 200 and out["ok"] is False and out["invitation"]["status"] == "waiting"
    assert "not sent" in out["message"]
    msg = demo.assistant.outbox[out["invitation"]["messageId"]]
    assert (msg.status, msg.sent_at) == ("waiting", None)
    # Sent later through the send path: only then does it count as sent.
    demo.mailer = FakeMailer()
    assert demo.send_waiting(msg.id)["sent"] is True
    assert get(demo, "/api/accountant/invitations")["invitations"][0]["status"] == "sent"


# =========================================================================== production: invitations (X36)


def _token(mailer: FakeMailer) -> str:
    found = re.search(r"/invite#([A-Za-z0-9_-]{32,128})", mailer.sent[-1][2])
    assert found, mailer.sent[-1][2]
    return found.group(1)


def _invite(h: Any, token: str, email: str, **extra: Any) -> dict[str, Any]:
    res = h.client.post("/api/accountant/invitations", json={"email": email, **extra}, headers=bearer(token))
    assert res.status_code == 200, res.text
    return res.json()


def test_accepting_an_invitation_grants_a_company_limited_accountant_membership(tmp_path: Path) -> None:
    mailer = FakeMailer()
    h = harness(tmp_path, mailer=mailer)
    carla = signup(h.client, CARLA, company="Contas Carla", tax_id=NIF_C, name="Carla Reis")
    out = _invite(h, carla["token"], "ana@example.pt", clientName="Padaria", taxIds=[NIF_B])
    assert out["message"] == "Done. I sent the invitation to ana@example.pt." and out["invitation"]["status"] == "sent"
    [(to, subject, body)] = mailer.sent
    assert to == ["ana@example.pt"] and subject == HEADLINE and "Carla Reis (Contas Carla)" in body
    token = _token(mailer)
    assert token not in json.dumps(out)  # the token is only in the email
    listed = h.client.get("/api/accountant/invitations", headers=bearer(carla["token"])).json()["invitations"]
    assert [(i["email"], i["status"]) for i in listed] == [("ana@example.pt", "sent")]

    ana = signup(h.client, "ana@example.pt")  # the invited owner signs up (Padaria Lda, NIF A) ...
    A = bearer(ana["token"])
    assert h.client.post("/api/onboarding/company", json={"name": "Second Company", "taxId": NIF_B},
                         headers=A).status_code == 200
    res = h.client.post("/api/invitations/accept", json={"token": token}, headers=A)  # ... and accepts
    assert res.status_code == 200, res.text
    assert res.json()["companies"] == ["second-company"]
    assert res.json()["message"] == "Done. Contas Carla is now your accountant and can see the companies you chose."
    tenant = ana["tenant"]["id"]
    assert h.store.membership_companies(tenant, carla["user"]["id"]) == ("second-company",)
    # The engine knows Carla is Second Company's accountant (Padaria has none): her questions, rules and package.
    settings = h.client.get("/api/settings/accountant", headers=A).json()
    by = {c["companyId"]: c for c in settings["companies"]}
    assert by["second-company"]["own"] and by["second-company"]["accountant"]["email"] == CARLA
    assert by["padaria-lda"]["accountant"] is None and settings["default"] is None
    # Carla's accountant home lists her client's company, by business, and opens it.
    C = bearer(carla["token"])
    rows = h.client.get("/api/accountant/clients", headers=C).json()["clients"]
    assert [(r["id"], r["name"]) for r in rows] == [(f"{tenant}~second-company", "Second Company")]
    assert "business" not in rows[0]  # the business is named after its first company, which is not hers
    view = h.client.get(f"/api/accountant/clients/{tenant}~second-company", headers=C)
    assert view.status_code == 200 and view.json()["id"] == f"{tenant}~second-company"
    assert view.json()["links"]["rules"] == f"/api/accountant/clients/{tenant}~second-company/rules"
    rule = h.client.post(view.json()["links"]["rules"], json={"text": "Treat all Adobe subscriptions as Software"},
                         headers=C)
    assert rule.status_code == 200 and rule.json()["rule"]["companyIds"] == ["second-company"]
    assert h.client.get(f"/api/accountant/clients/{tenant}~padaria-lda", headers=C).status_code == 404
    # The tenant rebuilt from its events says the same.
    h.manager.evict(tenant)
    assert h.client.get("/api/settings/accountant", headers=A).json() == settings


def test_an_invitation_works_once(tmp_path: Path) -> None:
    mailer = FakeMailer()
    h = harness(tmp_path, mailer=mailer)
    carla = signup(h.client, CARLA, company="Contas Carla", tax_id=NIF_C, name="Carla Reis")
    _invite(h, carla["token"], "ana@example.pt")
    token = _token(mailer)
    A = bearer(signup(h.client, "ana@example.pt")["token"])
    first = h.client.post("/api/invitations/accept", json={"token": token}, headers=A)
    assert first.status_code == 200 and first.json()["companies"] == []  # every company
    again = h.client.post("/api/invitations/accept", json={"token": token}, headers=A)
    assert (again.status_code, again.json()["message"]) == (409, "This invitation was already used.")
    listed = h.client.get("/api/accountant/invitations", headers=bearer(carla["token"])).json()["invitations"]
    assert listed[0]["status"] == "accepted"


def test_an_invitation_expires(tmp_path: Path) -> None:
    mailer = FakeMailer()
    h = harness(tmp_path, mailer=mailer)
    carla = signup(h.client, CARLA, company="Contas Carla", tax_id=NIF_C, name="Carla Reis")
    _invite(h, carla["token"], "ana@example.pt")
    token = _token(mailer)
    A = bearer(signup(h.client, "ana@example.pt")["token"])
    h.clock.advance(days=15)
    res = h.client.post("/api/invitations/accept", json={"token": token}, headers=A)
    assert (res.status_code, res.json()["message"]) == (
        410, "This invitation has expired. Ask your accountant to send a new one.")
    assert h.store.membership_companies(signup_tenant(h, "ana@example.pt"), carla["user"]["id"]) is None
    listed = h.client.get("/api/accountant/invitations", headers=bearer(carla["token"])).json()["invitations"]
    assert listed[0]["status"] == "expired"


def signup_tenant(h: Any, email: str) -> str:
    user = next(u for u in h.store._d.users.values() if u.email == email)
    return h.store.memberships(user.id)[0][0].id


def test_an_invitation_is_refused_to_another_account_and_to_a_wrong_token(tmp_path: Path) -> None:
    mailer = FakeMailer()
    h = harness(tmp_path, mailer=mailer)
    carla = signup(h.client, CARLA, company="Contas Carla", tax_id=NIF_C, name="Carla Reis")
    _invite(h, carla["token"], "ana@example.pt")
    token = _token(mailer)
    rui = bearer(signup(h.client, "rui@oficina.pt", company="Oficina Rui", tax_id=NIF_B, name="Rui")["token"])
    res = h.client.post("/api/invitations/accept", json={"token": token}, headers=rui)
    assert (res.status_code, res.json()["message"]) == (
        403, "This invitation was sent to another email address. Sign in with that address to accept it.")
    bad = h.client.post("/api/invitations/accept", json={"token": "x" * 43}, headers=rui)
    assert (bad.status_code, bad.json()["message"]) == (
        404, "This invitation link is not valid. Ask your accountant to send a new one.")
    # Only an owner accepts: an accountant's session cannot grant access to the business it reads.
    ana = signup(h.client, "ana@example.pt")
    joao = signup(h.client, "joao@contas.pt", company="Contas Joao", tax_id=None, name="Joao")
    h.store._d.memberships.discard((joao["tenant"]["id"], joao["user"]["id"], "owner"))
    h.store.add_membership(ana["tenant"]["id"], joao["user"]["id"], "accountant")
    J = bearer(h.client.post("/api/auth/login", json={"email": "joao@contas.pt", "password": PASSWORD}).json()["token"])
    assert h.client.post("/api/invitations/accept", json={"token": token}, headers=J).status_code == 403
    h.client.cookies.clear()
    assert h.client.post("/api/invitations/accept", json={"token": token}).status_code == 401
    # Still usable by the right account afterwards.
    A = bearer(ana["token"])
    assert h.client.post("/api/invitations/accept", json={"token": token}, headers=A).status_code == 200


def test_the_token_is_hashed_at_rest_and_never_recorded(tmp_path: Path) -> None:
    mailer = FakeMailer()
    h = harness(tmp_path, mailer=mailer)
    carla = signup(h.client, CARLA, company="Contas Carla", tax_id=NIF_C, name="Carla Reis")
    _invite(h, carla["token"], "ana@example.pt")
    token = _token(mailer)
    digest = hashlib.sha256(token.encode()).hexdigest()
    found = h.store.invitation(digest)
    assert found is not None and found.token_hash == digest and found.sent_at is not None
    assert token not in repr(h.store._d.invitations)
    for tenant in h.store.tenant_ids():
        assert all(token not in e.body for e in h.store.events(tenant))
    assert (found.expires_at - found.created_at).days == 14


def test_an_invitation_is_not_sent_without_a_transport(tmp_path: Path) -> None:
    h = harness(tmp_path)  # no mailer configured
    carla = signup(h.client, CARLA, company="Contas Carla", tax_id=NIF_C, name="Carla Reis")
    res = h.client.post("/api/accountant/invitations", json={"email": "ana@example.pt"}, headers=bearer(carla["token"]))
    assert (res.status_code, res.json()["message"]) == (
        503, "Email is not set up on this server yet, so I can't send invitations.")
    broken = harness(tmp_path / "b", mailer=FakeMailer(fail=True))
    carla = signup(broken.client, CARLA, company="Contas Carla", tax_id=NIF_C, name="Carla Reis")
    res = broken.client.post("/api/accountant/invitations", json={"email": "ana@example.pt"},
                             headers=bearer(carla["token"]))
    assert res.status_code == 502
    listed = broken.client.get("/api/accountant/invitations", headers=bearer(carla["token"])).json()["invitations"]
    assert [i["status"] for i in listed] == ["not_sent"]  # recorded, but never counted as sent


# =========================================================================== production: company-limited reads (O5)


def _two_company_business(h: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ana's business: Padaria Lda (company A, with documents, payments and an accountant question) and
    Second Company (company B, with one payment of its own)."""
    ana = signup(h.client)
    seen = build_business(h, ana["token"])
    H = bearer(ana["token"])
    bank = h.client.post("/api/sources", json={"kind": "bank", "bank": "Caixa Geral", "companyId": "second-company"},
                         headers=H)
    assert bank.status_code == 200, bank.text
    csv = (b"date,amount,counterparty,account,description,kind\n"
           b"2026-09-23,-41.20,PAPELARIA CENTRAL,{acct},COMPRA,card\n").replace(b"{acct}", bank.json()["id"].encode())
    assert h.client.post("/api/evidence", files={"file": ("b.csv", csv, "text/csv")}, headers=H).status_code == 200
    question = E.email(sender="Marc Vidal <marc@vidal.pt>", subject="EDP", at=h.clock.now_,
                       message_id="<edp-q@vidal.pt>",
                       text="Hi Ana,\nIs the €64.10 payment to EDP on 19 September the electricity bill?\n")
    asked = h.client.post("/api/evidence", json={"filename": "q.eml", "contentType": "message/rfc822",
                                                 "dataBase64": b64(question)}, headers=H)
    assert asked.status_code == 200, asked.text
    return ana, seen


def _markers(h: Any, H: dict[str, str], seen: dict[str, Any]) -> set[str]:
    """Everything that identifies company A's data: its id and name, documents, payments, evidence, questions."""
    a = h.client.get("/api/accountant/clients/padaria-lda", headers=H).json()
    marks = {"padaria-lda", "Padaria Lda", "EDP", *seen["documents"]}
    marks |= {r["id"] for r in a["reconciliation"]} | {e["id"] for e in a["evidenceLinks"]}
    marks |= {q["id"] for q in a["questions"]} | {q["question"] for q in a["questions"]}
    assert len(marks) > 8 and a["questions"], "company A needs documents, payments, evidence and a question"
    return marks


def test_an_accountant_of_company_b_cannot_read_company_a_through_any_get_route(tmp_path: Path) -> None:
    h = harness(tmp_path, mailer=FakeMailer())
    ana, seen = _two_company_business(h)
    H, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    marks = _markers(h, H, seen) | set(seen["cost_centers"])  # company A's cost center too
    a_evidence = [e["id"] for e in h.client.get("/api/accountant/clients/padaria-lda", headers=H).json()["evidenceLinks"]]
    report = h.client.post("/api/chat/tool", json={"name": "period_report", "input": {
        "date_from": "2026-09-01", "date_to": "2026-09-30"}}, headers=H).json()["result"]
    # Bob keeps the books of Second Company only.
    bob = signup(h.client, "bob@contas.pt", company="Contas Bob", tax_id=NIF_C, name="Bob")
    h.store._d.memberships.discard((bob["tenant"]["id"], bob["user"]["id"], "owner"))
    h.store.add_membership(tenant, bob["user"]["id"], "accountant", companies=["second-company"])
    login = h.client.post("/api/auth/login", json={"email": "bob@contas.pt", "password": PASSWORD}).json()
    B = bearer(login["token"])
    me = h.client.get("/api/auth/me", headers=B).json()
    assert (me["role"], me["tenant"]["id"], me["companies"]) == ("accountant", tenant, ["second-company"])

    a, ev = "padaria-lda", a_evidence[0]
    paths = ["/api/home", "/api/needs-you", "/api/activity", "/api/companies", f"/api/companies/{a}",
             f"/api/months/{a}/2026-09", f"/api/months/{a}/2026-10", "/api/sources", "/api/chat/tools", "/api/tasks",
             f"/api/reports/{report['id']}/file", "/api/documents", f"/api/documents?company={a}",
             "/api/documents?q=edp", *(f"/api/documents/{d}/file" for d in seen["documents"]),
             *(f"/api/documents/{d}" for d in seen["documents"]),
             *(f"/api/transactions/{t}" for t in seen["transactions"]), "/api/obligations",
             f"/api/companies/{a}/cost-centers", f"/api/companies/{a}/cost-centers?month=2026-09",
             *(f"/api/cost-centers/{cc}" for cc in seen["cost_centers"]),
             *(f"/api/cost-centers/{cc}/statement?month=2026-09" for cc in seen["cost_centers"]),
             "/api/settings/report",
             "/api/settings/accountant", "/api/settings/mailboxes", "/api/onboarding", "/api/settings/automation",
             "/api/accountant/api-keys", "/api/connections", "/api/accountant/clients",
             f"/api/accountant/clients/{a}", f"/api/accountant/clients/{a}/export",
             *(f"/api/accountant/clients/{a}/evidence/{e}/file" for e in a_evidence),
             *(f"/api/accountant/clients/second-company/evidence/{e}/file" for e in a_evidence),
             *(f"/api/evidence/{e}/file" for e in a_evidence), "/api/accountant/invitations", "/api/audit",
             "/api/pipeline", "/api/internal/overview", "/api/internal/operations", "/api/internal/readiness",
             f"/api/accountant/clients/{tenant}~{a}", f"/api/accountant/clients/{tenant}~{a}/export",
             f"/api/accountant/clients/{tenant}~{a}/evidence/{ev}/file",
             f"/api/accountant/clients/{tenant}~second-company/evidence/{ev}/file", "/healthz",
             # Employee cards and staff expenses (backoffice.staff): never an accountant's.
             "/api/employees", "/api/expense-claims", "/api/employee/card-payments",
             # Who opened sensitive documents (backoffice.sensitivity): the owner's only.
             "/api/documents/access-log",
             # The plan (backoffice.billing): the owner's only.
             "/api/billing"]
    for p in paths:
        res = h.client.get(p, headers=B)
        assert res.status_code in (200, 403, 404), (p, res.status_code, res.text)
        if res.status_code == 200:
            leaked = [m for m in marks if m in res.text]
            assert not leaked, (p, leaked)
    # Every GET route the engine serves was tried.
    svc = BackOfficeService.new_tenant("t-routes", owner_name="x", owner_email="x@example.pt", now=h.clock.now_)
    called = [p.split("?", 1)[0] for p in paths]
    for verb, pattern, _ in svc._routes():
        if verb == "GET":
            assert any(pattern.fullmatch(p) for p in called), pattern.pattern
    # ... and nothing through the POSTs an accountant may make.
    for p, body in (("/api/ask", {"question": "Did we pay EDP?"}),
                    ("/api/documents/export", {"company": a, "from": "2026-09-01", "to": "2026-09-30"}),
                    ("/api/documents/export", {"from": "2026-09-01", "to": "2026-09-30"}),
                    ("/api/accountant/rules", {"text": "Treat all EDP as Utilities", "companyId": a}),
                    (f"/api/accountant/clients/{a}/rules", {"text": "Treat all EDP as Utilities"}),
                    (f"/api/accountant/clients/{tenant}~{a}/rules", {"text": "Treat all EDP as Utilities"})):
        res = h.client.post(p, json=body, headers=B)
        assert res.status_code in (403, 404), (p, res.status_code, res.text)
    # What Bob may see: Second Company, its payment and its evidence.
    assert [c["id"] for c in h.client.get("/api/companies", headers=B).json()["companies"]] == ["second-company"]
    rows = h.client.get("/api/accountant/clients", headers=B).json()["clients"]
    assert [r["id"] for r in rows] == [f"{tenant}~second-company"]
    b_view = h.client.get("/api/accountant/clients/second-company", headers=B).json()
    assert [r["payee"] for r in b_view["reconciliation"]] == ["Papelaria Central"]
    for link in b_view["evidenceLinks"]:
        assert h.client.get(link["href"], headers=B).status_code == 200
    rule = h.client.post("/api/accountant/rules", json={"text": "Treat all Papelaria Central as Office supplies",
                                                        "companyId": "second-company"}, headers=B)
    assert rule.status_code == 200 and rule.json()["rule"]["companyIds"] == ["second-company"]
    export = h.client.post("/api/documents/export", json={"company": "second-company"}, headers=B)
    assert export.status_code == 200
    # The owner still sees everything; an accountant of every company (no limit) too, as before.
    assert h.client.get(f"/api/accountant/clients/{a}", headers=H).status_code == 200
    h.store.add_membership(tenant, bob["user"]["id"], "accountant")
    B = bearer(h.client.post("/api/auth/login", json={"email": "bob@contas.pt", "password": PASSWORD}).json()["token"])
    assert h.client.get(f"/api/accountant/clients/{a}", headers=B).status_code == 200
    assert h.client.get("/api/home", headers=B).status_code == 200


def test_an_accountant_reading_from_their_own_firm_sees_only_the_companies_they_were_given(tmp_path: Path) -> None:
    mailer = FakeMailer()
    h = harness(tmp_path, mailer=mailer)
    ana, seen = _two_company_business(h)
    tenant = ana["tenant"]["id"]
    marks = _markers(h, bearer(ana["token"]), seen)
    carla = signup(h.client, CARLA, company="Contas Carla", tax_id=NIF_C, name="Carla Reis")
    C = bearer(carla["token"])
    _invite(h, carla["token"], "ana@example.pt", taxIds=[NIF_B])
    assert h.client.post("/api/invitations/accept", json={"token": _token(mailer)},
                         headers=bearer(ana["token"])).status_code == 200
    ev = h.client.get("/api/accountant/clients/padaria-lda", headers=bearer(ana["token"])).json()["evidenceLinks"][0]
    for p in (f"/api/accountant/clients/{tenant}~padaria-lda", f"/api/accountant/clients/{tenant}~padaria-lda/export",
              f"/api/accountant/clients/{tenant}~padaria-lda/evidence/{ev['id']}/file",
              f"/api/accountant/clients/{tenant}~second-company/evidence/{ev['id']}/file"):
        assert h.client.get(p, headers=C).status_code == 404, p
    own = h.client.get(f"/api/accountant/clients/{tenant}~second-company", headers=C)
    assert own.status_code == 200 and not [m for m in marks if m in own.text]
    # Carla's own session is her firm: the catch-all routes read her business, never Ana's.
    assert [c["id"] for c in h.client.get("/api/companies", headers=C).json()["companies"]] == ["contas-carla"]
    assert h.client.get("/api/documents", headers=C).json()["items"] == []
    # A business she has nothing to do with is not found.
    other = signup(h.client, "rui@oficina.pt", company="Oficina Rui", tax_id=NIF_A, name="Rui")
    assert h.client.get(f"/api/accountant/clients/{other['tenant']['id']}~oficina-rui", headers=C).status_code == 404


def test_the_owner_sets_a_company_accountant_through_onboarding(tmp_path: Path) -> None:
    h = harness(tmp_path)
    ana = signup(h.client)
    A = bearer(ana["token"])
    h.client.post("/api/onboarding/company", json={"name": "Second Company", "taxId": NIF_B}, headers=A)
    assert h.client.post("/api/onboarding/accountant", json={"email": "marc@vidal.pt", "name": "Marc Vidal"},
                         headers=A).status_code == 200
    res = h.client.post("/api/onboarding/accountant", json={"email": CARLA, "name": "Carla Reis",
                                                            "companyId": "second-company"}, headers=A)
    assert res.status_code == 200 and res.json()["accountant"]["companyId"] == "second-company"
    by = {c["companyId"]: c for c in h.client.get("/api/settings/accountant", headers=A).json()["companies"]}
    assert by["second-company"]["accountant"]["email"] == CARLA
    assert by["padaria-lda"]["accountant"]["email"] == "marc@vidal.pt"
    res = h.client.post("/api/settings/accountant", json={"companyId": "second-company", "useDefault": True},
                        headers=A)
    assert res.status_code == 200
    by = {c["companyId"]: c for c in h.client.get("/api/settings/accountant", headers=A).json()["companies"]}
    assert by["second-company"]["accountant"]["email"] == "marc@vidal.pt"
    h.manager.evict(ana["tenant"]["id"])  # replayed from the log, the same
    by2 = {c["companyId"]: c for c in h.client.get("/api/settings/accountant", headers=A).json()["companies"]}
    assert by2 == by


# =========================================================================== the same on PostgreSQL (row-level security)


@pytest.fixture(scope="module")
def pg_store() -> Any:
    """The production store on a real PostgreSQL (as test_server_postgres.py), with 0011 applied."""
    pytest.importorskip("psycopg")
    import os
    import shutil
    import uuid
    from urllib.parse import urlsplit

    import test_server_postgres as tsp
    from backoffice_db.testing import PostgresUnavailable, TemporaryPostgres

    from backoffice.server.postgres import PostgresStore

    pg = None
    external = os.environ.get("BACKOFFICE_TEST_DATABASE_URL")
    if external:
        psql = shutil.which("psql")
        if not psql:
            pytest.skip("psql is not installed")
        server = tsp.Server(external, psql)
    else:
        try:
            pg = TemporaryPostgres()
            pg.start()
        except PostgresUnavailable as err:
            pytest.skip(f"no PostgreSQL available: {err}")
        server = tsp.Server(pg.url("postgres"), str(pg.bindir / "psql"))
    try:
        name = f"bo_acct_{uuid.uuid4().hex[:10]}"
        server.executor(urlsplit(server.admin_url).path.lstrip("/") or "postgres").query(f"CREATE DATABASE {name}")
        db = server.executor(name)
        tsp.migrate_all(db)
        login = f"bo_api_{uuid.uuid4().hex[:8]}"
        db.execute(f"CREATE ROLE {login} LOGIN PASSWORD '{tsp.APP_PASSWORD}' NOSUPERUSER NOBYPASSRLS "
                   "IN ROLE backoffice_app")
        store = PostgresStore(server.url(name, login, tsp.APP_PASSWORD), pool_size=4)
        yield store
        store.close()
    finally:
        if pg is not None:
            pg.stop()


def test_invitations_and_company_limits_on_postgres(tmp_path: Path, pg_store: Any) -> None:
    mailer = FakeMailer()
    h = harness(tmp_path, store=pg_store, mailer=mailer)
    carla = signup(h.client, CARLA, company="Contas Carla", tax_id=None, name="Carla Reis")
    _invite(h, carla["token"], "ana@example.pt", taxIds=[NIF_B])
    token = _token(mailer)
    digest = hashlib.sha256(token.encode()).hexdigest()
    found = pg_store.invitation(digest)
    assert found is not None and found.sent_at is not None and found.tax_ids == (NIF_B,)
    ana = signup(h.client, "ana@example.pt")
    A, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    assert h.client.post("/api/onboarding/company", json={"name": "Second Company", "taxId": NIF_B},
                         headers=A).status_code == 200
    # Row-level security: the invited owner's scope alone sees no invitation (only the token or the inviter does).
    with pg_store._tx(tenant=tenant, user=ana["user"]["id"]) as cur:
        cur.execute("SELECT count(*) FROM accountant_invitations")
        assert cur.fetchone()[0] == 0
    rui = bearer(signup(h.client, "rui@oficina.pt", company="Oficina Rui", tax_id=None, name="Rui")["token"])
    assert h.client.post("/api/invitations/accept", json={"token": token}, headers=rui).status_code == 403
    assert h.client.post("/api/invitations/accept", json={"token": token}, headers=A).status_code == 200
    assert h.client.post("/api/invitations/accept", json={"token": token}, headers=A).status_code == 409
    assert pg_store.membership_companies(tenant, carla["user"]["id"]) == ("second-company",)
    C = bearer(carla["token"])
    rows = h.client.get("/api/accountant/clients", headers=C).json()["clients"]
    assert [r["id"] for r in rows] == [f"{tenant}~second-company"]
    assert h.client.get(f"/api/accountant/clients/{tenant}~padaria-lda", headers=C).status_code == 404
    assert [i["status"] for i in h.client.get("/api/accountant/invitations", headers=C).json()["invitations"]] == \
        ["accepted"]
    # A second invitation, too late.
    _invite(h, carla["token"], "rui@oficina.pt")
    late = _token(mailer)
    h.clock.advance(days=15)
    assert h.client.post("/api/invitations/accept", json={"token": late}, headers=rui).status_code == 410
    # The client erases their account: the accountant's access goes with it.
    res = h.client.post("/api/account/delete", json={"confirm": "DELETE", "password": PASSWORD}, headers=A)
    assert res.status_code == 202, res.text
    assert pg_store.membership_companies(tenant, carla["user"]["id"]) is None
    assert f"{tenant}~" not in h.client.get("/api/accountant/clients", headers=C).text
