"""Acceptance: obligations beyond tax, overdue usual invoices and refund chains, in the LIVE pipeline.

Each test replays the demo tenant (Hazel Tree, Company B, Company C) and then feeds it the evidence
of one scenario through the real orchestrator and service, the way a connector or the owner would.

1. Obligations beyond tax (checklist M2-M6): insurance, contract and licence renewals, government
   requests, KYC and bank requests, rent and debt letters, in Portuguese and English, from uploads and
   emails, reach the owner's deadlines (Home "Due soon", Ask, the month view, the obligations list) and
   are done only by their required proof: the payment for what is to be paid; for the rest a confirming
   letter or message, or the owner's explicit confirmation stored as evidence. Never a guess.
2. A supplier's usual invoice that is overdue (checklist L7) is raised once as a missing item, asked
   for on the policy-gated send path, visible in Ask and the month view, and closed when it arrives.
3. Refund chains (checklist I6): invoice -> credit note -> refund, closed on the credit note and visible;
   a refund without its credit note expects one and is chased; a refund of another amount is one plain
   question; money back to a customer expects your own credit note.
"""

from __future__ import annotations

import base64
from datetime import date, datetime
from decimal import Decimal

import pytest
from test_acceptance_engine_fixes import CUSTOMER_NIF, NORTE_NIF, bank, hazel_sale, qr_document, stage, tx_by, upload

from backoffice.closure import BlockerKind, Month, VerificationCondition
from backoffice.demo import build_demo
from backoffice.demo import evidence as E
from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import DocumentType, ObligationKind, Quality, SourceKind, Supplier, TransactionKind
from backoffice.language import find_jargon, find_off_tone
from backoffice.mailer import SimulatedOutbox
from backoffice.orchestrator import Orchestrator, local_datetime
from backoffice.policy import TenantPolicy
from backoffice.service import BackOfficeService

OCT = Month(2026, 10)
BANK_SENDER = "Millennium BCP <avisos@millenniumbcp.pt>"
NORTE_IBAN = "PT50003300004532881710999"
LUME_IBAN = "PT50001000001234567890154"


# --------------------------------------------------------------------------- helpers


def at(month: int, day: int, hour: int = 10, minute: int = 0) -> datetime:
    return local_datetime(date(2026, month, day), hour, minute)


def mail(o: Orchestrator, when: datetime, *, sender: str, subject: str, text: str, message_id: str):  # type: ignore[no-untyped-def]
    data = E.email(sender=sender, subject=subject, at=when, text=text, message_id=message_id)
    return o.ingest_file(data, filename="message.eml", content_type="message/rfc822", source_kind=SourceKind.EMAIL,
                         at=when, origin="email")


def letter(o: Orchestrator, text: str, when: datetime, name: str = "carta.txt"):  # type: ignore[no-untyped-def]
    return o.ingest_file(text.encode("utf-8"), filename=name, content_type="text/plain", at=when)


def only(o: Orchestrator, kind: ObligationKind):  # type: ignore[no-untyped-def]
    found = [ob for ob in o.repo.obligations.values() if ob.obligation.kind is kind]
    assert len(found) == 1, [ob.title for ob in o.repo.obligations.values()]
    return found[0]


def plain(*texts: str) -> None:
    for text in texts:
        assert not find_jargon(text), (text, find_jargon(text))
        assert not find_off_tone(text), text
        assert "obl_" not in text and "exp_" not in text and "tx_" not in text and "doc_" not in text, text


def owner_texts(value) -> list[str]:  # type: ignore[no-untyped-def]
    """Every string an owner may read (ids, keys and machine fields excluded)."""
    skip = {"id", "href", "companyId", "evidenceIds", "kind", "status", "tone", "due", "date", "currency", "step",
            "transactionId", "today", "stage", "at", "optionId"}
    if isinstance(value, dict):
        return [t for k, v in value.items() if k not in skip for t in owner_texts(v)]
    if isinstance(value, list):
        return [t for v in value for t in owner_texts(v)]
    return [value] if isinstance(value, str) else []


def due_items(svc: BackOfficeService, title: str) -> list[dict]:
    return [d for d in svc.home()["dueSoon"] if d["title"] == title]


def questions(svc: BackOfficeService, text: str) -> list[dict]:
    return [i for i in svc.needs_you()["items"] if text in i.get("question", "")]


def of_kind(o: Orchestrator, kind: ObligationKind) -> list:
    return [ob for ob in o.repo.obligations.values() if ob.obligation.kind is kind]


def read_mail(o: Orchestrator, until: datetime) -> None:
    """The mailbox sync read everything up to ``until`` (what a connector reports after a sync, §47)."""
    gmail = o.repo.connectors["gmail"]
    gmail.covered_until = gmail.last_synced_at = until


def stored(o: Orchestrator, evidence_ids) -> None:  # type: ignore[no-untyped-def]
    for ev in evidence_ids:
        o.repo.evidence(ev)  # raises when it is not stored evidence


def golden_rule(o: Orchestrator) -> None:
    """Every transition carries stored evidence, a closed item is GREEN, the audit chain is intact (§3, §55),
    and every screen still reads (Home, Needs you, the pipeline and the team's operations view)."""
    for item in o.repo.items.values():
        for t in item.history:
            assert t.evidence_ids and t.actor, item.id
            stored(o, t.evidence_ids)
        if item.stage is Stage.CLOSED:
            assert item.quality is Quality.GREEN, item.id
    assert o.repo.audit.verify(o.repo.tenant_id).ok
    assert o.auditor.recheck() == []
    svc = BackOfficeService(o)
    for path in ("/api/home", "/api/needs-you", "/api/pipeline", "/api/obligations", "/api/activity"):
        status, body = svc.dispatch("GET", path, None)
        assert status == 200, (path, body)
    from backoffice.internal import operations, overview

    assert overview([svc]) and operations([svc])["activity"]


@pytest.fixture
def demo() -> Orchestrator:
    o = build_demo()
    o.repo.add_supplier(Supplier(id="sup-norte", tenant_id=o.repo.tenant_id, name="Papelaria Norte",
                                 aliases=["PAPELARIA NORTE"], tax_id=NORTE_NIF, countries=["PT"],
                                 contact_email="faturas@papelarianorte.pt", known_ibans=[NORTE_IBAN]))
    return o


@pytest.fixture
def svc(demo: Orchestrator) -> BackOfficeService:
    return BackOfficeService(demo)


# =========================================================================== 1. obligations beyond tax

KYC_REQUEST_PT = (
    "Caro cliente,\n"
    "No âmbito da atualização de dados da conta da Hazel Tree Interiores, Lda., NIF 516 123 459, "
    "solicitamos o envio do comprovativo de morada até 20/10/2026.\n"
    "Sem estes documentos poderemos bloquear a conta.\n")
KYC_DONE_PT = "Caro cliente,\nRecebemos os seus documentos. Os seus dados foram atualizados. Obrigado.\n"


def test_kyc_request_by_email_is_tracked_everywhere_and_done_only_by_the_banks_confirmation(
        demo: Orchestrator, svc: BackOfficeService) -> None:
    report = mail(demo, at(10, 5), sender=BANK_SENDER, subject="Atualização de dados", text=KYC_REQUEST_PT,
                  message_id="<kyc-2026-10@millenniumbcp.pt>")
    ob = only(demo, ObligationKind.KYC_REQUEST)
    o = ob.obligation
    # everything the library defines reaches the owner's obligation
    assert (o.entity_id, o.due_on, o.responsible) == ("hazel-tree", date(2026, 10, 20), "owner")
    assert o.consequence == "The letter mentions suspension."
    assert o.required_evidence == "A copy of the reply that was sent by 20 October 2026."
    cond = VerificationCondition.parse(o.verification_condition)
    assert cond.proof.value == "reply" and cond.by == date(2026, 10, 20) and cond.amount is None
    assert report.obligation_ids == [o.id] and not report.document_ids  # a letter, not an invoice
    assert ob.title == "Your bank needs updated details" and not ob.done
    assert demo.repo.activity[-1].text == "Read a message from your bank asking for updated details."

    # Home "Due soon", the obligations list, Ask and the month view show it like a tax deadline
    (due,) = due_items(svc, "Your bank needs updated details")
    assert due["companyName"] == "Hazel Tree" and due["due"] == "2026-10-20" and due["tone"] == "neutral"
    assert due["note"] == ("When you have sent what they ask for, forward me their confirmation or tell me it "
                           "is done.")
    listed = {i["id"]: i for i in svc.dispatch("GET", "/api/obligations", None)[1]["items"]}[o.id]
    assert (listed["title"], listed["companyName"], listed["due"], listed["amount"]) == (
        "Your bank needs updated details", "Hazel Tree", "2026-10-20", None)
    assert listed["responsible"] == "You" and listed["status"] == "open"
    assert listed["consequence"] == "The letter mentions suspension."
    assert listed["requiredProof"] == "A copy of the reply that was sent by 20 October 2026."
    assert listed["condition"] == "A copy of the reply that was sent by 20 October."
    assert [c["id"] for c in listed["confirmOptions"]] == ["sent"]
    stored(demo, listed["evidenceIds"])
    answer = svc.ask("what's due soon")
    assert "Your bank needs updated details for Hazel Tree, 20 October" in answer["answer"]
    remaining = [r["text"] for r in svc.month("hazel-tree", "2026-10")["remaining"]]
    assert "Your bank needs updated details is due on 20 October." in remaining
    status = demo.month_status("hazel-tree", OCT)
    assert o.id in status.open_obligations and BlockerKind.OBLIGATION in {b.kind for b in status.blockers}
    plain(due["note"], answer["answer"], *owner_texts(listed), *remaining)

    # a payment never proves a request for documents
    demo.ingest_bank([bank("mbcp-1006-01", "mbcp-ht", date(2026, 10, 6), "-20.00", "MILLENNIUM BCP",
                           "COMISSAO PEDIDO")])
    assert not ob.done

    # the bank's confirmation, from the same sender, closes it with that email as the evidence
    done = mail(demo, at(10, 9), sender=BANK_SENDER, subject="Atualização concluída", text=KYC_DONE_PT,
                message_id="<kyc-2026-10-done@millenniumbcp.pt>")
    assert ob.done and done.confirmed_ids == [o.id] and not done.document_ids
    proof = demo.repo.evidence(ob.satisfied_by[0])
    assert proof.format.value == "eml" and ob.obligation.satisfied_by_evidence_ids == list(ob.satisfied_by)
    assert ob.how == "They confirmed on 9 October that they received it."
    assert svc._report(done)["message"] == ("Got it. This closes “Your bank needs updated details”. They confirmed "
                                            "on 9 October that they received it.")
    assert not due_items(svc, "Your bank needs updated details")
    assert o.id not in demo.month_status("hazel-tree", OCT).open_obligations
    golden_rule(demo)


def test_a_confirmation_from_another_sender_or_a_follow_up_request_never_closes_it(demo: Orchestrator) -> None:
    mail(demo, at(10, 5), sender=BANK_SENDER, subject="Atualização de dados", text=KYC_REQUEST_PT,
         message_id="<kyc-a@millenniumbcp.pt>")
    ob = only(demo, ObligationKind.KYC_REQUEST)
    mail(demo, at(10, 6), sender="Banco Suspeito <no-reply@bcp-seguro.com>", subject="Dados", text=KYC_DONE_PT,
         message_id="<kyc-b@bcp-seguro.com>")
    assert not ob.done  # another sender's word is not the bank's confirmation
    mail(demo, at(10, 7), sender=BANK_SENDER, subject="Dados",
         text="Recebemos os seus documentos, mas ainda precisamos do comprovativo de morada até 20/10/2026.\n",
         message_id="<kyc-c@millenniumbcp.pt>")
    assert not ob.done  # still asking: not a confirmation


def test_a_letter_that_names_no_company_is_one_question_then_the_owners_confirmation_closes_it(
        demo: Orchestrator, svc: BackOfficeService) -> None:
    report = letter(demo, "Dear customer, as part of our KYC review please upload proof of address due by "
                          "20 October 2026, or your account may be suspended.", at(10, 5), "kyc.txt")
    assert not of_kind(demo, ObligationKind.KYC_REQUEST) and not report.document_ids  # no guess, not an invoice
    assert svc._report(report)["message"] == ("Got it. This letter does not say which of your companies it is "
                                              "for. I asked you in Needs you.")
    (item,) = questions(svc, "Your bank needs updated details")
    assert item["question"] == "Your bank needs updated details: which of your companies is this letter for?"
    assert [o["label"] for o in item["options"]][-1] == "None of them. It is personal or not for my companies."
    assert "It does not name one of your companies, so I won't guess which one." in item["why"]
    plain(*owner_texts(item))
    golden_rule(demo)  # every screen reads with the question open
    answered = svc.answer(item["id"], "company:company-b")
    assert answered["message"] == "Done. I added it to Company B's deadlines."
    ob = only(demo, ObligationKind.KYC_REQUEST)
    assert ob.obligation.entity_id == "company-b" and ob.obligation.due_on == date(2026, 10, 20)
    assert due_items(svc, "Your bank needs updated details")[0]["companyName"] == "Company B"

    # the owner's explicit confirmation, with the file they sent, is the proof (both stored)
    status, body = svc.dispatch("POST", f"/api/obligations/{ob.obligation.id}/done", {"outcome": "renewed"})
    assert status == 400 and not ob.done  # a request is not renewed
    sent = base64.b64encode(b"Comprovativo de morada enviado ao banco.").decode()
    status, body = svc.dispatch("POST", f"/api/obligations/{ob.obligation.id}/done",
                                {"outcome": "sent", "dataBase64": sent, "filename": "envio.txt",
                                 "contentType": "text/plain"})
    assert status == 200 and body["obligation"]["status"] == "done"
    assert ob.done and len(ob.confirmed_by) == 2
    stored(demo, ob.confirmed_by)
    assert set(ob.confirmed_by) <= set(ob.obligation.satisfied_by_evidence_ids)
    assert demo.repo.evidence(ob.confirmed_by[1]).filename == "envio.txt"
    assert body["message"] == "Done. Your bank needs updated details is closed with your confirmation."
    assert svc.dispatch("POST", f"/api/obligations/{ob.obligation.id}/done", {"outcome": "sent"})[0] == 409
    plain(body["message"], ob.how)


def test_a_letter_the_owner_says_is_not_theirs_is_not_tracked(demo: Orchestrator, svc: BackOfficeService) -> None:
    letter(demo, "Your contract renewal date: 1 November 2026.", at(10, 5), "contract.txt")
    (item,) = questions(svc, "Contract renewal")
    assert svc.answer(item["id"], "none")["message"] == "Done. I won't track it."
    assert not of_kind(demo, ObligationKind.CONTRACT_RENEWAL)
    report = letter(demo, "Your contract renewal date: 1 November 2026.", at(10, 6), "contract.txt")  # again
    assert not of_kind(demo, ObligationKind.CONTRACT_RENEWAL)
    assert not [n for n in demo.repo.open_needs() if n.kind == "obligation_company"]
    assert svc._report(report)["message"] == "Got it. You told me this letter is not for your companies."


def test_licence_renewal_closes_only_on_the_renewed_licence(demo: Orchestrator, svc: BackOfficeService) -> None:
    letter(demo, "Câmara Municipal de Lisboa\nHazel Tree Interiores, Lda. - NIF 516 123 459\n"
                 "A renovação do alvará de utilização deve ser pedida até 30/11/2026. "
                 "A falta de renovação implica coima.\n", at(11, 10), "alvara.txt")
    ob = only(demo, ObligationKind.LICENSE_RENEWAL)
    assert ob.title == "Licence renewal" and ob.obligation.due_on == date(2026, 11, 30)
    assert ob.obligation.consequence == "The letter mentions a fine."
    assert ob.obligation.required_evidence == "The renewed licence, or your decision not to renew."
    assert demo.repo.activity[-1].text == "Read a letter about a licence renewal."
    (due,) = due_items(svc, "Licence renewal")
    assert due["note"] == ("I close it when the renewed licence arrives, or when you tell me you are not "
                           "renewing.")
    # "renewed", but without how long it runs: not enough, it stays open (never a guess)
    report = letter(demo, "Hazel Tree Interiores, Lda.: o seu alvará foi renovado.", at(11, 12), "a.txt")
    assert not ob.done and not report.document_ids
    assert demo.repo.activity[-1].text == "Read a letter about “Licence renewal”. It is not enough to close it yet."
    # the renewed licence, running past the deadline, closes it
    letter(demo, "Câmara Municipal de Lisboa\nO alvará de utilização da Hazel Tree Interiores, Lda. foi renovado "
                 "até 30/11/2027.\n", at(11, 14), "b.txt")
    assert ob.done and ob.how == "Renewed until 30 November 2027."
    assert not due_items(svc, "Licence renewal")


def test_english_contract_renewal_is_done_by_the_confirmed_ending(demo: Orchestrator) -> None:
    letter(demo, "Company C Studio - your subscription contract renewal date: 1 December 2026.", at(11, 10),
           "contract.txt")
    ob = only(demo, ObligationKind.CONTRACT_RENEWAL)
    assert ob.obligation.entity_id == "company-c" and ob.obligation.due_on == date(2026, 12, 1)
    letter(demo, "Company C Studio: your contract has been terminated as you asked. No further payments are due.",
           at(11, 20), "ended.txt")
    assert ob.done and ob.how == "Confirmed on 20 November that it will not be renewed."


def test_insurance_that_renews_on_its_own_stays_information_only(demo: Orchestrator, svc: BackOfficeService) -> None:
    letter(demo, "Fidelidade - Companhia de Seguros\nHazel Tree Interiores, Lda.\nA sua apólice de seguro "
                 "multirriscos renova automaticamente a 01/11/2026. Prémio anual: 480,00 €.\n", at(10, 20), "apolice.txt")
    ob = only(demo, ObligationKind.INSURANCE_RENEWAL)
    assert ob.informational and ob.obligation.consequence == "It renews on its own unless you act."
    assert demo.repo.activity[-1].text == "Read a letter about an insurance renewal. It renews on its own."
    (due,) = due_items(svc, "Insurance renewal")
    assert due["tone"] == "neutral"
    assert due["note"] == "It renews on its own. Nothing to do unless you want to change or end it."
    listed = {i["id"]: i for i in svc.obligations()["items"]}[ob.obligation.id]
    assert listed["status"] == "information"
    # it never holds a month open, and once the date passed it is not "late"
    demo.run(at(11, 3))
    assert ob.obligation.id not in demo.month_status("hazel-tree", Month(2026, 11)).open_obligations
    assert not due_items(svc, "Insurance renewal")


def test_government_request_and_filing_close_on_the_tax_offices_confirmation(demo: Orchestrator,
                                                                               svc: BackOfficeService) -> None:
    letter(demo, "Autoridade Tributária e Aduaneira\nNIF: 514 987 650\nNotificação. Deverá apresentar os "
                 "documentos solicitados no prazo de 15 dias.\n", at(10, 5), "notificacao.txt")
    request = only(demo, ObligationKind.GOVERNMENT_REQUEST)
    assert request.title == "Request from the tax office" and request.obligation.entity_id == "company-b"
    assert request.obligation.due_on == date(2026, 10, 20)
    assert demo.repo.activity[-1].text == "Read a request from the tax office."
    letter(demo, "Autoridade Tributária e Aduaneira\nNIF: 514 987 650\nConfirmamos a receção da sua resposta à "
                 "notificação.\n", at(10, 12), "resposta.txt")
    assert request.done

    letter(demo, "Autoridade Tributária e Aduaneira\nNIF 516 123 459\nEntrega da declaração periódica de IVA do "
                 "período 2026/09: data limite 20/11/2026.\n", at(10, 14), "iva.txt")
    filing = only(demo, ObligationKind.FILING)
    assert filing.obligation.responsible == "accountant"
    (due,) = [d for d in svc.home()["dueSoon"] if d["title"] == "Tax return"] or [None]
    assert filing.obligation.due_on == date(2026, 11, 20)
    assert svc.orchestrator.obligations.next_step(filing) == ("Your accountant files this. I close it when the "
                                                              "filing receipt arrives.")
    letter(demo, "Autoridade Tributária e Aduaneira\nComprovativo de entrega da declaração periódica de IVA.\n"
                 "NIF 516 123 459\n", at(10, 30), "comprovativo.txt")
    assert filing.done and filing.how == "The filing receipt arrived on 30 October."


RENT_LETTER = ("Marta Gonçalves, NIF 234 567 899\nCaro inquilino Hazel Tree Interiores, Lda., a renda de novembro no "
               "valor de 1 200,00 € deve ser paga até ao dia 8/11/2026.\n")


def test_rent_letter_is_paid_only_by_a_payment_to_the_landlord(demo: Orchestrator, svc: BackOfficeService) -> None:
    letter(demo, RENT_LETTER, at(10, 25), "renda.txt")
    ob = only(demo, ObligationKind.RENT)
    assert ob.payable and ob.obligation.amount == Decimal("1200.00") and ob.payee_supplier_id == "sup-landlord"
    (due,) = due_items(svc, "Rent payment")
    assert due["note"] == "€1,200.00 · I will check the payment when it goes out."
    # the owner cannot close a payment by saying so
    status, body = svc.dispatch("POST", f"/api/obligations/{ob.obligation.id}/done", {"outcome": "sent"})
    assert status == 409 and body["message"] == "I close this one when I see the payment in your bank."
    # the right amount to someone the bank line does not identify: one plain question, nothing closed
    demo.ingest_bank([bank("mbcp-1102-01", "mbcp-ht", date(2026, 11, 2), "-1200.00", "IMOBILIARIA SOL LDA",
                           "TRF")], at=at(11, 2, 8))
    assert not ob.done
    (item,) = questions(svc, "rent letter")
    assert item["question"] == ("Does the €1,200.00 payment to Imobiliaria SOL on 2 November pay the rent letter "
                                "of 25 October?")
    assert "The bank line does not show that it went to whoever wrote the letter." in item["why"]
    plain(*owner_texts(item))
    golden_rule(demo)
    assert svc.answer(item["id"], "no")["message"] == "Done. I'll keep watching for the payment that pays the rent letter."
    assert not ob.done
    # the payment to the landlord (the supplier the letter names) proves it
    demo.ingest_bank([bank("mbcp-1103-01", "mbcp-ht", date(2026, 11, 3), "-1200.00", "MARTA GONCALVES",
                           "TRF RENDA NOVEMBRO")], at=at(11, 3, 8))
    rent = tx_by(demo, "TRF RENDA NOVEMBRO")
    assert ob.done and ob.satisfied_by == (rent.evidence_id,) and ob.how == "Paid on 3 November."
    assert not due_items(svc, "Rent payment")
    listed = {i["id"]: i for i in svc.obligations()["items"]}[ob.obligation.id]
    assert listed["status"] == "done" and listed["amount"] == 1200 and listed["confirmOptions"] == []
    golden_rule(demo)


def test_rent_paid_to_an_unrecognised_payee_is_done_when_the_owner_confirms(demo: Orchestrator,
                                                                            svc: BackOfficeService) -> None:
    letter(demo, "Caro inquilino Hazel Tree Interiores, Lda., a renda de novembro no valor de 950,00 € deve ser "
                 "paga até 8/11/2026.", at(10, 25), "renda.txt")
    ob = only(demo, ObligationKind.RENT)
    demo.ingest_bank([bank("mbcp-1102-01", "mbcp-ht", date(2026, 11, 2), "-950.00", "J SILVA", "TRF")],
                     at=at(11, 2, 8))
    (item,) = questions(svc, "rent letter")
    assert svc.answer(item["id"], "yes")["message"] == "Done. The rent letter is paid."
    payment = tx_by(demo, "TRF")
    assert ob.done and payment.evidence_id in ob.satisfied_by and ob.confirmed_by[0] in ob.satisfied_by
    stored(demo, ob.satisfied_by)


def test_english_debt_letter_with_a_reference_is_paid_only_by_the_referenced_payment(demo: Orchestrator) -> None:
    letter(demo, "FINAL NOTICE\nCompany B, Lda.: outstanding debt of €310.00 must be paid by 12 October 2026.\n"
                 "Payment reference: 778 901 234. Otherwise we will start legal action.\n", at(10, 5), "debt.txt")
    ob = only(demo, ObligationKind.DEBT_COLLECTION)
    assert ob.obligation.consequence == "The letter mentions legal action." and ob.reference == "778901234"
    demo.ingest_bank([bank("cgd-1006-01", "cgd-b", date(2026, 10, 6), "-310.00", "RECOVERY PARTNERS", "TRF")])
    assert not ob.done  # the right amount without the reference is not proof
    demo.ingest_bank([BankRowWithRef("cgd-1007-01", "cgd-b", date(2026, 10, 7), "-310.00", "RECOVERY PARTNERS",
                                     "TRF DIVIDA", "778901234")])
    assert ob.done and ob.how == "Paid on 7 October."


def BankRowWithRef(bank_id: str, account: str, day: date, amount: str, who: str, description: str,  # noqa: N802
                   reference: str):  # type: ignore[no-untyped-def]
    return E.row(bank_id, account, day, amount, who, description, TransactionKind.TRANSFER_OUT, reference=reference)


def test_an_invoice_with_a_due_date_is_not_a_letter(demo: Orchestrator) -> None:
    """An invoice's due date is proven by paying the invoice (§20): it never becomes a second deadline."""
    before = dict(demo.repo.obligations)
    report = letter(demo, f"Papelaria Norte, Lda.\nNIF: {NORTE_NIF}\nInvoice 2026/77: amount due €99.90, "
                          "due date 2026-10-10.\n", at(10, 5), "invoice.txt")
    assert not report.obligation_ids and demo.repo.obligations == before
    report = letter(demo, qr_document("FT", "FT A/300", "ABCD1234-300", "123.00", "100.00", "23.00", "2026-10-01",
                                      title="Fatura", extra=("Data de vencimento: 15/10/2026",)).decode(), at(10, 5))
    assert report.document_ids and not report.obligation_ids and demo.repo.obligations == before


def test_the_demo_letters_keep_their_outcome(demo: Orchestrator) -> None:
    """The demo world has only tax letters: they are tracked and proven as before."""
    kinds = sorted(ob.obligation.kind.value for ob in demo.repo.obligations.values())
    assert kinds == ["tax_deadline", "tax_deadline"]
    hazel = next(ob for ob in demo.repo.obligations.values() if ob.obligation.entity_id == "hazel-tree")
    assert hazel.done and hazel.how == "Paid on 21 September."
    assert not demo.repo.pending_obligations and not demo.repo.expected_invoices


# =========================================================================== 2. a usual invoice that is overdue


def norte_invoice(o: Orchestrator, number: int, day: str, when: datetime | None = None,
                  **kw):  # type: ignore[no-untyped-def]
    data = qr_document("FT", f"FT A/{number}", f"ABCD1234-{number}", "61.50", "50.00", "11.50", day, title="Fatura",
                       **kw)
    report = o.ingest_file(data, filename=f"ft{number}.txt", content_type="text/plain", at=when)
    return o.repo.documents[report.document_ids[0]]


def learn_norte(o: Orchestrator) -> None:
    """Four monthly Papelaria Norte invoices to Hazel Tree, usually around the 24th and at the latest the 26th,
    read newest first as a mailbox import delivers them."""
    for number, day in ((104, "2026-09-25"), (103, "2026-08-23"), (102, "2026-07-26"), (101, "2026-06-24")):
        norte_invoice(o, number, day)


def test_an_overdue_usual_invoice_is_raised_once_asked_for_and_closed_when_it_arrives(
        demo: Orchestrator, svc: BackOfficeService) -> None:
    repo = demo.repo
    learn_norte(demo)
    assert not repo.expected_invoices  # nothing is late while the history comes in
    read_mail(demo, at(10, 26, 17))
    demo.run(at(10, 26, 18))
    assert not repo.expected_invoices  # "by the 26th": not late on the 26th
    demo.run(at(10, 29, 9))
    assert not repo.expected_invoices  # the mailbox was not read past the 26th: it may be sitting there unread
    read_mail(demo, at(10, 29, 8, 55))
    report = demo.run(at(10, 29, 9))
    (expected,) = repo.expected_invoices.values()
    assert report.expected == [expected.id]
    notice = "Papelaria Norte normally issues an invoice by the 26th. Today is the 29th. Invoice missing."
    assert expected.notice == notice and expected.company_id == "hazel-tree" and expected.period == OCT
    assert any(a.text == notice for a in repo.activity)
    item = repo.items[expected.item_id]
    assert item.stage is Stage.UNDERSTOOD and item.history[-1].note == notice
    stored(demo, item.history[-1].evidence_ids)  # learned from the earlier invoices, kept as evidence
    assert expected.searched[0] == "I looked through your email and the documents you sent me: it is not there."

    # asked for on the same policy-gated send path: sent only because the demo's transport accepted it
    (message,) = [m for m in repo.outbox.values() if m.subject_id == expected.id]
    assert message.kind == "expected_invoice_request" and message.sent and message.to == "faturas@papelarianorte.pt"
    assert message.subject.startswith("Fatura de outubro de 2026 (Ref. ")
    assert "costuma chegar-nos até 26 de outubro e ainda não a recebemos" in message.body
    assert expected.sent and any(a.text == "Asked Papelaria Norte for its usual invoice for October."
                                 for a in repo.activity)

    # visible in the month view and in Ask; it holds October open as a missing document
    plan = f"{notice} I asked Papelaria Norte for it."
    assert plan in [r["text"] for r in svc.month("hazel-tree", "2026-10")["remaining"]]
    status = demo.month_status("hazel-tree", OCT)
    assert status.missing_documents == 1 and BlockerKind.MISSING_DOCUMENTS in {b.kind for b in status.blockers}
    answer = svc.ask("What is missing?")
    assert notice in answer["answer"] and "I asked Papelaria Norte for it." in answer["answer"]
    plain(plan, answer["answer"], message.subject)

    # raised once: later runs neither duplicate the item nor write again
    read_mail(demo, at(10, 30, 8))
    demo.run(at(10, 30, 9))
    assert len(repo.expected_invoices) == 1 and len([m for m in repo.outbox.values() if m.subject_id == expected.id]) == 1

    # the invoice arrives: the item closes with that invoice as its evidence
    arrived = norte_invoice(demo, 105, "2026-10-28", when=at(10, 30, 11))
    assert expected.status == "received" and expected.document_id == arrived.id
    assert item.stage is Stage.CLOSED and item.quality is Quality.GREEN
    assert set(item.history[-1].evidence_ids) == set(arrived.evidence_ids)
    assert any(a.text == "The Papelaria Norte invoice for October arrived." and a.kind == "recovered"
               for a in repo.activity)
    assert plan not in [r["text"] for r in svc.month("hazel-tree", "2026-10")["remaining"]]
    assert demo.month_status("hazel-tree", OCT).missing_documents == 0
    read_mail(demo, at(11, 20, 8))
    demo.run(at(11, 20, 9))
    assert len(repo.expected_invoices) == 1  # November is not late yet: nothing new
    golden_rule(demo)


def test_nothing_is_expected_from_a_supplier_without_a_learned_rhythm(demo: Orchestrator) -> None:
    norte_invoice(demo, 102, "2026-09-24")
    norte_invoice(demo, 101, "2026-08-24")  # two invoices: a rhythm, but not yet trusted
    read_mail(demo, at(11, 10, 8))
    demo.run(at(11, 10, 9))
    assert not demo.repo.expected_invoices
    company_b = {"buyer": E.COMPANY_B_NIF, "buyer_line": "Cliente: Company B, Lda."}
    for number, day in ((201, "2026-09-28"), (202, "2026-07-11"), (203, "2026-05-20"), (204, "2026-05-02")):
        norte_invoice(demo, number, day, **company_b)  # Company B buys from them now and then: no rhythm
    read_mail(demo, at(12, 20, 8))
    demo.run(at(12, 20, 9))
    assert not demo.repo.expected_invoices


def test_an_overdue_usual_invoice_waits_for_the_policy_and_for_a_transport(demo: Orchestrator,
                                                                           svc: BackOfficeService) -> None:
    repo = demo.repo
    learn_norte(demo)
    repo.policy = TenantPolicy(tenant_id=repo.tenant_id)  # the owner never allowed supplier requests
    read_mail(demo, at(10, 29, 8))
    demo.run(at(10, 29, 9))
    (expected,) = repo.expected_invoices.values()
    assert expected.message is None and not [m for m in repo.outbox.values() if m.subject_id == expected.id]
    assert demo.missing.plan_expected(expected).endswith("Invoice missing. I will match it when it arrives.")

    other = build_demo()
    other.repo.add_supplier(repo.suppliers["sup-norte"])
    other.transport = None  # allowed, but nothing can send it yet
    learn_norte(other)
    read_mail(other, at(10, 29, 8))
    other.run(at(10, 29, 9))
    (waiting,) = other.repo.expected_invoices.values()
    assert waiting.message is not None and not waiting.sent
    assert other.missing.plan_expected(waiting).endswith("I wrote to Papelaria Norte asking for it. It is waiting to "
                                                         "be sent.")
    other.transport = SimulatedOutbox()
    other.deliver(at(10, 29, 10))
    assert waiting.sent


def test_the_owner_can_say_a_usual_invoice_is_not_coming(demo: Orchestrator, svc: BackOfficeService) -> None:
    learn_norte(demo)
    read_mail(demo, at(10, 29, 8))
    demo.run(at(10, 29, 9))
    (expected,) = demo.repo.expected_invoices.values()
    status, body = svc.dispatch("POST", f"/api/expected-invoices/{expected.id}/not-coming", {})
    assert status == 200 and body["message"] == "Done. I won't wait for the Papelaria Norte invoice for October."
    item = demo.repo.items[expected.item_id]
    assert item.stage is Stage.NOT_REQUIRED and item.quality is Quality.GREEN
    stored(demo, item.history[-1].evidence_ids)
    assert demo.month_status("hazel-tree", OCT).missing_documents == 0
    read_mail(demo, at(11, 5, 8))
    demo.run(at(11, 5, 9))
    assert len(demo.repo.expected_invoices) == 1  # October stays settled; nothing raised again
    assert svc.dispatch("POST", f"/api/expected-invoices/{expected.id}/not-coming", {})[0] == 409


# =========================================================================== 3. refunds


def credit_note(o: Orchestrator, number: str, total: str, net: str, vat: str, day: str, corrects: str,
                **kw):  # type: ignore[no-untyped-def]
    data = qr_document("NC", number, f"ABCD1234-{number.rsplit('/', 1)[1]}", total, net, vat, day,
                       title="Nota de crédito", extra=(f"Referente à fatura {corrects}",), **kw)
    return o.repo.documents[upload(o, data, f"{number.replace('/', '_')}.txt").document_ids[0]]


def paid_invoice(o: Orchestrator):  # type: ignore[no-untyped-def]
    invoice = o.repo.documents[upload(o, qr_document(
        "FT", "FT A/183", "ABCD1234-183", "500.00", "406.50", "93.50", "2026-09-10", title="Fatura")).document_ids[0]]
    o.ingest_bank([bank("mbcp-0911-09", "mbcp-ht", date(2026, 9, 11), "-500.00", "PAPELARIA NORTE",
                        "TRF PAPELARIA NORTE")])
    return invoice, tx_by(o, "TRF PAPELARIA NORTE")


def refund(o: Orchestrator, bank_id: str, day: date, amount: str, description: str):  # type: ignore[no-untyped-def]
    o.ingest_bank([bank(bank_id, "mbcp-ht", day, amount, "PAPELARIA NORTE", description, TransactionKind.TRANSFER_IN)])
    return tx_by(o, description)


def test_refund_chain_invoice_credit_note_refund_closes_and_is_visible(demo: Orchestrator,
                                                                      svc: BackOfficeService) -> None:
    repo = demo.repo
    invoice, payment = paid_invoice(demo)
    assert stage(demo, invoice) is Stage.CLOSED and stage(demo, payment) is Stage.CLOSED
    note = credit_note(demo, "NC A/12", "123.00", "100.00", "23.00", "2026-09-12", "FT A/183")
    assert note.credit_for == invoice.id and stage(demo, note) is Stage.UNDERSTOOD  # waiting for its money
    back = refund(demo, "mbcp-0915-09", date(2026, 9, 15), "123.00", "DEVOLUCAO PAPELARIA NORTE")
    assert back.decision is not None and back.decision.expectation.value == "refund_or_credit_note"
    assert back.document_ids == [note.id] and stage(demo, back) is Stage.CLOSED and stage(demo, note) is Stage.CLOSED
    closed = repo.items[back.item_id].history[-1]
    assert closed.note == "Refund for credit note NC A/12, which corrects invoice FT A/183."
    assert set(note.evidence_ids) <= set(closed.evidence_ids)  # closed on the credit note
    assert "Credit note NC A/12: corrects invoice FT A/183" in back.match_why
    assert "Invoice FT A/183: paid €500.00 on 11 September" in back.match_why
    assert repo.items[note.item_id].history[-1].note == "Refunded in full on 15 September."
    assert demo.auditor.recheck() == []

    # the chain in the payment's and the documents' detail
    detail = svc.dispatch("GET", f"/api/transactions/{back.id}", None)[1]
    assert [s["step"] for s in detail["chain"]] == ["invoice", "payment", "credit_note", "refund"]
    assert [s["label"] for s in detail["chain"]] == [
        "Invoice FT A/183", "Paid €500.00 to Papelaria Norte on 11 September",
        "Credit note NC A/12, which corrects invoice FT A/183", "Refund of €123.00 received on 15 September"]
    assert detail["headline"] == "Refund for credit note NC A/12, which corrects invoice FT A/183."
    assert "Credit note NC A/12 corrects invoice FT A/183" in detail["why"]
    assert detail["history"][-1]["label"] == "Closed"
    of_invoice = svc.dispatch("GET", f"/api/documents/{invoice.id}", None)[1]
    assert [c["id"] for c in of_invoice["creditNotes"]] == [note.id] and of_invoice["chain"] == detail["chain"]
    of_note = svc.dispatch("GET", f"/api/documents/{note.id}", None)[1]
    assert of_note["corrects"] == {"id": invoice.id, "label": "Invoice FT A/183"}
    assert of_note["chain"] == detail["chain"] and of_note["payments"][0]["transactionId"] == back.id
    matched = {m["id"]: m for m in svc.month("hazel-tree", "2026-09")["matched"]}[f"m_{back.id}"]
    assert matched["description"] == "Credit note NC A/12"
    assert "Credit note NC A/12 corrects invoice FT A/183" in matched["reasons"]
    answer = svc.ask("find the 123 euro payment")["answer"]
    assert "Refund for credit note NC A/12, which corrects invoice FT A/183." in answer
    plain(*owner_texts(detail), *owner_texts(of_invoice), *owner_texts(of_note), answer)
    golden_rule(demo)


def test_one_refund_for_two_credit_notes_closes_both(demo: Orchestrator) -> None:
    first = credit_note(demo, "NC A/30", "50.00", "40.65", "9.35", "2026-09-12", "FT A/901")
    second = credit_note(demo, "NC A/31", "30.00", "24.39", "5.61", "2026-09-13", "FT A/902")
    back = refund(demo, "mbcp-0916-09", date(2026, 9, 16), "80.00", "DEVOLUCAO PAPELARIA NORTE")
    assert sorted(back.document_ids) == sorted([first.id, second.id]) and stage(demo, back) is Stage.CLOSED
    assert stage(demo, first) is Stage.CLOSED and stage(demo, second) is Stage.CLOSED
    assert demo.credit_left(first) == 0 and demo.credit_left(second) == 0
    golden_rule(demo)


def test_a_refund_without_its_credit_note_expects_one_and_is_chased(demo: Orchestrator,
                                                                     svc: BackOfficeService) -> None:
    repo = demo.repo
    back = refund(demo, "mbcp-0915-09", date(2026, 9, 15), "40.00", "DEVOLUCAO PAPELARIA NORTE")
    assert back.decision is not None and back.decision.expectation.value == "refund_or_credit_note"
    assert stage(demo, back) is Stage.UNDERSTOOD  # never closed without its credit note
    chase = repo.chases[back.id]
    assert chase.sent and chase.message.to == "faturas@papelarianorte.pt"
    assert chase.message.subject.startswith("Nota de crédito do reembolso de 40,00 € de 15 de setembro")
    assert "Recebemos um reembolso de 40,00 € a 15 de setembro." in chase.message.body
    assert any(a.text == "Asked Papelaria Norte for the credit note for the €40.00 refund." for a in repo.activity)
    plan = "I asked Papelaria Norte for the credit note for the €40.00 refund on 15 September. Suppliers usually " \
           "reply within a few days."
    assert demo.missing.plan(back) == plan
    assert plan in [r["text"] for r in svc.month("hazel-tree", "2026-09")["remaining"]]
    note = credit_note(demo, "NC A/20", "40.00", "32.52", "7.48", "2026-09-16", "FT A/999")
    assert back.document_ids == [note.id] and stage(demo, back) is Stage.CLOSED


def mismatched_refund(o: Orchestrator, amount: str):  # type: ignore[no-untyped-def]
    invoice, _ = paid_invoice(o)
    note = credit_note(o, "NC A/12", "123.00", "100.00", "23.00", "2026-09-12", "FT A/183")
    back = refund(o, "mbcp-0915-09", date(2026, 9, 15), amount, "DEVOLUCAO PAPELARIA NORTE")
    return invoice, note, back


def test_a_refund_of_another_amount_is_one_plain_question_not_a_close(demo: Orchestrator,
                                                                      svc: BackOfficeService) -> None:
    repo = demo.repo
    _, note, back = mismatched_refund(demo, "100.00")
    assert not back.document_ids and stage(demo, back) is Stage.NEEDS_OWNER and stage(demo, note) is Stage.UNDERSTOOD
    assert back.id not in repo.chases  # asked, not chased
    (item,) = [i for i in svc.needs_you()["items"] if i["id"].endswith("_refund")]
    assert item["question"] == ("Papelaria Norte refunded €100.00 on 15 September, but its credit note NC A/12 is "
                                "for €123.00. Is this refund part of it?")
    assert [o["label"] for o in item["options"]] == [
        "Part of credit note NC A/12. The rest is still to come.",
        "Something else. Ask Papelaria Norte for its credit note."]
    assert "The amounts are not the same, so I won't match them on a guess." in item["why"]
    assert demo.missing.plan(back) == ("Papelaria Norte refunded €100.00 on 15 September, but its credit note is for "
                                       "a different amount. I asked you about it.")
    plain(*owner_texts(item))
    golden_rule(demo)
    assert "Papelaria Norte refunded €100.00" in svc.ask("what needs my attention?")["answer"]

    # "part of it": the refund closes on the credit note and the answer; the rest is still to come
    out = svc.answer(item["id"], f"part:{note.id}")
    assert out["message"] == "Done. I counted it as part of credit note NC A/12. €23.00 is still to come."
    assert stage(demo, back) is Stage.CLOSED and stage(demo, note) is Stage.UNDERSTOOD
    assert back.refund_answer_ev in repo.items[back.item_id].history[-1].evidence_ids
    assert note.hold_reason == "€23.00 of the Papelaria Norte credit note NC A/12 is still to come."
    assert note.hold_reason in [r["text"] for r in svc.month("hazel-tree", "2026-09")["remaining"]]
    assert demo.auditor.recheck() == []

    # the rest arrives: it is matched to what is left, and the credit note closes with both refunds
    rest = refund(demo, "mbcp-0920-09", date(2026, 9, 20), "23.00", "DEVOLUCAO 2 PAPELARIA NORTE")
    assert rest.document_ids == [note.id] and stage(demo, rest) is Stage.CLOSED and stage(demo, note) is Stage.CLOSED
    closing = repo.items[note.item_id].history[-1]
    assert closing.note == "Refunded in full on 20 September."
    assert {back.evidence_id, rest.evidence_id} <= set(closing.evidence_ids)
    assert [s["step"] for s in demo.refund_chain(document_id=note.id)] == [
        "invoice", "payment", "credit_note", "refund", "refund"]
    golden_rule(demo)


def test_a_refund_the_owner_says_is_for_something_else_is_chased_for_its_own_credit_note(
        demo: Orchestrator, svc: BackOfficeService) -> None:
    _, note, back = mismatched_refund(demo, "100.00")
    (item,) = [i for i in svc.needs_you()["items"] if i["id"].endswith("_refund")]
    assert svc.answer(item["id"], "other")["message"] == "Done. I'm looking for its own credit note."
    assert not back.document_ids and note.id in back.not_for_document_ids
    assert demo.repo.chases[back.id].sent  # asked for its own credit note
    assert stage(demo, note) is Stage.UNDERSTOOD
    demo.run(at(10, 3))
    assert not [n for n in demo.repo.open_needs() if n.subject_id == back.id]  # not asked twice


def test_a_refund_larger_than_its_credit_note_is_never_part_of_it(demo: Orchestrator, svc: BackOfficeService) -> None:
    _, note, back = mismatched_refund(demo, "150.00")
    (item,) = [i for i in svc.needs_you()["items"] if i["id"].endswith("_refund")]
    assert item["question"].endswith("is for €123.00. What is this refund for?")
    assert [o["id"] for o in item["options"]] == ["other"]
    assert stage(demo, note) is Stage.UNDERSTOOD and not back.document_ids


def test_money_back_to_a_customer_expects_your_own_credit_note(demo: Orchestrator, svc: BackOfficeService) -> None:
    repo = demo.repo
    sale = repo.documents[upload(demo, hazel_sale()).document_ids[0]]
    demo.ingest_bank([E.row("mbcp-0924-09", "mbcp-ht", date(2026, 9, 24), "1230.00", "ATELIER LUME LDA",
                            "TRF FT HT2026/31", TransactionKind.TRANSFER_IN, iban=LUME_IBAN)])
    paid = tx_by(demo, "TRF FT HT2026/31")
    assert paid.document_ids == [sale.id]
    demo.ingest_bank([E.row("mbcp-0926-09", "mbcp-ht", date(2026, 9, 26), "-230.00", "ATELIER LUME LDA",
                            "DEVOLUCAO ATELIER LUME", TransactionKind.TRANSFER_OUT, iban=LUME_IBAN)])
    back = tx_by(demo, "DEVOLUCAO ATELIER LUME")
    assert back.decision is not None and back.decision.rule == "customer_refund"
    assert back.decision.expectation.value == "refund_or_credit_note" and back.decision.quality is Quality.GREEN
    assert back.decision.reason == "Money back to your customer Atelier LUME. Your own credit note covers it."
    assert stage(demo, back) is Stage.UNDERSTOOD and back.id not in repo.chases
    assert demo.missing.plan(back) == ("The €230.00 refund to Atelier LUME on 26 September needs your own credit "
                                       "note. Make it where you make your invoices and send it to me: I will match it.")
    # our own credit note, correcting our invoice, closes it; the chain shows the customer's side
    ours = qr_document("NC", "NC HT2026/4", "HTQ7K2MP-4", "230.00", "187.00", "43.00", "2026-09-25",
                       title="Nota de crédito", issuer=E.HAZEL_NIF, issuer_name="Hazel Tree Interiores, Lda.",
                       buyer=CUSTOMER_NIF, buyer_line="Cliente: Atelier Lume, Lda.",
                       extra=("Referente à fatura FT HT2026/31",))
    note = repo.documents[upload(demo, ours).document_ids[0]]
    assert note.sales and note.document.doc_type is DocumentType.CREDIT_NOTE and note.credit_for == sale.id
    assert back.document_ids == [note.id] and stage(demo, back) is Stage.CLOSED
    assert repo.items[back.item_id].history[-1].note == ("Refund to your customer for credit note NC HT2026/4, which "
                                                         "corrects invoice FT HT2026/31.")
    assert [s["label"] for s in demo.refund_chain(tx_id=back.id)] == [
        "Invoice FT HT2026/31", "Atelier LUME paid €1,230.00 on 24 September",
        "Credit note NC HT2026/4, which corrects invoice FT HT2026/31",
        "Refund of €230.00 paid to Atelier LUME on 26 September"]
    assert "Bank account: the one the customer paid invoice FT HT2026/31 from" in back.match_why
    golden_rule(demo)


def test_money_out_to_a_known_supplier_still_needs_its_invoice(demo: Orchestrator) -> None:
    """Only a customer's refund changes what is expected: a supplier paid twice is not 'money back'."""
    demo.ingest_bank([bank("mbcp-0920-09", "mbcp-ht", date(2026, 9, 20), "-40.00", "PAPELARIA NORTE", "TRF NORTE")])
    rec = tx_by(demo, "TRF NORTE")
    assert rec.decision is not None and rec.decision.expectation.value == "invoice"
