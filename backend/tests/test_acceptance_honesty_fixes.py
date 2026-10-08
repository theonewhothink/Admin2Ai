"""The product never claims success that did not happen (acceptance, §3, §22, §25–28, §47–48, §70).

1. A supplier request counts as sent only when a transport accepted it; until then every screen says it is
   written and waiting to be sent. "Keep blocked" really asks the supplier for a corrected invoice.
2. The accountant gets an answer only to a question the evidence fully supports, and it counts as answered
   only once it was delivered.
3. The chat never settles a conflict or an approval: those need the owner's own tap in Needs You.
4. Reconnecting a real mailbox means signing in again, and it is back only once a sync worked.
6. The accountant's rule and export buttons are backed by the engine (rules and the export period).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from _server_support import bearer, harness, signup

from backoffice.accountant_questions import PaymentFacts, answer_question, description_lines
from backoffice.assistant import run_tool
from backoffice.demo import build_demo
from backoffice.demo import evidence as E
from backoffice.demo import world
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import SourceKind
from backoffice.mailer import SimulatedOutbox
from backoffice.orchestrator import TZ, Orchestrator
from backoffice.server.events import state_digest
from backoffice.server.runtime import TenantManager
from backoffice.service import BackOfficeService, ServiceError

SEPT = "2026-09"


class RefusingMailer:
    """A transport that is down: it never accepts anything."""

    def __init__(self) -> None:
        self.tries = 0

    def send(self, *args: Any, **kwargs: Any) -> None:
        self.tries += 1
        raise ConnectionError("smtp down")


class CapturingMailer:
    def __init__(self) -> None:
        self.sent: list[tuple[list[str], str]] = []

    def send(self, to: list[str], subject: str, body: str, files: Any, headers: Any = None) -> None:
        self.sent.append((list(to), subject))


@pytest.fixture
def unsent(monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    """The demo exactly as usual, except nothing can send email (no transport at all)."""
    monkeypatch.setattr(world, "SimulatedOutbox", lambda: None)
    return build_demo()


def edp_payment(o: Orchestrator):  # type: ignore[no-untyped-def]
    return next(r for r in o.repo.transactions.values() if r.tx.description == "DD EDP COMERCIAL")


def handled(svc: BackOfficeService) -> dict[str, int]:
    return {h["id"]: h["count"] for h in svc.home()["handled"]}


# --------------------------------------------------------------------------- 1. supplier requests


def test_demo_supplier_request_goes_through_the_simulated_outbox() -> None:
    o = build_demo()
    assert isinstance(o.transport, SimulatedOutbox)
    chase = o.repo.chases[edp_payment(o).id]
    assert chase.sent and o.repo.outbox[chase.outbox_id].status == "sent"
    accepted = [m for m in o.transport.accepted if m.to == ("faturas@edp.pt",)]
    assert len(accepted) == 1 and "64,10" in accepted[0].body
    assert dict(accepted[0].headers)["Message-ID"] == chase.message.message_id
    svc = BackOfficeService(o)
    assert [a["text"] for a in svc.activity()["items"] if a["kind"] == "chased"] == [
        "Asked EDP for the invoice for the €64.10 payment."]
    assert "I asked EDP" in svc.ask("Did we pay EDP?")["answer"]


def test_unsent_supplier_request_is_written_and_waiting_everywhere(unsent: Orchestrator) -> None:
    o = unsent
    svc = BackOfficeService(o)
    rec = edp_payment(o)
    chase = o.repo.chases[rec.id]
    assert not chase.sent and o.repo.outbox[chase.outbox_id].status == "waiting"
    # Home: nothing about supplier emails counted as handled
    assert "h_supplier" not in handled(svc)
    # Activity: written and waiting, never "Asked EDP"
    items = svc.activity()["items"]
    assert not [a for a in items if a["kind"] == "chased"]
    waiting = [a for a in items if a["kind"] == "waiting"]
    assert any(a["text"] == "Wrote to EDP asking for the invoice for the €64.10 payment. It is waiting to be sent."
               for a in waiting)
    # Ask and the chat
    answer = svc.ask("Did we pay EDP?")["answer"]
    assert "waiting to be sent" in answer and "I asked" not in answer
    reply = svc.dispatch("POST", "/api/chat", {"message": "what happened with the EDP invoice"})[1]["reply"]
    assert "waiting to be sent" in reply and "I asked" not in reply
    # The month and the diagram
    month = svc.month("hazel-tree", SEPT)
    assert month is not None and month["stats"]["suppliersChased"] == 0
    assert any("waiting to be sent" in line["text"] and "EDP" in line["text"] for line in month["remaining"])
    row = next(i for i in svc.pipeline()["items"] if i["id"] == rec.item_id)
    assert "waiting to be sent" in row["reason"]
    assert chase.outbox_id in svc.waiting_messages()


def test_a_refusing_transport_leaves_it_waiting_and_a_working_one_sends_it(unsent: Orchestrator) -> None:
    o = unsent
    svc = BackOfficeService(o)
    chase = o.repo.chases[edp_payment(o).id]
    o.transport = RefusingMailer()
    o.run()
    assert not chase.sent and o.repo.outbox[chase.outbox_id].failures >= 1 and "h_supplier" not in handled(svc)
    o.transport = SimulatedOutbox()
    report = o.run()
    assert chase.outbox_id in report.sent and chase.sent
    assert handled(svc)["h_supplier"] == 1
    assert [a["text"] for a in svc.activity()["items"] if a["kind"] == "chased"] == [
        "Asked EDP for the invoice for the €64.10 payment."]
    month = svc.month("hazel-tree", SEPT)
    assert month is not None and month["stats"]["suppliersChased"] == 1
    assert "I asked EDP" in svc.ask("Did we pay EDP?")["answer"]


def test_production_service_sends_a_waiting_request_only_through_its_mailer(unsent: Orchestrator) -> None:
    svc = BackOfficeService(unsent)
    message_id = unsent.repo.chases[edp_payment(unsent).id].outbox_id
    with pytest.raises(ServiceError):
        svc.send_waiting(message_id)  # nothing to send it with
    svc.mailer = RefusingMailer()
    with pytest.raises(ConnectionError):
        svc.send_waiting(message_id)
    assert unsent.repo.outbox[message_id].status == "waiting"
    svc.mailer = CapturingMailer()
    assert svc.send_waiting(message_id) == {"ok": True, "sent": True}
    assert svc.mailer.sent == [(["faturas@edp.pt"], unsent.repo.outbox[message_id].subject)]
    assert svc.waiting_messages() == [m for m in svc.waiting_messages() if m != message_id]


def test_keep_blocked_really_asks_for_a_corrected_invoice() -> None:
    o = build_demo()
    svc = BackOfficeService(o)
    held = o.repo.documents[o.repo.needs["nd_vodafone_iban"].subject_id]
    result = svc.answer("nd_vodafone_iban", "keep_blocked")
    assert result["message"] == ("Done. It stays blocked. I asked Vodafone for a corrected invoice at "
                                 "faturacao@vodafone.pt.")
    request = next(m for m in o.repo.outbox.values() if m.kind == "correction_request")
    assert request.status == "sent" and request.subject_id == held.id
    # to the address on file (not one taken from the held invoice), never repeating the new bank details
    assert request.to == o.repo.suppliers["sup-vodafone"].contact_email
    assert E.VODAFONE_NEW_IBAN not in request.body and E.VODAFONE_NEW_IBAN[-4:] not in request.body
    assert any(m.to == ("faturacao@vodafone.pt",) for m in o.transport.accepted)
    assert "Asked Vodafone for a corrected invoice." in [a["text"] for a in svc.activity()["items"]]
    assert held.on_hold and o.repo.items[held.item_id].stage is Stage.CONFLICT


def test_keep_blocked_without_a_transport_says_it_is_waiting(unsent: Orchestrator) -> None:
    svc = BackOfficeService(unsent)
    result = svc.answer("nd_vodafone_iban", "keep_blocked")
    assert result["message"].endswith("asking for a corrected invoice. It is waiting to be sent.")
    request = next(m for m in unsent.repo.outbox.values() if m.kind == "correction_request")
    assert request.status == "waiting"
    texts = [a["text"] for a in svc.activity()["items"]]
    assert "Wrote to Vodafone asking for a corrected invoice. It is waiting to be sent." in texts
    assert "Asked Vodafone for a corrected invoice." not in texts


def test_keep_blocked_without_an_address_on_file_promises_nothing() -> None:
    o = build_demo()
    supplier = o.repo.suppliers["sup-vodafone"]
    o.repo.suppliers["sup-vodafone"] = supplier.model_copy(update={"contact_email": None})
    svc = BackOfficeService(o)
    approval = next(i for i in svc.needs_you()["items"] if i["id"] == "nd_vodafone_iban")
    assert approval["keepBlocked"]["message"] == "Done. It stays blocked."
    result = svc.answer("nd_vodafone_iban", "keep_blocked")
    assert "I don't have an email address for Vodafone" in result["message"]
    assert "I will ask" not in result["message"]
    assert not [m for m in o.repo.outbox.values() if m.kind == "correction_request"]


def test_owner_confirmed_email_is_sent_only_by_a_mailer(unsent: Orchestrator) -> None:
    svc = BackOfficeService(unsent)
    assert svc.mailer is None
    draft = svc.chat_tool({"name": "draft_email", "input": {"to": ["marc@vidal.pt"], "subject": "Hello",
                                                             "body": "Monthly package"}})["result"]["draft_id"]
    status, body = svc.dispatch("POST", f"/api/chat/outbox/{draft}/send", {})
    assert status == 200 and body["status"] == "waiting" and "waiting to be sent" in body["message"]
    assert draft in svc.waiting_messages()
    assert not [a for a in svc.activity()["items"] if a["text"].startswith("Sent “Hello”")]
    svc.mailer = SimulatedOutbox()
    body = svc.dispatch("POST", f"/api/chat/outbox/{draft}/send", {})[1]
    assert body["status"] == "sent" and body["message"] == "Sent. This is the demo: no real email leaves it."
    assert svc.mailer.accepted[0].to == ("marc@vidal.pt",)


def _draft(h: Any, H: dict[str, str]) -> str:
    reply = h.client.post("/api/chat/tool", json={"name": "draft_email", "input": {
        "to": ["marc@vidal.pt"], "subject": "Hello", "body": "Monthly package"}}, headers=H).json()
    return str(reply["result"]["draft_id"])


def test_production_sends_waiting_email_in_its_own_event_and_replays_it(tmp_path: Path) -> None:
    h = harness(tmp_path)  # no mailer configured
    account = signup(h.client)
    tenant, H = account["tenant"]["id"], bearer(account["token"])
    draft = _draft(h, H)
    sent = h.client.post(f"/api/chat/outbox/{draft}/send", json={}, headers=H).json()
    assert sent["status"] == "waiting"
    # The mailer is down: the owner's own change stands, the send event alone is void, it stays waiting.
    refusing = RefusingMailer()
    down = TenantManager(h.store, h.objects, now=h.clock, mailer=refusing, strict_reads=True)
    assert down.command(tenant, "usr_x", "POST", "/api/tasks", {"title": "Call the bank"})[0] == 200
    kinds = [r.kind for r in h.store.events(tenant)]
    assert kinds[-3:] == ["request", "outbox.send", "void"] and refusing.tries == 1
    down.command(tenant, "usr_x", "POST", "/api/tasks", {"title": "Again"})
    assert refusing.tries == 1  # not retried at once
    with down.open(tenant) as rt:
        assert rt.service.assistant.outbox[draft].status == "waiting"
    # Another process with a working mailer: the email goes out, recorded as its own event.
    mailer = CapturingMailer()
    up = TenantManager(h.store, h.objects, now=h.clock, mailer=mailer, strict_reads=True)
    up.command(tenant, "usr_x", "POST", "/api/tasks", {"title": "Renew the lease"})
    assert mailer.sent == [(["marc@vidal.pt"], "Hello")]
    assert [r.kind for r in h.store.events(tenant)][-1] == "outbox.send"
    with up.open(tenant) as rt:
        live = state_digest(rt.service)
        assert rt.service.assistant.outbox[draft].status == "sent"
    fresh = TenantManager(h.store, h.objects, now=h.clock, strict_reads=True)  # rebuilt from the log alone
    with fresh.open(tenant) as rt:
        assert state_digest(rt.service) == live
    assert mailer.sent == [(["marc@vidal.pt"], "Hello")]  # a replay never sends again


# --------------------------------------------------------------------------- 2. accountant answers


def test_demo_rent_question_is_answered_from_the_receipt_and_delivered() -> None:
    o = build_demo()
    q = next(q for q in o.repo.accountant_questions.values() if "1,200" in q.text)
    assert q.status == "answered"
    assert q.answer == ("Yes. The €1,200.00 transfer on 1 September went to Marta Gonçalves, and invoice-receipt "
                        "FR M2026/9 of 1 September matches it. The invoice-receipt says: “Renda do estúdio da Rua "
                        "da Rosa - setembro de 2026”.")
    delivered = [m for m in o.transport.accepted if m.to == (E.ACCOUNTANT_EMAIL,)]
    assert len(delivered) == 1 and delivered[0].subject == "Re: September - two questions"
    assert dict(delivered[0].headers)["In-Reply-To"] == "<sept-questions@contabilidadevidal.pt>"
    assert q.answer in delivered[0].body
    svc = BackOfficeService(o)
    assert handled(svc)["h_accountant"] == 1
    assert "Answered your accountant about the €1,200.00 payment to Marta Gonçalves." in [
        a["text"] for a in svc.activity()["items"]]


def test_a_decision_question_stays_open_even_when_its_payment_closes() -> None:
    o = build_demo()
    ikea = next(q for q in o.repo.accountant_questions.values() if "IKEA" in q.text)
    assert ikea.status == "waiting"
    o.answer("nd_ikea_418", "entity:hazel-tree")  # the IKEA payment now closes with its receipt
    rec = o.repo.transactions[o.repo.needs["nd_ikea_418"].subject_id]
    assert o.repo.items[rec.item_id].stage is Stage.CLOSED
    assert ikea.status == "waiting" and ikea.answer is None  # "which company should it go to?" is not a yes
    answer = BackOfficeService(o).ask("What did the accountant ask this month?")["answer"]
    assert "Still open: “Which company should the €418.00 IKEA payment of 29 September go to?”" in answer


def _ask_accountant(o: Orchestrator, text: str) -> list[Any]:
    raw = E.email(sender=f"Marc Vidal <{E.ACCOUNTANT_EMAIL}>", subject="More questions",
                  at=datetime(2026, 10, 2, 9, 40, tzinfo=TZ), text=text, message_id="<more@contabilidadevidal.pt>")
    report = o.ingest_file(raw, filename="message.eml", content_type="message/rfc822", source_kind=SourceKind.EMAIL,
                           origin="email")
    return [o.repo.accountant_questions[i] for i in report.question_ids]


@pytest.mark.parametrize("question", [
    "Is the €1,200 payment the office rent?",  # the receipt says studio, not office
    "Is the €1,200 payment for software?",  # nothing on the receipt says so
    "Is the €1,200 transfer to Predial Alfama the studio rent?",  # not the payee
    "Is the €1,200 transfer on 2 September the studio rent?",  # not the date
    "Is the €1,200 payment the studio rent for Company C?",  # not the company
    "Is the €1,200 payment VAT exempt?",  # a tax judgment: never confirmed from a document
    "Should the €1,200 payment be booked as rent?",  # a booking decision
])
def test_claims_the_evidence_does_not_support_are_never_confirmed(question: str) -> None:
    o = build_demo()
    (q,) = _ask_accountant(o, question)
    assert q.status == "waiting" and q.answer is None
    assert not [m for m in o.repo.outbox.values() if m.kind == "accountant_answer" and m.subject_id == q.id]


def test_factual_questions_get_factual_answers() -> None:
    o = build_demo()
    who, which = _ask_accountant(o, "Who was the €950 payment to?\nWhich document proves the €1,200 payment?")
    assert who.status == "answered" and who.company_id == "company-b"
    assert who.answer == ("The €950.00 transfer on 1 September went to Predial Alfama, as invoice-receipt "
                          "FR PA2026/211 of 1 September shows.")
    assert which.answer is not None and not which.answer.startswith("Yes")
    assert "invoice-receipt FR M2026/9" in which.answer


def test_an_answer_that_was_not_delivered_is_not_answered(unsent: Orchestrator) -> None:
    svc = BackOfficeService(unsent)
    q = next(q for q in unsent.repo.accountant_questions.values() if "1,200" in q.text)
    assert q.status == "written" and q.answer is not None
    assert "h_accountant" not in handled(svc)
    client = svc.accountant_client("hazel-tree")
    row = next(x for x in client["questions"] if x["id"] == q.id)
    assert row["status"] == "waiting" and "answer" not in row
    answer = svc.ask("What did the accountant ask this month?")["answer"]
    assert "waiting to be sent" in answer
    unsent.transport = SimulatedOutbox()
    unsent.run()
    assert q.status == "answered" and handled(svc)["h_accountant"] == 1
    assert next(x for x in svc.accountant_client("hazel-tree")["questions"] if x["id"] == q.id)["status"] == "answered"


def test_answering_rules_on_plain_facts() -> None:
    facts = PaymentFacts(
        amount=Decimal("950.00"), currency="EUR", booked_on=date(2026, 9, 1), kind="transfer", payee="Predial Alfama",
        payee_names=("PREDIAL ALFAMA LDA", "Predial Alfama"), company_names=("Company B", "Company B, Lda."),
        other_company_names=("Hazel Tree", "Company C"), document="invoice-receipt FR PA2026/211",
        document_word="invoice-receipt", document_date=date(2026, 9, 1),
        description=description_lines(E.PREDIAL_RECEIPT.decode()))
    today = date(2026, 10, 2)
    assert facts.description == ("Renda do escritório - setembro de 2026",)  # no header, no fields, no QR
    yes = answer_question("Is the €950 transfer to Predial Alfama the office rent for Company B?", facts, today)
    assert yes is not None and yes.text.startswith("Yes.") and yes.claims == ("office", "rent")
    assert answer_question("Is the €950 transfer the studio rent?", facts, today) is None
    assert answer_question("Is the €950 transfer for Hazel Tree?", facts, today) is None
    assert answer_question("Is the €951 transfer the office rent?", facts, today) is None
    assert answer_question("Is the €950 transfer from 1 October the office rent?", facts, today) is None
    assert answer_question("The €950 transfer is rent, right?", facts, today) is None


# --------------------------------------------------------------------------- 3. the chat and Needs You


@pytest.fixture
def with_conflict() -> BackOfficeService:
    """The demo plus an EDP invoice whose printed total (€46.10) disagrees with its QR code (€64.10)."""
    svc = BackOfficeService.demo()
    bad = E.EDP_INVOICE.replace("Total: 64,10 €".encode(), "Total: 46,10 €".encode())
    svc.upload_evidence("edp.txt", "text/plain", bad)
    assert svc.repo.needs["nd_edp_check"].kind == "check"
    return svc


def test_claude_tools_refuse_to_settle_a_conflict(with_conflict: BackOfficeService) -> None:
    svc = with_conflict
    status = svc.chat_tool({"name": "business_status", "input": {}})["result"]
    conflict = next(n for n in status["needs_owner"] if n["question_id"] == "nd_edp_check")
    assert conflict["kind"] == "conflict" and conflict["owner_must_confirm_in_needs_you"] and "options" not in conflict
    plain = next(n for n in status["needs_owner"] if n["question_id"] == "nd_ikea_418")
    assert not plain["owner_must_confirm_in_needs_you"] and plain["options"]
    out = svc.chat_tool({"name": "answer_question", "input": {"question_id": "nd_edp_check", "option_id": "source_1"}})
    assert out["isError"] and "Needs you" in out["result"]
    assert out["cards"][0]["items"][0]["id"] == "needs:nd_edp_check"
    assert svc.repo.needs["nd_edp_check"].status == "open"
    with pytest.raises(ValueError):
        run_tool(svc.assistant, "answer_question", {"question_id": "nd_vodafone_iban", "option_id": "keep_blocked"}, [])
    assert svc.repo.needs["nd_vodafone_iban"].status == "open"
    # a plain choice is still answered from the chat
    done = svc.chat_tool({"name": "answer_question", "input": {"question_id": "nd_ikea_418",
                                                                "option_id": "entity:hazel-tree"}})
    assert not done["isError"] and svc.repo.needs["nd_ikea_418"].status == "answered"


@pytest.mark.parametrize("message", [
    "The right total on the EDP invoice is €64.10",
    "use the QR value for EDP",
    "Neither, set the EDP invoice aside",
])
def test_rule_brain_refuses_to_settle_a_conflict(with_conflict: BackOfficeService, message: str) -> None:
    svc = with_conflict
    body = svc.dispatch("POST", "/api/chat", {"message": message})[1]
    assert "Needs you" in body["reply"] and "one tap" in body["reply"]
    chips = [i["id"] for c in body["cards"] if c["type"] == "evidence" for i in c["items"]]
    assert "needs:nd_edp_check" in chips
    assert svc.repo.needs["nd_edp_check"].status == "open"


def test_rule_brain_never_answers_an_approval_as_a_choice(with_conflict: BackOfficeService) -> None:
    svc = with_conflict
    body = svc.dispatch("POST", "/api/chat", {"message": "Put the Vodafone invoice on Hazel Tree"})[1]
    assert "Needs you" in body["reply"] and svc.repo.needs["nd_vodafone_iban"].status == "open"
    body = svc.dispatch("POST", "/api/chat", {"message": "Put the IKEA payment on Hazel Tree"})[1]
    assert body["reply"].startswith("Done.") and svc.repo.needs["nd_ikea_418"].status == "answered"


# --------------------------------------------------------------------------- 4. reconnecting a real mailbox


class _Authorizer:
    providers = ("google", "microsoft")

    def begin(self, provider: str, tenant_id: str, connection_id: str, login_hint: str | None = None) -> str:
        return f"https://accounts.google.com/o/oauth2/v2/auth?hint={login_hint}"


def _real_mailbox(authorizer: Any = None) -> BackOfficeService:
    now = datetime(2026, 10, 2, 9, 30, tzinfo=TZ)
    svc = BackOfficeService.new_tenant("t-real", owner_name="Ana Silva", owner_email="ana@padaria.pt", now=now,
                                       authorizer=_Authorizer())
    svc.add_company("Padaria Lda", "516123459")
    svc.add_source({"kind": "email", "provider": "google", "address": "ana@padaria.pt"})
    svc.finish_sign_in("mail-ana-padaria-pt")
    svc.sync_mail("mail-ana-padaria-pt", [], {"coverage_start": (now - timedelta(days=90)).isoformat(),
                                              "coverage_end": now.isoformat(), "last_successful_sync": now.isoformat()})
    svc.sync_failed("mail-ana-padaria-pt", {"reconnect_required": True}, reconnect=True)
    svc.authorizer = authorizer
    return svc


def test_production_reconnect_hands_out_the_sign_in_address_and_claims_nothing() -> None:
    svc = _real_mailbox(_Authorizer())
    out = svc.reconnect("mail-ana-padaria-pt")
    assert out["authorizeUrl"].startswith("https://accounts.google.com/") and "ana@padaria.pt" in out["authorizeUrl"]
    assert out["connection"]["status"] == "stale" and out["connection"]["action"] == "Sign in to Gmail again"
    assert svc.home()["headline"] != "Everything is under control."
    # Signed in again: catching up, still not "connected" ...
    svc.finish_sign_in("mail-ana-padaria-pt")
    conn = svc.connections()["connections"][0]
    assert conn["status"] == "stale" and conn["reconnect"] == "catching_up"
    # ... until a sync actually worked.
    later = datetime(2026, 10, 2, 9, 45, tzinfo=TZ).isoformat()
    svc.sync_mail("mail-ana-padaria-pt", [], {"coverage_start": "2026-07-04T09:30:00+01:00", "coverage_end": later,
                                              "last_successful_sync": later})
    conn = svc.connections()["connections"][0]
    assert conn["status"] == "healthy" and "reconnect" not in conn
    assert "Gmail is connected again: ana@padaria.pt synced." in [a["text"] for a in svc.activity()["items"]]


def test_production_reconnect_without_sign_in_set_up_says_so() -> None:
    svc = _real_mailbox(authorizer=None)
    status, body = svc.dispatch("POST", "/api/connections/mail-ana-padaria-pt/reconnect", {})
    assert status == 503 and "sign-in is not set up" in body["message"]
    assert svc.connections()["connections"][0]["status"] == "stale"


def test_demo_reconnect_keeps_its_simulated_behaviour() -> None:
    svc = BackOfficeService.demo()
    svc.mark_connection_stale("gmail")
    out = svc.reconnect("gmail")
    assert out["connection"]["status"] == "healthy" and "authorizeUrl" not in out


def test_production_imap_reconnect_retries_but_stays_stale_until_a_sync(tmp_path: Path) -> None:
    h = harness(tmp_path)
    account = signup(h.client)
    H = bearer(account["token"])
    h.client.post("/api/sources", json={"kind": "email", "provider": "imap", "address": "ana@padaria.pt",
                                        "host": "imap.padaria.pt", "password": "app-password-123"}, headers=H)
    h.client.post("/api/connections/mail-ana-padaria-pt/stale", headers=H)
    res = h.client.post("/api/connections/mail-ana-padaria-pt/reconnect", headers=H).json()
    assert "authorizeUrl" not in res and "once it has synced" in res["message"]
    conn = next(c for c in h.client.get("/api/connections", headers=H).json()["connections"]
                if c["id"] == "mail-ana-padaria-pt")
    assert conn["status"] == "stale" and conn["reconnect"] == "catching_up"


# --------------------------------------------------------------------------- 6. what the accountant's buttons call


def test_accountant_rule_and_export_are_real() -> None:
    svc = BackOfficeService.demo()
    status, body = svc.dispatch("POST", "/api/accountant/rules", {"text": "Treat all Adobe subscriptions as Software"})
    assert status == 200 and body["affected"] >= 1 and body["rule"]["label"] in body["message"]
    client = svc.accountant_client("company-c")
    assert client["period"] == {"key": SEPT, "from": "2026-09-01", "to": "2026-09-30"}
    assert [r["label"] for r in client["rules"]] == [body["rule"]["label"]]
    status, export = svc.dispatch("POST", "/api/documents/export",
                                  {"company": "company-c", "from": "2026-09-01", "to": "2026-09-30"})
    assert status == 200 and export["count"] >= 1 and export["filename"].endswith(".zip")
    status, refused = svc.dispatch("POST", "/api/accountant/rules", {"text": "hello"})
    assert status == 400 and refused["message"]


def test_the_state_digest_sees_the_outbox_through_the_audit_chain(unsent: Orchestrator) -> None:
    """Every change to what was sent is audited, so two tenants that differ in it never share a digest."""
    svc = BackOfficeService(unsent)
    before = state_digest(svc)
    unsent.transport = SimulatedOutbox()
    unsent.run()
    assert state_digest(svc) != before
    records = [json.loads(r.body) for r in unsent.repo.audit_store.records(unsent.repo.tenant_id)]
    assert {"write_email", "sent"} <= {r["action"] for r in records if r["agent"] == "mailer"}


def test_the_send_path_loads_in_the_browser_build_without_ssl() -> None:
    """The in-browser demo (Pyodide) has no ssl module: the send path must not need it to load."""
    import subprocess
    import sys

    code = (
        "import builtins\n"
        "real = builtins.__import__\n"
        "def guard(name, *a, **k):\n"
        "    if name.split('.')[0] in ('ssl', 'smtplib'):\n"
        "        raise ModuleNotFoundError(name)\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = guard\n"
        "from backoffice.service import BackOfficeService\n"
        "svc = BackOfficeService.demo()\n"
        "assert svc.dispatch('GET', '/api/home')[0] == 200\n"
    )
    src = Path(__file__).resolve().parents[1] / "src"
    done = subprocess.run([sys.executable, "-c", code], env={"PYTHONPATH": str(src), "PATH": ""},
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]


# --------------------------------------------------------------------------- plain language (§36, §69, §70)


def test_everything_new_the_owner_reads_is_plain(unsent: Orchestrator) -> None:
    from backoffice.language import find_jargon, find_off_tone

    svc = BackOfficeService(unsent)
    texts = [a["text"] for a in svc.activity()["items"]]
    texts += [svc.ask("Did we pay EDP?")["answer"], svc.ask("What did the accountant ask this month?")["answer"]]
    texts += [line["text"] for line in (svc.month("hazel-tree", SEPT) or {}).get("remaining", [])]
    texts.append(svc.answer("nd_vodafone_iban", "keep_blocked")["message"])
    draft = svc.chat_tool({"name": "draft_email", "input": {"to": ["a@b.pt"], "subject": "Hi", "body": "x"}})
    texts.append(svc.chat_send(draft["result"]["draft_id"])["message"])
    real = _real_mailbox(_Authorizer())
    texts.append(real.reconnect("mail-ana-padaria-pt")["message"])
    texts += [c.get("message", "") + " " + c.get("action", "") for c in real.connections()["connections"]]
    texts += [q.answer or "" for q in unsent.repo.accountant_questions.values()]
    assert len(texts) > 10
    for text in texts:
        assert find_jargon(text) == [] and find_off_tone(text) == [], text
