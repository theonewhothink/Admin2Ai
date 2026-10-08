"""FastAPI wrapper: every endpoint over HTTP, CORS, multipart uploads (§43, §44)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backoffice.api.app import ALLOWED_ORIGINS, create_app
from backoffice.demo import evidence as E
from backoffice.service import BackOfficeService

MOBILE_FIXTURE = Path(__file__).resolve().parents[2] / "mobile" / "contracts" / "fixtures" / "evidence-upload.multipart"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(BackOfficeService.demo()))


@pytest.fixture
def fresh() -> TestClient:
    return TestClient(create_app(BackOfficeService.demo()))


def test_healthz(client: TestClient) -> None:
    res = client.get("/healthz")
    assert res.status_code == 200 and res.json() == {"ok": True}


@pytest.mark.parametrize("path", [
    "/api/home", "/api/needs-you", "/api/activity", "/api/companies", "/api/companies/hazel-tree",
    "/api/months/hazel-tree/2026-09", "/api/connections", "/api/accountant/clients",
    "/api/accountant/clients/company-c", "/api/audit",
])
def test_every_read_endpoint(client: TestClient, path: str) -> None:
    res = client.get(path)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("application/json")
    assert res.json()


def test_home_over_http_equals_the_service(client: TestClient) -> None:
    service: BackOfficeService = client.app.state.service  # type: ignore[attr-defined]
    assert client.get("/api/home").json() == service.dispatch("GET", "/api/home", None)[1]


def test_unknown_and_wrong_method(client: TestClient) -> None:
    assert client.get("/api/nope").status_code == 404
    assert client.get("/api/months/hazel-tree/1999-99").status_code == 404
    res = client.post("/api/home", json={})
    assert res.status_code == 405 and res.json()["message"]


def test_ask(client: TestClient) -> None:
    res = client.post("/api/ask", json={"question": "Did we pay Vodafone?"})
    assert res.status_code == 200
    body = res.json()
    assert "FT VF2026/1183" in body["answer"] and body["evidence"]
    assert client.post("/api/ask", content=b"not json", headers={"content-type": "application/json"}).status_code == 400


def test_answer_and_learning(fresh: TestClient) -> None:
    res = fresh.post("/api/needs-you/nd_ikea_418/answer", json={"option_id": "entity:hazel-tree", "remember": True})
    assert res.status_code == 200 and res.json()["ok"] is True
    assert res.json()["learned"].startswith("Always use Hazel Tree for IKEA")
    ids = [i["id"] for i in fresh.get("/api/needs-you").json()["items"]]
    assert ids == ["nd_vodafone_iban"]
    assert fresh.post("/api/needs-you/nd_ikea_418/answer", json={"option_id": "entity:hazel-tree"}).status_code == 409


def test_vodafone_keep_blocked(fresh: TestClient) -> None:
    res = fresh.post("/api/needs-you/nd_vodafone_iban/answer", json={"option_id": "keep_blocked", "remember": False})
    assert res.status_code == 200 and "stays blocked" in res.json()["message"]
    home = fresh.get("/api/home").json()
    assert home["needsYouCount"] == 1


def test_multipart_evidence_upload_runs_the_pipeline(fresh: TestClient) -> None:
    res = fresh.post("/api/evidence", files={"file": ("edp.txt", E.EDP_INVOICE, "text/plain")})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["documents"][0]["matched"]
    assert body["message"].startswith("Got it. It matches the €64.10 payment to EDP")


def test_multipart_eml_upload(fresh: TestClient) -> None:
    from datetime import datetime

    from backoffice.orchestrator import TZ

    raw = E.email(sender="EDP <faturas@edp.pt>", subject="Fatura EDP", at=datetime(2026, 10, 2, 9, 0, tzinfo=TZ),
                  text="Segue a fatura pedida.\n", message_id="<edp-reply@edp.pt>",
                  attachments=(("FT_EDP2026_558120.txt", "text/plain", E.EDP_INVOICE),))
    res = fresh.post("/api/evidence", files={"file": ("reply.eml", raw, "message/rfc822")})
    assert res.status_code == 200 and res.json()["documents"][0]["matched"]


def test_mobile_offline_upload_contract(fresh: TestClient) -> None:
    """The phone's golden multipart request (mobile/contracts) gets a receipt with the server's hash."""
    raw = MOBILE_FIXTURE.read_bytes()
    boundary = raw.split(b"\r\n", 1)[0][2:].decode()
    res = fresh.post("/api/evidence/upload", content=raw,
                     headers={"content-type": f"multipart/form-data; boundary={boundary}",
                              "Idempotency-Key": "0b7e0e7c-6a55-4a8e-9b1e-2d0f3c9a1f00"})
    assert res.status_code == 200, res.text
    receipt = res.json()
    assert receipt["sha256"] == "ee2d14b09b1f174f566d039e4d1c5d6e250feb812d9aa4ca2e595550a2548461"
    assert receipt["evidence_id"] and receipt["delete_local"] is True and receipt["duplicate"] is False


def test_mobile_upload_hash_mismatch(fresh: TestClient) -> None:
    data = b"%PDF-1.7\n%%EOF\n"
    res = fresh.post("/api/evidence/upload", data={"sha256": "0" * 64, "source": "mobile_scan"},
                     files={"file": ("scan.pdf", data, "application/pdf")},
                     headers={"Idempotency-Key": "scan-000000000001"})
    assert res.status_code == 422 and res.json()["error"] == "hash_mismatch"
    good = hashlib.sha256(data).hexdigest()
    res = fresh.post("/api/evidence/upload", data={"sha256": good, "source": "mobile_scan"},
                     files={"file": ("scan.pdf", data, "application/pdf")},
                     headers={"Idempotency-Key": "scan-000000000002"})
    assert res.status_code == 200 and res.json()["sha256"] == good


def test_share_and_rules_and_connections(fresh: TestClient) -> None:
    assert fresh.post("/api/share", json={"kind": "text", "text": "Obrigado pela visita!"}).status_code == 200
    res = fresh.post("/api/accountant/rules", json={"text": "Treat all Adobe subscriptions as Software"})
    assert res.status_code == 200 and res.json()["affected"] == 1
    res = fresh.post("/api/connections/gmail/stale")
    assert res.status_code == 200 and res.json()["connection"]["status"] == "stale"
    assert fresh.get("/api/months/company-b/2026-09").json()["status"] == "open"


@pytest.mark.parametrize("origin", ALLOWED_ORIGINS)
def test_cors_allows_the_web_app(client: TestClient, origin: str) -> None:
    res = client.options("/api/home", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == origin
    assert client.get("/api/home", headers={"Origin": origin}).headers["access-control-allow-origin"] == origin


def test_cors_refuses_other_origins(client: TestClient) -> None:
    res = client.get("/api/home", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in res.headers
    assert {"http://localhost:3000", "https://theonewhothink.github.io"} <= set(ALLOWED_ORIGINS)
