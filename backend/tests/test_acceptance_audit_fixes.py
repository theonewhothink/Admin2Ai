"""Acceptance: gaps an independent audit of the QA matrix found, fixed in the product.

1. Q4  A later copy of an invoice that adds bank details (or any new beneficiary account for the supplier) goes
       through the fraud and new-beneficiary checks: an unverified account is held for the owner's verification
       exactly like a new beneficiary, never merged silently and never approved by the system.
2. R4  A connection whose sign-in ends on a known date (a bank consent, an OAuth grant with a known lifetime) is
       announced a week before, on Home and in Needs you, with one push in production. A connection with no
       knowable end never promises a reminder.
3. C8  Mailbox and portal sign-in secrets (IMAP app passwords, OAuth tokens, portal passwords in the vault) never
       reach anything sent to a model: the Claude chat through every tool, and the Claude vision reading path.
4. N1  The production accountant home lists the client companies of every business the accountant belongs to,
       with their complete, missing and needs-accountant figures.
5. T9  Every money answer (income, spending, per company, category, supplier and cost center, payouts, deposits)
       carries evidence links for the payments and documents behind it, capped with "and N more" and a link to
       the month view.
6. F6  An invoice addressed to a tax number that is not one of the business's companies is held ("addressed to
       another company ... Payment blocked."), and the buyer's tax number must be the company's for GREEN.
7. P1/P2 Country specifics live behind the country pack interface: no module outside backoffice.countries imports
       the Portuguese or Spanish pack; bank-line words and letter wording come from the relevant company's pack.
8. O2  A Microsoft 365 shared mailbox and a Google delegated or alias address can be connected per mailbox.

Everything runs through the live orchestrator and service, or the production server, the way a connector or the
owner would. Companies, people, tax numbers and IBANs are fictional.
"""

from __future__ import annotations

import ast
import base64
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from _server_support import NIF_A, NIF_C, bearer, harness, signup
from test_acceptance_engine_fixes import NORTE_NIF, bank, qr_document, stage, tx_by, upload

from backoffice.closure import Month
from backoffice.closure.obligations import detect_obligation, pack_vocabulary
from backoffice.countries import company_pack
from backoffice.demo import build_demo
from backoffice.demo import evidence as E
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import ObligationKind, Quality, SourceKind, Supplier, TransactionKind
from backoffice.language import find_jargon, find_off_tone
from backoffice.orchestrator import TZ, Account, BankRow, Orchestrator, local_datetime
from backoffice.policy import ActionKind, Approval, Requirement
from backoffice.reconciliation.expected import CORE_BANK_WORDING, EvidenceExpectation
from backoffice.service import BackOfficeService

SEPT = Month(2026, 9)
NEW_IBAN = "PT50003500001234567890163"  # an account Papelaria Norte was never paid into (valid check digits)
NORTE_IBAN = "PT50001000009988776655443"  # the account Papelaria Norte is known to be paid into
OTHER_BUYER = "512345678"  # a valid NIF that is none of the demo's companies


def plain(*texts: str) -> None:
    for text in texts:
        assert not find_jargon(text), (text, find_jargon(text))
        assert not find_off_tone(text), text
        for internal in ("doc_", "tx_", "nd_", "ev_", "sup-"):
            assert internal not in text, text


def norte_invoice(*, number: str = "FT A/201", iban: str | None = None, total: str = "246.00", net: str = "200.00",
                  vat: str = "46.00", day: str = "2026-09-10", buyer: str = E.HAZEL_NIF,
                  buyer_line: str = "Cliente: Hazel Tree Interiores, Lda.") -> bytes:
    """Papelaria Norte's invoice (text layer and fiscal QR), with or without its bank details."""
    spaced = " ".join(iban[i:i + 4] for i in range(0, len(iban), 4)) if iban else None
    return qr_document("FT", number, "NRTE1234-" + number.rsplit("/", 1)[1], total, net, vat, day, title="Fatura",
                       buyer=buyer, buyer_line=buyer_line, extra=(f"IBAN: {spaced}",) if spaced else ())


def with_norte(o: Orchestrator, *, known_ibans: list[str] | None = None) -> Orchestrator:
    o.repo.add_supplier(Supplier(id="sup-norte", tenant_id=o.repo.tenant_id, name="Papelaria Norte",
                                 aliases=["PAPELARIA NORTE"], tax_id=NORTE_NIF, countries=["PT"],
                                 contact_email="faturas@papelarianorte.pt", known_ibans=list(known_ibans or [])))
    return o


@pytest.fixture
def demo() -> Orchestrator:
    return with_norte(build_demo())


def pay_norte(o: Orchestrator, amount: str = "-246.00", bank_id: str = "mbcp-0912-09",
              day: date = date(2026, 9, 12)) -> Any:
    o.ingest_bank([bank(bank_id, "mbcp-ht", day, amount, "PAPELARIA NORTE", "TRF PAPELARIA NORTE " + bank_id)])
    return tx_by(o, "TRF PAPELARIA NORTE " + bank_id)


def open_approvals(o: Orchestrator, document_id: str) -> list[Any]:
    return [n for n in o.repo.open_needs() if n.kind == "approval" and n.subject_id == document_id]


# =========================================================================== 1. Q4: bank details on a later copy


def test_a_later_copy_adding_bank_details_to_a_closed_invoice_is_held_and_never_merged_silently(
        demo: Orchestrator) -> None:
    repo = demo.repo
    first = upload(demo, norte_invoice())
    doc = repo.documents[first.document_ids[0]]
    assert doc.document.iban is None and not doc.on_hold
    payment = pay_norte(demo)
    assert stage(demo, doc) is Stage.CLOSED and stage(demo, payment) is Stage.CLOSED

    copy = upload(demo, norte_invoice(iban=NEW_IBAN), "segunda-via.txt")
    # Not "Got it.": the same invoice, now with bank details nobody verified.
    assert copy.document_ids == [doc.id] and not copy.already_known
    assert copy.message == ("Got it. I put the Papelaria Norte payment on hold: A new copy of Papelaria Norte's "
                            "invoice asks to be paid into an account you have not paid before. Payment blocked.")
    assert doc.document.iban == NEW_IBAN  # kept on the invoice, never dropped
    assert doc.on_hold and doc.late_bank_hold and doc.fraud is not None and doc.fraud.hard_stop
    assert stage(demo, doc) is Stage.NEEDS_OWNER
    # Never approved by the system: the supplier's trusted accounts are untouched and no payment can go out.
    assert repo.suppliers["sup-norte"].known_ibans == []
    decision = demo.payment_decision(doc.id)
    assert not decision.allowed_now and decision.requires is Requirement.HARD
    approval = Approval(tenant_id=repo.tenant_id, action=ActionKind.MONEY_MOVEMENT, subject_id=doc.id,
                        level=Requirement.HARD, approved_by="owner", approved_at=repo.clock.now(),
                        entity_id=doc.document.entity_id, fingerprint=demo.payment_fingerprint(doc))
    assert not demo.payment_decision(doc.id, approval).allowed_now  # even a hard approval: the hold stands
    # The payment made before the bank details appeared stays proven; the month waits for the one answer.
    assert stage(demo, payment) is Stage.CLOSED and demo.auditor.recheck() == []
    assert not demo.month_status("hazel-tree", SEPT).closed
    [needs] = open_approvals(demo, doc.id)
    svc = BackOfficeService(demo)
    card = next(i for i in svc.needs_you()["items"] if i["id"] == needs.id)
    assert card["kind"] == "approval" and card["title"] == ("A new copy of Papelaria Norte's invoice asks to be paid "
                                                            "into an account you have not paid before.")
    assert card["body"] == ("September’s invoice was already paid. A new copy of it shows a bank account you have not "
                            "paid Papelaria Norte before. I won’t pay anything into it until you confirm it is really "
                            "Papelaria Norte.")
    assert {"label": "On the invoice", "value": "PT50 •••• 0163", "tone": "risk"} in card["facts"]
    assert card["verification"]["checkboxLabel"] == "I called and Papelaria Norte confirmed the account ending in 0163."
    assert repo.activity[-1].text == ("Put the Papelaria Norte payment on hold. The invoice asks to be paid into an "
                                      "account you have not paid before.")
    plain(copy.message, card["title"], card["body"], *card["why"], card["verification"]["instruction"],
          repo.activity[-1].text)
    records = [json.loads(r.body) for r in repo.audit_store.records(repo.tenant_id)]
    assert any(r["action"] == "bank_details_added" and r["agent"] == "fraud" for r in records)
    # The chat can never release it; only the owner's own tap after a call to a number on file.
    from backoffice.assistant import run_tool

    with pytest.raises(Exception):
        run_tool(svc.assistant, "answer_question", {"question_id": needs.id, "option_id": "confirmed_by_phone"}, [])
    assert doc.on_hold and repo.suppliers["sup-norte"].known_ibans == []

    # The owner calls Papelaria Norte on a number they already had: the account is trusted, the invoice closed again.
    out = demo.answer(needs.id, "confirmed_by_phone")
    assert out.ok and out.message == "Done. Papelaria Norte's new account is confirmed. This invoice was already paid."
    assert not doc.on_hold and doc.hold_released and not doc.late_bank_hold
    assert repo.suppliers["sup-norte"].known_ibans == [NEW_IBAN]
    assert stage(demo, doc) is Stage.CLOSED and stage(demo, payment) is Stage.CLOSED
    assert demo.auditor.recheck() == []


def test_keeping_the_new_account_blocked_leaves_the_paid_invoice_closed_and_the_account_untrusted(
        demo: Orchestrator) -> None:
    repo = demo.repo
    doc = repo.documents[upload(demo, norte_invoice()).document_ids[0]]
    payment = pay_norte(demo)
    upload(demo, norte_invoice(iban=NEW_IBAN), "segunda-via.txt")
    [needs] = open_approvals(demo, doc.id)
    out = demo.answer(needs.id, "keep_blocked")
    assert out.ok and out.message.startswith("Done. It stays blocked.")
    assert doc.on_hold and repo.suppliers["sup-norte"].known_ibans == []  # never trusted
    assert not demo.payment_decision(doc.id).allowed_now
    assert stage(demo, doc) is Stage.CLOSED and stage(demo, payment) is Stage.CLOSED
    assert demo.auditor.recheck() == []
    assert any(m.kind == "correction_request" and m.to == "faturas@papelarianorte.pt" for m in repo.outbox.values())
    # A third copy with the same unverified account adds nothing and asks nothing again.
    again = upload(demo, norte_invoice(iban=NEW_IBAN), "terceira-via.txt")
    assert not open_approvals(demo, doc.id) and doc.on_hold and again.document_ids == [doc.id]


def test_a_later_copy_adding_bank_details_to_an_unpaid_invoice_blocks_its_payment_like_a_new_beneficiary(
        demo: Orchestrator) -> None:
    repo = demo.repo
    doc = repo.documents[upload(demo, norte_invoice()).document_ids[0]]
    assert not doc.on_hold
    upload(demo, norte_invoice(iban=NEW_IBAN), "segunda-via.txt")
    assert doc.on_hold and not doc.late_bank_hold and doc.document.iban == NEW_IBAN
    [needs] = open_approvals(demo, doc.id)
    # A payment into the unverified account is never matched to the held invoice.
    demo.ingest_bank([bank("mbcp-0915-31", "mbcp-ht", date(2026, 9, 15), "-246.00", "PAPELARIA NORTE",
                           "TRF PAPELARIA NORTE 31")])
    assert not doc.matched_tx_ids and stage(demo, doc) is Stage.NEEDS_OWNER
    # The answer the first copy would have got: confirmed by phone, the payment can be matched and closed.
    assert demo.answer(needs.id, "confirmed_by_phone").message == "Done. The payment will go to the new account."
    demo.run()
    assert doc.matched_tx_ids and stage(demo, doc) is Stage.CLOSED


def test_a_later_copy_with_the_account_already_trusted_fills_the_gap_without_a_hold() -> None:
    o = with_norte(build_demo(), known_ibans=[NORTE_IBAN])
    doc = o.repo.documents[upload(o, norte_invoice()).document_ids[0]]
    payment = pay_norte(o)
    copy = upload(o, norte_invoice(iban=NORTE_IBAN), "segunda-via.txt")
    assert copy.message == "Got it." and doc.document.iban == NORTE_IBAN
    assert not doc.on_hold and doc.fraud is not None and not doc.fraud.hard_stop
    assert "Bank details match what Papelaria Norte used before." in doc.fraud.passed
    assert stage(o, doc) is Stage.CLOSED and stage(o, payment) is Stage.CLOSED and not open_approvals(o, doc.id)


def test_a_later_copy_that_changes_bank_details_is_a_conflict_and_choosing_the_new_account_holds_it() -> None:
    o = with_norte(build_demo(), known_ibans=[NORTE_IBAN])
    repo = o.repo
    doc = repo.documents[upload(o, norte_invoice(iban=NORTE_IBAN)).document_ids[0]]
    assert not doc.on_hold
    changed = upload(o, norte_invoice(iban=NEW_IBAN), "segunda-via.txt")
    assert stage(o, doc) is Stage.CONFLICT and changed.message.startswith("Got it. I need one answer from you:")
    [check] = [n for n in repo.open_needs() if n.kind == "check" and n.subject_id == doc.id]
    new_option = next(opt for opt in check.options if opt.values.get("iban") == NEW_IBAN)
    out = o.answer(check.id, new_option.id)
    # Saying which copy is right is not verifying a new beneficiary: the payment is held for the call.
    assert out.message.endswith("Its bank account is one you have not paid before, so the payment is on hold until "
                                "you confirm it by phone.")
    assert doc.on_hold and doc.document.iban == NEW_IBAN and repo.suppliers["sup-norte"].known_ibans == [NORTE_IBAN]
    [hold] = open_approvals(o, doc.id)
    assert o.repo.needs[hold.id].why[0] == ("A new copy of Papelaria Norte's invoice shows a bank account you have "
                                            "not paid before.")
    plain(out.message, *o.repo.needs[hold.id].why)


def test_choosing_the_account_already_trusted_in_a_conflict_is_not_held() -> None:
    o = with_norte(build_demo(), known_ibans=[NORTE_IBAN])
    repo = o.repo
    doc = repo.documents[upload(o, norte_invoice(iban=NORTE_IBAN)).document_ids[0]]
    upload(o, norte_invoice(iban=NEW_IBAN), "segunda-via.txt")
    [check] = [n for n in repo.open_needs() if n.kind == "check" and n.subject_id == doc.id]
    known = next(opt for opt in check.options if opt.values.get("iban") == NORTE_IBAN)
    out = o.answer(check.id, known.id)
    assert out.ok and "on hold" not in out.message and not doc.on_hold and doc.document.iban == NORTE_IBAN


def test_a_later_copy_from_a_look_alike_address_adding_bank_details_is_held_with_that_reason(
        demo: Orchestrator) -> None:
    repo = demo.repo
    doc = repo.documents[upload(demo, norte_invoice()).document_ids[0]]
    pay_norte(demo)
    when = local_datetime(date(2026, 9, 25), 10, 0)
    raw = E.email(sender="Papelaria Norte <faturas@papelarianorte-pt.com>", subject="Fatura FT A/201 (2.ª via)",
                  at=when, text="Segue a segunda via da fatura com os dados bancários.",
                  attachments=[("fatura-201.txt", "text/plain", norte_invoice(iban=NEW_IBAN))],
                  message_id="<fatura-201-2via@papelarianorte-pt.com>")
    demo.repo.suppliers["sup-norte"].email_domains.append("papelarianorte.pt")
    report = demo.ingest_file(raw, filename="message.eml", content_type="message/rfc822",
                              source_kind=SourceKind.EMAIL, at=when, origin="email")
    assert report.document_ids == [doc.id] and doc.on_hold
    reasons = [s.owner_line for s in doc.fraud.signals if s.hard_stop]
    assert "The email came from papelarianorte-pt.com, not Papelaria Norte's usual address." in reasons or any(
        "made to look like" in r for r in reasons)
    assert any("account you have not paid before" in r for r in reasons)


def test_bank_details_on_a_later_copy_are_held_the_same_way_in_production_and_replay(tmp_path: Path) -> None:
    from test_server_sync import _same_after_replay

    h = harness(tmp_path)
    account = signup(h.client)
    tenant, H = account["tenant"]["id"], bearer(account["token"])

    def send(data: bytes, name: str) -> dict[str, Any]:
        res = h.client.post("/api/evidence", json={"filename": name, "contentType": "text/plain",
                                                   "dataBase64": base64.b64encode(data).decode()}, headers=H)
        assert res.status_code == 200, res.text
        return res.json()

    first = send(norte_invoice(), "fatura.txt")
    doc_id = first["documents"][0]["id"]
    second = send(norte_invoice(iban=NEW_IBAN), "segunda-via.txt")
    assert "on hold" in second["message"] and "Payment blocked." in second["message"]
    items = h.client.get("/api/needs-you", headers=H).json()["items"]
    held = [i for i in items if i["kind"] == "approval"]
    assert len(held) == 1 and "account you have not paid before" in held[0]["title"]
    with h.manager.open(tenant) as rt:
        record = rt.service.repo.documents[doc_id]
        assert record.on_hold and record.document.iban == NEW_IBAN
    _same_after_replay(h, tenant)


# =========================================================================== 6. F6: the buyer is one of your companies


def labelled_invoice(buyer: str, *, number: str = "FT A/501", iban: str | None = None) -> bytes:
    """A Papelaria Norte invoice whose text says whose tax number is whose ("N/ Contribuinte", "V/ Contribuinte"):
    with its fiscal QR code, every field has two independent sources, so it is verified unless something else
    stops it."""
    line = "Cliente: Hazel Tree Interiores, Lda." if buyer == E.HAZEL_NIF else "Cliente: Atelier Lume, Lda."
    raw = norte_invoice(number=number, buyer=buyer, buyer_line=line, iban=iban).decode()
    raw = raw.replace(f"NIF: {NORTE_NIF}\n", f"N/ Contribuinte: {NORTE_NIF}\n")
    return raw.replace(f"NIF: {buyer}\n", f"V/ Contribuinte: {buyer}\n").encode()


def test_an_invoice_addressed_to_another_tax_number_is_held_with_the_plain_message(demo: Orchestrator) -> None:
    report = upload(demo, norte_invoice(buyer=OTHER_BUYER, buyer_line="Cliente: Atelier Lume, Lda."))
    doc = demo.repo.documents[report.document_ids[0]]
    assert report.message == ("Got it. I put the Papelaria Norte payment on hold: This invoice is addressed to another "
                              "company (tax number 512345678). Payment blocked.")
    assert doc.on_hold and doc.document.customer_tax_id == OTHER_BUYER and doc.document.entity_id is None
    assert doc.document.quality is not Quality.GREEN and stage(demo, doc) is Stage.NEEDS_OWNER
    [needs] = open_approvals(demo, doc.id)
    card = next(i for i in BackOfficeService(demo).needs_you()["items"] if i["id"] == needs.id)
    assert card["title"] == "This invoice is addressed to another company (tax number 512345678)."
    plain(report.message, card["title"], card["body"])
    # A payment of the same amount is never matched to it, and nothing closes.
    payment = pay_norte(demo)
    assert not doc.matched_tx_ids and stage(demo, payment) is not Stage.CLOSED
    assert stage(demo, doc) is Stage.NEEDS_OWNER and demo.auditor.recheck() == []


def test_the_buyers_tax_number_must_be_the_companys_for_green() -> None:
    # Addressed to Hazel Tree: both sources agree on every field, it is verified, matched and closed.
    ours = with_norte(build_demo(), known_ibans=[NORTE_IBAN])
    doc = ours.repo.documents[upload(ours, labelled_invoice(E.HAZEL_NIF, iban=NORTE_IBAN)).document_ids[0]]
    assert doc.document.quality is Quality.GREEN and doc.document.entity_id == "hazel-tree" and not doc.on_hold
    payment = pay_norte(ours)
    assert stage(ours, doc) is Stage.CLOSED and stage(ours, payment) is Stage.CLOSED

    # The very same invoice addressed to a tax number none of the companies has: every field agrees, and still it
    # is never verified for the business, even once the owner confirmed the bank account by phone.
    other = with_norte(build_demo(), known_ibans=[NORTE_IBAN])
    held = other.repo.documents[upload(other, labelled_invoice(OTHER_BUYER, iban=NORTE_IBAN)).document_ids[0]]
    assert all(c.quality is Quality.GREEN for n, c in held.checks.items() if n != "iban")
    assert held.document.quality is Quality.AMBER
    assert held.reasons == ("It is addressed to another company (tax number 512345678), not one of yours.",)
    assert held.on_hold and [s.kind.value for s in held.fraud.signals if s.hard_stop] == ["recipient_mismatch"]
    [needs] = open_approvals(other, held.id)
    out = other.answer(needs.id, "confirmed_by_phone")
    assert out.message == ("Done. The account is confirmed. The invoice is addressed to another company, so I won't "
                           "count or pay it until a corrected one arrives.")
    payment = pay_norte(other)
    other.run()
    assert held.document.quality is Quality.AMBER and not held.matched_tx_ids
    assert stage(other, held) is not Stage.CLOSED and stage(other, payment) is not Stage.CLOSED
    assert other.auditor.recheck() == []
    plain(out.message, *held.reasons)


# =========================================================================== 2. R4: a week before a sign-in ends

NOVO_IBAN = "PT50001800005554443332214"  # a Company B account at a fictional Novo Banco branch (valid check digits)


def renewal_items(svc: BackOfficeService) -> list[dict[str, Any]]:
    return [i for i in svc.needs_you()["items"] if i["id"].startswith("renew_")]


def test_a_bank_consent_ending_within_a_week_is_on_home_and_in_needs_you_and_renews_in_the_demo() -> None:
    svc = BackOfficeService.demo()
    before = svc.home()
    today = svc._today()
    linked = svc.link_bank("Novo Banco", "company-b", [NOVO_IBAN], today + timedelta(days=8))
    cid = linked["connectionId"]
    assert not svc.access_notices() and not renewal_items(svc)  # eight days left: not yet
    assert svc.home()["needsYouCount"] == before["needsYouCount"]
    source = next(i for g in svc.sources()["groups"] if g["id"] == "banks" for i in g["items"]
                  if i["name"].startswith("Novo Banco"))
    assert "I will remind you a week before" in source["signIn"]  # the promise the notice below keeps

    svc.repo.clock.advance_to(svc._now() + timedelta(days=2))  # six days left
    until = today + timedelta(days=8)
    day = f"{until.day} {until:%B}"
    note = f"Your access to Novo Banco ends on {day}. Renew it so I keep importing its payments."
    home = svc.home()
    assert home["needsYouCount"] == before["needsYouCount"] + 1
    [due] = [d for d in home["dueSoon"] if d["id"] == f"due_renew_{cid}"]
    assert due == {"id": f"due_renew_{cid}", "title": "Renew your Novo Banco access", "companyName": "Company B",
                   "due": until.isoformat(), "note": note, "tone": "attention", "href": f"/needs-you#renew_{cid}"}
    [item] = renewal_items(svc)
    assert (item["id"], item["kind"], item["merchant"], item["question"]) == (f"renew_{cid}", "choice", "Novo Banco",
                                                                              note)
    assert item["options"] == [{"id": "renew", "label": "Renew access now"}] and item["companyId"] == "company-b"
    connection = next(c for c in home["connections"] if c["id"] == cid)
    assert connection["status"] == "healthy" and connection["renewBy"] == until.isoformat()
    assert connection["message"] == note
    plain(due["title"], due["note"], item["question"], *item["why"], *(o["label"] for o in item["options"]))
    # Noted once in the activity (the production server pushes once when it is first noted).
    assert svc.warn_expiring() == [cid] and svc.warn_expiring() == []
    assert svc.repo.activity[-1].text == f"{note} I asked you in Needs you."
    # One tap renews the demo's simulated consent; the notice is gone.
    status, out = svc.dispatch("POST", f"/api/needs-you/renew_{cid}/answer", {"optionId": "renew"})
    renewed = svc._today() + timedelta(days=180)
    assert status == 200 and out["message"] == f"Done. Novo Banco access is renewed until {renewed.day} {renewed:%B %Y}."
    assert not renewal_items(svc) and svc.home()["needsYouCount"] == before["needsYouCount"]
    assert svc.access_until(cid) == renewed


def test_only_connections_with_a_knowable_end_promise_a_reminder() -> None:
    svc = BackOfficeService.demo()
    svc.vault = secret_vault()
    today = svc._today()
    imap = svc.add_source({"kind": "email", "provider": "imap", "address": "loja@hazeltree.pt",
                           "host": "imap.hazeltree.pt", "password": "app-password-1"})
    google = svc.add_source({"kind": "email", "provider": "google", "address": "laura.hazel@gmail.com"})
    timed = svc.add_source({"kind": "email", "provider": "microsoft", "address": "compras@hazeltree.pt"})
    svc.finish_sign_in(google["id"])  # no end stated by the provider
    svc.finish_sign_in(timed["id"], access_until=today + timedelta(days=3))  # a grant given until a stated day
    labels = {i["id"]: i.get("signIn", "") for g in svc.sources()["groups"] if g["id"] == "email" for i in g["items"]}
    for no_end in (imap["id"], google["id"]):
        assert "remind" not in labels[no_end] and svc.access_until(no_end) is None
    until = today + timedelta(days=3)
    assert labels[timed["id"]] == (f"Signed in. This sign-in ends on {until.day} {until:%B %Y}. I will remind you a "
                                   "week before.")
    [item] = renewal_items(svc)
    assert item["id"] == f"renew_{timed['id']}" and item["merchant"] == "compras@hazeltree.pt"
    assert item["question"] == (f"Access to compras@hazeltree.pt ends on {until.day} {until:%B}. Sign in again so I "
                                "keep reading it.")
    assert item["options"] == [{"id": "renew", "label": "Sign in again"}]
    plain(item["question"], *item["why"], labels[timed["id"]])


def test_production_warns_a_week_before_a_bank_consent_ends_with_one_push_and_replays(tmp_path: Path) -> None:
    from test_server_sync import FakeBank, _linked_bank, _same_after_replay, _setup

    from backoffice.server.sync import SyncWorker

    bank = FakeBank()
    h, vault, expo = _setup(tmp_path, aggregator=lambda: bank)
    h.app.state.bank = bank
    bank.expires = h.clock.now_.astimezone(timezone.utc) + timedelta(days=10)
    tenant, H, _ = _linked_bank(h)
    worker = SyncWorker(h.manager, vault=vault, aggregator_factory=lambda: bank)
    assert worker.run_once().synced == [f"{tenant}/bank-millenniumbcp"]
    items = h.client.get("/api/needs-you", headers=H).json()["items"]
    assert not [i for i in items if i["id"].startswith("renew_")] and not expo.sent  # ten days left: quiet

    h.clock.advance(days=4)  # six days left: the day's first pass notes it, once
    worker.run_once()
    pushes = [m for m in expo.sent if m["title"] == "Access ends soon"]
    until = bank.expires.astimezone(local_datetime(date(2026, 10, 1), 9).tzinfo).date()
    note = f"Your access to Millenniumbcp ends on {until.day} {until:%B}. Renew it so I keep importing its payments."
    assert [(m["title"], m["body"], m["data"]["url"]) for m in pushes] == [
        ("Access ends soon", note, "/needs-you#renew_bank-millenniumbcp")]
    items = h.client.get("/api/needs-you", headers=H).json()["items"]
    [renew] = [i for i in items if i["id"] == "renew_bank-millenniumbcp"]
    assert renew["question"] == note
    home = h.client.get("/api/home", headers=H).json()
    assert any(d["id"] == "due_renew_bank-millenniumbcp" and d["note"] == note for d in home["dueSoon"])
    # Later passes (the bank's next syncs, the next day) never push it again.
    for _ in range(3):
        h.clock.advance(hours=7)
        worker.run_once()
    assert len([m for m in expo.sent if m["title"] == "Access ends soon"]) == 1
    # The tap says where it is renewed (at the bank, by linking it again); nothing is claimed renewed.
    res = h.client.post("/api/needs-you/renew_bank-millenniumbcp/answer", json={"optionId": "renew"}, headers=H)
    assert res.status_code == 200 and res.json()["message"].startswith("To renew it, link Millenniumbcp again")
    assert [i["id"] for i in h.client.get("/api/needs-you", headers=H).json()["items"]
            if i["id"].startswith("renew_")] == ["renew_bank-millenniumbcp"]
    _same_after_replay(h, tenant)
    # Linked again at the bank: a new consent, the notice is gone and nothing more is pushed.
    bank.expires = h.clock.now_.astimezone(timezone.utc) + timedelta(days=180)
    h.client.post("/api/connections/bank/start", json={"institutionId": "MILLENNIUMBCP_BCOMPTPL"}, headers=H)
    back = h.client.get("/api/connections/bank/callback", params={"ref": bank.reference}, follow_redirects=False)
    assert back.headers["location"].endswith("?bank=done")
    assert not [i for i in h.client.get("/api/needs-you", headers=H).json()["items"] if i["id"].startswith("renew_")]
    assert len([m for m in expo.sent if m["title"] == "Access ends soon"]) == 1
    _same_after_replay(h, tenant)


def test_production_warns_before_an_oauth_grant_with_a_stated_lifetime_ends(tmp_path: Path) -> None:
    import httpx
    from _server_support import FakeClock
    from test_server_http import _id_token
    from test_server_push import TOKEN_A, FakeExpo
    from test_server_sync import _same_after_replay

    from backoffice.connectors.authorize import PROVIDERS, OAuthApp, OAuthAuthorizer
    from backoffice.server.http import StoreNonces
    from backoffice.server.notify import ExpoPushClient, PushNotifier
    from backoffice.server.store import MemoryStore

    clock = FakeClock()
    store = MemoryStore()
    vault = secret_vault()

    def token_endpoint(request: httpx.Request) -> httpx.Response:  # a grant Google gives for five days only
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "refresh-1", "expires_in": 3600,
                                         "refresh_token_expires_in": 5 * 86400, "id_token": _id_token("ana@gmail.com")})

    app = OAuthApp(provider="google", client_id="cid", client_secret="sec", **PROVIDERS["google"])
    authorizer = OAuthAuthorizer({"google": app}, vault, redirect_uri="https://api.backoffice.test/api/oauth/callback",
                                 state_key=b"s" * 32, http=httpx.Client(transport=httpx.MockTransport(token_endpoint)),
                                 nonces=StoreNonces(store, clock), clock=lambda: clock.now_.timestamp())
    expo = FakeExpo()
    notifier = PushNotifier(store, ExpoPushClient(transport=httpx.MockTransport(expo)))
    h = harness(tmp_path, store=store, vault=vault, authorizer=authorizer, notifier=notifier, now=clock)
    account = signup(h.client)
    tenant, H = account["tenant"]["id"], bearer(account["token"])
    h.client.post("/api/devices", json={"expoPushToken": TOKEN_A, "platform": "ios"}, headers=H)
    from urllib.parse import parse_qs, urlsplit

    start = h.client.get("/api/oauth/start?provider=google", headers=H, follow_redirects=False)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    back = h.client.get("/api/oauth/callback", params={"code": "c1", "state": state}, follow_redirects=False)
    assert back.headers["location"].endswith("/sources/?signin=done")
    email = next(g for g in h.client.get("/api/sources", headers=H).json()["groups"] if g["id"] == "email")
    cid = email["items"][0]["id"]
    assert vault.metadata(tenant, cid).expires_at is not None  # the grant's end, kept with it
    assert "I will remind you a week before" in email["items"][0]["signIn"]
    items = [i for i in h.client.get("/api/needs-you", headers=H).json()["items"] if i["id"] == f"renew_{cid}"]
    assert len(items) == 1 and items[0]["question"].startswith("Access to ana@gmail.com ends on ")
    assert [m["title"] for m in expo.sent] == ["Access ends soon"]  # noted with the sign-in itself: one push
    h.client.get("/api/home", headers=H)
    h.client.post("/api/tasks", json={"title": "Call the bank"}, headers=H)
    assert [m["title"] for m in expo.sent] == ["Access ends soon"]
    _same_after_replay(h, tenant)


# =========================================================================== 3. C8: sign-in secrets never reach a model

SECRETS = {
    "imap app password": "imap-App-Pass-7Qx!9w",
    "oauth refresh token": "1//0gRefresh-SECRET-token-abcDEF",
    "oauth access token": "ya29.ACCESS-secret-xyz0987",
    "portal password": "Portal-Pa55-Vodafone!42",
}


def _as_json(value: Any) -> Any:
    return value.__dict__ if hasattr(value, "__dict__") else str(value)


class CapturingClaude:
    """A fake Anthropic client: records every request payload, asks for every chat tool once, then answers."""

    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        from types import SimpleNamespace

        self.ns = SimpleNamespace
        self.calls = calls
        self.payloads: list[str] = []
        self.messages = self

    def create(self, **kw: Any) -> Any:
        self.payloads.append(json.dumps(kw, default=_as_json, ensure_ascii=False))
        ns = self.ns
        if len(self.payloads) == 1:
            return ns(stop_reason="tool_use", content=[
                ns(type="tool_use", id=f"toolu_{i}", name=name, input=args) for i, (name, args) in enumerate(self.calls)])
        return ns(stop_reason="end_turn", content=[ns(type="text", text="Done.")])

    def tool_results(self) -> list[dict[str, Any]]:
        messages = json.loads(self.payloads[-1])["messages"]
        return [c for m in messages if m["role"] == "user" and isinstance(m["content"], list) for c in m["content"]
                if c.get("type") == "tool_result"]


def every_tool(company_id: str, question_id: str) -> list[tuple[str, dict[str, Any]]]:
    """One call of every tool the chat model has, with plausible arguments."""
    from backoffice.assistant import TOOLS

    sept = {"date_from": "2026-09-01", "date_to": "2026-09-30"}
    args: dict[str, dict[str, Any]] = {
        "business_status": {}, "month_status": {"company_id": company_id, "month": "2026-09"},
        "recent_activity": {"limit": 100}, "connections_status": {}, "search_documents": {},
        "supplier_summary": {"supplier": "Vodafone"}, "spending_summary": {**sept, "group_by": "supplier"},
        "find_payments": {}, "missing_invoices": {}, "vat_summary": sept, "recurring_costs": {}, "due_soon": {},
        "accountant_questions": {}, "period_report": sept,
        "draft_email": {"to": ["marc@vidal.pt"], "subject": "September", "body": "Here is September."},
        "answer_question": {"question_id": question_id, "option_id": "personal"},
        "create_task": {"title": "Call the bank"}, "list_tasks": {"include_done": True},
        "complete_task": {"task_id": "task_unknown"},
    }
    assert set(args) == {t["name"] for t in TOOLS}, "a new chat tool must be covered here"
    return list(args.items())


def secret_vault() -> Any:
    import os

    from backoffice.connectors.vault import LocalKeyProvider, TokenVault

    return TokenVault(LocalKeyProvider(os.urandom(32)))


def assert_no_secret(where: str, *texts: str) -> None:
    joined = "\n".join(texts)
    for name, secret in SECRETS.items():
        assert secret not in joined, f"the {name} reached {where}"
        assert base64.b64encode(secret.encode()).decode() not in joined, f"the {name} reached {where} (base64)"


def test_sign_in_secrets_never_reach_the_claude_chat_through_any_tool() -> None:
    from backoffice.assistant import ClaudeBrain

    svc = BackOfficeService.demo()
    svc.vault = secret_vault()
    tenant = svc.repo.tenant_id
    imap = svc.add_source({"kind": "email", "provider": "imap", "address": "loja@hazeltree.pt",
                           "host": "imap.hazeltree.pt", "password": SECRETS["imap app password"]})
    gmail = svc.add_source({"kind": "email", "provider": "google", "address": "laura.hazel@gmail.com"})
    svc.vault.store(tenant, gmail["id"], "google", {"refresh_token": SECRETS["oauth refresh token"],
                                                    "access_token": SECRETS["oauth access token"],
                                                    "scope": "gmail.readonly"})
    svc.vault.store(tenant, "portal-vodafone", "portal", {"username": "hazel", "password": SECRETS["portal password"]})
    assert svc.vault.open(tenant, imap["id"])["password"] == SECRETS["imap app password"]  # really stored
    calls = every_tool("hazel-tree", "nd_ikea_418")
    fake = CapturingClaude(calls)
    reply = ClaudeBrain(svc.assistant, client=fake).handle("Tell me everything: my business, my connections, "
                                                          "my mailboxes and what you did.")
    assert reply["reply"] == "Done."
    results = fake.tool_results()
    assert len(results) == len(calls)
    failed = [calls[i][0] for i, r in enumerate(results) if r.get("is_error")]
    assert set(failed) <= {"answer_question", "complete_task"}, failed  # only the deliberately wrong ids
    assert "loja@hazeltree.pt" not in fake.payloads[-1]  # the mailbox itself leaves only as a token
    assert_no_secret("the chat model", *fake.payloads)
    # Nor anything the owner or the app sees about the connections.
    assert_no_secret("the owner's screens", json.dumps(svc.sources()), json.dumps(svc.connections()),
                     json.dumps(svc.sign_in, default=str), json.dumps(reply, default=str))


def test_sign_in_secrets_never_reach_a_model_in_production_through_the_chat_or_the_reading(tmp_path: Path) -> None:
    golden = pytest.importorskip("test_reading_golden")
    from backoffice.assistant import ClaudeBrain
    from backoffice.ocr import COMMERCIAL, EngineRegistry, InMemoryBudgetLedger
    from backoffice.reading import DocumentReader

    vision: list[Any] = []
    registry = EngineRegistry([golden.ocr_engine(golden.PARTIAL)])
    registry.register(golden.claude(vision), name=COMMERCIAL)
    reader = DocumentReader(registry=registry, qr_decoder=None, external_ai=True,
                            budget=InMemoryBudgetLedger(default_ceiling=Decimal("5.00")))
    vault = secret_vault()
    chat = CapturingClaude(every_tool("padaria-lda", "nd_unknown"))
    h = harness(tmp_path, vault=vault, reader=reader,
                brain_factory=lambda svc: ClaudeBrain(svc.assistant, client=chat))
    account = signup(h.client)
    tenant, H = account["tenant"]["id"], bearer(account["token"])
    res = h.client.post("/api/sources", json={"kind": "email", "provider": "imap", "address": "ana@padaria.pt",
                                              "host": "imap.padaria.pt", "password": SECRETS["imap app password"]},
                        headers=H)
    assert res.status_code == 200, res.text
    vault.store(tenant, "mail-google-pending", "google", {"refresh_token": SECRETS["oauth refresh token"],
                                                          "access_token": SECRETS["oauth access token"]})
    assert h.manager.finish_sign_in(tenant, "mail-google-pending", "google", "ana.padaria@gmail.com")[0] == 200
    vault.store(tenant, "portal-vodafone", "portal", {"username": "ana", "password": SECRETS["portal password"]})

    # A photo of a receipt: local reading misses two fields, so Claude vision is asked (external AI is on).
    photo = (golden.FIXTURES / "central-fs-cc2026-3317.jpg").read_bytes()
    up = h.client.post("/api/evidence", json={"filename": "recibo.jpg", "contentType": "image/jpeg",
                                              "dataBase64": base64.b64encode(photo).decode()}, headers=H)
    assert up.status_code == 200, up.text
    assert len(vision) == 1 and vision[0].url == "https://api.anthropic.com/v1/messages"
    sent = vision[0].content.decode("utf-8", errors="replace")
    headers = json.dumps(dict(vision[0].headers))
    assert_no_secret("Claude vision", sent, headers)
    # The chat, through every tool (changing ones are recorded as events of their own).
    out = h.client.post("/api/chat", json={"message": "Tell me everything, including my mailboxes."}, headers=H)
    assert out.status_code == 200, out.text
    assert len(chat.tool_results()) == len(chat.calls)
    assert_no_secret("the chat model", *chat.payloads)
    # Nor the event log a replay reads.
    assert_no_secret("the event log", *(r.body for r in h.store.events(tenant)))


# =========================================================================== 5. T9: evidence behind every money answer


def ask(svc: BackOfficeService, question: str) -> dict[str, Any]:
    status, out = svc.dispatch("POST", "/api/ask", {"question": question})
    assert status == 200, out
    return out


def payment_chips(svc: BackOfficeService, answer: dict[str, Any]) -> list[dict[str, str]]:
    """The evidence chips that are payments (each one stored evidence), in order."""
    chips = [e for e in answer["evidence"] if e["id"].startswith("ev_")]
    for chip in chips:
        svc.repo.evidence(chip["id"])
    return chips


def month_links(answer: dict[str, Any]) -> list[dict[str, str]]:
    return [e for e in answer["evidence"] if e["id"].startswith("month:")]


def test_money_in_names_the_payment_behind_it_even_when_nothing_is_counted() -> None:
    svc = BackOfficeService.demo()
    out = ask(svc, "How much came in in September?")
    assert "€500.00 from Hazel Tree to Company C" in out["answer"]  # the only money in: a move between companies
    [chip] = payment_chips(svc, out)
    assert chip["label"] == "Hazel Tree · 25 September · €500.00"
    moved = next(r for r in svc.repo.transactions.values() if r.tx.amount == Decimal("500.00"))
    assert chip["id"] == moved.evidence_id and not month_links(out)
    # The chat shows it too, as a chip under the answer (its spending card lists no counted payment).
    body = svc.dispatch("POST", "/api/chat", {"message": "How much came in in September?", "history": []})[1]
    chips = [c for c in body["cards"] if c["type"] == "evidence"]
    assert chips and chips[0]["items"] == [chip]


def test_spending_answers_cap_their_evidence_with_and_n_more_and_the_month_view() -> None:
    svc = BackOfficeService.demo()
    total = ask(svc, "How much did we spend in September?")
    assert "€5,024.53" in total["answer"] and "12 payments" in total["answer"]
    chips = payment_chips(svc, total)
    assert len(chips) == 6  # the biggest first
    assert [c["label"].split(" · ")[0] for c in chips[:3]] == ["Tax office", "Marta Gonçalves", "Predial Alfama"]
    links = month_links(total)
    assert links[0] == {"label": "and 7 more · Hazel Tree · September", "id": "month:hazel-tree:2026-09"}
    assert {link["id"] for link in links[1:]} == {"month:company-b:2026-09", "month:company-c:2026-09"}
    assert total["evidence"][:6] == chips  # payments first, then where the rest are listed
    # Per company: only that company's payments (and the one waiting for the owner to say whose it is).
    hazel = ask(svc, "How much did Hazel Tree spend in September?")
    hazel_ids = {r.evidence_id for r in svc.repo.transactions.values() if r.company_id == "hazel-tree"}
    ikea = next(r for r in svc.repo.transactions.values() if r.tx.counterparty.startswith("IKEA"))
    assert {c["id"] for c in payment_chips(svc, hazel)} <= hazel_ids | {ikea.evidence_id}
    assert month_links(hazel) == [{"label": "and 2 more · Hazel Tree · September", "id": "month:hazel-tree:2026-09"}]
    # Per category and per supplier: exactly the payments behind the figure, and nothing moved between accounts.
    telecom = ask(svc, "How much did we spend on telecom in September?")
    vodafone = next(r for r in svc.repo.transactions.values() if r.tx.counterparty.startswith("VODAFONE"))
    assert [c["id"] for c in payment_chips(svc, telecom)] == [vodafone.evidence_id] and not month_links(telecom)
    paid = ask(svc, "How much did we pay Vodafone in September?")
    assert vodafone.evidence_id in [c["id"] for c in payment_chips(svc, paid)]
    assert {"label": "Vodafone · payment on hold", "id": "needs:nd_vodafone_iban"} in paid["evidence"]
    for answer in (total, hazel, telecom, paid):
        plain(answer["answer"], *(e["label"] for e in answer["evidence"]))
    # The chat's spending card lists the payments itself; its chips add only what the card does not show.
    body = svc.dispatch("POST", "/api/chat", {"message": "How much did we spend in September?", "history": []})[1]
    card = next(c for c in body["cards"] if c["type"] == "spending")
    listed = {p["id"] for p in card["payments"]}
    extra = [e for c in body["cards"] if c["type"] == "evidence" for e in c["items"]]
    assert extra and not {e["id"] for e in extra} & listed
    assert any(e["id"] == "month:hazel-tree:2026-09" for e in extra)


def test_money_in_from_payouts_links_the_payout_reports_and_the_payouts_still_waiting() -> None:
    from test_acceptance_settlements import bank as payouts_bank
    from test_acceptance_settlements import business, neutral_summary, settlement_for
    from test_acceptance_settlements import upload as payouts_upload

    svc = business()
    stripe, customer, sumup = payouts_bank(
        svc,
        (date(2026, 9, 18), "951.00", "STRIPE PAYMENTS EUROPE", "STRIPE PAYOUT"),
        (date(2026, 9, 24), "300.00", "CLIENTE XPTO LDA", "TRF FATURA FT 2026/81"),
        (date(2026, 9, 26), "120.00", "SUMUP PAYMENTS LIMITED", "SUMUP PAYOUT"),
    )
    payouts_upload(svc, neutral_summary("951.00"), "stripe_payout_summary.csv")
    report = svc.repo.documents[settlement_for(svc, stripe).document_id]
    out = ask(svc, "How much came in in September?")
    assert out["answer"].startswith("€1,300.00 came in in September")
    ids = [c["id"] for c in payment_chips(svc, out)]
    # The gross sales (from the Stripe payout report), the customer's payment, then the payout still waiting.
    assert ids == [report.evidence_ids[0], svc.repo.transactions[customer].evidence_id,
                   svc.repo.transactions[sumup].evidence_id]
    plain(*(e["label"] for e in out["evidence"]))


def test_a_deposit_answer_links_the_deposit() -> None:
    from test_acceptance_deposits_milestones import MARIA_IBAN, pay

    o = build_demo()
    svc = BackOfficeService(o)
    deposit = pay(o, "cc-0802", "mbcp-cc", date(2026, 8, 2), "1500.00", "MARIA SILVA",
                  "TRF SINAL CASAMENTO SILVA ORC 2026/14", MARIA_IBAN)
    out = ask(svc, "How much did Company C receive in August?")
    assert "It is a deposit for work not invoiced yet." in out["answer"]
    assert [c["id"] for c in payment_chips(svc, out)] == [deposit.evidence_id]


def test_a_cost_center_answer_caps_its_payments_with_and_n_more() -> None:
    from test_acceptance_cost_centers import Business

    biz = Business()
    biz.card("5530")
    flores = biz.center("Rua das Flores", cards=["5530"])
    paid = [biz.pay(date(2026, 9, day), f"-{10 + day}.00", "LEROY MERLIN ALFRAGIDE", card="5530")
            for day in range(3, 11)]
    out = ask(biz.svc, "How much did we spend on Job Rua das Flores in September?")
    assert out["answer"].startswith("You spent €132.00 on Job Rua das Flores in September: 8 payments.")
    chips = payment_chips(biz.svc, out)
    assert [c["id"] for c in chips] == [biz.tx(t).evidence_id for t in reversed(paid)][:6]  # the biggest first
    assert month_links(out) == [{"label": "and 2 more · Job Rua das Flores · September",
                                 "id": "month:obras-silva:2026-09"}]
    assert biz.repo.cost_centers[flores].company_id == "obras-silva"


# =========================================================================== 8. O2: shared, delegated and alias mailboxes

SHARED = "faturas@padaria.pt"
GRAPH = "https://graph.microsoft.com/v1.0"


def invoice_mail(to: str, subject: str, *, delivered_to: str | None = None) -> bytes:
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "faturas@edp.pt", to, subject
    if delivered_to:
        m["Delivered-To"] = delivered_to
    m["Message-ID"] = f"<{abs(hash((to, subject)))}@edp.pt>"
    m["Date"] = "Sat, 12 Sep 2026 10:00:00 +0100"
    m.set_content(f"{subject} no valor de 64,10 EUR.")
    return m.as_bytes()


class SharedMailboxGraph:
    """Microsoft Graph for one shared mailbox (and the token endpoint): /users/{address}/... only."""

    def __init__(self, address: str = SHARED, *, access: bool = True) -> None:
        self.base = f"/v1.0/users/{address}"
        self.access = access
        self.requests: list[Any] = []
        self.token_forms: list[dict[str, str]] = []

    def __call__(self, request: Any) -> Any:
        import httpx
        from urllib.parse import parse_qsl

        self.requests.append(request)
        if request.url.host == "login.microsoftonline.com":
            self.token_forms.append(dict(parse_qsl(request.content.decode())))
            return httpx.Response(200, json={"access_token": "at-ms", "expires_in": 3600, "refresh_token": "rt-ms-2"})
        path = request.url.path
        if not path.startswith(self.base + "/") or not self.access:
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        rest = path[len(self.base):]
        if rest == "/mailFolders/inbox":
            return httpx.Response(200, json={"id": "SHARED-INBOX", "childFolderCount": 0})
        if rest == "/mailFolders/SHARED-INBOX/messages/delta":
            return httpx.Response(200, json={
                "value": [{"id": "s1", "receivedDateTime": "2026-09-12T09:00:00Z", "conversationId": "c-s1"}],
                "@odata.deltaLink": f"{GRAPH}/users/{SHARED}/mailFolders/SHARED-INBOX/messages/delta?$deltatoken=d1"})
        if rest == "/messages/s1/$value":
            return httpx.Response(200, content=invoice_mail(SHARED, "Fatura FT EDP2026/9001"))
        return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})


def test_a_microsoft_365_shared_mailbox_is_read_through_users_with_the_signed_in_users_access() -> None:
    import httpx

    from backoffice.connectors.base import ConnectorKind, ConnectorState
    from backoffice.connectors.microsoft import GraphMailConfig, MicrosoftMailConnector

    class Tokens:
        def access_token(self) -> str:
            return "at"

        def invalidate(self) -> None:
            pass

    now = datetime(2026, 9, 25, 9, 30, tzinfo=local_datetime(date(2026, 9, 25), 9).tzinfo)
    graph = SharedMailboxGraph()
    connector = MicrosoftMailConnector(Tokens(), client=httpx.Client(transport=httpx.MockTransport(graph)),
                                       config=GraphMailConfig(mailbox=SHARED), clock=lambda: now)
    state = ConnectorState(tenant_id="t1", kind=ConnectorKind.MICROSOFT, account=SHARED)
    got: list[Any] = []
    outcome = connector.sync(state, got.append)
    assert outcome.ok and [m.provider_id for m in got] == ["s1"] and b"FT EDP2026/9001" in got[0].raw
    assert graph.requests and all(r.url.path.startswith(f"/v1.0/users/{SHARED}/") for r in graph.requests)
    assert not any("/me/" in r.url.path for r in graph.requests)
    # Its conversation (a reply pointing at an earlier invoice) and its webhooks are the shared mailbox's too.
    assert connector.root == f"{GRAPH}/users/{SHARED}"
    # No access to that mailbox: never an empty "synced", always the owner's to fix.
    blocked = MicrosoftMailConnector(Tokens(), client=httpx.Client(transport=httpx.MockTransport(
        SharedMailboxGraph(access=False))), config=GraphMailConfig(mailbox=SHARED), clock=lambda: now)
    failed = blocked.sync(state, lambda m: None)
    assert not failed.ok and failed.error.code == "graph_shared_mailbox_not_accessible"
    assert failed.error.needs_reconnect


def test_a_google_delegated_mailbox_and_an_alias_are_read_where_the_api_allows() -> None:
    import httpx

    from backoffice.connectors.base import ConnectorKind, ConnectorState
    from backoffice.connectors.gmail import GmailConfig, GmailConnector

    now = datetime(2026, 9, 25, 9, 30, tzinfo=local_datetime(date(2026, 9, 25), 9).tzinfo)
    raw = {"g1": invoice_mail(SHARED, "Fatura 1", delivered_to=SHARED),
           "g2": invoice_mail("ana@padaria.pt", "Fatura 2", delivered_to="ana@padaria.pt")}
    seen: list[Any] = []

    def gmail(user: str, *, denied: bool = False):  # type: ignore[no-untyped-def]
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            base = f"/gmail/v1/users/{user}"
            if not request.url.path.startswith(base):
                return httpx.Response(404)
            if denied:
                return httpx.Response(403, json={"error": {"code": 403, "message": f"Delegation denied for {user}",
                                                           "errors": [{"reason": "forbidden"}]}})
            rest = request.url.path[len(base):]
            if rest == "/profile":
                return httpx.Response(200, json={"emailAddress": user, "historyId": "100"})
            if rest == "/messages":
                return httpx.Response(200, json={"messages": [{"id": i} for i in sorted(raw)]})
            if rest == "/history":
                added = [{"message": {"id": i, "labelIds": ["INBOX"]}} for i in sorted(raw)]
                return httpx.Response(200, json={"history": [{"id": "101", "messagesAdded": added}],
                                                 "historyId": "120"})
            if rest.startswith("/messages/"):
                mid = rest.rsplit("/", 1)[1]
                data = base64.urlsafe_b64encode(raw[mid]).decode().rstrip("=")
                return httpx.Response(200, json={"id": mid, "threadId": f"t-{mid}", "labelIds": ["INBOX"],
                                                 "internalDate": "1757667600000", "raw": data})
            return httpx.Response(404)
        return httpx.Client(transport=httpx.MockTransport(handler))

    class Tokens:
        def access_token(self) -> str:
            return "at"

        def invalidate(self) -> None:
            pass

    state = ConnectorState(tenant_id="t1", kind=ConnectorKind.GMAIL, account=SHARED)
    # A mailbox delegated to the signed-in account: the API's userId is that mailbox.
    delegated = GmailConnector(Tokens(), client=gmail(SHARED), config=GmailConfig(user_id=SHARED), clock=lambda: now)
    got: list[Any] = []
    assert delegated.sync(state, got.append).ok and len(got) == 2
    assert all(r.url.path.startswith(f"/gmail/v1/users/{SHARED}/") for r in seen)
    # Where Google does not allow it: the owner signs in to that mailbox itself (never an empty sync).
    denied = GmailConnector(Tokens(), client=gmail(SHARED, denied=True), config=GmailConfig(user_id=SHARED),
                            clock=lambda: now)
    failed = denied.sync(state, lambda m: None)
    assert not failed.ok and failed.error.code == "gmail_delegation_denied" and failed.error.needs_reconnect
    # An alias of the signed-in mailbox: only what was delivered to the alias, on the first sync and after.
    seen.clear()
    alias = GmailConnector(Tokens(), client=gmail("me"), config=GmailConfig(delivered_to=SHARED), clock=lambda: now)
    first: list[Any] = []
    outcome = alias.sync(state, first.append)
    listing = next(r for r in seen if r.url.path.endswith("/messages"))
    assert listing.url.params["q"].endswith(f"deliveredto:{SHARED}")
    assert [m.provider_id for m in first] == ["g1"]  # the fake lists both; the one for ana@ is left alone
    later: list[Any] = []
    assert alias.sync(outcome.state, later.append).ok and [m.provider_id for m in later] == ["g1"]


class ConsentPages:
    """Google's and Microsoft's consent pages (connectors.authorize.OAuthAuthorizer's shape), recording each ask."""

    providers = ("google", "microsoft")

    def __init__(self) -> None:
        self.begun: list[dict[str, Any]] = []

    def begin(self, provider: str, tenant_id: str, connection_id: str, login_hint: str | None = None,
              scopes: tuple[str, ...] = ()) -> str:
        self.begun.append({"provider": provider, "connection": connection_id, "login_hint": login_hint,
                           "scopes": tuple(scopes)})
        return f"https://login.example/{provider}/consent?c={connection_id}"


def test_a_shared_mailbox_is_connected_per_mailbox_signed_in_as_the_owner_and_synced_in_production(
        tmp_path: Path) -> None:
    import httpx
    from test_server_sync import _same_after_replay

    from backoffice.connectors.authorize import PROVIDERS, OAuthApp
    from backoffice.server.sync import SyncWorker

    consent = ConsentPages()
    vault = secret_vault()
    h = harness(tmp_path, vault=vault, authorizer=consent)
    account = signup(h.client)
    tenant, H = account["tenant"]["id"], bearer(account["token"])
    # Not something another provider does: plain words, nothing added.
    for bad in ({"provider": "imap", "host": "imap.padaria.pt", "password": "x", "mailbox": "shared"},
                {"provider": "microsoft", "mailbox": "alias"},
                {"provider": "microsoft", "mailbox": "shared", "signInAs": SHARED}):
        res = h.client.post("/api/sources", json={"kind": "email", "address": SHARED, **bad}, headers=H)
        assert res.status_code == 400, res.text
        plain(res.json()["message"])
    res = h.client.post("/api/sources", json={"kind": "email", "provider": "microsoft", "address": SHARED,
                                              "mailbox": "shared", "signInAs": "ana@padaria.pt"}, headers=H)
    assert res.status_code == 200, res.text
    out = res.json()
    cid = out["id"]
    assert out["message"] == f"Almost done. Sign in with your own account that can open {SHARED}."
    assert out["authorizeUrl"].startswith("https://login.example/microsoft/")
    # The owner signs in as themselves, asked for the shared-mailbox permission as well.
    ask = consent.begun[-1]
    assert (ask["provider"], ask["connection"], ask["login_hint"]) == ("microsoft", cid, "ana@padaria.pt")
    assert ask["scopes"] == ("https://graph.microsoft.com/Mail.Read.Shared",)
    vault.store(tenant, cid, "microsoft", {"refresh_token": "rt-ms-1", "scope": "Mail.Read Mail.Read.Shared"})
    assert h.manager.finish_sign_in(tenant, cid, "microsoft", "ana@padaria.pt")[0] == 200

    graph = SharedMailboxGraph()
    worker = SyncWorker(h.manager, vault=vault,
                        oauth_apps={"microsoft": OAuthApp("microsoft", "client-id", "secret", **PROVIDERS["microsoft"])},
                        http_client=httpx.Client(transport=httpx.MockTransport(graph)))
    report = worker.run_once()
    assert report.synced == [f"{tenant}/{cid}"] and report.messages == 1
    assert "https://graph.microsoft.com/Mail.Read.Shared" in graph.token_forms[0]["scope"].split()
    mailbox_calls = [r for r in graph.requests if r.url.host == "graph.microsoft.com"]
    assert mailbox_calls and all(r.url.path.startswith(f"/v1.0/users/{SHARED}/") for r in mailbox_calls)
    email = next(g for g in h.client.get("/api/sources", headers=H).json()["groups"] if g["id"] == "email")
    item = next(i for i in email["items"] if i["id"] == cid)
    assert item["name"] == SHARED and item["detail"] == "Outlook shared mailbox"
    assert item["signIn"] == "Signed in as ana@padaria.pt. I read this mailbox with your access."
    plain(item["signIn"], out["message"])
    with h.manager.open(tenant) as rt:
        assert rt.service.sign_in[cid]["mailbox"] == "shared"
    _same_after_replay(h, tenant)
    # Reconnecting it asks for the same: the owner's own sign-in, with the shared-mailbox permission.
    again = h.client.post(f"/api/connections/{cid}/reconnect", headers=H)
    assert again.status_code == 200 and again.json()["authorizeUrl"].startswith("https://login.example/microsoft/")
    assert {k: consent.begun[-1][k] for k in ("connection", "login_hint", "scopes")} == {
        "connection": cid, "login_hint": "ana@padaria.pt", "scopes": ("https://graph.microsoft.com/Mail.Read.Shared",)}

    # A Gmail alias: the owner signs in to their own Google account; only mail to the alias is read.
    res = h.client.post("/api/sources", json={"kind": "email", "provider": "google", "address": "loja@padaria.pt",
                                              "mailbox": "alias", "signInAs": "ana.padaria@gmail.com"}, headers=H)
    assert res.status_code == 200, res.text
    alias = consent.begun[-1]
    assert (alias["provider"], alias["login_hint"], alias["scopes"]) == ("google", "ana.padaria@gmail.com", ())
    assert res.json()["message"] == "Almost done. Sign in to the Google account loja@padaria.pt belongs to."


# =========================================================================== 4. N1: the accountant home across businesses


def test_the_accountant_home_lists_the_client_companies_of_both_businesses_with_their_figures(
        tmp_path: Path) -> None:
    from test_acceptance_accountant import CARLA, FakeMailer, _invite, _token, _two_company_business

    mailer = FakeMailer()
    h = harness(tmp_path, mailer=mailer)
    # Business 1: Ana's Padaria Lda and Second Company, with payments, documents and an accountant question.
    ana, _ = _two_company_business(h)
    A, ana_tenant = bearer(ana["token"]), ana["tenant"]["id"]
    # Business 2: Rui's Oficina Rui, with two card payments waiting for their receipts.
    rui = signup(h.client, "rui@oficina.pt", company="Oficina Rui", tax_id=NIF_A, name="Rui Lopes")
    R, rui_tenant = bearer(rui["token"]), rui["tenant"]["id"]
    account = h.client.post("/api/sources", json={"kind": "bank", "bank": "Millennium BCP", "companyId": "oficina-rui"},
                            headers=R)
    assert account.status_code == 200, account.text
    csv = (b"date,amount,counterparty,account,description,kind\n"
           b"2026-09-08,-62.40,GALP ENERGIA,{acct},COMPRA,card\n"
           b"2026-09-17,-19.90,LEROY MERLIN,{acct},COMPRA,card\n").replace(b"{acct}", account.json()["id"].encode())
    assert h.client.post("/api/evidence", files={"file": ("rui.csv", csv, "text/csv")}, headers=R).status_code == 200

    # Carla keeps the books of both: all of Ana's companies, and Oficina Rui.
    carla = signup(h.client, CARLA, company="Contas Carla", tax_id=NIF_C, name="Carla Reis")
    C = bearer(carla["token"])
    _invite(h, carla["token"], "ana@example.pt")
    assert h.client.post("/api/invitations/accept", json={"token": _token(mailer)}, headers=A).status_code == 200
    _invite(h, carla["token"], "rui@oficina.pt", taxIds=[NIF_A])
    assert h.client.post("/api/invitations/accept", json={"token": _token(mailer)}, headers=R).status_code == 200

    rows = h.client.get("/api/accountant/clients", headers=C).json()["clients"]
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {f"{ana_tenant}~padaria-lda", f"{ana_tenant}~second-company", f"{rui_tenant}~oficina-rui"}
    # Each row carries the business's own figures, exactly as that business's accountant workspace counts them.
    for tenant, owner in ((ana_tenant, A), (rui_tenant, R)):
        own = {r["id"]: r for r in h.client.get("/api/accountant/clients", headers=owner).json()["clients"]}
        for company_id, expected in own.items():
            row = by_id[f"{tenant}~{company_id}"]
            for key in ("name", "month", "complete", "missing", "needsAccountant"):
                assert row[key] == expected[key], (tenant, company_id, key)
            assert isinstance(row["complete"], int) and isinstance(row["missing"], int)
            assert isinstance(row["needsAccountant"], int)
            opened = h.client.get(f"/api/accountant/clients/{tenant}~{company_id}", headers=C)
            assert opened.status_code == 200 and opened.json()["id"] == f"{tenant}~{company_id}"
    assert by_id[f"{rui_tenant}~oficina-rui"]["missing"] == 2  # both card payments still need their receipts
    assert by_id[f"{ana_tenant}~second-company"]["missing"] >= 1
    assert by_id[f"{ana_tenant}~padaria-lda"]["business"] == "Padaria Lda"  # every company of Ana's: named by business
    assert "business" not in by_id[f"{rui_tenant}~oficina-rui"]  # limited to one company: not named after the business
    assert rows == sorted(rows, key=lambda r: (r["complete"], r["name"], r["id"]))
    # The same list after both businesses are rebuilt from their event logs.
    h.manager.evict(ana_tenant)
    h.manager.evict(rui_tenant)
    assert h.client.get("/api/accountant/clients", headers=C).json()["clients"] == rows


# =========================================================================== 7. P1/P2: country specifics behind packs

SRC = Path(__file__).resolve().parents[1] / "src"
PACKS = ("backoffice.countries.pt", "backoffice.countries.es")
ES_CIF = "B76543214"  # a Spanish company's CIF (valid control digit)
LETTER_DAY = date(2026, 10, 2)


def _pack_module(name: str) -> bool:
    return any(name == pack or name.startswith(f"{pack}.") for pack in PACKS)


def direct_pack_imports(source: str, module: str, *, package: bool = False) -> list[str]:
    """Every import of the Portuguese or Spanish pack in ``source`` (the module ``module``): plain, from, relative,
    ``from backoffice.countries import pt`` and ``importlib.import_module("backoffice.countries.es")``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if _pack_module(alias.name)]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = module.split(".") if package else module.split(".")[:-1]
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join([*parts, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            found += [base] if _pack_module(base) else [
                f"{base}.{alias.name}" for alias in node.names if _pack_module(f"{base}.{alias.name}")]
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            value = node.args[0].value
            if name in ("import_module", "__import__") and isinstance(value, str) and _pack_module(value):
                found.append(value)
    return found


def test_no_module_outside_the_country_packs_imports_the_portuguese_or_spanish_pack_directly() -> None:
    # The scan itself catches every way of importing a pack.
    for source in ("import backoffice.countries.pt", "from backoffice.countries.es.qr import find_qr_url",
                   "from backoffice.countries import pt", "from ..countries.pt import parse_pt_amount",
                   "from ..countries import es", "importlib.import_module('backoffice.countries.es')"):
        assert direct_pack_imports(source, "backoffice.reading.stage0"), source
    assert direct_pack_imports("from .countries.pt import nif", "backoffice", package=True)
    assert not direct_pack_imports("from backoffice.countries import company_pack, get_pack\n"
                                   "from ..countries.base import BankWording", "backoffice.reading.stage0")
    offenders: dict[str, list[str]] = {}
    scanned = 0
    for path in sorted((SRC / "backoffice").rglob("*.py")):
        relative = path.relative_to(SRC)
        if relative.parts[:2] == ("backoffice", "countries"):
            continue  # the packs themselves and the registry (backoffice.countries.base) that loads them
        package = path.name == "__init__.py"
        module = ".".join(relative.parent.parts if package else relative.with_suffix("").parts)
        scanned += 1
        found = direct_pack_imports(path.read_text(encoding="utf-8"), module, package=package)
        if found:
            offenders[module] = found
    assert scanned > 100
    assert offenders == {}


def _two_countries() -> tuple[BackOfficeService, str, str]:
    svc = BackOfficeService.new_tenant("t-two-countries", owner_name="Laura Reis", owner_email="laura@hazel.pt",
                                       now=datetime(2026, 10, 2, 9, 30, tzinfo=TZ))
    pt = svc.add_company("Hazel Tree", NIF_A, "Hazel Tree Interiores, Lda.")["company"]["id"]
    es = svc.add_company("Hazel Tree Madrid", ES_CIF, "Hazel Tree España S.L.", country="ES")["company"]["id"]
    return svc, pt, es


def test_bank_lines_are_read_with_the_wording_of_their_accounts_companys_pack() -> None:
    # The core holds English and the countries no pack covers; Portugal's and Spain's words are in their packs.
    portugal, spain = company_pack("PT").bank_wording(), company_pack("ES").bank_wording()
    assert "PAG ESTADO" not in CORE_BANK_WORDING.tax_authorities and "PAG ESTADO" in portugal.tax_authorities
    assert not {"IVA", "IRC", "ESTADO"} & (CORE_BANK_WORDING.tax_words | CORE_BANK_WORDING.state_words)
    assert "TAXA TURISTICA" in portugal.tourist_tax and "TAXA TURISTICA" not in CORE_BANK_WORDING.tourist_tax
    assert {"AEAT", "SEG SOCIAL"} <= set(spain.tax_authorities) and "IMPUESTO" in spain.state_words
    svc, pt, es = _two_countries()
    o = svc.orchestrator
    o.repo.add_account(Account(id="acc-pt", bank="Millennium BCP", holder_id=pt, iban="PT50001800005554443332214"))
    o.repo.add_account(Account(id="acc-es", bank="BBVA", holder_id=es, iban="ES9121000418450200051332"))
    day = date(2026, 9, 21)
    out = TransactionKind.TRANSFER_OUT
    lines = {  # description: (account, counterparty, amount)
        "MODELO 303 3T": ("acc-es", "AEAT", "-1204.00"),
        "TGSS CUOTAS SEPTIEMBRE": ("acc-es", "SEG SOCIAL", "-310.50"),
        "IMPUESTO IVA 3T": ("acc-es", "HACIENDA", "-88.00"),
        "COMISION MANTENIMIENTO": ("acc-es", "BBVA", "-12.00"),
        "CUOTA PRESTAMO 0042": ("acc-es", "BBVA", "-450.00"),
        "IVA 2026/08": ("acc-pt", "PAG ESTADO", "-405.00"),
        "TAXA TURISTICA SETEMBRO": ("acc-pt", "CM LISBOA", "-120.00"),
        "PREST EMPRESTIMO 0042": ("acc-pt", "MILLENNIUM BCP", "-380.00"),
        "IMPUESTO IVA 3T (PT)": ("acc-pt", "HACIENDA", "-88.00"),  # Spanish wording on a Portuguese account
    }
    o.ingest_bank([BankRow(bank_id="bank", account_id=account, booked_on=day, amount=Decimal(amount),
                           counterparty=who, description=description, kind=out)
                   for description, (account, who, amount) in lines.items()])
    decided = {d: tx_by(o, d).decision for d in lines}
    tax, fee, loan = (EvidenceExpectation.TAX_NOTICE_OR_PROOF, EvidenceExpectation.BANK_EVIDENCE_SUFFICES,
                      EvidenceExpectation.LOAN_STATEMENT)
    assert {d: decision.expectation for d, decision in decided.items()} == {
        "MODELO 303 3T": tax, "TGSS CUOTAS SEPTIEMBRE": tax, "IMPUESTO IVA 3T": tax, "COMISION MANTENIMIENTO": fee,
        "CUOTA PRESTAMO 0042": loan, "IVA 2026/08": tax, "TAXA TURISTICA SETEMBRO": tax,
        "PREST EMPRESTIMO 0042": loan, "IMPUESTO IVA 3T (PT)": EvidenceExpectation.INVOICE}
    assert decided["TAXA TURISTICA SETEMBRO"].rule == "tourist_tax"
    assert decided["COMISION MANTENIMIENTO"].reason == "Bank charge. Your bank statement is enough."


def test_letters_are_read_with_their_companys_pack_wording_and_portuguese_ones_as_before() -> None:
    svc, pt, es = _two_countries()
    entities = svc.repo.entities
    portuguese = (f"Autoridade Tributária e Aduaneira\nNota de cobrança\nHazel Tree Interiores, Lda. NIF {NIF_A}\n"
                  "Valor a pagar: 405,00 €\nData limite de pagamento: 20/10/2026\n"
                  "Referência para pagamento: 123 456 789\nO não pagamento pode originar coima e juros de mora.")
    # The core reads English only: without the Portugal pack's wording nothing in this letter is understood.
    assert detect_obligation(portuguese, tenant_id="t", received_on=LETTER_DAY, entities=entities,
                             vocabulary={}) is None
    readings = [detect_obligation(portuguese, tenant_id="t", received_on=LETTER_DAY, entities=entities,
                                  vocabulary=words)
                for words in (None, pack_vocabulary(["PT"]), svc.orchestrator.obligations.vocabulary())]
    for found in readings:
        assert found is not None
        assert (found.kind, found.title, found.amount, found.due_on, found.reference, found.entity_id,
                found.consequence) == (ObligationKind.TAX_DEADLINE, "Tax payment", Decimal("405.00"),
                                       date(2026, 10, 20), "123456789", pt, "The letter mentions a fine and interest.")
    spanish = (f"Agencia Tributaria\nNotificación de deuda\nHazel Tree España S.L. CIF {ES_CIF}\n"
               "Importe a ingresar: 1.204,00 €\nPlazo de ingreso: hasta el 30 de octubre de 2026\n"
               "Número de referencia: 303 123 456 789\nSi no paga en plazo se aplicará un recargo.")
    found = detect_obligation(spanish, tenant_id="t", received_on=LETTER_DAY, entities=entities)
    assert found is not None
    assert (found.kind, found.amount, found.due_on, found.reference, found.entity_id, found.consequence) == (
        ObligationKind.TAX_DEADLINE, Decimal("1204.00"), date(2026, 10, 30), "303123456789", es,
        "The letter mentions a late fee.")
    # Spanish relative deadlines count from the day the letter arrived.
    relative = detect_obligation(spanish.replace("hasta el 30 de octubre de 2026", "en el plazo de 15 días hábiles"),
                                 tenant_id="t", received_on=LETTER_DAY, entities=entities)
    assert relative is not None and relative.due_on == LETTER_DAY + timedelta(days=15)
    # A business with only the Portuguese company does not read Spain's month names.
    without = detect_obligation(spanish, tenant_id="t", received_on=LETTER_DAY,
                                entities=[e for e in entities if e.country == "PT"])
    assert without is None or without.due_on != date(2026, 10, 30)
    # Through the engine: the letter is the Spanish company's deadline.
    report = svc.orchestrator.ingest_file(spanish.encode(), filename="aeat.txt", content_type="text/plain",
                                          origin="upload")
    [oid] = report.obligation_ids
    ob = svc.repo.obligations[oid].obligation
    assert (ob.entity_id, ob.due_on, ob.amount) == (es, date(2026, 10, 30), Decimal("1204.00"))


def test_the_core_reads_fiscal_codes_and_amounts_through_the_country_packs() -> None:
    from backoffice.accountant_questions import amounts_in
    from backoffice.reading.stage0 import fiscal_qr_payloads

    verifactu = (f"https://www2.agenciatributaria.gob.es/wlpl/TIKE-CONT/ValidarQR?nif={ES_CIF}&numserie=F-2026-11"
                 "&fecha=21-09-2026&importe=121.00")
    text = f"Factura F-2026-11\nQR: {verifactu}\nFatura FT 2026/7\n{E.LANDLORD_QR}\n{E.LANDLORD_QR}"
    assert fiscal_qr_payloads(text) == [verifactu, E.LANDLORD_QR]  # each pack finds its own code, once each
    assert company_pack("PT").fiscal_qr_payload({"A": NIF_A, "B": "999999990", "C": "PT"}) == \
        f"A:{NIF_A}*B:999999990*C:PT"
    assert amounts_in("Paid €1.492,30 and 64,10 EUR, then €1,200.00") == [
        Decimal("1492.30"), Decimal("64.10"), Decimal("1200.00")]
    assert amounts_in("Pagado 1.204,00 € el martes", country="ES") == [Decimal("1204.00")]
    assert amounts_in("Paid €1.234.567", country="ES") == amounts_in("Paid €1.234.567", country="PT") == []
