"""Shared helpers for the production-server tests (test_server_*.py): a fake clock, apps, sign-up."""

from __future__ import annotations

import base64
import hashlib
import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from backoffice.evidence.store import LocalObjectStore
from backoffice.orchestrator import TZ
from backoffice.server.config import ServerConfig
from backoffice.server.http import build_production_app
from backoffice.server.store import MemoryStore

PASSWORD = "correct horse battery"
NIF_A = "516123459"  # valid check digits (country pack)
NIF_B = "501234560"
NIF_C = "510000002"
ORIGIN = "http://localhost:3000"


@dataclass
class FakeClock:
    """Moves a few seconds on every read, like real time; tests jump it with :meth:`advance`."""

    now_: datetime = field(default_factory=lambda: datetime(2026, 10, 2, 9, 30, tzinfo=TZ))
    step: timedelta = timedelta(seconds=2)

    def __call__(self) -> datetime:
        self.now_ = self.now_ + self.step
        return self.now_

    def advance(self, **kwargs: float) -> None:
        self.now_ = self.now_ + timedelta(**kwargs)


def config(**overrides: Any) -> ServerConfig:
    base: dict[str, Any] = {"allowed_origins": (ORIGIN,), "insecure_cookies": False, "state_key": b"s" * 32,
                            "admin_emails": frozenset({"admin@backoffice.test"})}
    base.update(overrides)
    return ServerConfig(**base)


@dataclass
class Harness:
    app: Any
    client: TestClient
    store: Any
    objects: LocalObjectStore
    clock: FakeClock

    @property
    def manager(self) -> Any:
        return self.app.state.manager


_ADDRESSES = itertools.count(1)


def harness(tmp_path: Path, *, store: Any = None, cfg: ServerConfig | None = None, **services: Any) -> Harness:
    """A production app on a fake clock; each harness calls from its own address (rate limits are per IP)."""
    clock = services.pop("now", None) or FakeClock()
    store = store if store is not None else MemoryStore()
    objects = services.pop("objects", None) or LocalObjectStore(tmp_path / "objects")
    services.setdefault("strict_reads", True)  # a read that changes a tenant fails the test
    app = build_production_app(cfg or config(), store=store, objects=objects, now=clock, **services)
    n = next(_ADDRESSES)
    client = TestClient(app, base_url="https://api.backoffice.test", client=(f"10.{n // 65536 % 256}.{n // 256 % 256}.{n % 256}", 50000))
    return Harness(app, client, store, objects, clock)


def signup(client: TestClient, email: str = "ana@example.pt", *, company: str = "Padaria Lda",
           tax_id: str | None = NIF_A, name: str = "Ana Silva", password: str = PASSWORD) -> dict[str, Any]:
    body: dict[str, Any] = {"email": email, "password": password, "name": name, "companyName": company}
    if tax_id is not None:
        body["taxId"] = tax_id
    res = client.post("/api/auth/signup", json=body)
    assert res.status_code == 201, res.text
    client.cookies.clear()  # tests choose cookie or bearer explicitly
    return res.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Every read the web and mobile apps make, for a tenant with these companies.
def read_paths(company_ids: list[str], document_ids: list[str] = ()) -> list[str]:  # type: ignore[assignment]
    paths = ["/api/home", "/api/needs-you", "/api/activity", "/api/companies", "/api/sources", "/api/audit",
             "/api/pipeline", "/api/documents", "/api/accountant/clients", "/api/connections", "/api/tasks",
             "/api/settings/report", "/api/accountant/api-keys", "/api/chat/tools", "/api/auth/me",
             "/api/documents?q=edp", "/api/documents?from=2026-09-01&to=2026-09-30"]
    for c in company_ids:
        paths += [f"/api/companies/{c}", f"/api/months/{c}/2026-09", f"/api/months/{c}/2026-10",
                  f"/api/accountant/clients/{c}"]
    paths += [f"/api/documents/{d}/file" for d in document_ids]
    return paths


BANK_CSV = (b"date,amount,counterparty,account,description,kind\n"
            b"2026-09-19,-64.10,EDP COMERCIAL,{acct},DD EDP COMERCIAL,direct_debit\n"
            b"2026-09-22,-59.99,ADOBE *CREATIVE CLOUD,{card},COMPRA CARTAO,card\n")


def build_business(h: Harness, token: str) -> dict[str, Any]:
    """Drive a new tenant through most of what an owner does, reading in between (reads must not change state)."""
    from backoffice.demo import evidence as E

    H = bearer(token)
    c = h.client
    seen: dict[str, Any] = {}

    def ok(res: Any, status: int = 200) -> Any:
        assert res.status_code == status, (res.request.url, res.text)
        return res.json() if res.content else None

    def glance() -> None:  # reads between writes, like the apps do
        for p in ("/api/home", "/api/needs-you", "/api/sources", "/api/settings/report", "/api/tasks"):
            ok(c.get(p, headers=H))

    glance()
    ok(c.post("/api/onboarding/company", json={"name": "Second Company", "taxId": NIF_B,
                                               "legalName": "Second Company, Lda."}, headers=H))
    glance()
    ok(c.post("/api/onboarding/accountant", json={"email": "marc@vidal.pt", "name": "Marc Vidal"}, headers=H))
    bank = ok(c.post("/api/sources", json={"kind": "bank", "bank": "Millennium BCP", "companyId": "padaria-lda",
                                           "iban": "PT50000201231234567890154"}, headers=H))
    card = ok(c.post("/api/sources", json={"kind": "card", "bank": "Millennium BCP", "companyId": "padaria-lda",
                                           "last4": "2291"}, headers=H))
    ok(c.post("/api/sources", json={"kind": "supplier", "name": "EDP", "taxId": "503504564",
                                    "email": "faturas@edp.pt"}, headers=H))
    ok(c.post("/api/sources", json={"kind": "email", "provider": "imap", "address": "ana@padaria.pt",
                                    "host": "imap.padaria.pt", "password": "app-password-123"}, headers=H))
    ok(c.post("/api/sources", json={"kind": "insurance", "name": "Fidelidade", "companyId": "padaria-lda",
                                    "renewsOn": "2027-01-15"}, headers=H))
    # A cost center (here an outlet) with the card it pays with: its costs are put on it as they arrive.
    outlet = ok(c.post("/api/companies/padaria-lda/cost-centers", json={
        "name": "Loja Baixa", "kind": "Outlet", "identifiers": {"cards": ["2291"]}}, headers=H))
    seen["cost_centers"] = [outlet["costCenter"]["id"]]
    glance()
    csv = BANK_CSV.replace(b"{acct}", bank["id"].encode()).replace(b"{card}", card["id"].encode())
    rows = ok(c.post("/api/evidence", files={"file": ("extrato.csv", csv, "text/csv")}, headers=H))
    seen["transactions"] = list(rows["transactions"])
    doc = ok(c.post("/api/evidence", files={"file": ("edp.txt", E.EDP_INVOICE, "text/plain")}, headers=H))
    seen["documents"] = [d["id"] for d in doc["documents"]]
    ok(c.post("/api/evidence", json={"filename": "letter.txt", "contentType": "text/plain",
                                     "dataBase64": b64(E.AT_LETTER_HAZEL)}, headers=H))
    scan = b"%PDF-1.7\n% a phone scan\n%%EOF\n"
    ok(c.post("/api/evidence/upload", data={"sha256": sha(scan), "source": "mobile_scan"},
              files={"file": ("scan.pdf", scan, "application/pdf")},
              headers={**H, "Idempotency-Key": "scan-000000000001"}))
    ok(c.post("/api/share", json={"kind": "text", "text": "Obrigado pela visita!"}, headers=H))
    glance()
    ok(c.post("/api/tasks", json={"title": "Call the bank", "due": "2026-10-09"}, headers=H))
    task = ok(c.post("/api/tasks", json={"title": "Renew the lease"}, headers=H))
    ok(c.post(f"/api/tasks/{task['task']['id']}/done", headers=H))
    ok(c.post("/api/chat", json={"message": "remind me to pay the rent tomorrow"}, headers=H))
    ok(c.post("/api/chat", json={"message": "what needs my attention?"}, headers=H))
    ok(c.post("/api/chat/tool", json={"name": "create_task", "input": {"title": "Ask Marc about VAT"}}, headers=H))
    ok(c.post("/api/chat/tool", json={"name": "business_status", "input": {}}, headers=H))
    ok(c.post("/api/ask", json={"question": "Did we pay EDP?"}, headers=H))
    ok(c.post("/api/settings/report", json={"recipients": [{"email": "marc@vidal.pt", "name": "Marc"}],
                                            "day": 5}, headers=H))
    key = ok(c.post("/api/accountant/api-keys", json={"name": "TOConline"}, headers=H))
    seen["api_key"] = key["key"]
    ok(c.post("/api/accountant/rules", json={"text": "Treat all Adobe subscriptions as Software"}, headers=H))
    ok(c.post("/api/connections/mail-ana-padaria-pt/stale", headers=H))
    ok(c.post("/api/connections/mail-ana-padaria-pt/reconnect", headers=H))
    for n in ok(c.get("/api/needs-you", headers=H))["items"]:
        if n["kind"] == "choice":
            ok(c.post(f"/api/needs-you/{n['id']}/answer", json={"optionId": n["options"][0]["id"],
                                                                 "remember": True}, headers=H))
    ok(c.post(f"/api/cost-centers/{seen['cost_centers'][0]}", json={"name": "Loja da Baixa"}, headers=H))
    ok(c.post("/api/cost-centers/allocate", json={"subjectId": seen["documents"][0], "general": True}, headers=H))
    ok(c.post("/api/documents/export", json={"from": "2026-09-01", "to": "2026-09-30"}, headers=H))
    glance()
    return seen
