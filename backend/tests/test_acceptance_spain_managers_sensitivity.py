"""Separate jurisdictions and a Spain pack, outlet managers, and sensitive documents.

Checklist O6, P1, P2 (cases 49, 50: "different jurisdictions"; the plan names Spain next): every company
carries its country and every country-specific step (tax number, VAT rates, fiscal documents, text labels,
letters) goes through that country's pack; a Spanish invoice is domestic for a Spanish company and foreign
for a Portuguese one, never read both ways.
Checklist X37 (case 48, franchise): a manager of one or more outlets sees and answers only their outlets'
questions, documents, payments and spending, sends receipts for them, and nothing else.
Checklist X32 (cases 21 law firm, 46 pharmacy): sensitive documents never reach an external AI, appear in
the chat only as a summary line, are hidden from employees and managers, and every read of one is logged.
"""

from __future__ import annotations

import ast
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
from _server_support import NIF_A, NIF_B, PASSWORD, b64, bearer, build_business, harness, signup
from test_acceptance_accountant import pg_store  # noqa: F401  (its PostgreSQL store fixture, with 0013 applied)

from backoffice.closure.obligations import detect_obligation
from backoffice.countries import FiscalQRError, TaxIdKind, UnknownCountryError, company_pack, get_pack
from backoffice.countries.base import CompanyPack, VATBucket
from backoffice.countries.es import (
    ESQRError,
    SpainPack,
    parse_es_qr,
    quarterly_vat_return,
    validate_nif,
    vat_return_due,
)
from backoffice.demo.evidence import qr_payload
from backoffice.domain.models import DocumentType, ObligationKind, Quality
from backoffice.orchestrator import TZ, BankRow
from backoffice.service import FOREIGN_VAT_FLAG, BackOfficeService

NOW = datetime(2026, 10, 2, 9, 30, tzinfo=TZ)
ES_COMPANY = "B76543214"  # CIF with a valid control digit
IBERIA = "B12345674"  # a Spanish supplier's CIF
LEROY = "503280470"  # a Portuguese supplier's NIF
PT_NIF = NIF_A  # the Portuguese company


def ok(result: tuple[int, dict[str, Any]], status: int = 200) -> dict[str, Any]:
    code, body = result
    assert code == status, body
    return body


def get(svc: BackOfficeService, path: str, status: int = 200) -> dict[str, Any]:
    return ok(svc.dispatch("GET", path, None), status)


def post(svc: BackOfficeService, path: str, body: dict[str, Any], status: int = 200) -> dict[str, Any]:
    return ok(svc.dispatch("POST", path, body), status)


def verifactu(nif: str, number: str, day: date, total: str) -> str:
    return (f"https://www2.agenciatributaria.gob.es/wlpl/TIKE-CONT/ValidarQR?nif={nif}&numserie={quote(number, safe='')}"
            f"&fecha={day:%d-%m-%Y}&importe={total}")


def es_invoice(number: str, day: date, *, customer_name: str, customer_label: str, customer_id: str,
               net: str = "100,00", vat: str = "21,00", total: str = "121,00", qr: bool = True) -> bytes:
    """A Spanish supplier's complete invoice (factura completa) with its Verifactu QR code."""
    lines = ["Suministros Iberia S.L.", f"CIF: {IBERIA}", "Calle Mayor 10, 28013 Madrid, España", "",
             "FACTURA", f"Factura nº: {number}          Fecha: {day:%d/%m/%Y}", "",
             f"Cliente: {customer_name}", f"{customer_label}: {customer_id}", "",
             "Material de oficina", f"Base imponible: {net} €", f"IVA 21%: {vat} €", f"Total: {total} €"]
    if qr:
        lines.append(f"QR: {verifactu(IBERIA, number, day, total.replace('.', '').replace(',', '.'))}")
    return ("\n".join(lines) + "\n").encode()


def two_country_business() -> BackOfficeService:
    """Laura's business: a Portuguese company and a Spanish one, each with its own accountant view."""
    svc = BackOfficeService.new_tenant("t-iberia", owner_name="Laura Reis", owner_email="laura@hazel.pt", now=NOW)
    svc.add_company("Hazel Tree", PT_NIF, "Hazel Tree Interiores, Lda.")
    out = svc.add_company("Hazel Tree Madrid", ES_COMPANY, "Hazel Tree España S.L.", country="ES")
    assert out["company"]["country"] == "ES"
    svc.set_accountant("marc@vidal.pt", "Marc Vidal")
    return svc


def upload(svc: BackOfficeService, name: str, data: bytes) -> dict[str, Any]:
    return svc.upload_evidence(name, "text/plain", data)


def only_document(svc: BackOfficeService, out: dict[str, Any]) -> Any:
    [doc] = out["documents"]
    return svc.repo.documents[doc["id"]]


# =========================================================================== 1. the Spain pack (P1, P2)


def test_spain_pack_validates_nif_nie_and_cif_with_check_characters() -> None:
    pack = company_pack("ES")
    assert isinstance(pack, SpainPack) and isinstance(pack, CompanyPack) and get_pack("es") is pack
    for raw, normalized, kind in (("12345678Z", "12345678Z", TaxIdKind.PERSON),  # DNI
                                  ("X1234567L", "X1234567L", TaxIdKind.PERSON),  # NIE
                                  ("B12345674", "B12345674", TaxIdKind.COMPANY),  # S.L., digit control
                                  ("ES B-1234567-4", "B12345674", TaxIdKind.COMPANY),
                                  ("A58818501", "A58818501", TaxIdKind.COMPANY),  # S.A.
                                  ("Q2826000H", "Q2826000H", TaxIdKind.PUBLIC_BODY),  # letter control
                                  (ES_COMPANY, ES_COMPANY, TaxIdKind.COMPANY)):
        check = pack.validate_tax_id(raw)
        assert (check.valid, check.normalized, check.kind) == (True, normalized, kind), raw
    for raw in ("12345678A", "X1234567T", "B12345675", "Q2826000J", "Q28260008"):  # wrong check characters
        check = validate_nif(raw)
        assert not check.valid and check.problem == "check_character"
        assert check.message == "That NIF doesn't look right. Please check it."
    shape = validate_nif("516123459")  # a Portuguese NIF is not a Spanish one
    assert not shape.valid and shape.message.startswith("That doesn't look like a Spanish NIF, NIE or CIF.")
    assert pack.is_private_person("12345678Z") and not pack.is_private_person(IBERIA)


def test_spain_pack_vat_rates_invoice_types_and_spanish_labels() -> None:
    pack = company_pack("ES")
    rates = {(r.bucket, r.rate) for r in pack.vat_rates(date(2026, 9, 18))}
    assert rates == {(VATBucket.NORMAL, Decimal("0.21")), (VATBucket.REDUCED, Decimal("0.10")),
                     (VATBucket.SUPER_REDUCED, Decimal("0.04")), (VATBucket.ZERO, Decimal("0"))}
    assert pack.is_plausible_vat(Decimal("100.00"), Decimal("21.00"), on=date(2026, 9, 18)) is True
    assert pack.is_plausible_vat(Decimal("100.00"), Decimal("23.00"), on=date(2026, 9, 18)) is False
    assert pack.vat_rates(date(2026, 9, 18), "ES-CN") == ()  # the Canary Islands have IGIC, not IVA
    assert pack.is_plausible_vat(Decimal("100"), Decimal("7"), region="ES-CN") is None
    # Invoice types: complete, simplified, corrective (a credit note), and what is never a tax invoice.
    assert [pack.map_document_type(c) for c in ("F1", "F2", "R1", "PF")] == [
        DocumentType.INVOICE, DocumentType.SIMPLIFIED_INVOICE, DocumentType.CREDIT_NOTE, DocumentType.PRO_FORMA]
    assert pack.document_kind("Factura rectificativa nº R-2026/4\nSustituye a F-2026/118") is DocumentType.CREDIT_NOTE
    assert pack.document_kind("FACTURA SIMPLIFICADA T-2026/991") is DocumentType.SIMPLIFIED_INVOICE
    assert pack.document_kind("Factura completa\nFactura nº F-1") is DocumentType.INVOICE
    assert pack.document_kind("Albarán 44") is DocumentType.DELIVERY_NOTE
    # Spanish labels: Factura nº, Fecha, Base imponible, IVA, Total, and the NIFs with their roles.
    text = es_invoice("F-2026/118", date(2026, 9, 18), customer_name="Hazel Tree España S.L.",
                      customer_label="CIF cliente", customer_id=ES_COMPANY, net="1.000,00", vat="210,00",
                      total="1.210,00", qr=False).decode()
    found = {o.field.value: o.value for o in pack.read_text(text, "ev_x", known_customer_tax_ids=[ES_COMPANY])
             .observations}
    assert found == {"invoice_number": "F-2026/118", "issue_date": date(2026, 9, 18),
                     "net_amount": Decimal("1000.00"), "vat_amount": Decimal("210.00"),
                     "gross_amount": Decimal("1210.00"), "supplier_tax_id": IBERIA, "customer_tax_id": ES_COMPANY}
    assert pack.lookup_term("Base imponible").concept == "net_amount"


def test_verifactu_and_ticketbai_qr_codes_are_read_as_one_independent_source() -> None:
    pack = company_pack("ES")
    url = verifactu(IBERIA, "F-2026/118", date(2026, 9, 18), "121.00")
    assert pack.find_fiscal_qr(f"QR: {url}") == url and pack.find_fiscal_qr("https://example.com/?nif=1") is None
    result = pack.parse_fiscal_qr(url, "ev_1")
    assert result is not None and result.country == "ES" and result.native_doc_type == "" and result.usable
    assert {(o.field.value, o.value) for o in result.observations} == {
        ("supplier_tax_id", IBERIA), ("invoice_number", "F-2026/118"), ("issue_date", date(2026, 9, 18)),
        ("gross_amount", Decimal("121.00"))}
    assert all(o.method.value == "qr" and o.location.startswith("qr:verifactu") for o in result.observations)
    assert result.issuer_tax_id == IBERIA and result.currency == "EUR"
    assert parse_es_qr(url.replace("ValidarQR", "ValidarQRNoVerifactu")).notes == ("verifactu:not_verifiable",)
    tbai = parse_es_qr("https://batuz.eus/QRTBAI/?id=TBAI-B12345674-180926-btFpwP8dcLGAF-237&s=T86&nf=270"
                       "&i=4.70&cr=007")
    assert (tbai.system, tbai.issuer_nif, tbai.number, tbai.issue_date, tbai.total) == (
        "ticketbai", IBERIA, "T86-270", date(2026, 9, 18), Decimal("4.70"))
    for bad in (url.replace("fecha=18-09-2026", "fecha=31-02-2026"), url.replace(f"nif={IBERIA}", "nif=B12345675"),
                url.replace("importe=121.00", "importe=abc"), url.replace("&numserie=F-2026%2F118", "")):
        with pytest.raises(ESQRError) as err:
            parse_es_qr(bad)
        assert isinstance(err.value, FiscalQRError)
    assert pack.parse_fiscal_qr("A:509442013*B:999999990", "ev") is None  # Portugal's code is not Spain's


def test_the_core_goes_through_each_companys_pack() -> None:
    source = (Path(__file__).resolve().parents[1] / "src" / "backoffice" / "orchestrator.py").read_text()
    imports = [n for n in ast.walk(ast.parse(source)) if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = [n.module or "" for n in imports if isinstance(n, ast.ImportFrom)] + \
        [a.name for n in imports if isinstance(n, ast.Import) for a in n.names]
    assert not [m for m in names if m.startswith("backoffice.countries.pt") or m.startswith("backoffice.countries.es")]
    svc = two_country_business()
    assert {c.id: c.country for c in svc.repo.companies.values()} == {"hazel-tree": "PT", "hazel-tree-madrid": "ES"}
    assert [c["country"] for c in get(svc, "/api/companies")["companies"]] == ["PT", "ES"]
    with pytest.raises(UnknownCountryError):
        svc.repo.add_company(id="x", name="X", legal_name="X", tax_id="", country="FR")
    assert svc.dispatch("POST", "/api/sources", {"kind": "supplier", "name": "Iberia", "taxId": IBERIA})[0] == 200
    # Each country's tax number is checked by its own pack.
    with pytest.raises(Exception) as err:
        svc.add_company("Otra", "B12345675", country="ES")
    assert getattr(err.value, "message", "") == "That NIF doesn't look right. Please check it."
    with pytest.raises(Exception) as err:
        svc.add_company("Outra", IBERIA)  # a Spanish CIF is not a Portuguese NIF
    assert getattr(err.value, "status", 0) == 400
    with pytest.raises(Exception) as err:
        svc.add_company("Société", "123", country="FR")
    assert getattr(err.value, "message", "") == \
        "I can't set up a company in that country yet. Portugal and Spain are supported."
    # A Portuguese business reads exactly as before: the demo is Portuguese throughout.
    demo = BackOfficeService.demo()
    assert {c.country for c in demo.repo.companies.values()} == {"PT"}
    assert all(d.country == "PT" and not (d.issuer and d.issuer.home != "PT") for d in demo.repo.documents.values())


def test_a_spanish_invoice_is_domestic_for_the_spanish_company_and_foreign_for_the_portuguese_one() -> None:
    svc = two_country_business()
    day = date(2026, 9, 18)
    # To the Spanish company: read by the Spain pack (Spanish labels and the Verifactu code), domestic.
    es = only_document(svc, upload(svc, "factura-es.txt", es_invoice(
        "F-2026/118", day, customer_name="Hazel Tree España S.L.", customer_label="CIF cliente",
        customer_id=ES_COMPANY)))
    assert (es.country, es.document.entity_id, es.is_foreign) == ("ES", "hazel-tree-madrid", False)
    assert es.issuer is not None and (es.issuer.country, es.issuer.home) == ("ES", "ES")
    methods = {o.method.value for obs in es.observations.values() for o in obs}
    assert methods == {"qr", "ocr"} or methods == {"qr", "embedded_text"}
    assert (es.document.invoice_number, es.document.gross_amount, es.document.vat_amount, es.document.supplier_tax_id,
            es.document.customer_tax_id) == ("F-2026/118", Decimal("121.00"), Decimal("21.00"), IBERIA, ES_COMPANY)
    # The code and the printed text agree on the issuer, number, date and total (two sources each); the code
    # has no VAT breakdown, so the net and the VAT (21%, a Spanish rate) rest on the text alone: likely, not
    # verified (§57: never upgraded to look better).
    assert {n for n, c in es.checks.items() if c.quality is Quality.GREEN} >= {
        "invoice_number", "supplier_tax_id", "gross_amount", "issue_date"}
    assert es.document.quality is Quality.AMBER
    assert es.reasons == ("I still need to confirm the amount before VAT and the VAT.",)
    # The same supplier's invoice to the Portuguese company: foreign (countries/foreign.py), never mixed.
    pt = only_document(svc, upload(svc, "factura-pt.txt", es_invoice(
        "F-2026/119", day, customer_name="Hazel Tree Interiores, Lda.", customer_label="NIF cliente",
        customer_id=f"PT{PT_NIF}")))
    assert (pt.country, pt.document.entity_id, pt.is_foreign) == ("PT", "hazel-tree", True)
    assert pt.issuer is not None and (pt.issuer.country, pt.issuer.home) == ("ES", "PT")
    qr_read = [o for obs in pt.observations.values() for o in obs if o.method.value == "qr"]
    assert qr_read == []  # Spain's code is Spain's: it named the issuer's country and was not read as Portugal's
    assert pt.document.supplier_tax_id == f"ES{IBERIA}" and pt.document.gross_amount == Decimal("121.00")
    # The accountant view is each company's own country's.
    es_view = get(svc, "/api/accountant/clients/hazel-tree-madrid")
    pt_view = get(svc, "/api/accountant/clients/hazel-tree")
    assert (es_view["country"], es_view["countryName"], pt_view["country"], pt_view["countryName"]) == (
        "ES", "Spain", "PT", "Portugal")
    assert not [f for f in es_view["taxFlags"] if "Foreign" in f["title"]]  # domestic in Spain
    [flag] = [f for f in pt_view["taxFlags"] if f["title"] == FOREIGN_VAT_FLAG]
    assert "Suministros Iberia" in flag["detail"] and "(Spain) charged €21.00 of VAT" in flag["detail"]
    # A Portuguese supplier's invoice (with Portugal's fiscal QR code) to the Spanish company is foreign there,
    # and its VAT is not deductible in Spain.
    qr = qr_payload(A=LEROY, B=ES_COMPANY, C="ES", D="FT", E="N", F="20260920", G="FT LM2026/77", H="CSDF7T5H-77",
                    I1="PT", I7="100.00", I8="23.00", N="23.00", O="123.00", Q="e1Dk", R="1422")
    text = "\n".join(["Leroy Merlin Portugal", f"NIF: {LEROY}", "Fatura n.º FT LM2026/77", "Fecha: 20/09/2026",
                      "Cliente: Hazel Tree España S.L.", f"NIF cliente: ES{ES_COMPANY}", "Base: 100,00",
                      "IVA 23%: 23,00 €", "Total: 123,00 €", f"Código QR: {qr}", ""]).encode()
    lm = only_document(svc, upload(svc, "leroy.txt", text))
    assert (lm.country, lm.document.entity_id, lm.is_foreign, lm.issuer.country) == (
        "ES", "hazel-tree-madrid", True, "PT")
    assert not [o for obs in lm.observations.values() for o in obs if o.method.value == "qr"]
    es_flags = get(svc, "/api/accountant/clients/hazel-tree-madrid")["taxFlags"]
    assert [f["title"] for f in es_flags] == [
        "Foreign VAT charged — may be reclaimable abroad, not deductible in Spain"]


def test_spanish_letters_are_read_with_the_spanish_wording() -> None:
    letter = ("Tesorería General de la Seguridad Social\nNotificación de deuda\n"
              f"Empresa: Hazel Tree España S.L. CIF {ES_COMPANY}\n"
              "Importe a ingresar: 350,00 €\nPlazo de pago: hasta el 30/10/2026\n")
    svc = two_country_business()
    spanish = detect_obligation(letter, tenant_id="t", received_on=date(2026, 10, 2),
                                entities=svc.repo.entities, vocabulary=svc.orchestrator.obligations.vocabulary())
    assert spanish is not None and (spanish.title, spanish.kind, spanish.amount, spanish.due_on, spanish.entity_id) == (
        "Social Security payment", ObligationKind.TAX_DEADLINE, Decimal("350.00"), date(2026, 10, 30),
        "hazel-tree-madrid")
    # A business without a Spanish company reads with the core's English and Portuguese only.
    assert company_pack("PT").obligation_vocabulary() == {}
    without = detect_obligation(letter, tenant_id="t", received_on=date(2026, 10, 2), entities=svc.repo.entities)
    assert without is None or without.title != "Social Security payment"
    # Through the engine: the letter becomes the Spanish company's deadline.
    report = svc.orchestrator.ingest_file(letter.encode(), filename="tgss.txt", content_type="text/plain",
                                          origin="upload")
    [oid] = report.obligation_ids
    ob = svc.repo.obligations[oid]
    assert (ob.obligation.entity_id, ob.title, ob.obligation.amount) == (
        "hazel-tree-madrid", "Social Security payment", Decimal("350.00"))


def test_the_quarterly_vat_return_modelo_303_is_a_deadline_for_spanish_companies_only() -> None:
    assert [vat_return_due(2026, q) for q in (1, 2, 3, 4)] == [
        date(2026, 4, 20), date(2026, 7, 20), date(2026, 10, 20), date(2027, 2, 1)]  # 30 Jan 2027 is a Saturday
    assert vat_return_due(2026, 6 // 3) == date(2026, 7, 20)
    assert quarterly_vat_return("c", date(2026, 10, 21)) == ()  # its window has passed: not raised late
    [q4] = quarterly_vat_return("c", date(2027, 1, 5))
    assert (q4.period, q4.due_on, q4.kind) == ("2026-Q4", date(2027, 2, 1), "vat_return")
    svc = two_country_business()
    vat = [o for o in svc.repo.obligations.values() if o.obligation.kind is ObligationKind.VAT_RETURN]
    assert len(vat) == 1
    [ob] = vat
    assert (ob.obligation.entity_id, ob.title, ob.obligation.due_on, ob.obligation.responsible, ob.proof.value) == (
        "hazel-tree-madrid", "Quarterly VAT return (modelo 303)", date(2026, 10, 20), "accountant", "submission")
    [item] = [o for o in get(svc, "/api/obligations")["items"] if o["kind"] == "vat_return"]
    assert (item["companyName"], item["due"], item["responsible"], item["status"]) == (
        "Hazel Tree Madrid", "2026-10-20", "Your accountant", "open")
    assert item["why"][:2] == ["VAT for July to September 2026", "Filed every quarter in Spain"]
    svc.orchestrator.run()
    assert len([o for o in svc.repo.obligations.values() if o.obligation.kind is ObligationKind.VAT_RETURN]) == 1
    # A reminder letter about the same return is the same deadline, read with the Spanish wording.
    reminder = ("Agencia Tributaria\nLe recordamos la presentación del modelo 303 del tercer trimestre.\n"
                f"NIF: {ES_COMPANY}\nPlazo de presentación: hasta el 20/10/2026\n")
    report = svc.orchestrator.ingest_file(reminder.encode(), filename="aviso.txt", content_type="text/plain",
                                          origin="upload")
    assert report.obligation_ids == [ob.obligation.id]
    assert len([o for o in svc.repo.obligations.values() if o.obligation.kind is ObligationKind.VAT_RETURN]) == 1
    # The tax agency's filing receipt closes it.
    receipt = ("Agencia Tributaria\nJustificante de presentación\nModelo 303 - IVA, tercer trimestre 2026\n"
               f"NIF: {ES_COMPANY} Hazel Tree España S.L.\nDeclaración presentada correctamente el 15/10/2026.\n")
    svc.orchestrator.ingest_file(receipt.encode(), filename="303.txt", content_type="text/plain", origin="upload")
    assert ob.done and ob.how
    # A Portuguese-only business never gets one.
    pt = BackOfficeService.new_tenant("t-pt", owner_name="Ana", owner_email="ana@x.pt", now=NOW)
    pt.add_company("Padaria", NIF_A)
    assert not [o for o in pt.repo.obligations.values() if o.obligation.kind is ObligationKind.VAT_RETURN]


def test_production_onboarding_adds_a_spanish_company_and_replays_it(tmp_path: Path) -> None:
    h = harness(tmp_path)
    ana = signup(h.client)
    A = bearer(ana["token"])
    res = h.client.post("/api/onboarding/company", json={"name": "Padaria Madrid", "taxId": "ES B-7654321-4",
                                                         "country": "ES"}, headers=A)
    assert res.status_code == 200, res.text
    assert res.json()["company"]["country"] == "ES" and res.json()["company"]["taxId"] == ES_COMPANY
    bad = h.client.post("/api/onboarding/company", json={"name": "Otra", "taxId": "B12345675", "country": "ES"},
                        headers=A)
    assert (bad.status_code, bad.json()["message"]) == (400, "That NIF doesn't look right. Please check it.")
    missing = h.client.post("/api/onboarding/company", json={"name": "Otra", "country": "ES"}, headers=A)
    assert missing.json()["message"] == "I need the company's NIF or CIF. It has 9 characters."
    before = h.client.get("/api/companies", headers=A).json()
    assert [(c["id"], c["country"]) for c in before["companies"]] == [("padaria-lda", "PT"), ("padaria-madrid", "ES")]
    h.manager.evict(ana["tenant"]["id"])  # rebuilt from its events: the same companies, in the same countries
    assert h.client.get("/api/companies", headers=A).json() == before
    # A Portuguese company's event is recorded as before (no country), so older logs replay unchanged.
    kinds = [json.loads(e.body) for e in h.store.events(ana["tenant"]["id"])]
    added = [e["data"] for e in kinds if e["kind"] == "company.added"]
    assert "country" not in added[0] and added[1]["country"] == "ES"


# =========================================================================== 2. outlet managers (X37, case 48)


def payslip(name: str = "Ana Maria Costa", net: str = "1.012,50", employer_nif: str = NIF_A) -> bytes:
    """A Portuguese payslip ("recibo de vencimento"): always sensitive (pay and staff information)."""
    return ("\n".join(["Recibo de Vencimento", "Entidade patronal: Padaria Lda", f"NIF: {employer_nif}",
                       f"Trabalhador: {name} - NIF 234567890", "IBAN: PT50 0002 0123 1234 5678 9015 4",
                       "Período: setembro de 2026", "Data: 30/09/2026", "Vencimento base: 1.250,00",
                       "Total ilíquido: 1.250,00 €", "Segurança Social (11%): 137,50 €", "Retenção IRS: 100,00 €",
                       "Total de descontos: 237,50 €", f"Líquido a receber: {net} €"]) + "\n").encode()


def prescription() -> bytes:
    """A pharmacy's copy of a prescription with the patient's details: medical, sensitive."""
    return ("Farmácia Central\nNIF: 516722344\nReceita médica n.º 1234567\nNome do utente: Maria Silva\n"
            "Número de utente: 123456789\nDiagnóstico: hipertensão\nData: 18/09/2026\nTotal: 12,40 €\n").encode()


def case_file() -> bytes:
    """A barrister's fee invoice to a law firm for a client's court case: a legal matter, sensitive."""
    return ("Sofia Mendes, Advogada\nNIF: 516722344\nFatura n.º FT SM2026/31\nData de emissão: 18/09/2026\n"
            f"Cliente: Padaria Lda\nNIF: {NIF_A}\nHonorários - Processo n.º 1234/26.0T8LSB, Tribunal Judicial da "
            "Comarca de Lisboa\nAutor: Maria Silva\nBase tributável (23%): 500,00\nIVA 23%: 115,00\n"
            "Total: 615,00 €\n").encode()


class Outlets:
    """Padaria Lda runs two outlets (Loja Baixa, card 2291; Loja Porto, card 7788); Studio Two is another
    company of the same owner. Each outlet has a card payment with its invoice; Padaria has a payslip and a
    payment to Leroy Merlin from its bank account that no outlet claims yet."""

    def __init__(self) -> None:
        from test_acceptance_cost_centers import GALP, LEROY as CC_LEROY, invoice

        self.svc = svc = BackOfficeService.new_tenant("t-outlets", owner_name="Ana Silva", owner_email="ana@padaria.pt",
                                                      now=NOW)
        svc.add_company("Padaria", NIF_A)
        svc.add_company("Studio Two", NIF_B)
        self.bank = post(svc, "/api/sources", {"kind": "bank", "bank": "Millennium BCP", "companyId": "padaria",
                                               "iban": "PT50000201231234567890154"})["id"]
        self.bank_b = post(svc, "/api/sources", {"kind": "bank", "bank": "Caixa", "companyId": "studio-two",
                                                 "iban": "PT50003504120005678123007"})["id"]
        self.cards = {c: post(svc, "/api/sources", {"kind": "card", "bank": "Millennium BCP", "companyId": "padaria",
                                                    "last4": c})["id"] for c in ("2291", "7788")}
        for name, tax_id in (("Leroy Merlin", CC_LEROY), ("Galp", GALP)):
            post(svc, "/api/sources", {"kind": "supplier", "name": name, "taxId": tax_id})
        self.baixa = post(svc, "/api/companies/padaria/cost-centers", {
            "name": "Loja Baixa", "kind": "Outlet", "identifiers": {"cards": ["2291"]}})["costCenter"]["id"]
        self.porto = post(svc, "/api/companies/padaria/cost-centers", {
            "name": "Loja Porto", "kind": "Outlet", "identifiers": {"cards": ["7788"]}})["costCenter"]["id"]
        self.tx_baixa = self.pay("2026-09-10", "-123.00", "LEROY MERLIN", card="2291")
        self.tx_porto = self.pay("2026-09-11", "-61.50", "GALP", card="7788")
        self.tx_hq = self.pay("2026-09-12", "-250.00", "LEROY MERLIN SA", account=self.bank)
        self.tx_b = self.pay("2026-09-13", "-40.00", "PAPELARIA", account=self.bank_b)
        self.doc_baixa = upload(svc, "leroy.txt", invoice("Leroy Merlin", CC_LEROY, "FT LM2026/10", date(2026, 9, 10),
                                                          "100.00", "23.00"))["documents"][0]["id"]
        self.doc_porto = upload(svc, "galp.txt", invoice("Galp", GALP, "FT GP2026/5", date(2026, 9, 11), "50.00",
                                                         "11.50"))["documents"][0]["id"]
        self.payslip = upload(svc, "recibo.txt", payslip())["documents"][0]["id"]

    def pay(self, day: str, amount: str, who: str, *, card: str | None = None, account: str | None = None) -> str:
        from backoffice.domain.models import TransactionKind

        row = BankRow(bank_id=f"b-{who}-{day}", account_id=account or self.cards[card or ""],
                      booked_on=date.fromisoformat(day), amount=Decimal(amount), counterparty=who, description="COMPRA",
                      kind=TransactionKind.CARD if card else TransactionKind.TRANSFER_OUT, card_last4=card)
        return self.svc.orchestrator.ingest_bank([row]).transaction_ids[0]

    def as_manager(self, method: str, path: str, body: Any = None, *, outlets: tuple[str, ...] | None = None,
                   status: int | None = 200) -> dict[str, Any]:
        code, out = self.svc.dispatch_manager(method, path, body, outlets or (self.baixa,))
        if status is not None:
            assert code == status, (path, code, out)
        return out


def conflicting_receipt(number: str) -> bytes:
    """A Galp receipt whose QR code and printed total disagree: one question for whoever may answer it."""
    from test_acceptance_cost_centers import GALP, invoice

    return invoice("Galp", GALP, number, date(2026, 9, 14), "20.00", "4.60").replace(b"Total: 24,60", b"Total: 24,90")


def test_a_manager_sees_and_answers_only_their_outlets_questions_documents_payments_and_spending() -> None:
    biz = Outlets()
    svc = biz.svc
    assert svc.repo.transactions[biz.tx_baixa].tx.cost_allocation.shares[0].cost_center_id == biz.baixa
    # Their outlet's spending, payments and documents; nothing of the other outlet, company or payslip.
    outlets = biz.as_manager("GET", "/api/manager/outlets")
    assert [(o["name"], o["spent"]) for o in outlets["outlets"]] == [("Loja Baixa", 123)]
    docs = biz.as_manager("GET", "/api/documents")["items"]
    assert [d["id"] for d in docs] == [biz.doc_baixa]
    assert biz.as_manager("GET", f"/api/documents/{biz.doc_baixa}")["id"] == biz.doc_baixa
    assert biz.as_manager("GET", f"/api/documents/{biz.doc_baixa}/file")["data"]
    assert biz.as_manager("GET", f"/api/transactions/{biz.tx_baixa}")["amount"] == 123
    detail = biz.as_manager("GET", f"/api/cost-centers/{biz.baixa}")
    assert biz.tx_baixa in json.dumps(detail) and biz.as_manager("GET", f"/api/cost-centers/{biz.baixa}/statement")
    for path in (f"/api/documents/{biz.doc_porto}", f"/api/documents/{biz.doc_porto}/file",
                 f"/api/documents/{biz.payslip}", f"/api/documents/{biz.payslip}/file",
                 f"/api/transactions/{biz.tx_porto}", f"/api/transactions/{biz.tx_hq}", f"/api/transactions/{biz.tx_b}",
                 f"/api/cost-centers/{biz.porto}", f"/api/cost-centers/{biz.porto}/statement"):
        assert biz.as_manager("GET", path, status=None) is not None
        assert svc.dispatch_manager("GET", path, None, (biz.baixa,))[0] == 404, path
    # The owner's question about the bank payment no outlet claims is not theirs.
    owner_questions = {n["id"] for n in get(svc, "/api/needs-you")["items"]}
    assert owner_questions and biz.as_manager("GET", "/api/needs-you")["items"] == []
    # A receipt they send for their outlet is theirs; its question (the code and the total disagree) too.
    sent = biz.as_manager("POST", "/api/manager/receipts", {"filename": "galp.txt", "contentType": "text/plain",
                                                             "dataBase64": b64(conflicting_receipt("FT GP2026/9"))})
    [doc] = sent["documents"]
    assert svc.repo.documents[doc["id"]].uploaded_for == biz.baixa
    [question] = biz.as_manager("GET", "/api/needs-you")["items"]
    assert question["id"] in {n["id"] for n in get(svc, "/api/needs-you")["items"]}
    assert biz.as_manager("GET", "/api/needs-you", outlets=(biz.porto,))["items"] == []  # not the other outlet's
    code, _ = svc.dispatch_manager("POST", f"/api/needs-you/{question['id']}/answer",
                                   {"optionId": question["options"][0]["id"]}, (biz.porto,))
    assert code == 404
    other = next(iter(owner_questions))
    assert svc.dispatch_manager("POST", f"/api/needs-you/{other}/answer", {"optionId": "x"}, (biz.baixa,))[0] == 404
    answered = biz.as_manager("POST", f"/api/needs-you/{question['id']}/answer",
                              {"optionId": question["options"][0]["id"]})
    assert answered["ok"] and biz.as_manager("GET", "/api/needs-you")["items"] == []
    assert doc["id"] in [d["id"] for d in biz.as_manager("GET", "/api/documents")["items"]]
    assert doc["id"] not in [d["id"] for d in biz.as_manager("GET", "/api/documents", outlets=(biz.porto,))["items"]]
    # Everything else is refused: other companies, bank connections, settings, the owner's routes.
    for method, path in (("GET", "/api/companies"), ("GET", "/api/sources"), ("GET", "/api/connections"),
                         ("GET", "/api/settings/report"), ("GET", "/api/home"), ("GET", "/api/documents/access-log"),
                         ("POST", "/api/sources"), ("POST", "/api/cost-centers/allocate"),
                         ("POST", f"/api/documents/{doc['id']}/sensitive"), ("GET", "/api/companies/padaria")):
        assert svc.dispatch_manager(method, path, {}, (biz.baixa,))[0] in (403, 404), path  # refused, or no such doc
    # A scope spanning two companies' outlets is none at all; an unknown outlet is nothing.
    from backoffice.managers import ManagerViews

    studio = post(svc, "/api/companies/studio-two/cost-centers", {"name": "Estúdio", "kind": "Outlet"})["costCenter"]
    assert ManagerViews(svc, (biz.baixa, studio["id"])).centers == {}
    assert biz.as_manager("GET", "/api/documents", outlets=(biz.baixa, studio["id"]))["items"] == []
    assert biz.as_manager("GET", "/api/documents", outlets=("cc-nope",))["items"] == []


def _manager_business(h: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Ana's production business (build_business) with a second outlet, a payslip and a medical document."""
    ana = signup(h.client)
    seen = build_business(h, ana["token"])
    H = bearer(ana["token"])
    porto = h.client.post("/api/companies/padaria-lda/cost-centers", json={
        "name": "Loja Porto", "kind": "Outlet", "identifiers": {"cards": ["7788"]}}, headers=H).json()["costCenter"]
    card = h.client.post("/api/sources", json={"kind": "card", "bank": "Millennium BCP", "companyId": "padaria-lda",
                                                "last4": "7788"}, headers=H).json()["id"]
    csv = (f"date,amount,counterparty,account,description,kind,card\n"
           f"2026-09-24,-48.20,GALP PORTO,{card},COMPRA,card,7788\n").encode()
    assert h.client.post("/api/evidence", files={"file": ("porto.csv", csv, "text/csv")}, headers=H).status_code == 200
    extra: dict[str, Any] = {"porto": porto["id"]}
    for name, data in (("recibo.txt", payslip()), ("receita.txt", prescription())):
        out = h.client.post("/api/evidence", json={"filename": name, "contentType": "text/plain",
                                                   "dataBase64": b64(data)}, headers=H).json()
        extra[name] = out["documents"][0]["id"]
    return ana, seen, extra


def _login(h: Any, email: str) -> dict[str, str]:
    res = h.client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return bearer(res.json()["token"])


def _member(h: Any, tenant: str, email: str, role: str, **scope: Any) -> dict[str, str]:
    person = signup(h.client, email, company=f"{email} Lda", tax_id=None, name=email.split("@")[0].title())
    h.store._d.memberships.discard((person["tenant"]["id"], person["user"]["id"], "owner"))
    h.store.add_membership(tenant, person["user"]["id"], role, **scope)
    return _login(h, email)


def test_a_manager_cannot_read_another_outlet_company_or_setting_through_any_get_route(tmp_path: Path) -> None:
    h = harness(tmp_path)
    ana, seen, extra = _manager_business(h)
    H, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    baixa = seen["cost_centers"][0]
    R = _member(h, tenant, "rui@padaria.pt", "manager", companies=["padaria-lda"], cost_centers=[baixa])
    me = h.client.get("/api/auth/me", headers=R).json()
    assert (me["role"], me["costCenters"]) == ("manager", [baixa])
    # What Rui may see: Loja da Baixa's payment (the Adobe card payment) and spending.
    outlets = h.client.get("/api/manager/outlets", headers=R).json()["outlets"]
    assert [(o["name"], o["spent"]) for o in outlets] == [("Loja da Baixa", 59.99)]
    a_view = h.client.get("/api/accountant/clients/padaria-lda", headers=H).json()
    b_view = h.client.get("/api/accountant/clients/second-company", headers=H).json()
    theirs = {r["id"] for r in a_view["reconciliation"] if r["payee"] == "Adobe"}
    [adobe] = theirs
    assert h.client.get(f"/api/transactions/{adobe}", headers=R).json()["counterparty"] == "Adobe"
    assert adobe in h.client.get(f"/api/cost-centers/{baixa}", headers=R).text
    # Everything that identifies another outlet, the other company, the owner's settings or a sensitive document.
    marks = {"Second Company", "second-company", "Loja Porto", extra["porto"], "EDP", "Marc Vidal", "marc@vidal.pt",
             "PT50000201231234567890154", "Fidelidade", "TOConline", "Ana Maria Costa", "Maria Silva",
             extra["recibo.txt"], extra["receita.txt"], *seen["documents"],
             *({r["id"] for r in a_view["reconciliation"]} - theirs), *(r["id"] for r in b_view["reconciliation"]),
             *(e["id"] for e in b_view["evidenceLinks"])}
    report = h.client.post("/api/chat/tool", json={"name": "period_report", "input": {
        "date_from": "2026-09-01", "date_to": "2026-09-30"}}, headers=H).json()["result"]
    a, ev = "padaria-lda", a_view["evidenceLinks"][0]["id"]
    paths = ["/api/home", "/api/needs-you", "/api/activity", "/api/companies", f"/api/companies/{a}",
             f"/api/months/{a}/2026-09", "/api/sources", "/api/chat/tools", "/api/tasks",
             f"/api/reports/{report['id']}/file", "/api/documents", f"/api/documents?company={a}",
             "/api/documents?q=edp", *(f"/api/documents/{d}/file" for d in (*seen["documents"], extra["recibo.txt"])),
             *(f"/api/documents/{d}" for d in (*seen["documents"], extra["recibo.txt"], extra["receita.txt"])),
             *(f"/api/transactions/{t}" for t in seen["transactions"]), "/api/obligations",
             f"/api/companies/{a}/cost-centers", f"/api/cost-centers/{extra['porto']}",
             f"/api/cost-centers/{extra['porto']}/statement", f"/api/cost-centers/{baixa}",
             f"/api/cost-centers/{baixa}/statement?month=2026-09", "/api/settings/report", "/api/settings/accountant",
             "/api/settings/mailboxes", "/api/onboarding", "/api/accountant/api-keys", "/api/connections",
             "/api/accountant/clients", f"/api/accountant/clients/{a}", f"/api/accountant/clients/{a}/export",
             f"/api/accountant/clients/{a}/evidence/{ev}/file", f"/api/evidence/{ev}/file",
             "/api/accountant/invitations", "/api/audit", "/api/pipeline", "/api/internal/overview",
             "/api/internal/operations", "/api/internal/readiness", f"/api/accountant/clients/{tenant}~{a}",
             "/api/employees", "/api/expense-claims", "/api/employee/card-payments", "/api/documents/access-log",
             "/api/manager/outlets", "/api/account/export", "/healthz"]
    for p in paths:
        res = h.client.get(p, headers=R)
        assert res.status_code in (200, 403, 404), (p, res.status_code, res.text)
        if res.status_code == 200:
            leaked = [m for m in marks if m in res.text]
            assert not leaked, (p, leaked)
    svc = BackOfficeService.new_tenant("t-routes", owner_name="x", owner_email="x@example.pt", now=h.clock.now_)
    called = [p.split("?", 1)[0] for p in paths]
    for verb, pattern, _ in svc._routes():
        if verb == "GET":
            assert any(pattern.fullmatch(p) for p in called), pattern.pattern
    # ... and nothing they may not do through a POST.
    for p, body in (("/api/sources", {"kind": "bank", "bank": "X", "companyId": a}), ("/api/settings/report", {}),
                    ("/api/cost-centers/allocate", {"subjectId": seen["transactions"][0], "general": True}),
                    (f"/api/documents/{seen['documents'][0]}/sensitive", {"sensitive": False}), ("/api/ask", {}),
                    ("/api/chat", {"message": "how much did we spend?"}), ("/api/evidence", {}),
                    ("/api/documents/export", {}), ("/api/accountant/rules", {"text": "x"}),
                    ("/api/employee/receipts", {})):
        res = h.client.post(p, json=body, headers=R)
        assert res.status_code == 403, (p, res.status_code, res.text)


def test_a_manager_sends_receipts_for_their_outlet_and_the_log_replays(tmp_path: Path) -> None:
    h = harness(tmp_path)
    ana, seen, extra = _manager_business(h)
    H, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    baixa = seen["cost_centers"][0]
    R = _member(h, tenant, "rui@padaria.pt", "manager", companies=["padaria-lda"], cost_centers=[baixa])
    P = _member(h, tenant, "pedro@padaria.pt", "manager", companies=["padaria-lda"], cost_centers=[extra["porto"]])
    sent = h.client.post("/api/manager/receipts", files={"file": ("galp.txt", conflicting_receipt("FT GP2026/12"),
                                                                  "text/plain")}, headers=R)
    assert sent.status_code == 200, sent.text
    [doc] = sent.json()["documents"]
    assert doc["id"] in [d["id"] for d in h.client.get("/api/documents", headers=R).json()["items"]]
    assert doc["id"] not in [d["id"] for d in h.client.get("/api/documents", headers=P).json()["items"]]
    [question] = h.client.get("/api/needs-you", headers=R).json()["items"]
    assert h.client.get("/api/needs-you", headers=P).json()["items"] == []
    assert h.client.post(f"/api/needs-you/{question['id']}/answer", json={"optionId": question["options"][0]["id"]},
                         headers=P).status_code == 404
    res = h.client.post(f"/api/needs-you/{question['id']}/answer", json={"optionId": question["options"][0]["id"]},
                        headers=R)
    assert res.status_code == 200 and res.json()["ok"], res.text
    assert h.client.post("/api/manager/receipts", json={"filename": "x.txt", "dataBase64": b64(b"x"),
                                                        "costCenterId": extra["porto"]}, headers=R).status_code == 400
    assert h.client.post("/api/manager/receipts", json={}, headers=H).status_code == 403  # the owner has /api/receipts
    before = (h.client.get("/api/documents", headers=R).json(), h.client.get("/api/needs-you", headers=H).json())
    h.manager.evict(tenant)  # rebuilt from its events: the manager's changes replay through their outlets only
    assert (h.client.get("/api/documents", headers=R).json(), h.client.get("/api/needs-you", headers=H).json()) == \
        before
    events = [json.loads(e.body) for e in h.store.events(tenant)]
    # Each manager's request (the refused ones too) is recorded with that manager's outlets.
    scopes = [(e["data"]["manager"]["email"], e["data"]["manager"]["costCenters"]) for e in events
              if "manager" in e.get("data", {})]
    assert scopes == [("rui@padaria.pt", [baixa]), ("pedro@padaria.pt", [extra["porto"]]),
                      ("rui@padaria.pt", [baixa]), ("rui@padaria.pt", [baixa])]


# =========================================================================== 3. sensitive documents (X32, cases 21, 46)


def test_sensitive_documents_are_marked_by_their_wording_or_by_the_owner() -> None:
    biz = Outlets()
    svc = biz.svc
    [medical] = upload(svc, "receita.txt", prescription())["documents"]
    [legal] = upload(svc, "honorarios.txt", case_file())["documents"]
    records = {d.id: d for d in svc.repo.documents.values()}
    assert records[biz.payslip].sensitive == "hr" and records[biz.payslip].document.doc_type is DocumentType.PAYROLL
    assert (records[medical["id"]].sensitive, records[legal["id"]].sensitive) == ("medical", "legal")
    assert {records[i].sensitive_by for i in (biz.payslip, medical["id"], legal["id"])} == {"wording"}
    assert not records[biz.doc_baixa].sensitive and not records[biz.doc_porto].sensitive
    listed = {d["id"]: d for d in get(svc, "/api/documents")["items"]}
    assert listed[biz.payslip]["sensitive"] is True
    assert listed[biz.payslip]["sensitiveReason"] == "It has pay or staff information."
    assert "sensitive" not in listed[biz.doc_baixa]
    from backoffice.sensitivity import classify

    assert classify(case_file().decode()) == "legal" and classify(prescription().decode()) == "medical"
    assert classify("Fatura n.º FT 1\nMaterial de escritório\nTotal: 10,00 €") is None
    assert classify("Nómina de septiembre\nTrabajador: Juan") == "hr"
    # The owner marks an ordinary one sensitive, and back.
    out = post(svc, f"/api/documents/{biz.doc_porto}/sensitive", {"sensitive": True})
    assert out["message"].startswith("Done. Only you and the company's accountant can see it now")
    assert svc.repo.documents[biz.doc_porto].sensitive == "owner"
    access = get(svc, "/api/documents/access-log")
    assert {d["id"]: d["reason"] for d in access["sensitiveDocuments"]}[biz.doc_porto] == "You marked it sensitive."
    assert post(svc, f"/api/documents/{biz.doc_porto}/sensitive", {"sensitive": False})["message"] == \
        "Done. It is no longer marked sensitive."
    assert post(svc, f"/api/documents/{biz.doc_porto}/sensitive", {"sensitive": "yes"}, status=400)
    assert post(svc, "/api/documents/doc_nope/sensitive", {"sensitive": True}, status=404)
    # Every read of a sensitive original is logged (the demo-style service records at once, as the owner).
    assert get(svc, f"/api/documents/{biz.payslip}/file")["data"]
    get(svc, f"/api/documents/{biz.doc_baixa}/file")  # an ordinary document: not logged
    [entry] = get(svc, "/api/documents/access-log")["entries"]
    assert (entry["documentId"], entry["who"], entry["role"], entry["how"]) == (
        biz.payslip, "owner", "owner", "opened the original")


def test_a_sensitive_document_never_goes_to_an_external_ai_even_when_it_is_on() -> None:
    golden = pytest.importorskip("test_reading_golden")
    from backoffice.ocr import COMMERCIAL, EngineRegistry, InMemoryBudgetLedger
    from backoffice.reading import DocumentReader

    for extra, called in (("", 1), ("Receita médica n.º 99\nNome do utente: Maria Silva\n", 0)):
        requests: list[Any] = []
        registry = EngineRegistry([golden.ocr_engine(golden.PARTIAL + "\n" + extra)])
        registry.register(golden.claude(requests), name=COMMERCIAL)
        svc = golden.service(DocumentReader(registry=registry, qr_decoder=None, external_ai=True,
                                            budget=InMemoryBudgetLedger(default_ceiling=Decimal("5.00"))))
        out = golden.upload(svc, "central-fs-cc2026-3317.jpg", "image/jpeg")
        assert len(requests) == called  # external AI is on: it is asked only about what is not sensitive
        steps = {s.step: s for s in svc.repo.reads[out["evidenceIds"][0]].steps}
        if called:
            assert steps["ocr_commercial"].engine == "claude-vision"
        else:
            assert "sensitive document: kept on our servers" in steps["ocr_commercial"].detail
            assert golden.only_document(svc, out).sensitive == "medical"
    # Sensitive by its own text before any engine runs: no external engine is even routed.
    from backoffice.reading import ReadRequest

    reader = DocumentReader(registry=EngineRegistry([golden.ocr_engine()]), qr_decoder=None, external_ai=True)
    sent: list[Any] = []
    reader._registry.register(golden.claude(sent), name=COMMERCIAL)
    data = (golden.FIXTURES / "central-fs-cc2026-3317.jpg").read_bytes()
    outcome = reader.read(ReadRequest(tenant_id="t", evidence_id="ev_1", data=data, mime_type="image/jpeg",
                                      extractor=BackOfficeService.demo().orchestrator.documents.text_extractor(),
                                      sensitive=lambda text: True))
    assert sent == [] and any(s.step == "external_ai" and "kept on our servers" in s.detail for s in outcome.steps)


def test_the_chat_sees_a_sensitive_document_only_as_its_summary_line() -> None:
    from backoffice.assistant import run_tool

    biz = Outlets()
    svc = biz.svc
    cards: list[dict[str, Any]] = []
    found = run_tool(svc.assistant, "search_documents", {}, cards)
    [slip] = [d for d in found if d.get("id") == biz.payslip]
    assert slip == {"id": biz.payslip, "summary": "Staff document · 30 September 2026 · Padaria (sensitive: details "
                                                   "are not shown here)", "sensitive": True}
    text = json.dumps(found, ensure_ascii=False)
    assert "Ana Maria Costa" not in text and "1012.5" not in text and "Leroy Merlin" in text
    assert "Ana Maria Costa" in json.dumps(cards, ensure_ascii=False)  # the owner's own card keeps the details
    activity = run_tool(svc.assistant, "recent_activity", {"limit": 50}, [])
    assert "Ana Maria Costa" not in json.dumps(activity, ensure_ascii=False)
    assert any(a.get("sensitive") for a in activity)
    chat = svc.dispatch("POST", "/api/chat/tool", {"name": "search_documents", "input": {}})[1]
    assert "Ana Maria Costa" not in json.dumps(chat["result"], ensure_ascii=False)


def test_the_claude_chat_redacts_tool_results_before_they_leave() -> None:
    from backoffice.assistant import ClaudeBrain

    biz = Outlets()
    svc = biz.svc
    svc.set_accountant("marc@vidal.pt", "Marc Vidal")
    seen: list[dict[str, Any]] = []

    def block(**kw: Any) -> Any:
        return SimpleNamespace(**kw)

    class Messages:
        def create(self, **kw: Any) -> Any:
            seen.append(json.loads(json.dumps(kw, default=lambda o: o.__dict__)))
            if len(seen) == 1:
                return block(stop_reason="tool_use", content=[
                    block(type="tool_use", id="t1", name="connections_status", input={}),
                    block(type="tool_use", id="t2", name="search_documents", input={})])
            return block(stop_reason="end_turn", content=[block(type="text", text="Your accountant is [EMAIL_1].")])

    out = ClaudeBrain(svc.assistant, client=SimpleNamespace(messages=Messages())).handle("who is connected?")
    results = [c for m in seen[1]["messages"] if m["role"] == "user" and isinstance(m["content"], list)
               for c in m["content"]]
    sent = json.dumps(results, ensure_ascii=False)
    assert "marc@vidal.pt" not in sent and "[EMAIL_1]" in sent  # contact details leave as tokens
    assert "Ana Maria Costa" not in sent and "PT50000201231234567890154" not in sent
    assert "Staff document · 30 September 2026" in sent  # the payslip only as its summary line
    assert out["reply"] == "Your accountant is marc@vidal.pt."  # restored here, for the owner


def test_sensitive_documents_are_hidden_from_employees_and_managers_and_every_read_is_logged(
        tmp_path: Path) -> None:
    h = harness(tmp_path)
    ana, seen, extra = _manager_business(h)
    H, tenant = bearer(ana["token"]), ana["tenant"]["id"]
    slip, medical = extra["recibo.txt"], extra["receita.txt"]
    docs = {d["id"]: d for d in h.client.get("/api/documents", headers=H).json()["items"]}
    assert docs[slip]["sensitive"] and docs[medical]["sensitive"]
    # The company's accountant sees them and opens them: on the record, with who they are.
    C = _member(h, tenant, "carla@contas.pt", "accountant", companies=["padaria-lda"])
    assert slip in [d["id"] for d in h.client.get("/api/documents", headers=C).json()["items"]]
    assert h.client.get(f"/api/documents/{slip}/file", headers=C).status_code == 200
    evidence = docs[slip]["evidenceIds"][0]
    assert h.client.get(f"/api/accountant/clients/padaria-lda/evidence/{evidence}/file", headers=C).status_code == 200
    # An accountant of the other company only, an employee and a manager: not at all.
    B = _member(h, tenant, "bob@contas.pt", "accountant", companies=["second-company"])
    assert slip not in h.client.get("/api/documents", headers=B).text
    assert h.client.get(f"/api/documents/{slip}/file", headers=B).status_code == 404
    E_ = _member(h, tenant, "rui@padaria.pt", "employee")
    assert h.client.get(f"/api/documents/{slip}/file", headers=E_).status_code == 403
    M = _member(h, tenant, "joana@padaria.pt", "manager", companies=["padaria-lda"],
                cost_centers=[seen["cost_centers"][0]])
    assert h.client.get(f"/api/documents/{slip}/file", headers=M).status_code == 404
    assert slip not in h.client.get("/api/documents", headers=M).text
    # The owner opens one too, and the accounting software through its API key.
    assert h.client.get(f"/api/documents/{medical}/file", headers=H).status_code == 200
    key = bearer(seen["api_key"])
    assert h.client.get(f"/api/v1/documents/{slip}/file", headers=key).status_code == 200
    log = h.client.get("/api/documents/access-log", headers=H).json()
    got = sorted((e["documentId"], e["who"], e["role"]) for e in log["entries"])
    assert got == sorted([(slip, "carla@contas.pt", "accountant"), (slip, "carla@contas.pt", "accountant"),
                          (medical, "ana@example.pt", "owner"), (slip, "accounting software (API key)", "api")])
    assert not [e for e in log["entries"] if e["who"] in ("bob@contas.pt", "joana@padaria.pt", "rui@padaria.pt")]
    assert all(e["at"] and e["document"] for e in log["entries"])
    assert {d["id"] for d in log["sensitiveDocuments"]} >= {slip, medical}
    # The access log is the owner's: not the accountant's, the manager's or the employee's.
    for who in (C, B, M, E_):  # refused (a manager's /api/documents/<id> finds no such document)
        assert h.client.get("/api/documents/access-log", headers=who).status_code in (403, 404)
    # It is part of the tenant's log: rebuilt from the events, the same.
    h.manager.evict(tenant)
    assert h.client.get("/api/documents/access-log", headers=H).json() == log
    # Opening ordinary documents is not logged.
    before = len(log["entries"])
    assert h.client.get(f"/api/documents/{seen['documents'][0]}/file", headers=H).status_code == 200
    assert len(h.client.get("/api/documents/access-log", headers=H).json()["entries"]) == before


# =========================================================================== the same on PostgreSQL (0013)


def test_manager_memberships_and_the_vat_return_kind_on_postgres(tmp_path: Path, pg_store: Any) -> None:
    h = harness(tmp_path, store=pg_store)
    ana = signup(h.client, "ana.pg@example.pt")
    tenant, A = ana["tenant"]["id"], bearer(ana["token"])
    outlet = h.client.post("/api/companies/padaria-lda/cost-centers", json={"name": "Loja Baixa", "kind": "Outlet"},
                           headers=A).json()["costCenter"]["id"]
    rui = signup(h.client, "rui.pg@padaria.pt", company="Rui", tax_id=None, name="Rui")
    pg_store.add_manager(tenant, rui["user"]["id"], company="padaria-lda", cost_centers=[outlet],
                         invited_by=ana["user"]["id"])
    assert pg_store.manager_scope(tenant, rui["user"]["id"]) == ("padaria-lda", (outlet,))
    assert pg_store.manager_scope(tenant, ana["user"]["id"]) is None
    assert pg_store.membership_companies(tenant, rui["user"]["id"]) is None  # not an accountant's limit
    # The schema refuses a manager without outlets, and outlets on any other role.
    for role, companies, centers in (("manager", ["padaria-lda"], []), ("manager", [], [outlet]),
                                     ("accountant", [], [outlet])):
        with pytest.raises(Exception, match="memberships_c"):  # a check constraint of 0013
            with pg_store._tx(tenant=tenant, user=ana["user"]["id"]) as cur:
                cur.execute("INSERT INTO memberships (tenant_id, user_id, role, created_at, company_ids, "
                            "cost_center_ids) VALUES (%s, %s, %s, now(), %s, %s)",
                            (tenant, rui["user"]["id"], role, companies, centers))
    with pg_store._tx() as cur:
        cur.execute("SELECT 'vat_return'::obligation_kind, 'manager'::membership_role")
        assert cur.fetchone() == ("vat_return", "manager")
