"""Chat operator, document repository, report delivery settings and the accountant API."""
import base64
import io
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backoffice.api.app import create_app
from backoffice.assistant import ClaudeBrain
from backoffice.service import BackOfficeService


@pytest.fixture()
def svc():
    return BackOfficeService.demo()


def chat(svc, text):
    status, body = svc.dispatch("POST", "/api/chat", {"message": text})
    assert status == 200, body
    return body


def test_find_and_send_invoice_needs_one_tap(svc):
    body = chat(svc, "Find the Vodafone invoice and send it to marc@contabilidadevidal.pt")
    kinds = [c["type"] for c in body["cards"]]
    assert kinds == ["documents", "email"]
    email = body["cards"][1]
    assert email["status"] == "draft" and email["attachments"][0]["kind"] == "document"
    assert svc.assistant.outbox[email["id"]].status == "draft"  # nothing leaves without the owner
    status, sent = svc.dispatch("POST", f"/api/chat/outbox/{email['id']}/send", {})
    assert status == 200 and sent["status"] == "sent"


def test_cancel_draft(svc):
    email = chat(svc, "send a report for September to a@b.pt")["cards"][-1]
    assert svc.dispatch("POST", f"/api/chat/outbox/{email['id']}/send", {"cancel": True})[1]["status"] == "cancelled"
    assert svc.dispatch("POST", f"/api/chat/outbox/{email['id']}/send", {})[1]["status"] == "cancelled"


def test_period_report_counts_only_payments_that_need_invoices(svc):
    body = chat(svc, "Create a report between 1 September 2026 and 30 September 2026")
    report = body["cards"][0]
    assert report["type"] == "report" and report["from"] == "2026-09-01" and report["to"] == "2026-09-30"
    assert report["missingInvoices"] == 1  # bank fees, tax and transfers never count as missing
    f = svc.dispatch("GET", f"/api/reports/{report['id']}/file", None)[1]
    assert "counterparty" in base64.b64decode(f["data"]).decode()


def test_supplier_summary_flags_price_increase_and_states_coverage(svc):
    body = chat(svc, "Summarize all expenses of Adobe in the past 2 years and check for issues")
    s = body["cards"][0]
    assert s["supplier"] == "Adobe" and s["payments"] == 4
    assert any("54.99" in i and "59.99" in i for i in s["issues"])
    assert "only have records from" in s["coverageNote"]


def test_unknown_request_falls_back_to_answers(svc):
    assert "September" in chat(svc, "Did we pay Vodafone?")["reply"]
    assert svc.dispatch("POST", "/api/chat", {"message": " "})[0] == 400


def test_documents_filter_download_and_export(svc):
    all_docs = svc.dispatch("GET", "/api/documents", None)[1]
    ht = svc.dispatch("GET", "/api/documents?company=hazel-tree", None)[1]
    assert 0 < ht["total"] < all_docs["total"]
    doc = ht["items"][0]
    f = svc.dispatch("GET", f"/api/documents/{doc['id']}/file", None)[1]
    assert base64.b64decode(f["data"])
    z = svc.dispatch("POST", "/api/documents/export", {"from": "2026-09-01", "to": "2026-09-30"})[1]
    names = zipfile.ZipFile(io.BytesIO(base64.b64decode(z["data"]))).namelist()
    assert "ledger.csv" in names and "manifest.json" in names
    assert sum(n.startswith("documents/") for n in names) == z["count"]
    assert svc.dispatch("GET", "/api/documents?from=yesterday", None)[0] == 400


def test_report_settings_validate(svc):
    cfg = svc.dispatch("GET", "/api/settings/report", None)[1]
    assert cfg["recipients"][0]["role"] == "Accountant"
    ok = svc.dispatch("POST", "/api/settings/report",
                      {"recipients": [{"email": "Marc@Vidal.pt", "name": "Marc"}, {"email": "cfo@x.pt"}], "day": 5})
    assert ok[0] == 200 and ok[1]["recipients"][0]["email"] == "marc@vidal.pt" and "day 5" in ok[1]["summary"]
    for bad in ({"recipients": [{"email": "nope"}]}, {"day": 31}, {"format": "pdf"}):
        assert svc.dispatch("POST", "/api/settings/report", bad)[0] == 400


def test_accountant_api_requires_key():
    svc = BackOfficeService.demo()
    client = TestClient(create_app(svc))
    assert client.get("/api/v1/documents").status_code == 401
    key = client.post("/api/accountant/api-keys", json={"name": "TOConline"}).json()["key"]
    assert "hash" not in str(client.get("/api/accountant/api-keys").json())
    h = {"Authorization": f"Bearer {key}"}
    docs = client.get("/api/v1/documents", headers=h).json()["items"]
    assert docs and client.get(f"/api/v1/documents/{docs[0]['id']}/file", headers=h).status_code == 200
    r = client.get("/api/v1/export?from=2026-09-01&to=2026-09-30", headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    kid = client.get("/api/accountant/api-keys").json()["keys"][0]["id"]
    client.post(f"/api/accountant/api-keys/{kid}/revoke")
    assert client.get("/api/v1/documents", headers=h).status_code == 401


def test_claude_brain_runs_tools_and_only_drafts(svc):
    """The model's tool calls run against real data; its email is a draft."""
    calls = []

    def block(**kw):
        return SimpleNamespace(**kw)

    turns = [
        SimpleNamespace(stop_reason="tool_use", content=[
            block(type="tool_use", id="t1", name="search_documents", input={"supplier": "Vodafone"})]),
        None,  # filled once we know a document id
        SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="Drafted. Tap Send.")]),
    ]

    class Messages:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) == 2:
                doc_id = svc.assistant.documents(supplier="Vodafone")[0]["id"]
                return SimpleNamespace(stop_reason="tool_use", content=[block(
                    type="tool_use", id="t2", name="draft_email",
                    input={"to": ["m@x.pt"], "subject": "Invoice", "body": "Attached.", "document_ids": [doc_id]})])
            return turns[len(calls) - 1]

    brain = ClaudeBrain(svc.assistant, client=SimpleNamespace(messages=Messages()))
    out = brain.handle("Send the Vodafone invoice to m@x.pt")
    assert out["reply"] == "Drafted. Tap Send."
    assert [c["type"] for c in out["cards"]] == ["documents", "email"]
    assert calls[0]["model"] == "claude-opus-5" and all(o.status == "draft" for o in svc.assistant.outbox.values())
