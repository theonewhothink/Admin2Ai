"""BackOfficeService: every endpoint through ``dispatch``, shaped like web/lib/types.ts (§34–41, §69–70)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from backoffice.demo import evidence as E
from backoffice.language import find_jargon
from backoffice.service import BackOfficeService

SRC = Path(__file__).resolve().parents[1] / "src"

# Required keys of the web contract (web/lib/types.ts).
HOME_KEYS = {"greeting", "needsYouCount", "dueSoon", "currentMonth", "companies", "handledPeriodLabel", "handled",
             "connections"}
COMPANY_KEYS = {"id", "name", "legalName", "taxId", "tone", "statusLabel", "detail", "currentMonth", "months"}
DUE_KEYS = {"id", "title", "companyName", "due", "note", "tone"}
CONNECTION_KEYS = {"id", "name", "kind", "account", "status", "lastSyncedAt"}
CHOICE_KEYS = {"id", "kind", "tone", "eyebrow", "merchant", "amount", "currency", "date", "question", "options", "why"}
APPROVAL_KEYS = {"id", "kind", "tone", "eyebrow", "merchant", "title", "amount", "currency", "date", "body", "facts",
                 "why", "verification", "keepBlocked"}
VERIFICATION_KEYS = {"optionLabel", "instruction", "checkboxLabel", "confirmLabel", "confirmOptionId",
                     "confirmedMessage"}
MONTH_KEYS = {"companyId", "month", "status", "percentClosed", "transactionsTotal", "stats", "remaining", "matched"}
STATS_KEYS = {"transactionsChecked", "documentsCollected", "missingDocumentsRetrieved", "suppliersChased",
              "accountantQuestionsResolved", "taxObligationsVerified", "unresolvedIssues", "minutesSpent"}
MATCHED_KEYS = {"id", "supplier", "description", "amount", "currency", "date", "reasons"}
ACTIVITY_KINDS = {"collected", "recovered", "chased", "answered", "checked", "closed", "protected", "learned"}
TONES = {"good", "attention", "risk", "neutral"}
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
# Never shown to an owner: ids, internal codes, stack traces (§70).
INTERNAL = re.compile(r"\b(?:ev|tx|doc|item|obl|rule)_[0-9a-f]{6,}|Traceback|Exception|None\b|\bnull\b")


@pytest.fixture(scope="module")
def svc() -> BackOfficeService:
    return BackOfficeService.demo()


@pytest.fixture
def fresh() -> BackOfficeService:
    return BackOfficeService.demo()


def get(s: BackOfficeService, path: str) -> dict:
    status, body = s.dispatch("GET", path, None)
    assert status == 200, body
    json.dumps(body)  # JSON-able, no Decimal / datetime left
    return body


def post(s: BackOfficeService, path: str, body: dict | str | None) -> tuple[int, dict]:
    status, out = s.dispatch("POST", path, body)
    json.dumps(out)
    return status, out


def owner_texts(value) -> list[str]:  # type: ignore[no-untyped-def]
    """Every string an owner may read in a response (ids and keys excluded)."""
    out: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if k in {"id", "href", "companyId", "evidenceIds", "confirmOptionId", "optionId", "currentMonth",
                     "months", "month", "key", "at", "date", "due", "lastSyncedAt", "pendingItemIds", "evidence",
                     "documents", "transactions", "tone", "kind", "status", "currency", "head", "taxId"}:
                continue
            out += owner_texts(v)
    elif isinstance(value, list):
        for v in value:
            out += owner_texts(v)
    elif isinstance(value, str):
        out.append(value)
    return out


def assert_plain(value) -> None:  # type: ignore[no-untyped-def]
    for text in owner_texts(value):
        assert not INTERNAL.search(text), text
        assert not find_jargon(text), (text, find_jargon(text))


# --------------------------------------------------------------------------- reads


def test_home_matches_contract_and_is_computed(svc: BackOfficeService) -> None:
    home = get(svc, "/api/home")
    assert HOME_KEYS <= home.keys()
    assert home["greeting"] == "Good morning."
    assert home["needsYouCount"] == 2  # the IKEA answer and the Vodafone approval
    assert home["currentMonth"]["key"] == "2026-09" and home["currentMonth"]["label"] == "September"
    assert 0 < home["currentMonth"]["percentClosed"] < 100
    for due in home["dueSoon"]:
        assert DUE_KEYS <= due.keys() and ISO_DATE.match(due["due"]) and due["tone"] in TONES
    assert [d["title"] for d in home["dueSoon"]][:1] == ["Vodafone payment"]
    assert home["dueSoon"][0]["tone"] == "risk" and home["dueSoon"][0]["href"] == "/needs-you#nd_vodafone_iban"
    for company in home["companies"]:
        assert COMPANY_KEYS <= company.keys() and company["tone"] in TONES
    for c in home["connections"]:
        assert CONNECTION_KEYS <= c.keys() and c["kind"] in {"email", "bank", "accountant"}
        assert ISO_DATETIME.match(c["lastSyncedAt"])
    labels = {h["label"]: h["count"] for h in home["handled"]}
    assert labels["missing invoice recovered"] == 1  # Adobe, fetched from the email's "View invoice" link
    assert labels["accountant question answered"] == 1
    assert all(isinstance(h["count"], int) and h["count"] > 0 for h in home["handled"])
    assert_plain(home)


def test_companies_statuses(svc: BackOfficeService) -> None:
    companies = {c["id"]: c for c in get(svc, "/api/companies")["companies"]}
    assert set(companies) == {"hazel-tree", "company-b", "company-c"}
    b = companies["company-b"]
    assert (b["statusLabel"], b["tone"], b["detail"]) == ("Closed", "good", "September closed on 1 October")
    c = companies["company-c"]
    assert c["statusLabel"] == "Needs one answer" and c["tone"] == "attention"
    assert c["pendingItemIds"] == ["nd_ikea_418"]
    ht = companies["hazel-tree"]
    assert ht["statusLabel"] == "On track" and ht["detail"].startswith("September · ")
    assert ht["months"][0] == "2026-10"  # October's held Vodafone invoice
    assert get(svc, "/api/companies/company-b")["name"] == "Company B"
    assert svc.dispatch("GET", "/api/companies/nope", None)[0] == 404


def test_needs_you_shapes(svc: BackOfficeService) -> None:
    items = {i["id"]: i for i in get(svc, "/api/needs-you")["items"]}
    assert set(items) == {"nd_ikea_418", "nd_vodafone_iban"}
    ikea = items["nd_ikea_418"]
    assert CHOICE_KEYS <= ikea.keys() and ikea["kind"] == "choice"
    assert ikea["merchant"] == "IKEA" and ikea["amount"] == 418 and ikea["paidWith"] == "card •••• 4817"
    assert {"Hazel Tree", "Company B", "Company C", "Personal"} <= {o["label"] for o in ikea["options"]}
    assert "{choice}" in ikea["remember"]["template"] and ikea["remember"]["defaultChecked"] is True
    assert any("4817" in line for line in ikea["why"])
    voda = items["nd_vodafone_iban"]
    assert APPROVAL_KEYS <= voda.keys() and voda["kind"] == "approval" and voda["tone"] == "risk"
    assert VERIFICATION_KEYS <= voda["verification"].keys()
    assert voda["verification"]["confirmOptionId"] == "confirmed_by_phone"
    assert voda["keepBlocked"]["optionId"] == "keep_blocked"
    assert voda["title"] == "Vodafone changed the IBAN shown on its invoice."
    assert {"label": "On the new invoice", "value": "LT24 •••• 1187", "tone": "risk"} in voda["facts"]
    assert {"label": "Paid until now", "value": "PT50 •••• 7741"} in voda["facts"]
    assert "1187" in voda["verification"]["instruction"]
    assert_plain(items)


def test_activity_feed(svc: BackOfficeService) -> None:
    feed = get(svc, "/api/activity")
    assert feed["today"] == "2026-10-02"
    ats = [i["at"] for i in feed["items"]]
    assert ats == sorted(ats, reverse=True)
    assert {i["kind"] for i in feed["items"]} <= ACTIVITY_KINDS
    assert {"collected", "recovered", "chased", "answered", "checked", "closed", "protected"} <= {
        i["kind"] for i in feed["items"]}
    assert all(ISO_DATETIME.match(i["at"]) for i in feed["items"])
    texts = [i["text"] for i in feed["items"]]
    assert "Asked EDP for the invoice for the €64.10 payment." in texts
    assert "Closed September. Nothing is left open." in texts
    assert_plain(feed)


def test_months(svc: BackOfficeService) -> None:
    b = get(svc, "/api/months/company-b/2026-09")
    assert MONTH_KEYS <= b.keys() and STATS_KEYS <= b["stats"].keys()
    assert (b["status"], b["percentClosed"], b["closedOn"], b["remaining"]) == ("closed", 100, "2026-10-01", [])
    assert b["stats"]["unresolvedIssues"] == 0 and b["transactionsTotal"] == 3
    for m in b["matched"]:
        assert MATCHED_KEYS <= m.keys() and ISO_DATE.match(m["date"]) and m["reasons"]
    ht = get(svc, "/api/months/hazel-tree/2026-09")
    assert ht["status"] == "open" and 0 < ht["percentClosed"] < 100
    assert any("EDP" in r["text"] for r in ht["remaining"])
    assert any(n["tone"] == "risk" and "Vodafone" in n["text"] for n in ht["notices"])
    suppliers = {m["supplier"] for m in ht["matched"]}
    assert {"Marta Gonçalves", "Vodafone", "Uber", "Tax office"} <= suppliers
    c = get(svc, "/api/months/company-c/2026-09")
    assert any(r.get("href") == "/needs-you#nd_ikea_418" for r in c["remaining"])
    assert_plain([b, ht, c])
    assert svc.dispatch("GET", "/api/months/company-b/2026-13", None)[0] == 404
    assert svc.dispatch("GET", "/api/months/nobody/2026-09", None)[0] == 404


@pytest.mark.parametrize(
    ("question", "expect", "evidence_prefix"),
    [
        ("Is September complete?", "Company B is closed", "month:"),
        ("Did we pay Vodafone?", "matched to invoice FT VF2026/1183", "ev_"),
        ("What still needs my attention?", "IKEA", "needs:"),
        ("What did the accountant ask this month?", "Marta Gonçalves", "ev_"),
        ("Show subscriptions that increased.", "Adobe: €54.99 → €59.99", "ev_"),
        ("Find the invoice for the €59.99 payment", "Adobe", "ev_"),
    ],
)
def test_ask_routes_and_cites_evidence(svc: BackOfficeService, question: str, expect: str,
                                       evidence_prefix: str) -> None:
    status, answer = post(svc, "/api/ask", {"question": question})
    assert status == 200
    assert expect in answer["answer"]
    assert answer["evidence"] and all({"label", "id"} == e.keys() for e in answer["evidence"])
    assert answer["evidence"][0]["id"].startswith(evidence_prefix)
    for e in answer["evidence"]:
        if e["id"].startswith("ev_"):
            svc.repo.evidence(e["id"])  # every cited evidence id is stored evidence


def test_ask_vodafone_mentions_the_hold(svc: BackOfficeService) -> None:
    _, answer = post(svc, "/api/ask", {"question": "Did we pay Vodafone?"})
    assert "on hold" in answer["answer"]
    assert {"label": "October payment on hold", "id": "needs:nd_vodafone_iban"} in answer["evidence"]


def test_ask_fallback_and_bad_request(svc: BackOfficeService) -> None:
    status, answer = post(svc, "/api/ask", {"question": "What is the meaning of life"})
    assert status == 200 and answer["evidence"] == [] and "Did we pay Vodafone?" in answer["answer"]
    assert post(svc, "/api/ask", {})[0] == 400
    assert post(svc, "/api/ask", "{not json")[0] == 400


def test_connections(svc: BackOfficeService) -> None:
    conns = get(svc, "/api/connections")["connections"]
    assert {c["id"] for c in conns} == {"gmail", "millennium", "cgd", "accountant"}
    assert all(c["status"] == "healthy" for c in conns)


def test_accountant_clients_and_detail(svc: BackOfficeService) -> None:
    rows = {r["id"]: r for r in get(svc, "/api/accountant/clients")["clients"]}
    assert set(rows) == {"hazel-tree", "company-b", "company-c"}
    for r in rows.values():
        assert {"id", "name", "month", "complete", "missing", "needsAccountant"} == r.keys()
    assert rows["company-b"]["complete"] == 100 and rows["company-b"]["missing"] == 0
    # Needs accountant = what only the accountant decides: Hazel Tree's rent tax flag. The accountant's own
    # IKEA question (Company C) waits for the owner, so it is not the accountant's to settle.
    assert rows["hazel-tree"]["needsAccountant"] == 1 and rows["company-c"]["needsAccountant"] == 0
    detail = get(svc, "/api/accountant/clients/hazel-tree")
    assert {"taxId", "software", "evidence", "anomalies", "taxFlags", "questions", "exportState"} <= detail.keys()
    assert detail["exportState"]["state"] == "partial"
    assert any(q["status"] == "answered" for q in detail["questions"])
    assert any("Vodafone" in a["title"] for a in detail["anomalies"])
    assert svc.dispatch("GET", "/api/accountant/clients/unknown", None)[0] == 404


def test_audit(svc: BackOfficeService) -> None:
    audit = get(svc, "/api/audit")
    assert {"companyName", "periodLabel", "findings"} <= audit.keys()
    ids = {f["id"] for f in audit["findings"]}
    assert {"f_expenses", "f_docs", "f_subs", "f_missing", "f_increased", "f_other"} == ids
    for f in audit["findings"]:
        assert {"id", "value", "label", "tone", "examples"} == f.keys() and f["tone"] in TONES
    assert audit["log"]["intact"] is True and audit["log"]["records"] > 100


def test_routing_errors(svc: BackOfficeService) -> None:
    assert svc.dispatch("GET", "/api/nothing-here", None)[0] == 404
    assert svc.dispatch("DELETE", "/api/home", None)[0] == 405
    assert svc.dispatch("GET", "/healthz", None) == (200, {"ok": True})
    assert svc.dispatch("GET", "/api/home?demo=1", None)[0] == 200
    assert svc.dispatch("GET", "/api/home/", None)[0] == 200


# --------------------------------------------------------------------------- writes


def test_answer_endpoint_validates(fresh: BackOfficeService) -> None:
    assert post(fresh, "/api/needs-you/nd_nope/answer", {"option_id": "x"})[0] == 404
    assert post(fresh, "/api/needs-you/nd_ikea_418/answer", {"option_id": "nonsense"})[0] == 400
    assert post(fresh, "/api/needs-you/nd_ikea_418/answer", {})[0] == 400
    status, body = post(fresh, "/api/needs-you/nd_ikea_418/answer", {"option_id": "hazel-tree", "remember": False})
    assert status == 200 and body["ok"] is True and body["message"].startswith("Done.")
    assert post(fresh, "/api/needs-you/nd_ikea_418/answer", {"option_id": "hazel-tree"})[0] == 409


def test_upload_evidence_runs_the_pipeline(fresh: BackOfficeService) -> None:
    body = {"filename": "edp.txt", "contentType": "text/plain",
            "dataBase64": base64.b64encode(E.EDP_INVOICE).decode()}
    status, out = post(fresh, "/api/evidence", body)
    assert status == 200 and out["ok"] is True
    assert out["message"] == "Got it. It matches the €64.10 payment to EDP on 19 September."
    assert out["documents"][0]["matched"] and out["documents"][0]["verified"]
    month = get(fresh, "/api/months/hazel-tree/2026-09")
    assert not any("EDP" in r["text"] for r in month["remaining"])


def test_upload_image_is_stored_and_reported(fresh: BackOfficeService) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    status, out = post(fresh, "/api/evidence", {"filename": "receipt.png", "contentType": "image/png",
                                                "dataBase64": base64.b64encode(png).decode()})
    assert status == 200 and out["storedOnly"] is True and out["evidenceIds"]
    # The demo has no document reader (the browser build has only pydantic): stored, waiting, said plainly.
    assert out["message"] == "Got it. I stored it. Reading photos and PDFs is switched off in this demo."
    assert post(fresh, "/api/evidence", {"filename": "x.txt"})[0] == 400


def test_upload_ubl_xml(fresh: BackOfficeService) -> None:
    status, out = post(fresh, "/api/evidence", {"filename": "ft.xml", "contentType": "application/xml",
                                                "dataBase64": base64.b64encode(E.VODAFONE_SEPT_UBL).decode()})
    assert status == 200
    assert out["message"] == "Got it. I already had this one."  # the same invoice arrived by email already


def test_upload_receipt_hash_contract(fresh: BackOfficeService) -> None:
    data = E.EDP_INVOICE
    digest = hashlib.sha256(data).hexdigest()
    payload = {"sha256": digest, "dataBase64": base64.b64encode(data).decode(), "filename": "edp.txt",
               "contentType": "text/plain", "client_item_id": "0b7e0e7c-6a55-4a8e-9b1e-2d0f3c9a1f00",
               "source": "mobile_scan", "captured_at": "2026-10-02T09:20:00+01:00"}
    status, receipt = post(fresh, "/api/evidence/upload", payload)
    assert status == 200 and receipt["sha256"] == digest and receipt["delete_local"] is True
    assert receipt["evidence_id"] and receipt["duplicate"] is False
    status, again = post(fresh, "/api/evidence/upload", payload)
    assert status == 200 and again["sha256"] == digest  # a retry gets the same receipt
    bad = {**payload, "client_item_id": "0b7e0e7c-6a55-4a8e-9b1e-2d0f3c9a1f01", "sha256": "0" * 64}
    status, refused = post(fresh, "/api/evidence/upload", bad)
    assert status == 422 and refused["error"] == "hash_mismatch" and refused["delete_local"] is False


def test_share_text_url_and_file(fresh: BackOfficeService) -> None:
    status, out = post(fresh, "/api/share", {"kind": "url", "url": E.ADOBE_INVOICE_URL})
    assert status == 200 and out["message"].startswith("Got it.")
    status, out = post(fresh, "/api/share", {"kind": "url", "url": "https://example.org/invoice/1"})
    assert status == 200 and out["pendingLinks"] == ["https://example.org/invoice/1"]
    status, out = post(fresh, "/api/share", {"kind": "file", "filename": "edp.txt", "mimeType": "text/plain",
                                             "dataBase64": base64.b64encode(E.EDP_INVOICE).decode()})
    assert status == 200 and out["documents"][0]["matched"]
    assert post(fresh, "/api/share", {"kind": "fax"})[0] == 400


def test_accountant_rule(fresh: BackOfficeService) -> None:
    status, out = post(fresh, "/api/accountant/rules", {"text": "Treat all Adobe subscriptions as Software",
                                                        "scope": "client"})
    assert status == 200 and out["ok"] and out["affected"] == 1
    assert out["rule"]["scope"] == "client" and "Adobe" in out["rule"]["label"]
    status, out = post(fresh, "/api/accountant/rules", {"text": "Treat all Uber as Travel", "scope": "all"})
    assert status == 200 and out["rule"]["scope"] == "all_clients_of_accountant" and out["affected"] == 2
    assert post(fresh, "/api/accountant/rules", {"text": "hello"})[0] == 400


def test_stale_connection_endpoint(fresh: BackOfficeService) -> None:
    status, out = post(fresh, "/api/connections/gmail/stale", None)
    assert status == 200 and out["connection"]["status"] == "stale"
    assert out["connection"]["message"].startswith("Gmail needs reconnecting. Your email has not synced since")
    assert get(fresh, "/api/months/company-b/2026-09")["status"] == "open"
    assert post(fresh, "/api/connections/nope/stale", None)[0] == 404
    status, out = post(fresh, "/api/connections/gmail/reconnect", None)
    assert status == 200 and out["connection"]["status"] == "healthy"
    assert get(fresh, "/api/months/company-b/2026-09")["status"] == "closed"


# --------------------------------------------------------------------------- browser build (Pyodide)


def test_service_imports_without_fastapi_httpx_or_temporal() -> None:
    code = ("import sys; sys.modules['fastapi']=None; sys.modules['httpx']=None; sys.modules['temporalio']=None; "
            "sys.modules['starlette']=None; sys.modules['uvicorn']=None; import backoffice.service as s; "
            "svc=s.BackOfficeService.demo(); print(svc.dispatch('GET','/api/home',None)[0]); "
            "bad=[m for m in ('fastapi','httpx','temporalio','starlette','uvicorn') if sys.modules.get(m)]; "
            "print(bad)")
    result = subprocess.run([sys.executable, "-c", code], cwd=SRC, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["200", "[]"]
