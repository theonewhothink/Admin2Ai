"""Accountant package (§27 Day 0/+1): manifest, CSV, evidence index, ZIP, delivery."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backoffice.closure import (
    MANIFEST_SCHEMA,
    PT_EXCEL,
    CsvFormat,
    DeliveryChannel,
    DeliveryError,
    DeliveryState,
    EvidenceIntegrityError,
    EvidenceReader,
    Month,
    ObjectStoreLike,
    OpenQuestion,
    PackageDelivery,
    PackageEntry,
    StoredEvidenceReader,
    build_package,
    to_jsonable,
    verify_package,
)
from backoffice.domain.lifecycle import ORDER, Stage, TrackedItem
from backoffice.domain.models import (
    Document,
    DocumentType,
    Evidence,
    EvidenceFormat,
    LegalEntity,
    Quality,
    SourceKind,
    Transaction,
)

SEPT = Month(2026, 9)
GENERATED = datetime(2026, 10, 5, 8, 30, 15, tzinfo=timezone.utc)
ENTITY = LegalEntity(id="ent_hazel", tenant_id="t1", name="Hazel Tree, Lda.", country="PT", tax_id="PT509123456")

PDF_BYTES = b"%PDF-1.7 vodafone invoice FT 2026/183"
QR_BYTES = b"A:509123456*B:999999990*..."


def evidence(eid: str, data: bytes, filename: str | None, fmt=EvidenceFormat.PDF) -> Evidence:
    return Evidence(
        id=eid,
        tenant_id="t1",
        source_kind=SourceKind.EMAIL,
        format=fmt,
        sha256=Evidence.hash_bytes(data),
        filename=filename,
        retrieved_at=datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc),
        original_url="https://example.com/invoice",
    )


EV_PDF = evidence("ev_pdf", PDF_BYTES, "Fatura FT 2026/183 — Vodafone.pdf")
EV_QR = evidence("ev_qr", QR_BYTES, None, EvidenceFormat.QR)


class MemoryReader:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs
        self.reads: list[str] = []

    def read(self, ev: Evidence) -> bytes:
        self.reads.append(ev.id)
        return self.blobs[ev.id]


def closed_item(subject_id: str, subject_type: str = "transaction") -> TrackedItem:
    item = TrackedItem(id=f"item_{subject_id}", tenant_id="t1", subject_type=subject_type, subject_id=subject_id)
    for stage in ORDER[1:]:
        item.advance(stage, actor="system", evidence_ids=["ev"], quality=Quality.GREEN)
    return item


def vodafone_entry() -> PackageEntry:
    tx = Transaction(id="tx_voda", tenant_id="t1", account_id="acc", booked_on=date(2026, 9, 18),
                     amount=Decimal("-117.20"), counterparty="VODAFONE PORTUGAL", description="DD 0917")  # fmt: skip
    doc = Document(
        id="doc_voda", tenant_id="t1", evidence_ids=["ev_pdf"], doc_type=DocumentType.INVOICE,
        supplier_name="Café Ribeira & Vodafone", supplier_tax_id="PT502544180", invoice_number="FT 2026/183",
        issue_date=date(2026, 9, 17), net_amount=Decimal("95.28"), vat_amount=Decimal("21.92"),
        gross_amount=Decimal("117.20"), quality=Quality.GREEN,
    )  # fmt: skip
    return PackageEntry.from_domain(
        closed_item("tx_voda"),
        transaction=tx,
        documents=[doc],
        evidence=[EV_QR, EV_PDF],
        why=["Invoice total €117.20", "Bank charge €117.20", "Dates 1 day apart"],
        category="Telecom",
    )


def open_entry() -> PackageEntry:
    tx = Transaction(id="tx_ikea", tenant_id="t1", account_id="acc", booked_on=date(2026, 9, 3),
                     amount=Decimal("-1234.56"), counterparty="IKEA")  # fmt: skip
    item = TrackedItem(id="item_tx_ikea", tenant_id="t1", subject_type="transaction", subject_id="tx_ikea")
    return PackageEntry.from_domain(item, transaction=tx)


def refund_entry() -> PackageEntry:
    tx = Transaction(id="tx_refund", tenant_id="t1", account_id="acc", booked_on=date(2026, 9, 20),
                     amount=Decimal("50.00"), counterparty="=HYPERLINK(\"http://x\")")  # fmt: skip
    return PackageEntry.from_domain(closed_item("tx_refund"), transaction=tx, evidence=[EV_PDF])


def build(**kw):
    kw.setdefault("generated_at", GENERATED)
    entries = kw.pop("entries", [vodafone_entry(), open_entry(), refund_entry()])
    return build_package(ENTITY, SEPT, entries, **kw)


def read_zip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {name: z.read(name) for name in z.namelist()}


# --------------------------------------------------------------------------- manifest


def test_manifest_describes_the_month_completely():
    pkg = build(questions=[OpenQuestion("q_1", "Is the IKEA purchase for the office?", "item_tx_ikea")])
    m = pkg.manifest
    assert m["schema"] == MANIFEST_SCHEMA
    assert m["entity"] == {"id": "ent_hazel", "name": "Hazel Tree, Lda.", "tax_id": "PT509123456", "country": "PT"}
    assert m["period"] == {"month": "2026-09", "first_day": "2026-09-01", "last_day": "2026-09-30"}
    assert m["generated_at"] == "2026-10-05T08:30:15+00:00"
    assert [i["item_id"] for i in m["items"]] == ["item_tx_ikea", "item_tx_voda", "item_tx_refund"]
    voda = m["items"][1]
    assert voda["amount"] == "-117.20" and voda["status"] == "closed" and voda["quality"] == "verified"
    assert voda["why"] == ["Invoice total €117.20", "Bank charge €117.20", "Dates 1 day apart"]
    assert voda["evidence"] == [{"id": "ev_pdf", "sha256": EV_PDF.sha256}, {"id": "ev_qr", "sha256": EV_QR.sha256}]
    assert voda["documents"][0]["gross"] == "117.20" and voda["documents"][0]["invoice_number"] == "FT 2026/183"
    assert m["items"][0]["status"] == "open" and m["items"][0]["quality"] == "likely"
    assert m["open_questions"] == [
        {"id": "q_1", "text": "Is the IKEA purchase for the office?", "item_id": "item_tx_ikea",
         "asked_by": "accountant", "asked_at": None}
    ]  # fmt: skip
    assert m["counts"] == {"items": 3, "done": 2, "open": 1, "evidence": 2, "evidence_files": 0, "open_questions": 1}
    assert m["totals"] == {"EUR": {"money_in": "50.00", "money_out": "1351.76"}}
    assert m["complete"] is False
    assert not pkg.complete
    assert pkg.filename == "hazel-tree-lda-2026-09.zip"


def test_complete_only_when_every_item_is_done_and_no_question_is_open():
    assert build(entries=[vodafone_entry()]).complete
    assert not build(entries=[vodafone_entry()], questions=[OpenQuestion("q", "?")]).complete


def test_money_never_appears_as_a_float():
    raw = read_zip(build().data)["manifest.json"].decode()
    assert '"amount": "-117.20"' in raw
    with pytest.raises(TypeError):
        to_jsonable({"amount": 1.5})


# --------------------------------------------------------------------------- zip and integrity


def test_zip_holds_manifest_csvs_and_untouched_originals():
    reader = MemoryReader({"ev_pdf": PDF_BYTES, "ev_qr": QR_BYTES})
    assert isinstance(reader, EvidenceReader)
    pkg = build(evidence_reader=reader)
    files = read_zip(pkg.data)
    pdf_path = f"evidence/{EV_PDF.sha256[:16]}_Fatura_FT_2026_183_Vodafone.pdf"
    qr_path = f"evidence/{EV_QR.sha256[:16]}_original.bin"
    assert set(files) == {"manifest.json", "ledger.csv", "evidence_index.csv", pdf_path, qr_path}
    assert files[pdf_path] == PDF_BYTES
    assert sorted(reader.reads) == ["ev_pdf", "ev_qr"]  # shared evidence is read once
    manifest = json.loads(files["manifest.json"])
    assert manifest["files"][pdf_path] == EV_PDF.sha256
    pdf = next(e for e in manifest["evidence"] if e["id"] == "ev_pdf")
    assert pdf["item_ids"] == ["item_tx_refund", "item_tx_voda"] and pdf["path"] == pdf_path
    assert verify_package(pkg.data) == []


def test_stored_evidence_reader_reads_by_storage_key_and_skips_records_without_files():
    class DictStore:
        def __init__(self, blobs: dict[str, bytes]) -> None:
            self.blobs = blobs

        def get(self, key: str) -> bytes:
            return self.blobs[key]

    assert isinstance(DictStore({}), ObjectStoreLike)
    stored = EV_PDF.model_copy(update={"storage_key": "t1/ab/pdf"})
    bank_record = EV_QR  # no storage_key: nothing to ship, only indexed
    entry = PackageEntry.from_domain(
        closed_item("tx_s"),
        transaction=Transaction(id="tx_s", tenant_id="t1", account_id="a", booked_on=date(2026, 9, 1),
                                amount=Decimal("-1.00"), counterparty="X"),
        evidence=[stored, bank_record],
    )  # fmt: skip
    pkg = build(entries=[entry], evidence_reader=StoredEvidenceReader(DictStore({"t1/ab/pdf": PDF_BYTES})))
    paths = {e["id"]: e["path"] for e in pkg.manifest["evidence"]}
    assert paths["ev_qr"] is None and paths["ev_pdf"].startswith("evidence/")
    assert verify_package(pkg.data) == []
    with pytest.raises(KeyError):  # a missing original is never silently left out
        build(entries=[entry], evidence_reader=StoredEvidenceReader(DictStore({})))


def test_evidence_from_another_tenant_is_refused():
    foreign = EV_PDF.model_copy(update={"tenant_id": "t2"})
    tx = Transaction(id="tx_f", tenant_id="t1", account_id="a", booked_on=date(2026, 9, 1),
                     amount=Decimal("-1.00"), counterparty="X")  # fmt: skip
    with pytest.raises(ValueError):  # refused as soon as the entry is assembled
        PackageEntry.from_domain(closed_item("tx_f"), transaction=tx, evidence=[foreign])
    direct = PackageEntry("item_f", "transaction", "tx_f", date(2026, 9, 1), "X", Stage.CLOSED, Quality.GREEN,
                          evidence=(foreign,))  # fmt: skip
    with pytest.raises(ValueError):  # and again when the package is built
        build(entries=[direct])


def test_without_a_reader_the_package_lists_hashes_only():
    pkg = build()
    assert {e["path"] for e in pkg.manifest["evidence"]} == {None}
    assert not [n for n in read_zip(pkg.data) if n.startswith("evidence/")]


def test_a_changed_original_is_refused_not_shipped():
    reader = MemoryReader({"ev_pdf": PDF_BYTES + b" edited", "ev_qr": QR_BYTES})
    with pytest.raises(EvidenceIntegrityError):
        build(evidence_reader=reader)


def test_the_same_evidence_id_with_two_hashes_is_refused():
    clash = evidence("ev_pdf", b"something else", "x.pdf")
    tx_entry = PackageEntry.from_domain(closed_item("tx_x"), transaction=Transaction(
        id="tx_x", tenant_id="t1", account_id="a", booked_on=date(2026, 9, 1), amount=Decimal("-1"),
        counterparty="X"), evidence=[clash])  # fmt: skip
    with pytest.raises(EvidenceIntegrityError):
        build(entries=[vodafone_entry(), tx_entry])


def test_packages_are_reproducible_byte_for_byte():
    reader = MemoryReader({"ev_pdf": PDF_BYTES, "ev_qr": QR_BYTES})
    a = build(evidence_reader=reader)
    b = build(evidence_reader=reader, entries=[refund_entry(), open_entry(), vodafone_entry()])
    assert a.data == b.data and a.sha256 == b.sha256


def test_verify_package_detects_tampering_and_extra_files():
    pkg = build()
    files = read_zip(pkg.data)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in files.items():
            z.writestr(name, data.replace(b"-117.20", b"-17.20") if name == "ledger.csv" else data)
        z.writestr("extra.txt", b"hello")
    assert verify_package(buffer.getvalue()) == [
        "ledger.csv does not match its hash",
        "extra.txt is not listed in the manifest",
    ]
    assert verify_package(b"not a zip") == ["unreadable package: BadZipFile"]


def test_verify_package_reports_malformed_manifests():
    def zipped(manifest: bytes | None) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            if manifest is not None:
                z.writestr("manifest.json", manifest)
        return buffer.getvalue()

    assert verify_package(zipped(None)) == ["manifest.json is missing"]
    assert verify_package(zipped(b"[1, 2]")) == ["manifest.json is not a package manifest"]
    assert verify_package(zipped(b"{not json")) == ["unreadable package: JSONDecodeError"]
    assert verify_package(zipped(b'{"files": {"ledger.csv": "00"}}')) == ["ledger.csv is missing"]


# --------------------------------------------------------------------------- CSV


def ledger_rows(pkg, delimiter=",", bom=False) -> list[list[str]]:
    raw = read_zip(pkg.data)["ledger.csv"]
    assert raw.startswith(b"\xef\xbb\xbf") is bom
    text = raw.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))


def test_standard_ledger_csv():
    rows = ledger_rows(build())
    header = rows[0]
    assert header[:6] == ["date", "description", "amount", "currency", "status", "quality"]
    voda = next(r for r in rows if r[-1] == "item_tx_voda")
    as_dict = dict(zip(header, voda))
    assert as_dict["amount"] == "-117.20"
    assert as_dict["supplier"] == "Café Ribeira & Vodafone"
    assert as_dict["document_number"] == "FT 2026/183"
    assert as_dict["gross"] == "117.20"
    assert as_dict["why"] == "Invoice total €117.20 | Bank charge €117.20 | Dates 1 day apart"
    assert as_dict["description"] == "VODAFONE PORTUGAL — DD 0917"


def test_portuguese_excel_ledger_uses_semicolons_decimal_commas_and_a_bom():
    rows = ledger_rows(build(csv_format=PT_EXCEL), delimiter=";", bom=True)
    header = rows[0]
    ikea = dict(zip(header, next(r for r in rows if r[-1] == "item_tx_ikea")))
    assert ikea["amount"] == "-1234,56"
    voda = dict(zip(header, next(r for r in rows if r[-1] == "item_tx_voda")))
    assert (voda["net"], voda["vat"]) == ("95,28", "21,92")


def test_spreadsheet_formulas_are_neutralised():
    rows = ledger_rows(build())
    refund = next(r for r in rows if r[-1] == "item_tx_refund")
    assert refund[1].startswith("'=HYPERLINK")
    assert refund[2] == "50.00"  # numbers are not escaped


def test_several_documents_keep_the_amount_on_the_first_row_only():
    tx = Transaction(id="tx_multi", tenant_id="t1", account_id="a", booked_on=date(2026, 9, 10),
                     amount=Decimal("-300.00"), counterparty="Supplier")  # fmt: skip
    docs = [
        Document(id=f"doc_{i}", tenant_id="t1", evidence_ids=[], doc_type=DocumentType.INVOICE,
                 invoice_number=f"FT {i}", gross_amount=Decimal("150.00"))
        for i in (1, 2)
    ]  # fmt: skip
    entry = PackageEntry.from_domain(closed_item("tx_multi"), transaction=tx, documents=docs)
    rows = ledger_rows(build(entries=[entry]))
    assert [r[2] for r in rows[1:]] == ["-300.00", ""]
    assert [r[10] for r in rows[1:]] == ["FT 1", "FT 2"]


def test_evidence_index_csv():
    reader = MemoryReader({"ev_pdf": PDF_BYTES, "ev_qr": QR_BYTES})
    raw = read_zip(build(evidence_reader=reader).data)["evidence_index.csv"].decode()
    rows = list(csv.reader(io.StringIO(raw)))
    assert rows[0] == ["evidence_id", "sha256", "filename", "format", "source", "retrieved_at",
                       "original_url", "file_in_package", "item_ids"]  # fmt: skip
    pdf = next(r for r in rows if r[0] == "ev_pdf")
    assert pdf[1] == EV_PDF.sha256 and pdf[-1] == "item_tx_refund item_tx_voda"


def test_csv_format_validation():
    with pytest.raises(ValueError):
        CsvFormat(delimiter=",", decimal_comma=True)
    with pytest.raises(ValueError):
        CsvFormat(delimiter="|")


# --------------------------------------------------------------------------- entries


def test_entry_from_domain_checks_that_things_belong_together():
    tx = Transaction(id="tx_other", tenant_id="t1", account_id="a", booked_on=date(2026, 9, 1),
                     amount=Decimal("-1"), counterparty="X")  # fmt: skip
    with pytest.raises(ValueError):
        PackageEntry.from_domain(closed_item("tx_voda"), transaction=tx)
    doc_item = closed_item("doc_a", "document")
    with pytest.raises(ValueError):
        PackageEntry.from_domain(doc_item)  # no date anywhere
    doc = Document(id="doc_b", tenant_id="t1", evidence_ids=[], issue_date=date(2026, 9, 2))
    with pytest.raises(ValueError):
        PackageEntry.from_domain(doc_item, documents=[doc])  # wrong document


def test_document_only_entry_uses_the_issue_date_and_supplier():
    doc = Document(id="doc_a", tenant_id="t1", evidence_ids=[], supplier_name="Adobe", invoice_number="IE-77",
                   issue_date=date(2026, 9, 2), gross_amount=Decimal("29.99"))  # fmt: skip
    entry = PackageEntry.from_domain(closed_item("doc_a", "document"), documents=[doc])
    assert (entry.booked_on, entry.description, entry.amount) == (date(2026, 9, 2), "Adobe IE-77", None)


def test_entry_status_values():
    assert vodafone_entry().status == "closed"
    assert open_entry().status == "open"
    item = TrackedItem(id="item_c", tenant_id="t1", subject_type="transaction", subject_id="tx_c")
    item.advance(Stage.CONFLICT, actor="system", evidence_ids=["ev"])
    tx = Transaction(id="tx_c", tenant_id="t1", account_id="a", booked_on=date(2026, 9, 1),
                     amount=Decimal("-1"), counterparty="X")  # fmt: skip
    assert PackageEntry.from_domain(item, transaction=tx).status == "conflict"


def test_entries_are_validated():
    with pytest.raises(TypeError):
        PackageEntry("i", "transaction", "tx", date(2026, 9, 1), "x", Stage.CLOSED, Quality.GREEN, amount=1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build(entries=[vodafone_entry(), vodafone_entry()])
    with pytest.raises(ValueError):
        build(generated_at=datetime(2026, 10, 5, 8, 30))


# --------------------------------------------------------------------------- delivery (Day +1)


def test_delivery_moves_from_prepared_to_delivered_to_confirmed():
    pkg = build()
    d = pkg.delivery()
    assert d.state is DeliveryState.PREPARED and d.package_sha256 == pkg.sha256
    assert d.owner_line == "Ready for your accountant."
    sent = d.deliver(at=GENERATED + timedelta(minutes=5), channel=DeliveryChannel.EMAIL,
                     recipient="contabilista@example.pt", evidence_id="ev_sent_mail")  # fmt: skip
    assert sent.state is DeliveryState.DELIVERED and sent.owner_line == "Sent to your accountant."
    assert not sent.confirmed
    resent = sent.deliver(at=GENERATED + timedelta(hours=5), channel=DeliveryChannel.EMAIL,
                          recipient="contabilista@example.pt", evidence_id="ev_resent")  # fmt: skip
    done = resent.confirm(at=GENERATED + timedelta(days=1), evidence_id="ev_reply")
    assert done.confirmed and done.owner_line == "Your accountant has it."
    assert (done.confirmed_by, done.confirmation_evidence_id) == ("accountant", "ev_reply")
    assert d.state is DeliveryState.PREPARED  # records are immutable


def test_delivery_refuses_impossible_steps():
    d = build().delivery()
    with pytest.raises(DeliveryError):
        d.confirm(at=GENERATED, evidence_id="ev")  # not sent yet
    with pytest.raises(DeliveryError):
        d.deliver(at=GENERATED - timedelta(seconds=1), channel=DeliveryChannel.EMAIL, recipient="a@b.pt",
                  evidence_id="ev")  # fmt: skip
    with pytest.raises(DeliveryError):
        d.deliver(at=GENERATED, channel=DeliveryChannel.EMAIL, recipient="a@b.pt", evidence_id=" ")
    with pytest.raises(DeliveryError):
        d.deliver(at=GENERATED, channel=DeliveryChannel.EMAIL, recipient=" ", evidence_id="ev")
    sent = d.deliver(at=GENERATED, channel=DeliveryChannel.EMAIL, recipient="a@b.pt", evidence_id="ev")
    with pytest.raises(DeliveryError):
        sent.confirm(at=GENERATED - timedelta(minutes=1), evidence_id="ev_r")
    done = sent.confirm(at=GENERATED, evidence_id="ev_r")
    with pytest.raises(DeliveryError):
        done.deliver(at=GENERATED, channel=DeliveryChannel.EMAIL, recipient="a@b.pt", evidence_id="ev")
    with pytest.raises(DeliveryError):
        done.confirm(at=GENERATED, evidence_id="ev_r2")
    with pytest.raises(DeliveryError):
        PackageDelivery(entity_id="e", month=SEPT, package_sha256="short", prepared_at=GENERATED)
