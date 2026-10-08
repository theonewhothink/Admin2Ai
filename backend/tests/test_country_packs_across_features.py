"""Country packs across the features merged together (§49; checklist O6, P1, P2 with X4, X5, X12, X24, X26, X30,
N9, G3).

Leasing contracts, till reports and receipts lists, the monthly accountant package, copies of a photographed
invoice, and tourist tax and grant letters each go through the pack of the company's own country: a Spanish
company is never read with Portuguese rules (its working days, its tax numbers, its unique document code, its
letters), and a Portuguese company reads exactly as before.
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from _server_support import NIF_A

from backoffice._reading import tax_ids
from backoffice.captures import document_code
from backoffice.closure import Month
from backoffice.closure.obligations import detect_obligation
from backoffice.countries import company_pack
from backoffice.domain.models import ObligationKind
from backoffice.orchestrator import TZ
from backoffice.package_delivery import working_day
from backoffice.service import BackOfficeService

NOW = datetime(2026, 10, 2, 9, 30, tzinfo=TZ)
ES_COMPANY = "B76543214"  # a CIF with a valid control digit
ES_LESSOR = "A12345674"  # a Spanish leasing company's CIF (valid control digit)


def two_countries() -> tuple[BackOfficeService, str, str]:
    """A Portuguese company and a Spanish one under one owner."""
    svc = BackOfficeService.new_tenant("t-two-countries", owner_name="Laura Reis", owner_email="laura@hazel.pt",
                                       now=NOW)
    pt = svc.add_company("Hazel Tree", NIF_A, "Hazel Tree Interiores, Lda.")["company"]["id"]
    es = svc.add_company("Hazel Tree Madrid", ES_COMPANY, "Hazel Tree España S.L.", country="ES")["company"]["id"]
    return svc, pt, es


def upload(svc: BackOfficeService, text: str, filename: str) -> list[str]:
    return svc.orchestrator.ingest_file(text.encode("utf-8"), filename=filename,
                                        content_type="text/plain").document_ids


def test_the_monthly_package_goes_on_the_working_day_of_each_companys_own_country() -> None:
    svc, pt, es = two_countries()
    packages = svc.orchestrator.packages
    september = Month(2026, 9)
    assert packages.due_on(september, pt) == date(2026, 10, 6)  # 5 October is Portugal's Republic Day
    assert packages.due_on(september, es) == date(2026, 10, 5)  # an ordinary Monday in Spain
    assert working_day(2026, 10, 7) == date(2026, 10, 12)  # Portugal's holidays by default, as before
    assert working_day(2026, 10, 7, "ES") == date(2026, 10, 9)  # 12 October is Spain's national day
    portugal = company_pack("PT").public_holidays(2027)
    assert {date(2027, 3, 26), date(2027, 3, 28), date(2027, 5, 27), date(2027, 6, 10)} <= portugal
    spain = company_pack("ES").public_holidays(2027)
    assert {date(2027, 1, 6), date(2027, 3, 26), date(2027, 10, 12), date(2027, 12, 6)} <= spain
    assert date(2027, 6, 10) not in spain and date(2027, 1, 6) not in portugal


def test_an_unnumbered_copy_is_proven_the_same_only_by_its_own_countrys_document_code() -> None:
    svc, _, _ = two_countries()
    captures = svc.orchestrator.captures
    text = "Papelaria Central\nATCUD: CSDF7T5H-0035\nTotal: 12,30 €"
    assert document_code(text, "PT") == ("ATCUD", "CSDF7T5H-0035")
    assert document_code(text, "ES") is None  # Spain prints no such code: the owner is asked instead
    portuguese = SimpleNamespace(country="PT", text=text)
    assert captures.proven_copy(text, portuguese, home="PT") == "ATCUD"
    assert captures.proven_copy(text, portuguese, home="ES") is None  # a copy for another country's company
    assert captures.proven_copy(text, SimpleNamespace(country="ES", text=text), home="ES") is None


def test_spanish_tax_numbers_are_read_through_the_spanish_pack_and_portuguese_ones_as_before() -> None:
    assert tax_ids(f"CIF: B-7654321-4\nNIF cliente: 12345678Z\nES {ES_COMPANY}") == [ES_COMPANY, "12345678Z"]
    assert tax_ids("CIF: B76543215") == []  # a wrong control digit is no tax number
    assert tax_ids(f"NIF: {NIF_A} PT 501234560 GB 123 4567 89 tel. 912 345 678") == [
        NIF_A, "501234560", "GB123456789"]


def test_a_spanish_companys_leasing_contract_follows_spains_practice_and_names_the_spanish_company() -> None:
    svc, pt, es = two_countries()
    repo = svc.repo
    (doc_id,) = upload(svc, f"""Contrato de leasing
Contrato n.º L-2026-77
Locador: Iberia Leasing, S.A.
CIF: {ES_LESSOR}
Locatário: Hazel Tree España S.L.
CIF: {ES_COMPANY}
Bem locado: Renault Kangoo Van, Matrícula: 1234-KLM
Data da primeira renda: 05/03/2026
Prazo: 48 meses
Renda mensal (sem IVA): 289,26 €
IVA sobre a renda (21%): 60,74 €
Renda mensal com IVA: 350,00 €
""", "contrato.txt")
    lease = repo.leases[doc_id]
    assert lease.company_id == es and lease.contract.customer_tax_id == ES_COMPANY  # found by its CIF
    assert lease.contract.country == "ES" and not lease.contract.contract_suffices  # each cuota has its factura
    assert repo.documents[doc_id].country == "ES"
    assert repo.suppliers[lease.supplier_id].countries == ["ES"]
    assert lease.company_id != pt


def test_a_spanish_companys_till_report_is_found_by_its_cif_and_read_as_spanish() -> None:
    svc, _, es = two_countries()
    repo = svc.repo
    (z,) = upload(svc, f"Z REPORT #0412\nCIF: {ES_COMPANY}\nDate: 21/09/2026\nCash: 98.40\nCard: 311.25\n"
                       "Total: 409.65\n", "z.txt")
    assert repo.till_days[z].company_id == es and repo.documents[z].country == "ES"


def test_spanish_tourist_tax_and_grant_letters_are_read_with_the_spanish_wording() -> None:
    svc, _, es = two_countries()
    vocabulary = svc.orchestrator.obligations.vocabulary()
    entities = svc.repo.entities
    tax = detect_obligation(f"Ajuntament de Barcelona\nHazel Tree España S.L., CIF {ES_COMPANY}\n"
                            "Impuesto sobre estancias turisticas\nImporte a ingresar: 312,40 EUR\n"
                            "Plazo: hasta el 20/10/2026", tenant_id="t", received_on=NOW.date(),
                            sender="tributs@bcn.cat", entities=entities, vocabulary=vocabulary)
    assert tax is not None and tax.kind is ObligationKind.TOURIST_TAX and tax.obligation.entity_id == es
    grant = detect_obligation(f"Subvención concedida a Hazel Tree España S.L., CIF {ES_COMPANY}\n"
                              "Debe aportar la documentación justificativa antes del 30/11/2026", tenant_id="t",
                              received_on=NOW.date(), sender="ayudas@gva.es", entities=entities,
                              vocabulary=vocabulary)
    assert grant is not None and grant.kind is ObligationKind.GRANT_DOCUMENTS
    # A Portuguese letter reads as before, with or without the Spanish wording beside it.
    for words in (None, vocabulary):
        pt_tax = detect_obligation(f"Câmara Municipal de Lisboa\nHazel Tree Interiores, Lda., NIF {NIF_A}\n"
                                   "Taxa Municipal Turística de setembro\nValor a pagar: 120,00 €\n"
                                   "Data limite de pagamento: 15/10/2026", tenant_id="t", received_on=NOW.date(),
                                   sender="taxas@cm-lisboa.pt", entities=entities, vocabulary=words)
        assert pt_tax is not None and pt_tax.kind is ObligationKind.TOURIST_TAX
