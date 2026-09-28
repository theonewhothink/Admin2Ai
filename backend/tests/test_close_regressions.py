"""Regression tests for defects found in review of the closure module.

Each test names the defect it pins down. They were written failing first.
"""

from __future__ import annotations

import csv
import io
import sys
import time
import warnings
import zipfile
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from backoffice.closure import (
    Activity,
    ActivityKind,
    CloseProgress,
    DeliveryChannel,
    EvidenceFact,
    InteractionKind,
    Month,
    OwnerInteraction,
    PackageDelivery,
    PackageEntry,
    ProofKind,
    StepKind,
    StepState,
    VerificationCondition,
    build_package,
    compute_month_status,
    detect_obligation,
    evaluate_schedule,
    home_summary,
    plan_month_end,
    satisfy,
    verify_package,
)
from backoffice.domain.lifecycle import ORDER, Stage, TrackedItem
from backoffice.domain.models import (
    Document,
    DocumentType,
    Evidence,
    EvidenceFormat,
    LegalEntity,
    Obligation,
    ObligationKind,
    Quality,
    SourceKind,
    Transaction,
)

SEPT = Month(2026, 9)
RECEIVED = date(2026, 9, 20)
NOW = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)
ENTITY = LegalEntity(id="ent_hazel", tenant_id="t1", name="Hazel Tree", country="PT", tax_id="PT509123456")


def detect(text: str, **kw):
    kw.setdefault("tenant_id", "t1")
    kw.setdefault("received_on", RECEIVED)
    kw.setdefault("default_entity_id", ENTITY.id)
    return detect_obligation(text, **kw)


@dataclass(frozen=True)
class Covered:
    name: str = "Gmail"
    healthy: bool = True
    covered_from: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)
    covered_until: datetime = NOW


def walk(subject_id: str, to: Stage, subject_type: str = "transaction") -> TrackedItem:
    item = TrackedItem(id=f"item_{subject_id}", tenant_id="t1", subject_type=subject_type, subject_id=subject_id)
    for stage in ORDER[1 : ORDER.index(to) + 1]:
        item.advance(stage, actor="system", evidence_ids=["ev"], quality=Quality.GREEN)
    return item


# =========================================================================== obligations: detection


def test_detection_time_grows_linearly_not_quadratically():
    """Defect: anchor x hit loops made a 290 KB email take ~10 s (a 1 MB one, minutes)."""
    text = "Autoridade Tributária. " + "pagamento até 1/1/2027 valor 5,00 € " * 8000
    started = time.perf_counter()
    f = detect(text)
    elapsed = time.perf_counter() - started
    assert f is not None and f.due_on == date(2027, 1, 1) and f.amount == Decimal("5.00")
    assert elapsed < 3.0, f"detection took {elapsed:.1f}s"


@pytest.mark.parametrize(
    "snippet",
    ["Valor a pagar: -405,00 €", "Total a pagar: € -405,00", "Total a pagar: −405,00 €",
     "Amount due: -€405.00", "Total a pagar: -405,00"],
)  # fmt: skip
def test_a_negative_total_is_not_read_as_an_amount_to_pay(snippet):
    """Defect: the minus sign was dropped, so a credit of €405 became 'pay €405'."""
    f = detect(f"Autoridade Tributária. {snippet}. Data limite de pagamento 20/10/2026.")
    assert f is not None
    assert f.amount is None
    assert f.obligation is not None and f.obligation.amount is None
    assert any("so there may be nothing to pay" in r for r in f.reasons)


def test_a_negative_total_does_not_fall_back_to_another_amount():
    f = detect("Autoridade Tributária. Imposto 400,00 €. Total a pagar: -5,00 €. Data limite 20/10/2026.")
    assert f is not None and f.amount is None


def test_a_zero_total_is_nothing_to_pay():
    f = detect("Autoridade Tributária. Juros 12,00 €. Total a pagar: 0,00 €. Data limite 20/10/2026.")
    assert f is not None and f.amount is None
    assert any("€0.00" in r for r in f.reasons)


def test_a_dash_used_as_a_separator_is_not_a_minus_sign():
    f = detect("Autoridade Tributária. Total a pagar - 405,00 €. Data limite 20/10/2026.")
    assert f is not None and f.amount == Decimal("405.00")


def test_conflicting_amounts_are_never_settled_by_paying_either_one():
    """Defect: a RED letter kept the smaller amount in its condition, so paying it closed the obligation."""
    f = detect(
        "Autoridade Tributária. Referência para pagamento: 123 456 789\n"
        "Total a pagar: 100,00 €. Valor a pagar: 110,00 €. Data limite de pagamento: 20/10/2026."
    )
    assert f is not None and f.quality is Quality.RED
    assert f.amount is None and f.obligation is not None and f.obligation.amount is None
    assert "the amount" in f.missing
    assert f.condition.disputed
    for paid in ("100.00", "110.00"):
        proof = EvidenceFact("ev_bank", ProofKind.PAYMENT, date(2026, 10, 1), Quality.GREEN, Decimal(paid),
                             reference="123456789")  # fmt: skip
        result = satisfy(f.obligation, [proof])
        assert not result.satisfied
        assert result.quality is Quality.RED
        assert result.obligation.satisfied_by_evidence_ids == []


def test_disputed_condition_round_trips():
    c = VerificationCondition(ProofKind.PAYMENT, reference="123", by=date(2026, 10, 20), disputed=True)
    assert "disputed=1" in c.encode()
    assert VerificationCondition.parse(c.encode()) == c
    with pytest.raises(ValueError):
        VerificationCondition.parse("v1;proof=payment;disputed=yes")


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ("Referência para pagamento: 123 456 789.", "123456789"),
        ("Referência: 123 456 789, valor 405,00 €.", "123456789"),
        ("Ref. RF18 5390 0754 7034.", "RF18539007547034"),  # the ISO 11649 example
        ("Payment reference: RF18 5390 0754 7034 before the due date.", "RF18539007547034"),
        ("Ref. RF18 5390 0754 7034 20/10/2026 is the date.", "RF18539007547034"),
        ("Ref. PROC-2026-17 20/10/2026 is the date.", "PROC202617"),  # a following date is not glued on
        ("Ref. RF18 5390 0754.", None),  # check digits fail: not guessed
    ],
)
def test_references_are_read_whole(snippet, expected):
    """Defect: a full stop after '123 456 789' cut it to '123456'; RF references kept only 'RF18'."""
    f = detect(f"Autoridade Tributária. {snippet} Data limite de pagamento: 20/10/2026.")
    assert f is not None and f.reference == expected


# =========================================================================== obligations: proof


def test_detected_conditions_start_on_the_day_the_letter_arrived():
    f = detect("Senhorio: a renda de outubro no valor de 900,00 € deve ser paga até 01/10/2026.")
    assert f is not None and f.condition.since == RECEIVED
    assert "since=2026-09-20" in f.obligation.verification_condition
    assert VerificationCondition.parse(f.obligation.verification_condition) == f.condition


def test_proof_older_than_the_letter_does_not_close_it():
    """Defect: last month's rent, last year's filing or an old reply closed a new obligation."""
    rent = detect("Senhorio: a renda de outubro no valor de 900,00 € deve ser paga até 01/10/2026.")
    september_rent = EvidenceFact("ev_sep", ProofKind.PAYMENT, date(2026, 9, 1), Quality.GREEN, Decimal("-900.00"))
    result = satisfy(rent.obligation, [september_rent])
    assert not result.satisfied and result.quality is Quality.AMBER
    assert result.reasons == ("The proof is older than the letter.",)
    on_time = EvidenceFact("ev_oct", ProofKind.PAYMENT, date(2026, 9, 28), Quality.GREEN, Decimal("-900.00"))
    assert satisfy(rent.obligation, [september_rent, on_time]).evidence_ids == ("ev_oct",)

    filing = detect("HMRC: your VAT return must be filed no later than 7 November 2026.")
    last_year = EvidenceFact("ev_2025", ProofKind.SUBMISSION, date(2025, 11, 2), Quality.GREEN)
    assert not satisfy(filing.obligation, [last_year]).satisfied

    kyc = detect("KYC review: please send proof of address due by 5 October 2026.", source_kind=SourceKind.BANK)
    march_reply = EvidenceFact("ev_march", ProofKind.REPLY, date(2026, 3, 1), Quality.GREEN)
    assert not satisfy(kyc.obligation, [march_reply]).satisfied

    renewal = detect("A sua apólice de seguro renova automaticamente a 01/11/2026.")
    old_decision = EvidenceFact("ev_dec", ProofKind.DECISION, date(2026, 1, 5), Quality.GREEN)
    assert not satisfy(renewal.obligation, [old_decision]).satisfied


def test_identity_proof_from_before_the_letter_still_counts():
    tax = detect("Autoridade Tributária. Referência para pagamento: 123 456 789\n"
                 "Total a pagar: 405,00 €. Data limite de pagamento: 20/10/2026.")  # fmt: skip
    paid_before = EvidenceFact("ev_bank", ProofKind.PAYMENT, date(2026, 9, 18), Quality.GREEN, Decimal("405.00"),
                               reference="123456789")  # fmt: skip
    assert satisfy(tax.obligation, [paid_before]).satisfied  # the reference identifies this debt

    renewal = detect("A sua apólice de seguro renova automaticamente a 01/11/2026.")
    renewed_early = EvidenceFact("ev_policy", ProofKind.RENEWAL, date(2026, 9, 1), Quality.GREEN,
                                 valid_until=date(2027, 11, 1))  # fmt: skip
    assert satisfy(renewal.obligation, [renewed_early]).satisfied


def test_evidence_fact_accepts_enum_values_as_strings():
    """Defect: a fact built from stored strings ('decision') was compared with `is` and never matched."""
    cond = VerificationCondition(ProofKind.RENEWAL, by=date(2026, 11, 1))
    ob = Obligation(tenant_id="t1", entity_id=ENTITY.id, kind=ObligationKind.CONTRACT_RENEWAL, title="Contract renewal",
                    due_on=date(2026, 11, 1), verification_condition=cond.encode())  # fmt: skip
    fact = EvidenceFact("ev_owner", "decision", date(2026, 10, 1), "verified")  # type: ignore[arg-type]
    assert fact.kind is ProofKind.DECISION and fact.quality is Quality.GREEN
    assert satisfy(ob, [fact]).satisfied
    with pytest.raises(ValueError):
        EvidenceFact("ev", "guess", date(2026, 10, 1), Quality.GREEN)  # type: ignore[arg-type]


# =========================================================================== activity log


def test_activity_and_interaction_records_accept_enum_values_as_strings():
    """Defect: actor='system' (a stored string) was compared with `is` and silently not counted."""
    at = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    retrieved = Activity("missing_document_retrieved", at, ENTITY.id, actor="system", subject_id="tx_1",  # type: ignore[arg-type]
                         period="2026-09")  # type: ignore[arg-type]  # fmt: skip
    assert retrieved.kind is ActivityKind.MISSING_DOCUMENT_RETRIEVED and retrieved.period == SEPT
    onboarding = OwnerInteraction(at, 600, "onboarding", ENTITY.id)  # type: ignore[arg-type]
    answer = OwnerInteraction(at, 60, "answer", ENTITY.id)  # type: ignore[arg-type]
    assert onboarding.kind is InteractionKind.ONBOARDING
    s = compute_month_status(ENTITY.id, SEPT, [], now=NOW, connectors=[Covered()],
                             activities=[retrieved], interactions=[onboarding, answer])  # fmt: skip
    assert s.summary.missing_documents_retrieved == 1
    assert s.summary.owner_minutes == 1  # onboarding time is reported separately
    with pytest.raises(ValueError):
        Activity("something_else", at, ENTITY.id)  # type: ignore[arg-type]


# =========================================================================== Home (§35)


def test_home_percent_matches_the_only_company_when_weighted():
    """Defect: Home showed 'September 50% closed' next to a company at 90%."""
    items = [walk("tx_big", Stage.CLOSED), walk("tx_small", Stage.VERIFIED)]
    weights = {"tx_big": Decimal("900.00"), "tx_small": Decimal("100.00")}
    s = compute_month_status(ENTITY.id, SEPT, items, now=NOW, connectors=[Covered()], weights=weights)
    home = home_summary([s], {ENTITY.id: "Hazel Tree"}, today=date(2026, 10, 3))
    assert s.percent_closed == 90
    assert home.percent_closed == 90 and home.month_line == "September 90% closed"


def test_home_percent_combines_weighted_companies_by_their_size():
    heavy = compute_month_status("e1", SEPT, [walk("a", Stage.CLOSED), walk("b", Stage.VERIFIED)], now=NOW,
                                 connectors=[Covered()], weights={"a": Decimal(3), "b": Decimal(1)})  # fmt: skip
    plain = compute_month_status("e2", SEPT, [walk("c", Stage.VERIFIED), walk("d", Stage.VERIFIED)], now=NOW,
                                 connectors=[Covered()])  # fmt: skip
    home = home_summary([heavy, plain], {}, today=date(2026, 10, 3))
    # (75% of 2 items + 0% of 2 items) / 4 items = 37.5% -> 37
    assert home.percent_closed == 37


# =========================================================================== accountant package


def closed_item(subject_id: str) -> TrackedItem:
    return walk(subject_id, Stage.CLOSED)


def evidence(url: str | None) -> Evidence:
    return Evidence(id="ev_1", tenant_id="t1", source_kind=SourceKind.EMAIL, format=EvidenceFormat.PDF,
                    sha256=Evidence.hash_bytes(b"x"), original_url=url,
                    retrieved_at=datetime(2026, 9, 25, tzinfo=timezone.utc))  # fmt: skip


def entry_with(documents=(), url: str | None = None) -> PackageEntry:
    tx = Transaction(id="tx_1", tenant_id="t1", account_id="a", booked_on=date(2026, 9, 3),
                     amount=Decimal("-60.00"), counterparty="Adobe")  # fmt: skip
    return PackageEntry.from_domain(closed_item("tx_1"), transaction=tx, documents=list(documents),
                                    evidence=[evidence(url)])  # fmt: skip


def build(entries, **kw):
    kw.setdefault("generated_at", datetime(2026, 10, 5, 8, 30, tzinfo=timezone.utc))
    return build_package(ENTITY, SEPT, entries, **kw)


def zipped_files(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n) for n in z.namelist()}


def test_evidence_links_are_shared_without_credentials_or_tokens():
    """Defect: magic-link tokens and user:password in URLs were shipped to the accountant."""
    url = "https://user:secret@portal.example.com/invoices/77.pdf?token=abc123&sig=zz#page=2"
    pkg = build([entry_with(url=url)])
    assert pkg.manifest["evidence"][0]["original_url"] == "https://portal.example.com/invoices/77.pdf"
    index = zipped_files(pkg.data)["evidence_index.csv"].decode()
    assert "secret" not in index and "abc123" not in index and "token" not in index
    assert "https://portal.example.com/invoices/77.pdf" in index
    assert build([entry_with(url="javascript:alert(1)")]).manifest["evidence"][0]["original_url"] is None


def test_ledger_shows_document_currency_and_signs_credit_notes():
    """Defect: a USD document sat under the EUR payment currency, and credit notes added to spend."""
    credit = Document(id="doc_cn", tenant_id="t1", evidence_ids=["ev_1"], doc_type=DocumentType.CREDIT_NOTE,
                      currency="USD", net_amount=Decimal("48.78"), vat_amount=Decimal("11.22"),
                      gross_amount=Decimal("60.00"))  # fmt: skip
    rows = list(csv.reader(io.StringIO(zipped_files(build([entry_with([credit])]).data)["ledger.csv"].decode())))
    row = dict(zip(rows[0], rows[1]))
    assert row["currency"] == "EUR" and row["document_currency"] == "USD"
    assert (row["net"], row["vat"], row["gross"]) == ("-48.78", "-11.22", "-60.00")
    assert rows[0][-1] == "item_id"


def _rezip(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(buffer, "w") as z:
        warnings.simplefilter("ignore")  # zipfile warns about the duplicate name we add on purpose
        for name, data in entries:
            z.writestr(name, data)
    return buffer.getvalue()


def test_verify_package_flags_duplicate_entries():
    """Defect: a second 'ledger.csv' placed before the genuine one went unnoticed."""
    pkg = build([entry_with()])
    files = zipped_files(pkg.data)
    tampered = [("ledger.csv", b"evil")] + list(files.items())
    assert verify_package(_rezip(tampered)) == ["ledger.csv appears more than once"]


def test_verify_package_refuses_oversized_entries_without_inflating_them():
    """Defect: a 200 KB zip inflated to 200 MB in memory before any check."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", b'{"files": {"bomb.bin": "00"}}')
        z.writestr("bomb.bin", b"\0" * (2 * 1024 * 1024))
    assert verify_package(buffer.getvalue(), max_file_bytes=1024 * 1024) == ["bomb.bin is too large to check"]
    assert verify_package(buffer.getvalue(), max_total_bytes=1024 * 1024) == ["the package is too large to check"]


def test_verify_package_reports_unsupported_entries_instead_of_raising():
    """Defect: an entry with an unknown compression method raised NotImplementedError."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("manifest.json", b'{"files": {"a.bin": "00"}}')
        z.writestr("a.bin", b"data")
    raw = bytearray(buffer.getvalue())
    local = raw.rindex(b"PK\x03\x04")  # a.bin is the last entry
    central = raw.rindex(b"PK\x01\x02")
    raw[local + 8 : local + 10] = (99).to_bytes(2, "little")  # compression method 99
    raw[central + 10 : central + 12] = (99).to_bytes(2, "little")
    assert verify_package(bytes(raw)) == ["unreadable package: NotImplementedError"]


def test_package_bytes_do_not_depend_on_the_operating_system(monkeypatch):
    """Defect: ZipInfo records the host OS, so Windows and Linux builds differed."""
    linux = build([entry_with()]).data
    monkeypatch.setattr(sys, "platform", "win32")
    assert build([entry_with()]).data == linux


# =========================================================================== autopilot (§27 Day +1)


def test_confirmation_of_an_older_package_does_not_complete_day_plus_one():
    """Defect: the accountant confirming last week's package counted after the package was rebuilt."""
    old = PackageDelivery(entity_id=ENTITY.id, month=SEPT, package_sha256="a" * 64,
                          prepared_at=datetime(2026, 10, 5, tzinfo=timezone.utc))  # fmt: skip
    old = old.deliver(at=datetime(2026, 10, 5, 9, tzinfo=timezone.utc), channel=DeliveryChannel.EMAIL,
                      recipient="contabilista@example.pt", evidence_id="ev_mail")  # fmt: skip
    old = old.confirm(at=datetime(2026, 10, 6, 9, tzinfo=timezone.utc), evidence_id="ev_reply")
    plan = plan_month_end(SEPT, date(2026, 10, 5))
    base = dict(audit_ran_on=date(2026, 9, 28), delivery=old)
    same = evaluate_schedule(plan, CloseProgress(**base, package_sha256="a" * 64), date(2026, 10, 6))
    day_plus_one = next(st for st in same.steps if st.plan.kind is StepKind.DELIVERY_CONFIRMED)
    assert day_plus_one.state is StepState.DONE
    rebuilt = evaluate_schedule(plan, CloseProgress(**base, package_sha256="b" * 64), date(2026, 10, 6))
    step = rebuilt.next_step
    assert step is not None and step.plan.kind is StepKind.DELIVERY_CONFIRMED
    assert step.detail == "The package changed after it was sent."
    with pytest.raises(ValueError):
        CloseProgress(package_sha256="not-a-hash")


# =========================================================================== second pass


def test_the_same_proof_listed_twice_is_counted_once():
    """Defect: one €202.50 payment listed twice 'added up' to a €405.00 debt."""
    cond = VerificationCondition(ProofKind.PAYMENT, Decimal("405.00"), reference="123", by=date(2026, 10, 20))
    ob = Obligation(tenant_id="t1", entity_id=ENTITY.id, kind=ObligationKind.TAX_DEADLINE, title="Tax payment",
                    due_on=date(2026, 10, 20), verification_condition=cond.encode())  # fmt: skip
    half = EvidenceFact("ev_a", ProofKind.PAYMENT, date(2026, 10, 2), Quality.GREEN, Decimal("202.50"), reference="123")
    result = satisfy(ob, [half, half])
    assert not result.satisfied
    other_half = EvidenceFact("ev_b", ProofKind.PAYMENT, date(2026, 10, 3), Quality.GREEN, Decimal("202.50"),
                              reference="123")  # fmt: skip
    assert satisfy(ob, [half, half, other_half]).evidence_ids == ("ev_a", "ev_b")


def test_one_proof_read_two_ways_is_a_conflict():
    cond = VerificationCondition(ProofKind.PAYMENT, Decimal("405.00"), reference="123", by=date(2026, 10, 20))
    ob = Obligation(tenant_id="t1", entity_id=ENTITY.id, kind=ObligationKind.TAX_DEADLINE, title="Tax payment",
                    due_on=date(2026, 10, 20), verification_condition=cond.encode())  # fmt: skip
    read_once = EvidenceFact("ev_a", ProofKind.PAYMENT, date(2026, 10, 2), Quality.GREEN, Decimal("405.00"),
                             reference="123")  # fmt: skip
    read_again = EvidenceFact("ev_a", ProofKind.PAYMENT, date(2026, 10, 2), Quality.GREEN, Decimal("450.00"),
                              reference="123")  # fmt: skip
    result = satisfy(ob, [read_once, read_again])
    assert not result.satisfied and result.quality is Quality.RED
    assert result.reasons == ("The same proof was read two different ways.",)


def _oak_transaction(**kw) -> Transaction:
    base = dict(id="tx_1", tenant_id="t1", account_id="a", booked_on=date(2026, 9, 3), amount=Decimal("-5.00"),
                counterparty="Supplier")  # fmt: skip
    base.update(kw)
    return Transaction(**base)


def test_package_entries_never_mix_tenants():
    """Defect: a document from another tenant could be copied into this tenant's package (§52)."""
    foreign = Document(id="doc_x", tenant_id="t2", evidence_ids=[], supplier_name="Someone else's supplier")
    with pytest.raises(ValueError):
        PackageEntry.from_domain(closed_item("tx_1"), transaction=_oak_transaction(), documents=[foreign])
    with pytest.raises(ValueError):
        PackageEntry.from_domain(closed_item("tx_1"), transaction=_oak_transaction(tenant_id="t2"))


def test_package_refuses_another_companys_items():
    """Defect: Oak's payment could be sent to Hazel Tree's accountant (§51)."""
    oak_entry = PackageEntry.from_domain(closed_item("tx_1"), transaction=_oak_transaction(entity_id="ent_oak"))
    assert oak_entry.entity_id == "ent_oak"
    with pytest.raises(ValueError):
        build([oak_entry])
    with pytest.raises(ValueError):
        build([replace(PackageEntry.from_domain(closed_item("tx_1"), transaction=_oak_transaction()), tenant_id="t2")])
    own = PackageEntry.from_domain(closed_item("tx_1"), transaction=_oak_transaction(entity_id=ENTITY.id))
    assert build([own]).manifest["counts"]["items"] == 1


def test_client_view_refuses_another_companys_items():
    from backoffice.closure import build_client_view

    s = compute_month_status(ENTITY.id, SEPT, [], now=NOW, connectors=[Covered()])
    oak_entry = PackageEntry.from_domain(closed_item("tx_1"), transaction=_oak_transaction(entity_id="ent_oak"))
    with pytest.raises(ValueError):
        build_client_view(ENTITY, s, entries=[oak_entry])


def test_a_subject_tracked_twice_is_counted_once_in_the_summary():
    """Defect: a re-imported transaction with two tracked items read '2 transactions checked'."""
    first = walk("tx_1", Stage.CLOSED)
    second = first.model_copy(update={"id": "item_again"})
    doc = walk("doc_1", Stage.CLOSED, "document")
    doc_again = doc.model_copy(update={"id": "item_doc_again"})
    s = compute_month_status(ENTITY.id, SEPT, [first, second, doc, doc_again], now=NOW, connectors=[Covered()])
    assert s.closed
    assert (s.summary.transactions_checked, s.summary.documents_collected) == (1, 1)
