"""Company details from the EU VAT register, Portugal's statutory tax calendar, French, German and Italian
invoices, and bank fees by country.

Checklist A2: the VAT number entered at onboarding is checked by its country's pack, then looked up in the EU
VAT register (VIES); the registered name and address are offered for one tap, never written over what the owner
typed; an invalid, unreachable or busy register is a plain message and never stops onboarding. The production
server asks the register before it records the event and keeps the answer in it: a replay never calls it.
Checklist P6: Portugal's statutory deadlines (VAT return and payment, monthly or quarterly; invoice report;
salaries report, tax withheld and Social Security; Modelo 22, IES, Modelo 10; advance payments) come from the
pack's calendar, by the company's regime (set by the owner or the accountant, or learned from its evidence),
each with who does it, its deadline, consequence and the proof that closes it. The rules reproduce the official
2026 calendar. Spain keeps its modelo 303.
Checklist E9: French, German and Italian invoices are read with their own labels and number formats, their VAT
numbers checked by their country's rules; they close GREEN against the bank when those rules hold, never when
their fields disagree.
Checklist J6: whether the bank statement alone covers a bank charge is each country's policy (Portugal: fees,
stamp duty and interest; Spain: commissions); an accountant's rule still wins.

The register is never reached from here (the sandbox blocks it): a fake transport answers with the register's
own response shapes. All companies, numbers and IBANs are fictional.
"""

from __future__ import annotations

import ast
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from _server_support import NIF_A, NIF_B, bearer, harness, signup
from test_acceptance_foreign_documents import OWN, card, closed, pay, tenant, upload

from backoffice.company_lookup import (
    TIMEOUT,
    VIES_API,
    CompanyLookup,
    ViesClient,
    lookup_company,
    parse_vies_response,
)
from backoffice.countries import TaxProfile, TaxSignal, company_pack
from backoffice.countries.foreign import (
    check_tax_number,
    detect_issuer,
    document_language,
    find_sirens,
    find_tax_numbers,
    french_vat_from_siren,
    read_foreign_text,
)
from backoffice.domain.models import CriticalField, ExtractionMethod, ObligationKind, Quality, TransactionKind
from backoffice.language import find_jargon, find_off_tone, identity_check_message
from backoffice.orchestrator import TZ, Account, BankRow, local_datetime
from backoffice.reconciliation import EvidenceExpectation, ExpectedEvidenceEngine
from backoffice.server.events import Event, state_digest
from backoffice.server.runtime import TenantManager
from backoffice.service import BackOfficeService

F = CriticalField
D = Decimal
K = TransactionKind
NOW = datetime(2026, 10, 8, 9, 30, tzinfo=TZ)
ES_COMPANY = "B76543214"  # a Spanish CIF with a valid control digit
SRC = Path(__file__).resolve().parents[1] / "src" / "backoffice"


def plain(*texts: str) -> None:
    for text in texts:
        assert not find_jargon(text) and not find_off_tone(text), text


# =========================================================================== 1. the EU VAT register (A2)

_APPROXIMATE = {"name": "---", "street": "---", "postalCode": "---", "city": "---", "companyType": "---",
                "matchName": 3, "matchStreet": 3, "matchPostalCode": 3, "matchCity": 3, "matchCompanyType": 3}
# What the register answers (GET .../ms/{CC}/vat/{number}), as it answers it.
VALID_PT = {"isValid": True, "requestDate": "2026-10-08T08:30:02.124Z", "userError": "VALID",
            "name": "PADARIA LUSITANA LDA", "address": "RUA DE SANTA CATARINA 112\n4000-447 PORTO",
            "requestIdentifier": "", "originalVatNumber": NIF_A, "vatNumber": NIF_A, "viesApproximate": _APPROXIMATE}
VALID_ES = {"isValid": True, "requestDate": "2026-10-08T08:31:15.502Z", "userError": "VALID", "name": "---",
            "address": "---", "requestIdentifier": "", "originalVatNumber": ES_COMPANY, "vatNumber": ES_COMPANY,
            "viesApproximate": _APPROXIMATE}
INVALID = {"isValid": False, "requestDate": "2026-10-08T08:32:40.019Z", "userError": "INVALID", "name": "---",
           "address": "---", "requestIdentifier": "", "originalVatNumber": NIF_B, "vatNumber": NIF_B,
           "viesApproximate": _APPROXIMATE}
MS_UNAVAILABLE = {"isValid": False, "requestDate": "2026-10-08T08:33:00.000Z", "userError": "MS_UNAVAILABLE",
                  "name": "---", "address": "---", "requestIdentifier": "", "vatNumber": NIF_A}
SERVICE_DOWN = {"actionSucceed": False, "errorWrappers": [{"error": "SERVICE_UNAVAILABLE"}]}
BUSY = {"actionSucceed": False, "errorWrappers": [{"error": "MS_MAX_CONCURRENT_REQ"}]}


class Register:
    """A fake EU VAT register: answers by path, and counts every request it gets."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        for tail, answer in self.answers.items():
            if path.endswith(tail):
                if isinstance(answer, Exception):
                    raise answer
                status, body = answer if isinstance(answer, tuple) else (200, answer)
                return httpx.Response(status, json=body)
        return httpx.Response(200, json=INVALID)

    def client(self) -> ViesClient:
        return ViesClient(transport=httpx.MockTransport(self))


def test_the_register_endpoint_and_timeouts_are_documented() -> None:
    assert VIES_API == "https://ec.europa.eu/taxation_customs/vies/rest-api"
    assert TIMEOUT["connect"] == 3.0 and TIMEOUT["read"] == 6.0  # one attempt, no retry while the owner waits
    register = Register({f"/ms/PT/vat/{NIF_A}": VALID_PT})
    found = register.client().lookup("PT", NIF_A)
    assert register.paths == [f"/taxation_customs/vies/rest-api/ms/PT/vat/{NIF_A}"]
    assert (found.status, found.vat_number, found.legal_name, found.address, found.checked_at) == (
        "found", f"PT{NIF_A}", "PADARIA LUSITANA LDA", "RUA DE SANTA CATARINA 112, 4000-447 PORTO",
        "2026-10-08T08:30:02.124Z")
    # The check-vat-number endpoint's shape reads the same.
    posted = {"countryCode": "PT", "vatNumber": NIF_A, "requestDate": "2026-10-08T08:30:02.124Z", "valid": True,
              "requestIdentifier": "WAPIAAAAYua2Xl4O", "name": "PADARIA LUSITANA LDA",
              "address": "RUA DE SANTA CATARINA 112\n4000-447 PORTO", "traderName": "---"}
    other = parse_vies_response("PT", NIF_A, posted)
    assert (other.status, other.legal_name, other.consultation) == ("found", "PADARIA LUSITANA LDA",
                                                                     "WAPIAAAAYua2Xl4O")


def test_the_register_confirms_a_spanish_number_without_name_or_address() -> None:
    found = Register({f"/ms/ES/vat/{ES_COMPANY}": VALID_ES}).client().lookup("ES", ES_COMPANY)
    assert (found.status, found.legal_name, found.address, found.has_details) == ("valid", None, None, False)
    message = identity_check_message(found.status)
    assert message.startswith("The EU VAT register confirms this VAT number") and "Please type them." in message


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        (INVALID, "invalid"),
        (MS_UNAVAILABLE, "unavailable"),
        ((500, SERVICE_DOWN), "unavailable"),
        ((503, None), "unavailable"),
        (httpx.ReadTimeout("read timed out"), "unavailable"),
        (httpx.ConnectError("connection refused"), "unavailable"),
        ((200, BUSY), "rate_limited"),
        ((429, {"message": "Too many requests"}), "rate_limited"),
        ({"isValid": False, "userError": "GLOBAL_MAX_CONCURRENT_REQ", "name": "---", "address": "---"}, "rate_limited"),
    ],
)
def test_invalid_unreachable_and_busy_answers_are_plain_messages(answer: Any, status: str) -> None:
    found = Register({f"/ms/PT/vat/{NIF_A}": answer}).client().lookup("PT", NIF_A)
    assert found.status == status and not found.has_details
    message = identity_check_message(found.status, vat_number=found.vat_number)
    assert message and "type" in message
    plain(message)
    assert found.detail and found.detail not in message  # the register's own code is for the audit trail only


def test_check_digits_are_checked_before_anything_is_looked_up() -> None:
    register = Register({f"/ms/PT/vat/{NIF_A}": VALID_PT})
    client = register.client()
    assert lookup_company("516123458", "PT", client) is None  # wrong check digit: refused, nothing sent
    assert lookup_company("B12345675", "ES", client) is None
    assert lookup_company(NIF_A, "PT", None) is None  # no register (the demo): nothing at all
    assert register.paths == []
    assert lookup_company(f"PT {NIF_A[:3]} {NIF_A[3:6]} {NIF_A[6:]}", "PT", client).status == "found"
    assert register.paths == [f"/taxation_customs/vies/rest-api/ms/PT/vat/{NIF_A}"]

    def broken(country: str, number: str) -> CompanyLookup:
        raise RuntimeError("socket closed")

    assert lookup_company(NIF_A, "PT", broken).status == "unavailable"  # a failing client never blocks
    svc = BackOfficeService.new_tenant("t-digits", owner_name="Ana Silva", owner_email="ana@x.pt", now=NOW)
    svc.company_lookup = client
    status, body = svc.dispatch("POST", "/api/onboarding/company", {"name": "Padaria", "taxId": "516123458"})
    assert (status, body["message"]) == (400, "That NIF doesn't add up. Please check the digits.")
    assert len(register.paths) == 1


def test_onboarding_offers_the_registered_details_for_one_tap_and_keeps_what_was_typed() -> None:
    register = Register({f"/ms/PT/vat/{NIF_A}": VALID_PT, f"/ms/PT/vat/{NIF_B}": INVALID})
    svc = BackOfficeService.new_tenant("t-identity", owner_name="Ana Silva", owner_email="ana@x.pt", now=NOW)
    svc.company_lookup = register.client()
    status, out = svc.dispatch("POST", "/api/onboarding/company",
                               {"name": "Padaria", "taxId": NIF_A, "legalName": "Padaria da Ana, Lda."})
    assert status == 200 and out["message"] == "Done. Padaria is set up."
    identity = out["identity"]
    assert (identity["status"], identity["legalName"], identity["address"]) == (
        "found", "PADARIA LUSITANA LDA", "RUA DE SANTA CATARINA 112, 4000-447 PORTO")
    assert identity["message"] == ("The EU VAT register lists this number as PADARIA LUSITANA LDA, RUA DE SANTA "
                                   "CATARINA 112, 4000-447 PORTO. Use these details?")
    assert [o["id"] for o in identity["options"]] == ["use", "keep"]
    assert identity["confirmPath"] == "/api/companies/padaria/identity"
    plain(identity["message"], *(o["label"] for o in identity["options"]))
    # Nothing was written over what the owner typed, and nothing applied on its own.
    assert svc.repo.legal_names["padaria"] == "Padaria da Ana, Lda." and "padaria" not in svc.repo.company_addresses
    [company] = svc.dispatch("GET", "/api/companies", None)[1]["companies"]
    assert company["legalName"] == "Padaria da Ana, Lda." and company["identityCheck"]["status"] == "found"
    # The register's answer is kept as evidence, with the audit entry that names it.
    entries = [json.loads(r.body) for r in svc.repo.audit_store.records(svc.repo.tenant_id)]
    [looked] = [e for e in entries if e["action"] == "company_lookup"]
    assert looked["extracted_values"]["status"] == "found" and looked["extracted_values"]["source"] == "VIES"
    kept = json.loads(svc.repo.registry.open(svc.repo.tenant_id, looked["evidence_ids"][0]))
    assert (kept["kind"], kept["status"], kept["source"], kept["checked_at"]) == (
        "vat_register", "found", "VIES", "2026-10-08T08:30:02.124Z")
    # One tap uses the register's details.
    status, used = svc.dispatch("POST", "/api/companies/padaria/identity", {"use": True})
    assert status == 200 and used["message"] == "Done. Padaria now has the legal name and address from the EU VAT register."
    assert svc.repo.legal_names["padaria"] == "PADARIA LUSITANA LDA"
    assert svc.repo.company_addresses["padaria"] == ["RUA DE SANTA CATARINA 112, 4000-447 PORTO"]
    assert "identityCheck" not in svc.dispatch("GET", "/api/companies", None)[1]["companies"][0]
    assert svc.dispatch("POST", "/api/companies/padaria/identity", {"use": True})[0] == 409
    # A number the register does not list: the company is still added, the owner reads why.
    status, out = svc.dispatch("POST", "/api/onboarding/company", {"name": "Second", "taxId": NIF_B,
                                                                  "legalName": "Second, Lda."})
    assert status == 200 and out["identity"]["status"] == "invalid" and "options" not in out["identity"]
    assert out["identity"]["message"].startswith(f"The EU VAT register doesn't list PT{NIF_B}")
    assert svc.repo.legal_names["second"] == "Second, Lda."
    # Keeping what was typed is one tap too.
    register.answers[f"/ms/PT/vat/{NIF_B}"] = dict(VALID_PT, vatNumber=NIF_B, name="SECOND COMPANY LDA")
    status, out = svc.dispatch("POST", "/api/onboarding/company", {"name": "Third", "taxId": NIF_B})
    assert status == 409  # the same number is already a company here: nothing looked up twice matters
    third = BackOfficeService.new_tenant("t-keep", owner_name="Ana Silva", owner_email="ana@x.pt", now=NOW)
    third.company_lookup = register.client()
    third.dispatch("POST", "/api/onboarding/company", {"name": "Third", "taxId": NIF_B})
    status, kept = third.dispatch("POST", "/api/companies/third/identity", {"use": False})
    assert status == 200 and kept["message"] == "Done. I kept the details you typed for Third."
    assert third.repo.legal_names["third"] == "Third"
    assert third.dispatch("POST", "/api/companies/third/identity", {"use": "maybe"})[0] == 409


def test_a_register_that_is_down_or_busy_never_stops_onboarding() -> None:
    for answer, words in (((503, None), "couldn't reach"), ((429, {}), "busy")):
        register = Register({f"/ms/PT/vat/{NIF_A}": answer})
        svc = BackOfficeService.new_tenant("t-down", owner_name="Ana Silva", owner_email="ana@x.pt", now=NOW)
        svc.company_lookup = register.client()
        status, out = svc.dispatch("POST", "/api/onboarding/company", {"name": "Padaria", "taxId": NIF_A})
        assert status == 200 and out["company"]["id"] == "padaria"
        assert words in out["identity"]["message"] and "type" in out["identity"]["message"]
        assert "options" not in out["identity"] and not svc.repo.identity_suggestions
    # The demo has no register at all: nothing is looked up, nothing is said.
    demo = BackOfficeService.demo()
    assert demo.company_lookup is None
    out = demo.add_company("Company D", NIF_B)
    assert "identity" not in out


def test_production_looks_the_number_up_before_recording_and_replays_without_the_register(tmp_path: Path) -> None:
    register = Register({f"/ms/PT/vat/{NIF_A}": VALID_PT, f"/ms/ES/vat/{ES_COMPANY}": VALID_ES})
    h = harness(tmp_path, company_lookup=register.client())
    ana = signup(h.client)  # the first company comes with sign-up: looked up then
    A, tenant_id = bearer(ana["token"]), ana["tenant"]["id"]
    assert register.paths == [f"/taxation_customs/vies/rest-api/ms/PT/vat/{NIF_A}"]
    res = h.client.post("/api/onboarding/company", json={"name": "Padaria Madrid", "taxId": ES_COMPANY,
                                                          "country": "ES"}, headers=A)
    assert res.status_code == 200 and res.json()["identity"]["status"] == "valid"
    assert len(register.paths) == 2
    events = [Event.parse(r) for r in h.store.events(tenant_id)]
    recorded = [e.data["lookup"] for e in events if e.kind == "company.added"]
    assert [(r["status"], r["country"], r.get("legal_name")) for r in recorded] == [
        ("found", "PT", "PADARIA LUSITANA LDA"), ("valid", "ES", None)]
    companies = h.client.get("/api/companies", headers=A).json()["companies"]
    assert companies[0]["identityCheck"]["legalName"] == "PADARIA LUSITANA LDA"
    assert companies[0]["legalName"] == "Padaria Lda"  # what was typed at sign-up, until the owner taps
    used = h.client.post("/api/companies/padaria-lda/identity", json={"use": True}, headers=A)
    assert used.status_code == 200 and used.json()["company"]["legalName"] == "PADARIA LUSITANA LDA"
    with h.manager.open(tenant_id) as rt:
        live = state_digest(rt.service)
    # Another process rebuilds the tenant from its log alone, with no register: the same state, nothing asked.
    fresh = TenantManager(h.store, h.objects, now=h.clock, strict_reads=True)
    with fresh.open(tenant_id) as rt:
        assert state_digest(rt.service) == live
        assert rt.service.repo.legal_names["padaria-lda"] == "PADARIA LUSITANA LDA"
    assert len(register.paths) == 2
    # A register that raises is recorded as unavailable; the company is still added.
    h2 = harness(tmp_path / "b", company_lookup=lambda country, number: (_ for _ in ()).throw(OSError("down")))
    bea = signup(h2.client, "bea@example.pt")
    first = [Event.parse(r) for r in h2.store.events(bea["tenant"]["id"]) if Event.parse(r).kind == "company.added"]
    assert first[0].data["lookup"]["status"] == "unavailable"


# =========================================================================== 2. Portugal's tax calendar (P6)

PT_2026 = {  # the official 2026 calendar (Agenda Fiscal 2026, Autoridade Tributária), read 2026-10-08
    "pt-vat-return-monthly": {"2025-11": (1, 20), "2025-12": (2, 20), "2026-01": (3, 20), "2026-02": (4, 20),
                              "2026-03": (5, 20), "2026-04": (6, 22), "2026-05": (7, 20), "2026-06": (9, 21),
                              "2026-07": (9, 21), "2026-08": (10, 20), "2026-09": (11, 20), "2026-10": (12, 21)},
    "pt-vat-payment-monthly": {"2025-11": (1, 26), "2025-12": (2, 25), "2026-01": (3, 25), "2026-02": (4, 27),
                               "2026-03": (5, 25), "2026-04": (6, 25), "2026-05": (7, 27), "2026-06": (9, 25),
                               "2026-07": (9, 25), "2026-08": (10, 26), "2026-09": (11, 25), "2026-10": (12, 28)},
    "pt-vat-return-quarterly": {"2025-Q4": (2, 20), "2026-Q1": (5, 20), "2026-Q2": (9, 21), "2026-Q3": (11, 20)},
    "pt-vat-payment-quarterly": {"2025-Q4": (2, 25), "2026-Q1": (5, 25), "2026-Q2": (9, 25), "2026-Q3": (11, 25)},
    "pt-invoice-report": {"2025-12": (1, 9), "2026-01": (2, 5), "2026-02": (3, 5), "2026-03": (4, 8),
                          "2026-04": (5, 8), "2026-05": (6, 5), "2026-06": (7, 6), "2026-07": (8, 31),
                          "2026-08": (9, 7), "2026-09": (10, 6), "2026-10": (11, 5), "2026-11": (12, 7)},
    "pt-pay-declaration": {"2025-12": (1, 12), "2026-01": (2, 10), "2026-02": (3, 10), "2026-03": (4, 10),
                           "2026-04": (5, 11), "2026-05": (6, 11), "2026-06": (7, 10), "2026-07": (8, 31),
                           "2026-08": (9, 10), "2026-09": (10, 12), "2026-10": (11, 10), "2026-11": (12, 10)},
    "pt-withholding-payment": {"2025-12": (1, 20), "2026-01": (2, 20), "2026-02": (3, 20), "2026-03": (4, 20),
                               "2026-04": (5, 20), "2026-05": (6, 22), "2026-06": (7, 20), "2026-07": (8, 31),
                               "2026-08": (9, 21), "2026-09": (10, 20), "2026-10": (11, 20), "2026-11": (12, 21)},
    "pt-modelo-22": {"2025": (6, 30)},
    "pt-ies": {"2025": (7, 15)},
    "pt-modelo-10": {"2025": (3, 2)},
    "pt-advance-payment-1": {"2026": (7, 31)},
    "pt-advance-payment-3": {"2026": (12, 15)},
}


def test_portugal_calendar_rules_give_the_official_2026_dates() -> None:
    from backoffice.countries.pt.calendar import deadline

    for code, dates in PT_2026.items():
        for period, (month, day) in dates.items():
            assert deadline(code, period) == date(2026, month, day), (code, period)
    # Social Security: by the 25th since January 2026's contributions (gov.pt, 9 February 2026); July's by 31 August.
    assert [deadline("pt-social-security", p) for p in ("2026-01", "2026-03", "2026-07", "2026-09", "2026-11")] == [
        date(2026, 2, 25), date(2026, 4, 27), date(2026, 8, 31), date(2026, 10, 26), date(2026, 12, 28)]
    assert deadline("pt-advance-payment-2", "2026") == date(2026, 9, 30)
    # The law, not a dispatch, from 2027 on: the Modelo 22 by the last day of May and the IES by 15 July, even on
    # a weekend (CIRC arts. 120 and 121: "independentemente de esse dia ser útil ou não útil").
    assert deadline("pt-modelo-22", "2030") == date(2031, 5, 31)  # a Saturday
    assert deadline("pt-ies", "2027") == date(2028, 7, 15)  # a Saturday
    assert deadline("pt-modelo-10", "2026") == date(2027, 3, 1)  # 28 February 2027 is a Sunday
    assert deadline("pt-vat-return-quarterly", "2027-Q2") == date(2027, 9, 20)  # the second quarter: September
    with pytest.raises(ValueError):
        deadline("pt-vat-return-monthly", "2026-Q3")  # a monthly entry has no quarters


def test_every_calendar_entry_names_its_sources_and_when_they_were_read() -> None:
    from backoffice.countries.pt import calendar as cal

    assert cal.RETRIEVED == date(2026, 10, 8)
    codes = {e.code for e in cal.ENTRIES}
    assert codes == {"pt-vat-return-monthly", "pt-vat-payment-monthly", "pt-vat-return-quarterly",
                     "pt-vat-payment-quarterly", "pt-invoice-report", "pt-pay-declaration", "pt-withholding-payment",
                     "pt-social-security", "pt-modelo-22", "pt-ies", "pt-modelo-10", "pt-advance-payment-1",
                     "pt-advance-payment-2", "pt-advance-payment-3"}
    for entry in cal.ENTRIES:
        assert entry.sources and all(s.startswith(("https://", "http://")) for s in entry.sources), entry.code
        assert entry.responsible == ("accountant" if entry.kind in ("filing", "vat_return") else "owner"), entry.code
        assert entry.consequence and entry.proof and entry.rule
        plain(entry.title.format(period="August 2026"), entry.consequence, entry.proof.format(period="August 2026"),
              entry.rule)
    assert {code for code, _ in cal.OVERRIDES} <= codes
    # The official calendar's own two sources are cited by the entries they confirm.
    source = (SRC / "countries" / "pt" / "calendar.py").read_text(encoding="utf-8")
    assert source.count("read 2026-10-08") >= 13 and "Obrigacoes_declarativas.pdf" in source


def test_each_company_gets_the_deadlines_of_its_regime_with_who_does_them() -> None:
    pack = company_pack("PT")
    today = date(2026, 10, 8)
    assert pack.periodic_obligations("c", today) == ()  # nothing known: nothing guessed
    monthly = pack.periodic_obligations("c", today, TaxProfile(vat="monthly", tax_id=NIF_A))
    assert [(o.calendar, o.period, o.due_on) for o in monthly] == [
        ("pt-vat-return-monthly", "2026-08", date(2026, 10, 20)), ("pt-vat-payment-monthly", "2026-08", date(2026, 10, 26)),
        ("pt-vat-return-monthly", "2026-09", date(2026, 11, 20)), ("pt-vat-payment-monthly", "2026-09", date(2026, 11, 25))]
    ret = monthly[0]
    assert (ret.title, ret.kind, ret.responsible, ret.issuer) == ("VAT return for August 2026", "vat_return",
                                                                  "accountant", "tax_authority")
    assert ret.consequence == "Filing late may lead to a fine."
    assert ret.required_evidence == "The tax office's filing receipt for the VAT return for August 2026."
    assert ret.reasons[:2] == ("VAT return for August 2026", "Your company files VAT every month.")
    assert monthly[1].responsible == "owner" and monthly[1].kind == "tax_deadline"
    quarterly = pack.periodic_obligations("c", today, TaxProfile(vat="quarterly", tax_id=NIF_A))
    assert [(o.calendar, o.period, o.due_on) for o in quarterly] == [
        ("pt-vat-return-quarterly", "2026-Q3", date(2026, 11, 20)),
        ("pt-vat-payment-quarterly", "2026-Q3", date(2026, 11, 25))]
    # Salaries: the salaries report, the tax withheld and Social Security, each for September.
    staff = pack.periodic_obligations("c", today, TaxProfile(employees=True, tax_id=NIF_A))
    assert [(o.calendar, o.due_on, o.responsible, o.issuer) for o in staff] == [
        ("pt-pay-declaration", date(2026, 10, 12), "accountant", "tax_authority"),
        ("pt-withholding-payment", date(2026, 10, 20), "owner", "tax_authority"),
        ("pt-social-security", date(2026, 10, 26), "owner", "social_security")]
    # The invoice report of September was due on 6 October: on 8 October it is not raised late.
    early = pack.periodic_obligations("c", date(2026, 10, 1), TaxProfile(vat="exempt", tax_id=NIF_A))
    assert [(o.calendar, o.due_on) for o in early] == [("pt-invoice-report", date(2026, 10, 6))]
    assert pack.periodic_obligations("c", today, TaxProfile(vat="exempt", tax_id=NIF_A)) == ()
    # A company's yearly returns from 1 January; advance payments in their own month.
    year = pack.periodic_obligations("c", date(2027, 2, 1), TaxProfile(other_income=True, tax_id=NIF_A))
    assert [(o.calendar, o.period, o.due_on) for o in year] == [
        ("pt-withholding-payment", "2027-01", date(2027, 2, 22)), ("pt-modelo-10", "2026", date(2027, 3, 1)),
        ("pt-modelo-22", "2026", date(2027, 5, 31)), ("pt-ies", "2026", date(2027, 7, 15))]
    person = pack.periodic_obligations("c", date(2027, 2, 1), TaxProfile(tax_id="234567899"))
    assert person == ()  # a person's NIF: no corporate income tax return
    advance = pack.periodic_obligations("c", date(2026, 12, 2), TaxProfile(advance_payments=True, tax_id=NIF_A))
    assert [(o.calendar, o.due_on, o.title) for o in advance] == [
        ("pt-advance-payment-3", date(2026, 12, 15), "Third advance payment of corporate income tax for 2026")]


def _business(*, accountant: bool = True) -> BackOfficeService:
    svc = BackOfficeService.new_tenant("t-calendar", owner_name="Ana Silva", owner_email="ana@x.pt", now=NOW)
    svc.add_company("Padaria", NIF_A, "Padaria Lusitana, Lda.")
    svc.repo.add_account(Account(id="acc", bank="Millennium BCP", holder_id="padaria", iban="PT50001800005554443332214"))
    if accountant:
        svc.set_accountant("marc@vidal.pt", "Marc Vidal")
    return svc


def _bank(svc: BackOfficeService, day: date, amount: str, who: str, what: str, *, ref: str | None = None) -> Any:
    o = svc.orchestrator
    report = o.ingest_bank([BankRow(bank_id=f"b-{day}-{what}", account_id="acc", booked_on=day, amount=D(amount),
                                    counterparty=who, description=what, kind=K.TRANSFER_OUT, reference=ref)],
                           at=local_datetime(day, 18, 0))
    return o.repo.transactions[report.transaction_ids[0]]


def _calendar(svc: BackOfficeService) -> dict[str, Any]:
    return {f"{ob.calendar}:{ob.period}": ob for ob in svc.repo.obligations.values() if ob.calendar}


def test_the_regime_is_learned_from_the_companys_own_payments_never_from_one() -> None:
    pack = company_pack("PT")
    day = date(2026, 10, 8)

    def tax(on: date, text: str) -> TaxSignal:
        return TaxSignal(on=on, kind="tax", text=text)

    one = pack.learn_tax_profile([tax(date(2026, 9, 21), "PAG ESTADO IVA 2026/07")], day)
    assert one.profile.vat is None  # one payment says nothing about a rhythm
    two = pack.learn_tax_profile([tax(date(2026, 9, 21), "PAG ESTADO IVA 2026/07"),
                                  tax(date(2026, 10, 20), "PAG ESTADO IVA 2026/08")], day)
    assert two.profile.vat == "monthly" and two.reasons["vat"] == (
        "VAT payments for July 2026, August 2026 name single months.",)
    quarters = pack.learn_tax_profile([tax(date(2026, 5, 20), "AT IVA 2026/03T"),
                                       tax(date(2026, 9, 25), "PAG ESTADO IVA 2T 2026")], day)
    assert quarters.profile.vat == "quarterly"
    mixed = pack.learn_tax_profile([tax(date(2026, 5, 20), "AT IVA 2026/03T"), tax(date(2026, 6, 25), "AT IVA 2026/04"),
                                    tax(date(2026, 7, 27), "AT IVA 2026/05")], day)
    assert mixed.profile.vat is None  # both monthly and quarterly periods: asked, never guessed
    rhythm = pack.learn_tax_profile([tax(date(2026, 8, 25), "PAG ESTADO IVA"), tax(date(2026, 9, 25), "PAG ESTADO IVA")],
                                    day)
    assert rhythm.profile.vat == "monthly"
    staff = pack.learn_tax_profile([TaxSignal(on=date(2026, 8, 28), kind="salary"),
                                    tax(date(2026, 9, 20), "SEG SOCIAL CONTRIBUICOES")], day)
    assert staff.profile.employees is True and staff.reasons["employees"] == (
        "Salaries paid in August 2026.", "Social Security paid in September 2026.")
    assert pack.learn_tax_profile([TaxSignal(on=date(2026, 8, 28), kind="salary")], day).profile.employees is None
    advance = pack.learn_tax_profile([tax(date(2026, 7, 30), "PAG ESTADO IRC PAGAMENTO POR CONTA")], day)
    assert advance.profile.advance_payments is True
    assert company_pack("ES").learn_tax_profile([tax(date(2026, 9, 21), "AEAT IVA 2026/07")], day).profile == TaxProfile()
    # Through the engine: two VAT payments of the company switch its calendar on.
    svc = _business()
    _bank(svc, date(2026, 9, 21), "-2184.37", "AUTORIDADE TRIBUTARIA", "PAG ESTADO IVA 2026/07")
    assert not _calendar(svc)
    _bank(svc, date(2026, 10, 7), "-405.00", "AUTORIDADE TRIBUTARIA", "PAG ESTADO IVA 2026/08")
    found = _calendar(svc)
    assert set(found) == {"pt-vat-return-monthly:2026-08", "pt-vat-payment-monthly:2026-08",
                          "pt-vat-return-monthly:2026-09", "pt-vat-payment-monthly:2026-09"}
    # The August payment named its period: that deadline is met; the others wait for their proof.
    assert found["pt-vat-payment-monthly:2026-08"].done
    assert found["pt-vat-payment-monthly:2026-08"].how == "Paid on 7 October. The bank line names the tax and the period."
    assert not found["pt-vat-return-monthly:2026-08"].done
    profile = svc.tax_calendar("padaria")
    assert (profile["vat"], profile["setBy"]["vat"]) == ("monthly", "learned") and profile["why"]["vat"]
    # The demo learned nothing it could act on: Laura's companies keep exactly the deadlines of their letters.
    demo = BackOfficeService.demo()
    assert len(demo.repo.obligations) == 2 and not [o for o in demo.repo.obligations.values() if o.calendar]


def test_the_owner_and_the_accountant_set_the_regime_and_win_over_what_was_learned() -> None:
    svc = _business()
    status, out = svc.dispatch("POST", "/api/companies/padaria/profile", {"vat": "quarterly", "employees": True})
    assert status == 200 and out["message"] == "Done. Padaria is updated."
    tax = out["profile"]["taxCalendar"]
    assert (tax["vat"], tax["employees"], tax["setBy"]["vat"], tax["setBy"]["employees"]) == (
        "quarterly", True, "owner", "owner")
    assert [(d["title"], d["due"], d["responsible"]) for d in tax["deadlines"]] == [
        ("Salaries report to the tax office for September 2026", "2026-10-12", "Your accountant"),
        ("Payment of the tax withheld in September 2026", "2026-10-20", "You"),
        ("Social Security payment for September 2026", "2026-10-26", "You"),
        ("VAT return for July to September 2026", "2026-11-20", "Your accountant"),
        ("VAT payment for July to September 2026", "2026-11-25", "You")]
    assert svc.dispatch("POST", "/api/companies/padaria/profile", {"vat": "yearly"})[0] == 400
    assert svc.dispatch("POST", "/api/companies/padaria/profile", {"employees": "yes"})[0] == 400
    # What the owner said wins over two monthly VAT payments.
    _bank(svc, date(2026, 9, 21), "-100.00", "PAG ESTADO", "IVA 2026/07")
    _bank(svc, date(2026, 10, 7), "-100.00", "PAG ESTADO", "IVA 2026/08")
    assert svc.tax_calendar("padaria")["vat"] == "quarterly"
    assert not any(k.startswith("pt-vat-return-monthly") for k in _calendar(svc))
    # The accountant says it in one sentence, like any rule; it replaces the owner's answer.
    status, rule = svc.dispatch("POST", "/api/accountant/clients/padaria/rules",
                                {"text": "Padaria files VAT every month"})
    assert status == 200 and rule["rule"]["label"] == "Files VAT every month"
    assert rule["message"] == "Done. Padaria: files VAT every month. I added 4 deadlines from its tax calendar."
    assert rule["taxCalendar"]["setBy"]["vat"] == "accountant"
    plain(rule["message"])
    status, rule = svc.dispatch("POST", "/api/accountant/clients/padaria/rules", {"text": "No employees."})
    assert status == 200 and rule["rule"]["label"] == "Pays no salaries"
    assert svc.tax_calendar("padaria")["employees"] is False
    # Ordinary rules are still rules.
    status, other = svc.dispatch("POST", "/api/accountant/clients/padaria/rules", {"text": "Treat all EDP as Utilities"})
    assert status == 200 and other["rule"]["label"] == "Treat all EDP costs as Utilities"
    # Without an accountant there is nobody to say it.
    lone = _business(accountant=False)
    assert lone.dispatch("POST", "/api/accountant/rules", {"text": "IVA trimestral", "companyId": "padaria"})[0] == 409


def test_calendar_deadlines_close_only_by_the_proof_that_names_them() -> None:
    svc = _business()
    svc.dispatch("POST", "/api/companies/padaria/profile", {"vat": "monthly", "employees": True})
    o = svc.orchestrator
    found = _calendar(svc)
    # A tax payment that names another tax, or no period, proves nothing about the VAT.
    _bank(svc, date(2026, 10, 9), "-412.50", "PAG ESTADO", "IRS RETENCOES 2026/09")
    _bank(svc, date(2026, 10, 9), "-99.00", "PAG ESTADO", "IVA")
    assert not found["pt-vat-payment-monthly:2026-08"].done
    assert found["pt-withholding-payment:2026-09"].done  # the IRS line named its tax and period
    # A filing receipt for another period never closes this one; the right one does.
    def receipt(text: str, day: date) -> list[str]:
        return o.ingest_file(text.encode(), filename="comprovativo.txt", content_type="text/plain", origin="upload",
                             at=local_datetime(day, 9, 0)).obligation_ids

    head = f"Autoridade Tributária e Aduaneira\nComprovativo de entrega\nNIF: {NIF_A}\n"
    receipt(head + "Declaração Mensal de Remunerações\nPeríodo: 2026/09\nDeclaração entregue com sucesso.\n",
            date(2026, 10, 10))
    assert found["pt-pay-declaration:2026-09"].done
    receipt(head + "Declaração periódica de IVA\nPeríodo: 2026/07\nDeclaração submetida com sucesso.\n",
            date(2026, 10, 12))
    assert not found["pt-vat-return-monthly:2026-08"].done
    receipt(head + "Declaração periódica de IVA\nPeríodo: 2026/08\nDeclaração submetida com sucesso.\n",
            date(2026, 10, 15))
    ret = found["pt-vat-return-monthly:2026-08"]
    assert ret.done and ret.how == "The filing receipt arrived on 15 October."
    assert not found["pt-vat-return-monthly:2026-09"].done
    _bank(svc, date(2026, 10, 23), "-1204.00", "PAG ESTADO", "IVA 2026/08")
    assert found["pt-vat-payment-monthly:2026-08"].done and not found["pt-vat-payment-monthly:2026-09"].done
    # The owner's word closes a filing, as for any filing (it is kept as evidence).
    later = date(2026, 11, 2)
    o.run(local_datetime(later, 9, 0))
    report = _calendar(svc)["pt-invoice-report:2026-10"]
    assert report.obligation.due_on == date(2026, 11, 5) and report.obligation.responsible == "accountant"
    status, body = svc.dispatch("POST", f"/api/obligations/{report.obligation.id}/done", {"outcome": "filed"})
    assert status == 200 and report.done and report.how == "You told me on 2 November that it was filed."
    # Payments are never closed by the owner's word: only a payment proves a payment.
    social = _calendar(svc)["pt-social-security:2026-10"]
    status, refused = svc.dispatch("POST", f"/api/obligations/{social.obligation.id}/done", {"outcome": "filed"})
    assert (status, refused["message"]) == (409, "I close this one when I see the payment in your bank.")
    items = {i["id"]: i for i in svc.dispatch("GET", "/api/obligations", None)[1]["items"]}
    item = items[social.obligation.id]
    assert (item["title"], item["responsible"], item["due"], item["kind"]) == (
        "Social Security payment for October 2026", "You", "2026-11-25", "tax_deadline")
    assert item["why"][:2] == ["Social Security payment for October 2026", "Your company pays salaries."]
    assert item["consequence"] == "Paying late adds interest and may lead to a fine."
    plain(*item["why"], item["nextStep"], item["requiredProof"])


def test_a_tax_letter_for_a_calendar_deadline_is_that_same_deadline() -> None:
    svc = _business()
    svc.dispatch("POST", "/api/companies/padaria/profile", {"vat": "monthly"})
    o = svc.orchestrator
    payment = _calendar(svc)["pt-vat-payment-monthly:2026-08"]
    letter = (f"Autoridade Tributária e Aduaneira\nNIF: {NIF_A}\nPadaria Lusitana, Lda.\n"
              "Pagamento de IVA — período 2026/08.\nReferência para pagamento: 161 555 902\n"
              "Total a pagar: 1.204,00 €\nData limite de pagamento: 26/10/2026.\n")
    before = set(o.repo.obligations)
    report = o.ingest_file(letter.encode(), filename="carta.txt", content_type="text/plain", origin="upload",
                           at=local_datetime(date(2026, 10, 12), 9, 0))
    assert report.obligation_ids == [payment.obligation.id] and set(o.repo.obligations) == before  # no second one
    assert (payment.obligation.amount, payment.reference) == (D("1204.00"), "161555902")
    assert payment.obligation.kind is ObligationKind.TAX_DEADLINE and payment.calendar == "pt-vat-payment-monthly"
    # The payment carrying the letter's reference proves it, and the letter is the payment's tax notice.
    rec = _bank(svc, date(2026, 10, 20), "-1204.00", "AUTORIDADE TRIBUTARIA", "PAGAMENTO AO ESTADO", ref="161555902")
    assert payment.done and rec.proof_evidence_ids == [payment.evidence_id]
    # A reminder to file is the calendar's filing too, by the words it uses (and closed by the one receipt).
    reminder = (f"Autoridade Tributária e Aduaneira\nNIF: {NIF_A}\nPadaria Lusitana, Lda.\n"
                "Lembrete: entrega da declaração periódica de IVA do período 2026/08.\nPrazo: até 20/10/2026.\n")
    vat_return = _calendar(svc)["pt-vat-return-monthly:2026-08"]
    report = o.ingest_file(reminder.encode(), filename="lembrete.txt", content_type="text/plain", origin="upload",
                           at=local_datetime(date(2026, 10, 13), 9, 0))
    assert report.obligation_ids == [vat_return.obligation.id] and set(o.repo.obligations) == before
    o.ingest_file((f"Autoridade Tributária e Aduaneira\nComprovativo de entrega\nNIF: {NIF_A}\n"
                   "Declaração periódica de IVA\nPeríodo: 2026/08\nDeclaração submetida com sucesso.\n").encode(),
                  filename="comprovativo.txt", content_type="text/plain", origin="upload",
                  at=local_datetime(date(2026, 10, 14), 9, 0))
    assert vat_return.done
    # A letter on file first: the calendar adds no second deadline for it.
    other = _business()
    o2 = other.orchestrator
    o2.ingest_file(letter.encode(), filename="carta.txt", content_type="text/plain", origin="upload",
                   at=local_datetime(date(2026, 10, 9), 9, 0))
    other.dispatch("POST", "/api/companies/padaria/profile", {"vat": "monthly"})
    assert "pt-vat-payment-monthly:2026-08" not in _calendar(other)
    assert "pt-vat-return-monthly:2026-08" in _calendar(other)


def test_spain_keeps_its_modelo_303_and_the_calendar_replays_identically(tmp_path: Path) -> None:
    es = company_pack("ES")
    [q3] = es.periodic_obligations("c", date(2026, 10, 8), TaxProfile(vat="monthly"))
    assert (q3.title, q3.due_on, q3.calendar) == ("Quarterly VAT return (modelo 303)", date(2026, 10, 20), "")
    assert es.periodic_obligations("c", date(2026, 10, 8)) == (q3,)
    # Production: the owner's answer is an event; replaying the log rebuilds the same deadlines.
    h = harness(tmp_path)
    ana = signup(h.client)
    A, tenant_id = bearer(ana["token"]), ana["tenant"]["id"]
    res = h.client.post("/api/companies/padaria-lda/profile", json={"vat": "monthly", "employees": True}, headers=A)
    # On 2 October: the salaries report, tax withheld and Social Security for September, August's and September's
    # VAT return and payment, and September's invoice report (due 6 October).
    deadlines = res.json()["profile"]["taxCalendar"]["deadlines"]
    assert res.status_code == 200 and len(deadlines) == 8
    assert ("Invoice report to the tax office for September 2026", "2026-10-06") in {(d["title"], d["due"])
                                                                                    for d in deadlines}
    obligations = h.client.get("/api/obligations", headers=A).json()["items"]
    assert len([i for i in obligations if i["companyId"] == "padaria-lda"]) == 8
    with h.manager.open(tenant_id) as rt:
        live = state_digest(rt.service)
    fresh = TenantManager(h.store, h.objects, now=h.clock, strict_reads=True)
    with fresh.open(tenant_id) as rt:
        assert state_digest(rt.service) == live


def test_the_core_reaches_each_country_only_through_its_pack() -> None:
    for name in ("tax_profiles.py", "company_lookup.py", "service.py", "orchestrator.py",
                 "reconciliation/expected.py", "server/runtime.py"):
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        modules += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not [m for m in modules if m.startswith(("backoffice.countries.pt", "backoffice.countries.es"))], name


# =========================================================================== 3. French, German, Italian invoices (E9)

HAZEL = "516123459"
FRENCH = f"""Atelier Lumière SARL
18 rue des Martyrs, 75009 Paris, France
SIRET : 912 345 675 00009
N° TVA intracommunautaire : FR 65 912 345 675

Facture n° FA-2026-0915
Date de facture : 15/09/2026
Client : Hazel Tree Interiores, Lda.
N° TVA client : PT{HAZEL}
Montage de stand, salon de la décoration, Paris, 10-14 septembre 2026
Total HT : 1 250,00 €
TVA 20 % : 250,00 €
Total TTC : 1 500,00 €
Échéance : 30/09/2026
"""
GERMAN = f"""Messebau Nord GmbH
Hafenstraße 12, 20457 Hamburg, Deutschland
USt-IdNr.: DE812345673
Steuernummer: 47/123/45678

Rechnung Nr. 2026-0418
Rechnungsdatum: 18.09.2026
Kunde: Hazel Tree Interiores, Lda.
USt-IdNr. Kunde: PT{HAZEL}
Messekatalog, 500 Stück, geliefert an den Messestand in Frankfurt am Main
Nettobetrag: 1.000,00 €
MwSt. 19 %: 190,00 €
Gesamtbetrag: 1.190,00 €
Zahlbar bis: 02.10.2026
"""
ITALIAN = f"""Ceramiche Vietri S.r.l.
Via Costiera 21, 84019 Vietri sul Mare (SA), Italia
P.IVA 09123456783

Fattura n. 118/2026
Data fattura: 22/09/2026
Cliente: Hazel Tree Interiores, Lda.
P.IVA cliente: PT{HAZEL}
Piastrelle decorate, consegna presso il nostro stand alla Fiera di Milano
Imponibile: 800,00 €
IVA 22%: 176,00 €
Totale: 976,00 €
Scadenza: 22/10/2026
"""
SAMPLES = {
    "FR": (FRENCH, date(2026, 9, 15), "-1500.00", "ATELIER LUMIERE PARIS", "FA-2026-0915", "FR65912345675",
           ("1250.00", "250.00", "1500.00"), "0.20", "France", date(2026, 9, 30)),
    "DE": (GERMAN, date(2026, 9, 18), "-1190.00", "MESSEBAU NORD", "2026-0418", "DE812345673",
           ("1000.00", "190.00", "1190.00"), "0.19", "Germany", date(2026, 10, 2)),
    "IT": (ITALIAN, date(2026, 9, 22), "-976.00", "CERAMICHE VIETRI", "118/2026", "IT09123456783",
           ("800.00", "176.00", "976.00"), "0.22", "Italy", date(2026, 10, 22)),
}


@pytest.mark.parametrize("country", ["FR", "DE", "IT"])
def test_french_german_and_italian_labels_and_number_formats(country: str) -> None:
    text, day, _, _, number, vat_number, (net, vat, gross), rate, _, due = SAMPLES[country]
    issuer = detect_issuer(text, own_tax_ids=OWN)
    assert (issuer.country, issuer.language, document_language(text)) == (country, country.lower(), country.lower())
    assert issuer.has_valid_vat_number and issuer.is_foreign
    fields = read_foreign_text(text, "ev", method=ExtractionMethod.EMBEDDED_TEXT, issuer=issuer, own_tax_ids=OWN)
    got = {f: [o.value for o in obs] for f, obs in fields.observations.items()}
    assert got[F.INVOICE_NUMBER] == [number] and got[F.ISSUE_DATE] == [day] and got[F.DUE_DATE] == [due]
    assert (got[F.NET_AMOUNT], got[F.VAT_AMOUNT], got[F.GROSS_AMOUNT]) == ([D(net)], [D(vat)], [D(gross)])
    assert got[F.CURRENCY] == ["EUR"] and got[F.SUPPLIER_TAX_ID] == [vat_number]
    assert got[F.CUSTOMER_TAX_ID] == [f"PT{HAZEL}"] and fields.stated_rates == (D(rate),)


def test_vat_numbers_of_france_germany_and_italy_are_checked_by_their_own_rules() -> None:
    for raw, valid in (("FR65912345675", True), ("FR66912345675", False),  # France: the key from the SIREN
                       ("DE812345673", True), ("DE812345674", False),  # Germany: ISO 7064 MOD 11,10
                       ("IT09123456783", True), ("IT09123456784", False)):  # Italy: the Luhn check
        number = check_tax_number(raw)
        assert number is not None and number.valid is valid and number.checksum is valid, raw
    # An Italian partita IVA printed without IT is read by its label; a customer's is the customer's.
    [supplier] = find_tax_numbers("Ceramiche Vietri S.r.l.\nPartita IVA: 09123456783\n")
    assert (supplier.country, supplier.printed, supplier.role) == ("IT", "IT09123456783", None)
    [customer] = find_tax_numbers("Cliente:\nStudio Bianchi\nP.IVA 02876543212\n")
    assert customer.role == "customer"
    assert find_tax_numbers("P.IVA 09123456784") == []  # wrong check digit: never a number
    # A French SIREN (or SIRET) carries the French VAT number: FR + key + SIREN.
    assert french_vat_from_siren("912345675") == "FR65912345675"
    assert find_sirens("SIRET : 912 345 675 00009\nSIREN 890 123 458") == [("912345675", 1, None), ("890123458", 2, None)]
    assert find_sirens("SIRET : 912 345 675 00008") == []  # its Luhn check fails


@pytest.mark.parametrize("country", ["FR", "DE", "IT"])
def test_french_german_and_italian_invoices_close_green_against_the_bank(country: str) -> None:
    text, day, amount, who, number, vat_number, (_, vat, gross), rate, name, _ = SAMPLES[country]
    o = tenant()
    doc = upload(o, text, day)
    assert doc.is_foreign and doc.issuer.country == country and doc.document.doc_type.value == "invoice"
    assert (doc.document.invoice_number, doc.document.supplier_tax_id, doc.document.gross_amount) == (
        number, vat_number, D(gross))
    assert doc.document.quality is Quality.AMBER  # one source so far: nothing confirmed, nothing guessed
    tx = pay(o, card(f"c-{country}", day, amount, who))
    assert tx.document_ids == [doc.id] and doc.document.quality is Quality.GREEN and closed(o, doc, tx)
    for field in (F.INVOICE_NUMBER, F.ISSUE_DATE, F.SUPPLIER_TAX_ID, F.GROSS_AMOUNT, F.NET_AMOUNT, F.VAT_AMOUNT):
        assert doc.checks[field.value].quality is Quality.GREEN, field
    percent = f"{int(D(rate) * 100)}%"
    assert doc.checks["vat_amount"].reasons[0] == (
        f"It adds up to the total the bank charged, with VAT at {percent}, a rate used in {name}.")
    assert "check digits" in doc.checks["supplier_tax_id"].reasons[0]
    assert o.run(local_datetime(date(2026, 10, 2), 9, 30)).reopened == [] and closed(o, doc, tx)


def test_french_german_and_italian_invoices_whose_fields_disagree_never_close() -> None:
    # Germany: the printed rate (7%) is not the rate the amounts carry (19%): the bank's total alone proves nothing.
    o = tenant()
    doc = upload(o, GERMAN.replace("MwSt. 19 %: 190,00 €", "MwSt. 7 %: 190,00 €"), date(2026, 9, 18))
    tx = pay(o, card("c-de", date(2026, 9, 18), "-1190.00", "MESSEBAU NORD"))
    assert doc.document.quality is Quality.AMBER and doc.checks["vat_amount"].quality is Quality.AMBER
    assert not closed(o, tx) and tx.document_ids == [] and tx.likely_document_ids == [doc.id]
    # Germany: net + VAT is not the total printed: a conflict, never closed.
    o = tenant()
    doc = upload(o, GERMAN.replace("Gesamtbetrag: 1.190,00 €", "Gesamtbetrag: 1.290,00 €"), date(2026, 9, 18))
    tx = pay(o, card("c-de", date(2026, 9, 18), "-1290.00", "MESSEBAU NORD"))
    assert doc.document.quality is Quality.RED and not closed(o, tx) and tx.document_ids == []
    # Italy: the bank charged another amount than the invoice's total: nothing is matched.
    o = tenant()
    doc = upload(o, ITALIAN, date(2026, 9, 22))
    tx = pay(o, card("c-it", date(2026, 9, 22), "-967.00", "CERAMICHE VIETRI"))
    assert doc.document.quality is Quality.AMBER and tx.document_ids == [] and not closed(o, doc)
    # France: the SIRET belongs to another company than the VAT number printed: two readings, a conflict.
    o = tenant()
    doc = upload(o, FRENCH.replace("SIRET : 912 345 675 00009", "SIRET : 890 123 458 00006"), date(2026, 9, 15))
    tx = pay(o, card("c-fr", date(2026, 9, 15), "-1500.00", "ATELIER LUMIERE PARIS"))
    assert doc.checks["supplier_tax_id"].quality is Quality.RED and doc.document.quality is not Quality.GREEN
    assert not closed(o, tx) and tx.document_ids == []


# =========================================================================== 4. bank fees by country (J6)


def test_whether_the_bank_statement_covers_a_bank_charge_is_each_countrys_policy() -> None:
    from backoffice.domain.models import LegalEntity, Transaction

    pt = LegalEntity(id="pt", tenant_id="t", name="Hazel Tree Lda", country="PT", tax_id=NIF_A)
    es = LegalEntity(id="es", tenant_id="t", name="Hazel Tree SL", country="ES", tax_id=ES_COMPANY)
    engine = ExpectedEvidenceEngine(entities=[pt, es], account_countries={"acc-pt": "PT", "acc-es": "ES"})

    def decide(account: str, amount: str, words: str, kind: TransactionKind = K.TRANSFER_OUT) -> Any:
        return engine.classify(Transaction(id=f"tx-{account}-{words}", tenant_id="t", account_id=account,
                                           booked_on=date(2026, 9, 30), amount=D(amount), counterparty=words,
                                           kind=kind, entity_id=account[4:]))

    statement, bank_document = EvidenceExpectation.BANK_EVIDENCE_SUFFICES, EvidenceExpectation.LOAN_STATEMENT
    assert company_pack("PT").bank_fee_policy().covers == frozenset({"fee", "stamp_duty", "interest"})
    assert company_pack("ES").bank_fee_policy().covers == frozenset({"fee"})
    # Portugal: fees, commissions, stamp duty and interest charged: the statement is enough.
    for words in ("COMISSAO MANUTENCAO CONTA", "IMPOSTO DO SELO COMISSAO", "IMP SELO S/ JUROS", "JUROS DEVEDORES",
                  "MONTHLY FEE"):
        decision = decide("acc-pt", "-4.16", words)
        assert (decision.expectation, decision.reason) == (statement, "Bank charge. Your bank statement is enough."), words
    assert decide("acc-pt", "-3.00", "", K.FEE).quality is Quality.GREEN
    # Spain: commissions only. Interest charged needs the bank's own settlement of it.
    commission = decide("acc-es", "-12.00", "COMISION MANTENIMIENTO")
    assert (commission.expectation, commission.reason) == (statement, "Bank charge. Your bank statement is enough.")
    assert decide("acc-es", "-3.00", "", K.FEE).expectation is statement  # the bank's own fee: a commission
    interest = decide("acc-es", "-25.40", "LIQUIDACION INTERESES DEUDORES")
    assert (interest.expectation, interest.rule, interest.quality) == (bank_document, "bank_fee_document",
                                                                       Quality.AMBER)
    assert interest.reason == ("Charged by your bank. In Spain the statement line is not enough for this charge: I "
                               "need the bank's own document for it.")
    assert interest.requires_document and interest.provider.value == "bank"
    plain(interest.reason)
    declared = decide("acc-es", "-25.40", "INTERESES", K.FEE)
    assert (declared.expectation, declared.quality) == (bank_document, Quality.GREEN)
    # Interest the bank pays in is never a charge, anywhere.
    assert decide("acc-es", "0.12", "INTERESES ACREEDORES", K.TRANSFER_IN).expectation is statement


def test_an_accountants_rule_still_wins_over_the_countrys_bank_fee_policy() -> None:
    svc = BackOfficeService.new_tenant("t-fees", owner_name="Laura Reis", owner_email="laura@hazel.pt", now=NOW)
    pt = svc.add_company("Hazel Tree", NIF_A, "Hazel Tree Interiores, Lda.")["company"]["id"]
    es = svc.add_company("Hazel Tree Madrid", ES_COMPANY, "Hazel Tree España S.L.", country="ES")["company"]["id"]
    svc.set_accountant("marc@vidal.pt", "Marc Vidal")
    o = svc.orchestrator
    o.repo.add_account(Account(id="acc-pt", bank="Millennium BCP", holder_id=pt, iban="PT50001800005554443332214"))
    o.repo.add_account(Account(id="acc-es", bank="BBVA", holder_id=es, iban="ES9121000418450200051332"))

    def lines(day: date) -> tuple[Any, Any]:
        rows = [BankRow(bank_id=f"f1-{day}", account_id="acc-pt", booked_on=day, amount=D("-6.24"),
                        counterparty="MILLENNIUM BCP", description="COMISSAO MANUTENCAO CONTA", kind=K.FEE),
                BankRow(bank_id=f"f2-{day}", account_id="acc-es", booked_on=day, amount=D("-25.40"),
                        counterparty="BBVA", description="LIQUIDACION INTERESES", kind=K.TRANSFER_OUT)]
        report = o.ingest_bank(rows, at=local_datetime(day, 23, 0))
        return tuple(o.repo.transactions[t] for t in report.transaction_ids)  # type: ignore[return-value]

    fee, interest = lines(date(2026, 9, 30))
    assert fee.decision.expectation is EvidenceExpectation.BANK_EVIDENCE_SUFFICES
    assert interest.decision.expectation is EvidenceExpectation.LOAN_STATEMENT
    svc.accountant_rule("Millennium BCP always needs an invoice", "client", pt)
    svc.accountant_rule("BBVA never has an invoice", "client", es)
    # The Spanish interest still waiting for its document is decided again by the rule.
    assert (interest.decision.expectation, interest.decision.rule) == (
        EvidenceExpectation.BANK_EVIDENCE_SUFFICES, "learned")
    # Every later charge follows the accountant, whatever the country's policy says.
    fee, interest = lines(date(2026, 10, 31))
    assert (fee.decision.expectation, fee.decision.rule) == (EvidenceExpectation.INVOICE, "learned")
    assert (interest.decision.expectation, interest.decision.rule) == (
        EvidenceExpectation.BANK_EVIDENCE_SUFFICES, "learned")
