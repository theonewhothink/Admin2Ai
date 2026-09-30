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
    assert calls[0]["model"] == "claude-sonnet-5-5" and all(o.status == "draft" for o in svc.assistant.outbox.values())


def test_rule_chat_keeps_the_owners_tasks(svc):
    added = chat(svc, "Remind me to call the accountant on Friday")
    assert added["reply"] == "Added to your tasks for 9 Oct 2026: Call the accountant."  # demo today: Fri 2 Oct
    assert added["cards"][0]["type"] == "tasks"
    chat(svc, "add a task: renew the car insurance by 15 October")
    chat(svc, "Task: check Uber receipts")
    listed = chat(svc, "what are my tasks?")
    assert listed["reply"] == "You have 3 open tasks."
    assert [t["title"] for t in listed["cards"][0]["items"]] == [
        "Call the accountant", "Renew the car insurance", "Check Uber receipts"]  # by due date, undated last
    assert chat(svc, "mark call the accountant as done")["reply"] == "Done: Call the accountant."
    assert chat(svc, "done: something else entirely")["reply"] == "I can't find an open task like that."
    tasks = svc.dispatch("GET", "/api/tasks", None)[1]["tasks"]
    assert [t["status"] for t in tasks] == ["open", "open", "done"]


def test_rule_chat_never_moves_money_and_says_hello(svc):
    pay = chat(svc, "Pay the Vodafone invoice")
    assert pay["reply"].startswith("I don't move money.") and "Vodafone's payment is on hold" in pay["reply"]
    assert pay["cards"][0]["items"][0]["id"] == "needs:nd_vodafone_iban"
    assert chat(svc, "hello")["reply"].startswith("Hello.")


def test_task_endpoints_validate(svc):
    ok = svc.dispatch("POST", "/api/tasks", {"title": "  Send  the lease  ", "due": "2026-10-09", "companyId": "hazel-tree"})
    assert ok[0] == 200 and ok[1]["task"]["title"] == "Send the lease" and ok[1]["task"]["company"] == "Hazel Tree"
    for bad in ({"title": ""}, {"title": "x", "due": "Friday"}, {"title": "x", "companyId": "nope"}):
        assert svc.dispatch("POST", "/api/tasks", bad)[0] == 400
    tid = ok[1]["task"]["id"]
    assert svc.dispatch("POST", f"/api/tasks/{tid}/done", {})[1]["task"]["status"] == "done"
    assert svc.dispatch("POST", "/api/tasks/nope/done", {})[0] == 404


def tool(svc, name, **args):
    status, body = svc.dispatch("POST", "/api/chat/tool", {"name": name, "input": args})
    assert status == 200, body
    return body


def test_browser_chat_gets_instructions_and_every_tool(svc):
    status, body = svc.dispatch("GET", "/api/chat/tools", None)
    assert status == 200 and body["model"] == "claude-sonnet-5-5" and body["today"] == "2026-10-02"
    names = {t["name"] for t in body["tools"]}
    assert {"business_status", "answer_question", "create_task", "draft_email"} <= names
    assert "never move money" in body["system"]
    assert svc.dispatch("POST", "/api/chat/tool", {"name": "rm_rf", "input": {}})[0] == 400


def test_browser_chat_tools_act_within_the_owners_limits(svc):
    status = tool(svc, "business_status")["result"]
    held = {n["question_id"]: n for n in status["needs_owner"]}
    assert held["nd_vodafone_iban"]["owner_must_confirm_in_needs_you"] is True
    assert "options" not in held["nd_vodafone_iban"]  # nothing for the model to pick

    # Changed bank details: never answered from the chat, and nothing changes.
    refused = tool(svc, "answer_question", question_id="nd_vodafone_iban", option_id="confirmed_by_phone")
    assert refused["isError"] and "Needs you" in refused["result"]
    assert refused["cards"][0]["items"][0]["id"] == "needs:nd_vodafone_iban"
    assert any(n["id"] == "nd_vodafone_iban" for n in svc.needs_you()["items"])

    # A plain question the owner answered in the chat: recorded.
    done = tool(svc, "answer_question", question_id="nd_ikea_418", option_id="entity:hazel-tree", remember=True)
    assert not done["isError"] and done["result"]["ok"]
    assert all(n["id"] != "nd_ikea_418" for n in svc.needs_you()["items"])

    task = tool(svc, "create_task", title="Call Vodafone about the new IBAN", due_date="2026-10-05")
    assert task["result"]["due"] == "2026-10-05" and task["cards"][0]["type"] == "tasks"
    assert tool(svc, "create_task", title="x", due_date="Monday")["isError"]
    assert tool(svc, "complete_task", task_id=task["result"]["id"])["result"]["status"] == "done"
    assert tool(svc, "complete_task", task_id="nope")["isError"]

    month = tool(svc, "month_status", company_id="hazel-tree", month="2026-09")["result"]
    assert month["month"] == "2026-09"
    assert tool(svc, "month_status", company_id="nope", month="2026-09")["isError"]
    assert tool(svc, "recent_activity", limit=2)["result"].__len__() == 2
    assert "connections" in tool(svc, "connections_status")["result"]
    draft = tool(svc, "draft_email", to=["m@x.pt"], subject="Hi", body="Hello")
    assert svc.assistant.outbox[draft["result"]["draft_id"]].status == "draft"
    assert tool(svc, "draft_email", to=["not-an-email"], subject="Hi", body="Hello")["isError"]
