"""The deterministic orchestrator end to end on the demo tenant (§3, §20–27, §38, §46–48, §55–57)."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from backoffice.closure import BlockerKind, Month, MonthState
from backoffice.demo import DEMO_TODAY, build_demo
from backoffice.demo import evidence as E
from backoffice.domain.lifecycle import ORDER, Stage
from backoffice.domain.models import Quality, TransactionKind
from backoffice.orchestrator import TZ, BankRow, Orchestrator
from backoffice.policy import ActionKind, Approval
from backoffice.policy.actions import Requirement
from backoffice.service import BackOfficeService

SEPT = Month(2026, 9)
AGENTS = {"discovery", "retrieval", "document", "verification", "fraud", "entity", "reconciliation", "obligation",
          "missing_evidence", "accountant", "closure", "auditor"}


@pytest.fixture(scope="module")
def demo() -> Orchestrator:
    return build_demo()


@pytest.fixture
def fresh() -> Orchestrator:
    return build_demo()


def ikea_row(bank_id: str, day: date, amount: str = "212.00") -> BankRow:
    return BankRow(bank_id=bank_id, account_id="card-4817", booked_on=day, amount=Decimal(amount),
                   counterparty="IKEA ALFRAGIDE", description="COMPRA CARTAO", kind=TransactionKind.CARD,
                   card_last4="4817")


# --------------------------------------------------------------------------- the golden rule, audited


def test_every_transition_carries_stored_evidence(demo: Orchestrator) -> None:
    repo = demo.repo
    assert repo.items
    for item in repo.items.values():
        assert item.history, item.id
        for t in item.history:
            assert t.evidence_ids and t.actor
            for ev in t.evidence_ids:
                repo.evidence(ev)  # raises if the id is not stored evidence
            assert t.at.tzinfo is not None
        if item.stage is Stage.CLOSED:
            assert item.quality is Quality.GREEN
            stages = [t.to_stage for t in item.history]
            assert [s for s in stages if s in ORDER][-4:] == [Stage.VERIFIED, Stage.MATCHED, Stage.CONFIRMED,
                                                              Stage.CLOSED]


def test_audit_chain_is_intact_and_every_agent_reported(demo: Orchestrator) -> None:
    repo = demo.repo
    report = repo.audit.verify(repo.tenant_id)
    assert report.ok and report.checked > 100
    agents = {json.loads(r.body)["agent"] for r in repo.audit_store.records(repo.tenant_id)}
    assert AGENTS - {"auditor"} <= agents  # the auditor only writes when it reopens something


def test_demo_is_deterministic() -> None:
    a, b = BackOfficeService(build_demo()), BackOfficeService(build_demo())
    for path in ("/api/home", "/api/needs-you", "/api/activity", "/api/months/hazel-tree/2026-09"):
        assert a.dispatch("GET", path, None) == b.dispatch("GET", path, None)
    assert a.repo.audit.checkpoint(a.repo.tenant_id) == b.repo.audit.checkpoint(b.repo.tenant_id)
    assert a.repo.clock.today() == DEMO_TODAY


def test_home_numbers_are_computed_from_items(demo: Orchestrator) -> None:
    repo = demo.repo
    for company_id in repo.companies:
        items = repo.items_for(company_id, SEPT)
        status = demo.month_status(company_id, SEPT)
        done = sum(1 for i in items if i.is_done and i.quality is Quality.GREEN)
        assert status.counts.done == done and status.counts.total == len(items)
        if not status.closed:
            assert status.percent_closed == done * 100 // len(items)
    assert demo.month_status("company-b", SEPT).state is MonthState.CLOSED
    assert demo.month_status("hazel-tree", SEPT).state is MonthState.ON_TRACK
    assert demo.month_status("company-c", SEPT).state is MonthState.NEEDS_OWNER


def test_pipeline_outcomes_per_evidence_kind(demo: Orchestrator) -> None:
    repo = demo.repo
    by_desc = {r.tx.description: r for r in repo.transactions.values()}
    # internal transfer, bank fee: no document needed (§21), closed as NOT_REQUIRED with GREEN evidence
    for desc in ("TRF COMPANY C", "TRF HAZEL TREE", "COMISSAO MANUTENCAO CONTA", "IMPOSTO DO SELO COMISSAO"):
        rec = by_desc[desc]
        assert repo.items[rec.item_id].stage is Stage.NOT_REQUIRED and rec.decision is not None
        assert not rec.decision.requires_document
    # tax payment proven by the AT letter's amount and reference (§24)
    tax = by_desc["PAG ESTADO IVA 2026/07"]
    assert repo.items[tax.item_id].stage is Stage.CLOSED and tax.proof_evidence_ids
    letter = next(o for o in repo.obligations.values() if o.obligation.entity_id == "hazel-tree")
    assert letter.satisfied_by == (tax.evidence_id,)
    # UBL e-invoice + email body -> GREEN, matched to the direct debit
    vodafone = by_desc["DD VODAFONE PORTUGAL"]
    doc = repo.documents[vodafone.document_ids[0]]
    assert doc.document.quality is Quality.GREEN and len(doc.evidence_ids) == 2
    assert repo.evidence(doc.evidence_ids[0]).format.value == "ubl"
    # the .eml "View invoice" link followed through the portal adapter (§9)
    adobe = next(d for d in repo.documents.values() if d.retrieved)
    assert repo.evidence(adobe.evidence_ids[0]).original_url == E.ADOBE_INVOICE_URL
    assert adobe.matched_tx_ids and repo.items[adobe.item_id].stage is Stage.CLOSED
    # the missing EDP invoice is chased once, politely, in Portuguese
    edp = by_desc["DD EDP COMERCIAL"]
    chase = repo.chases[edp.id]
    assert chase.message.to == "faturas@edp.pt" and "64,10" in chase.message.body
    assert repo.items[edp.item_id].stage is Stage.UNDERSTOOD
    # the accountant's rent question was answered from the matched receipt (§28)
    answered = [q for q in repo.accountant_questions.values() if q.status == "answered"]
    assert len(answered) == 1 and "Marta Gonçalves" in answered[0].answer


# --------------------------------------------------------------------------- one-tap learning (§38)


def test_one_tap_learning_resolves_similar_items_now_and_later(fresh: Orchestrator) -> None:
    repo = fresh.repo
    fresh.ingest_bank([ikea_row("mbcp-1001-01", date(2026, 10, 1))])
    open_ikea = [n for n in repo.open_needs() if n.id.startswith("nd_ikea")]
    assert len(open_ikea) == 2
    outcome = fresh.answer("nd_ikea_418", "entity:hazel-tree", remember=True)
    assert outcome.ok and outcome.learned == "Always use Hazel Tree for IKEA Alfragide paid with card •••• 4817"
    assert [n.id for n in repo.open_needs() if n.id.startswith("nd_ikea")] == []
    second = next(n for n in repo.needs.values() if n.id == "nd_ikea_212")
    assert second.status == "resolved" and second.resolution == "rule"
    assert repo.transactions[second.subject_id].tx.entity_id == "hazel-tree"
    # a future IKEA payment on the same card never reaches Needs You
    before = len(repo.needs)
    report = fresh.ingest_bank([ikea_row("mbcp-1002-01", date(2026, 10, 2), "64.50")])
    assert len(repo.needs) == before
    assert repo.transactions[report.transaction_ids[0]].tx.entity_id == "hazel-tree"
    assert any(a.kind == "learned" for a in repo.activity)
    # the answered IKEA payment closes with its receipt, now under Hazel Tree
    ikea = repo.transactions[repo.needs["nd_ikea_418"].subject_id]
    assert repo.items[ikea.item_id].stage is Stage.CLOSED
    assert demo_status(fresh, "company-c").needs_you == 0


def test_without_remember_similar_items_still_ask(fresh: Orchestrator) -> None:
    fresh.ingest_bank([ikea_row("mbcp-1001-01", date(2026, 10, 1))])
    fresh.answer("nd_ikea_418", "entity:hazel-tree", remember=False)
    assert [n.id for n in fresh.repo.open_needs() if n.id.startswith("nd_ikea")] == ["nd_ikea_212"]


def test_personal_answer_sets_payment_aside(fresh: Orchestrator) -> None:
    fresh.answer("nd_ikea_418", "personal")
    rec = fresh.repo.transactions[fresh.repo.needs["nd_ikea_418"].subject_id]
    assert rec.private and fresh.repo.items[rec.item_id].stage is Stage.NOT_REQUIRED
    assert demo_status(fresh, "company-c").needs_you == 0


def demo_status(o: Orchestrator, company: str):  # type: ignore[no-untyped-def]
    return o.month_status(company, SEPT)


# --------------------------------------------------------------------------- IBAN hard stop (§25, §26)


def test_changed_iban_blocks_payment_and_matching(fresh: Orchestrator) -> None:
    repo = fresh.repo
    needs = repo.needs["nd_vodafone_iban"]
    doc = repo.documents[needs.subject_id]
    assert doc.on_hold and doc.fraud is not None and doc.fraud.hard_stop
    assert repo.items[doc.item_id].stage is Stage.NEEDS_OWNER
    decision = fresh.payment_decision(doc.id)
    assert not decision.allowed_now and decision.requires is Requirement.HARD
    # even a correctly bound hard approval cannot release a payment while the hold stands
    approval = Approval(tenant_id=repo.tenant_id, action=ActionKind.MONEY_MOVEMENT, subject_id=doc.id,
                        level=Requirement.HARD, approved_by="owner", approved_at=repo.clock.now(),
                        entity_id=doc.document.entity_id, fingerprint=fresh.payment_fingerprint(doc),
                        acknowledged_risk=False)
    assert not fresh.payment_decision(doc.id, approval).allowed_now
    # a payment to the new account is never matched to the held invoice
    fresh.ingest_bank([BankRow(bank_id="mbcp-1005-01", account_id="mbcp-ht", booked_on=date(2026, 10, 5),
                               amount=Decimal("-92.40"), counterparty="VODAFONE PORTUGAL", description="TRF",
                               kind=TransactionKind.TRANSFER_OUT, counterparty_iban=E.VODAFONE_NEW_IBAN)])
    assert not doc.matched_tx_ids
    assert E.VODAFONE_NEW_IBAN not in repo.suppliers["sup-vodafone"].known_ibans
    # keeping it blocked leaves the supplier's bank details untouched
    fresh.answer("nd_vodafone_iban", "keep_blocked")
    assert doc.on_hold and repo.items[doc.item_id].stage is Stage.CONFLICT
    assert repo.suppliers["sup-vodafone"].known_ibans == [E.VODAFONE_IBAN]
    assert not fresh.payment_decision(doc.id).allowed_now


def test_confirmation_by_phone_is_the_only_release(fresh: Orchestrator) -> None:
    repo = fresh.repo
    doc = repo.documents[repo.needs["nd_vodafone_iban"].subject_id]
    with pytest.raises(ValueError):
        fresh.answer("nd_vodafone_iban", "approve")  # there is no "just approve" option
    fresh.answer("nd_vodafone_iban", "confirmed_by_phone")
    assert not doc.on_hold and doc.hold_released
    assert E.VODAFONE_NEW_IBAN in repo.suppliers["sup-vodafone"].known_ibans
    # the payment itself still needs a hard approval bound to these exact facts (§25)
    assert not fresh.payment_decision(doc.id).allowed_now
    approval = Approval(tenant_id=repo.tenant_id, action=ActionKind.MONEY_MOVEMENT, subject_id=doc.id,
                        level=Requirement.HARD, approved_by="owner", approved_at=repo.clock.now(),
                        entity_id=doc.document.entity_id, fingerprint=fresh.payment_fingerprint(doc))
    assert fresh.payment_decision(doc.id, approval).allowed_now
    records = [json.loads(r.body) for r in repo.audit_store.records(repo.tenant_id)]
    assert any(r["action"] == "trust_iban" and r["actor"] == "owner" for r in records)


# --------------------------------------------------------------------------- self-healing connections (§47–48)


def test_stale_connector_keeps_the_month_from_green(fresh: Orchestrator) -> None:
    svc = BackOfficeService(fresh)
    assert demo_status(fresh, "company-b").closed
    svc.mark_connection_stale("gmail", since=datetime(2026, 9, 20, 14, 42, tzinfo=TZ))
    status = demo_status(fresh, "company-b")
    assert not status.closed and status.state is MonthState.NEEDS_OWNER
    assert BlockerKind.CONNECTOR in {b.kind for b in status.blockers}
    assert status.percent_closed < 100
    assert ("company-b", "2026-09") not in fresh.repo.closed_months
    home = svc.home()
    assert home["needsYouCount"] == 3 and home["currentMonth"]["percentClosed"] < 100
    company_b = next(c for c in home["companies"] if c["id"] == "company-b")
    assert company_b["statusLabel"] == "Needs reconnecting" and company_b["tone"] == "risk"
    month = svc.month("company-b", "2026-09")
    assert month is not None and month["status"] == "open"
    assert month["remaining"][0]["tone"] == "risk" and month["remaining"][0]["linkLabel"] == "Reconnect"
    svc.reconnect("gmail")
    assert demo_status(fresh, "company-b").closed


# --------------------------------------------------------------------------- auditor (§55, §57)


def test_auditor_reopens_a_closed_item_that_no_longer_holds(fresh: Orchestrator) -> None:
    repo = fresh.repo
    uber = next(d for d in repo.documents.values() if d.document.invoice_number == "FS UBR2026/45077")
    tx = repo.transactions[uber.matched_tx_ids[0]]
    assert repo.items[tx.item_id].stage is Stage.CLOSED and demo_status(fresh, "company-b").closed
    # a later reading contradicts the receipt: its details no longer verify
    uber.document = uber.document.model_copy(update={"quality": Quality.AMBER})
    report = fresh.run()
    assert tx.item_id in report.reopened and uber.item_id in report.reopened
    item = repo.items[tx.item_id]
    assert item.stage is Stage.CONFLICT and item.quality is Quality.RED
    assert item.history[-1].actor == "system:auditor" and item.history[-1].evidence_ids
    assert not demo_status(fresh, "company-b").closed
    records = [json.loads(r.body) for r in repo.audit_store.records(repo.tenant_id)]
    assert any(r["agent"] == "auditor" and r["action"] == "reopen" for r in records)


# --------------------------------------------------------------------------- missing evidence (§22)


def test_chased_invoice_arrives_and_closes_the_payment(fresh: Orchestrator) -> None:
    repo = fresh.repo
    before = demo_status(fresh, "hazel-tree")
    report = fresh.ingest_file(E.EDP_INVOICE, filename="edp.txt", content_type="text/plain")
    doc = repo.documents[report.document_ids[0]]
    assert doc.document.quality is Quality.GREEN and doc.matched_tx_ids
    edp = repo.transactions[doc.matched_tx_ids[0]]
    assert repo.items[edp.item_id].stage is Stage.CLOSED
    after = demo_status(fresh, "hazel-tree")
    assert after.percent_closed > before.percent_closed
    assert after.summary.missing_documents_retrieved == before.summary.missing_documents_retrieved + 1
    assert after.missing_documents == 0


def test_bank_csv_upload_becomes_transactions(fresh: Orchestrator) -> None:
    csv_data = ("date,amount,counterparty,description,account,kind\n"
                "2026-10-01,-4.99,MILLENNIUM BCP,COMISSAO MANUTENCAO CONTA,mbcp-cc,fee\n").encode()
    report = fresh.ingest_file(csv_data, filename="extrato.csv", content_type="text/csv")
    assert report.route == "bank" and len(report.transaction_ids) == 1
    rec = fresh.repo.transactions[report.transaction_ids[0]]
    assert rec.company_id == "company-c" and fresh.repo.items[rec.item_id].stage is Stage.NOT_REQUIRED
