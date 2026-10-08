"""The production HTTP API: hardening, isolation between businesses, GDPR export and erasure,
devices, the accountant API, mailbox and bank connections (§25, §28, §42, §47, §52)."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from _server_support import NIF_B, PASSWORD, b64, bearer, config, harness, sha, signup

from backoffice.api.app import create_app
from backoffice.connectors.authorize import PROVIDERS, OAuthApp, OAuthAuthorizer
from backoffice.connectors.open_banking import BankAccountInfo, BankConsent, BankLink, ConsentStatus
from backoffice.connectors.vault import LocalKeyProvider, TokenVault
from backoffice.demo import evidence as E
from backoffice.server.config import ConfigError, ServerConfig
from backoffice.server.http import JsonLogFormatter, StoreNonces, route_template

TOKEN = "ExponentPushToken[abcdefghijklmnopqrstuv]"


# --------------------------------------------------------------------------- modes


def test_demo_mode_is_the_default_and_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.delenv("BACKOFFICE_MODE", raising=False)
    client = TestClient(create_app())
    assert client.get("/api/home").status_code == 200  # no sign-in in the demo
    assert client.get("/api/companies").json()["companies"][0]["name"] == "Hazel Tree"
    assert "strict-transport-security" not in client.get("/api/home").headers


def test_production_mode_needs_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKOFFICE_MODE", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        create_app()
    with pytest.raises(ConfigError):
        ServerConfig.from_env({"BACKOFFICE_MODE": "staging"})
    with pytest.raises(ConfigError):
        ServerConfig.from_env({"BACKOFFICE_ALLOWED_ORIGINS": "*"})
    with pytest.raises(ConfigError):
        ServerConfig.from_env({"BACKOFFICE_STATE_KEY": "too-short"})
    cfg = ServerConfig.from_env({"BACKOFFICE_MODE": "production",
                                 "BACKOFFICE_ALLOWED_ORIGINS": "https://app.example.com, http://localhost:3000",
                                 "BACKOFFICE_ADMIN_EMAILS": "A@x.pt", "DATABASE_URL": "postgresql://u:p@h/db"})
    assert cfg.allowed_origins == ("https://app.example.com", "http://localhost:3000")
    assert cfg.admin_emails == frozenset({"a@x.pt"}) and cfg.mode == "production"
    assert "p@h" not in repr(cfg)  # secrets stay out of reprs and logs


# --------------------------------------------------------------------------- hardening


def test_security_headers_on_every_response(tmp_path: Path) -> None:
    h = harness(tmp_path)
    for res in (h.client.get("/healthz"), h.client.get("/api/home"), h.client.get("/nope"),
                h.client.post("/api/auth/login", json={"email": "a@b.pt", "password": "x" * 12})):
        assert res.headers["strict-transport-security"].startswith("max-age=")
        assert res.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in res.headers["content-security-policy"]
        assert res.headers["content-security-policy"].startswith("default-src 'none'")
        assert res.headers["referrer-policy"] == "no-referrer"
        assert res.headers["x-frame-options"] == "DENY"
        assert res.headers["cache-control"] == "no-store"
        assert len(res.headers["x-request-id"]) == 16


def test_errors_are_plain_json_never_stack_traces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness(tmp_path)
    token = signup(h.client)["token"]
    assert h.client.get("/nope").json() == {"error": "not_found", "message": "I can't find that."}
    assert h.client.get("/api/home").json() == {"error": "unauthorized", "message": "Please sign in."}

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("secret internal detail: ana@example.pt")

    monkeypatch.setattr(h.manager, "view", explode)
    res = h.client.get("/api/home", headers=bearer(token))
    assert res.status_code == 500
    assert res.json() == {"error": "server_error", "message": "Something went wrong on our side. Please try again."}
    assert "secret" not in res.text and "Traceback" not in res.text


def test_database_outage_is_a_calm_503(tmp_path: Path) -> None:
    h = harness(tmp_path)
    token = signup(h.client)["token"]
    h.store.available = False
    res = h.client.get("/api/home", headers=bearer(token))
    assert res.status_code == 503 and res.headers["retry-after"] == "30"
    assert res.json()["message"] == "I can't reach your data right now. Please try again in a minute."
    assert h.client.get("/healthz").status_code == 200  # the process is up
    assert h.client.get("/readyz").status_code == 503  # but not ready
    h.store.available = True
    assert h.client.get("/readyz").json() == {"ok": True}


def test_request_size_limits(tmp_path: Path) -> None:
    h = harness(tmp_path, cfg=config(max_json_bytes=64 * 1024))
    token = signup(h.client)["token"]
    big = {"title": "x" * (70 * 1024)}
    res = h.client.post("/api/tasks", json=big, headers=bearer(token))
    assert res.status_code == 413 and res.json()["error"] == "too_large"
    # uploads have their own, larger limit
    data = b"%PDF-1.7\n" + b"0" * (100 * 1024) + b"\n%%EOF\n"
    res = h.client.post("/api/evidence/upload", data={"sha256": sha(data)},
                        files={"file": ("big.pdf", data, "application/pdf")}, headers=bearer(token))
    assert res.status_code == 200, res.text


def test_one_json_log_line_per_request_without_personal_data(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    h = harness(tmp_path)
    caplog.set_level(logging.INFO, logger="backoffice.http")
    account = signup(h.client, "private.person@example.pt")
    h.client.post("/api/sources/mail-private-person-example-pt/remove", headers=bearer(account["token"]))
    lines = [JsonLogFormatter().format(r) for r in caplog.records if r.getMessage() == "request"]
    assert lines
    for line in lines:
        record = json.loads(line)
        assert {"ts", "level", "request_id", "method", "route", "status", "ms"} <= set(record)
        assert "private" not in line and "example" not in line and PASSWORD not in line
    routes = {json.loads(line)["route"] for line in lines}
    assert "/api/sources/{id}/remove" in routes and "/api/auth/signup" in routes
    assert route_template("/api/documents/doc_1/file") == "/api/documents/{id}/file"


# --------------------------------------------------------------------------- isolation (§52)


def test_one_business_can_never_read_another(tmp_path: Path) -> None:
    h = harness(tmp_path)
    a = signup(h.client, "ana@example.pt")
    b = signup(h.client, "bruno@example.pt", company="Bruno Studio", tax_id=NIF_B)
    A, B = bearer(a["token"]), bearer(b["token"])
    doc = h.client.post("/api/evidence", json={"filename": "edp.txt", "contentType": "text/plain",
                                               "dataBase64": b64(E.EDP_INVOICE)}, headers=B).json()
    b_doc = doc["documents"][0]["id"]
    h.client.post("/api/tasks", json={"title": "Bruno's secret task"}, headers=B)
    assert [c["name"] for c in h.client.get("/api/companies", headers=A).json()["companies"]] == ["Padaria Lda"]
    assert h.client.get("/api/documents", headers=A).json()["items"] == []
    assert h.client.get(f"/api/documents/{b_doc}/file", headers=A).status_code == 404
    assert h.client.get("/api/tasks", headers=A).json()["tasks"] == []
    assert h.client.get("/api/companies/bruno-studio", headers=A).status_code == 404
    assert h.client.get(f"/api/documents/{b_doc}/file", headers=B).status_code == 200
    # A's accountant key opens A's documents only
    key = h.client.post("/api/accountant/api-keys", json={"name": "TOConline"}, headers=A).json()["key"]
    assert h.client.get("/api/v1/documents", headers=bearer(key)).json()["items"] == []
    assert h.client.get(f"/api/v1/documents/{b_doc}/file", headers=bearer(key)).status_code == 404
    # the tenant a session belongs to is fixed server-side: nothing in the request can change it
    me = h.client.get("/api/auth/me", headers={**A, "X-Tenant": b["tenant"]["id"]}).json()
    assert me["tenant"]["id"] == a["tenant"]["id"]


# --------------------------------------------------------------------------- GDPR


def test_export_holds_every_event_document_and_file(tmp_path: Path) -> None:
    h = harness(tmp_path)
    account = signup(h.client)
    H = bearer(account["token"])
    h.client.post("/api/evidence", files={"file": ("edp.txt", E.EDP_INVOICE, "text/plain")}, headers=H)
    h.client.post("/api/tasks", json={"title": "Call the bank"}, headers=H)
    res = h.client.get("/api/account/export", headers=H)
    assert res.status_code == 200 and res.headers["content-type"] == "application/zip"
    assert "attachment" in res.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(res.content))
    names = set(z.namelist())
    assert {"README.txt", "account.json", "events.jsonl", "documents.json", f"files/{sha(E.EDP_INVOICE)}"} <= names
    assert z.read(f"files/{sha(E.EDP_INVOICE)}") == E.EDP_INVOICE
    events = z.read("events.jsonl").decode().splitlines()
    assert len(events) == len(h.store.events(account["tenant"]["id"]))
    assert json.loads(z.read("account.json"))["user"]["email"] == "ana@example.pt"
    docs = json.loads(z.read("documents.json"))["documents"]
    assert docs and docs[0]["number"] == "FT EDP2026/558120"


def test_delete_needs_confirmation_and_the_password_then_erases_everything(tmp_path: Path) -> None:
    h = harness(tmp_path)
    account = signup(h.client)
    tenant, H = account["tenant"]["id"], bearer(account["token"])
    h.client.post("/api/evidence", files={"file": ("edp.txt", E.EDP_INVOICE, "text/plain")}, headers=H)
    h.client.post("/api/devices", json={"expoPushToken": TOKEN, "platform": "ios"}, headers=H)
    assert (h.objects._root / tenant).is_dir()
    assert h.client.post("/api/account/delete", json={"password": PASSWORD}, headers=H).status_code == 400
    wrong = h.client.post("/api/account/delete", json={"confirm": "DELETE", "password": "not my password"}, headers=H)
    assert wrong.status_code == 401 and wrong.json()["message"] == "That password is not right."
    res = h.client.post("/api/account/delete", json={"confirm": "DELETE", "password": PASSWORD}, headers=H)
    assert res.status_code == 202 and res.json()["ok"] is True
    assert f'{"a2a_session"}=""' in res.headers["set-cookie"]
    assert h.store.tenant_ids() == [] and h.store.user_ids() == [] and h.store.events(tenant) == []
    assert h.store.devices(tenant) == []
    assert not (h.objects._root / tenant).exists()
    erasure = h.store.erasure(tenant)
    assert erasure.events_erased >= 3 and erasure.objects_purged_at is not None
    assert h.client.get("/api/home", headers=H).status_code == 401
    assert h.client.post("/api/auth/login", json={"email": "ana@example.pt", "password": PASSWORD}).status_code == 401


# --------------------------------------------------------------------------- onboarding and devices


def test_onboarding_company_and_accountant(tmp_path: Path) -> None:
    h = harness(tmp_path)
    H = bearer(signup(h.client, tax_id=None)["token"])
    res = h.client.post("/api/onboarding/company", json={"name": "Studio Two", "taxId": NIF_B,
                                                         "legalName": "Studio Two, Unipessoal Lda."}, headers=H)
    assert res.status_code == 200 and res.json()["company"]["legalName"] == "Studio Two, Unipessoal Lda."
    dup = h.client.post("/api/onboarding/company", json={"name": "Again", "taxId": NIF_B}, headers=H)
    assert dup.status_code == 409
    bad = h.client.post("/api/onboarding/company", json={"name": "Bad", "taxId": "999999999"}, headers=H)
    assert bad.status_code == 400 and bad.json()["message"].startswith("That NIF")
    none = h.client.post("/api/onboarding/company", json={"name": "No NIF"}, headers=H)
    assert none.status_code == 400 and none.json()["message"] == "I need the company's NIF. It has 9 digits."
    acct = h.client.post("/api/onboarding/accountant", json={"email": "Marc@Vidal.pt", "name": "Marc Vidal"}, headers=H)
    assert acct.status_code == 200 and acct.json()["accountant"]["email"] == "marc@vidal.pt"
    report = h.client.get("/api/settings/report", headers=H).json()
    assert report["recipients"] == [{"email": "marc@vidal.pt", "name": "Marc Vidal", "role": "Accountant"}]
    names = [c["name"] for c in h.client.get("/api/accountant/clients", headers=H).json()["clients"]]
    assert sorted(names) == ["Padaria Lda", "Studio Two, Unipessoal Lda."]


def test_devices_register_and_remove(tmp_path: Path) -> None:
    h = harness(tmp_path)
    account = signup(h.client)
    H, tenant = bearer(account["token"]), account["tenant"]["id"]
    assert h.client.post("/api/devices", json={"expoPushToken": TOKEN, "platform": "ios"}, headers=H).status_code == 204
    assert h.client.post("/api/devices", json={"expoPushToken": TOKEN, "platform": "android"},
                         headers=H).status_code == 204
    assert [(d.token, d.platform) for d in h.store.devices(tenant)] == [(TOKEN, "android")]
    for bad in ({"expoPushToken": "nope", "platform": "ios"}, {"expoPushToken": TOKEN, "platform": "web"}):
        assert h.client.post("/api/devices", json=bad, headers=H).status_code == 400
    assert h.client.post("/api/devices/remove", json={"expoPushToken": TOKEN}, headers=H).status_code == 204
    assert h.store.devices(tenant) == []


def test_accountant_api_keys_work_until_revoked(tmp_path: Path) -> None:
    h = harness(tmp_path)
    H = bearer(signup(h.client)["token"])
    h.client.post("/api/evidence", files={"file": ("edp.txt", E.EDP_INVOICE, "text/plain")}, headers=H)
    created = h.client.post("/api/accountant/api-keys", json={"name": "TOConline"}, headers=H).json()
    K = bearer(created["key"])
    docs = h.client.get("/api/v1/documents", headers=K).json()["items"]
    assert len(docs) == 1
    assert h.client.get(f"/api/v1/documents/{docs[0]['id']}/file", headers=K).content == E.EDP_INVOICE
    zipped = h.client.get("/api/v1/export?from=2026-09-01&to=2026-09-30", headers=K)
    assert zipped.status_code == 200 and zipped.headers["content-type"] == "application/zip"
    assert h.client.get("/api/v1/documents", headers=bearer("bo_live_" + "x" * 32)).status_code == 401
    assert h.client.get("/api/v1/documents").status_code == 401
    h.client.post(f"/api/accountant/api-keys/{created['id']}/revoke", headers=H)
    assert h.client.get("/api/v1/documents", headers=K).status_code == 401
    listed = h.client.get("/api/accountant/api-keys", headers=H).json()
    assert listed == {"keys": []} and "hash" not in json.dumps(listed)


# --------------------------------------------------------------------------- mailbox sign-in (OAuth)


def _id_token(email: str) -> str:
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{part({'alg': 'RS256'})}.{part({'email': email, 'sub': '123'})}.sig"


def _oauth(tmp_path: Path, store_holder: dict) -> tuple:
    vault = TokenVault(LocalKeyProvider(os.urandom(32)))
    exchanged: list[str] = []

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        exchanged.append(request.content.decode())
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "refresh-1", "expires_in": 3600,
                                         "id_token": _id_token("ana@gmail.com")})

    from backoffice.server.store import MemoryStore

    store = MemoryStore()
    store_holder["store"] = store
    clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    app = OAuthApp(provider="google", client_id="cid", client_secret="sec", **PROVIDERS["google"])
    authorizer = OAuthAuthorizer({"google": app}, vault, redirect_uri="https://api.backoffice.test/api/oauth/callback",
                                 state_key=b"s" * 32, http=httpx.Client(transport=httpx.MockTransport(token_endpoint)),
                                 nonces=StoreNonces(store, clock))
    return vault, authorizer, exchanged


def test_mailbox_sign_in_without_an_address(tmp_path: Path) -> None:
    holder: dict = {}
    vault, authorizer, exchanged = _oauth(tmp_path, holder)
    h = harness(tmp_path, store=holder["store"], vault=vault, authorizer=authorizer)
    account = signup(h.client)
    H, tenant = bearer(account["token"]), account["tenant"]["id"]
    start = h.client.get("/api/oauth/start?provider=google", headers=H, follow_redirects=False)
    assert start.status_code == 302 and start.headers["location"].startswith("https://accounts.google.com/")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    back = h.client.get("/api/oauth/callback", params={"code": "c1", "state": state}, follow_redirects=False)
    assert back.status_code == 302 and back.headers["location"].endswith("/sources/?signin=done")
    assert "code_verifier=" in exchanged[0]
    email = next(g for g in h.client.get("/api/sources", headers=H).json()["groups"] if g["id"] == "email")
    assert email["items"][0]["name"] == "ana@gmail.com" and email["items"][0]["status"] == "healthy"
    assert email["items"][0]["signIn"] == "Signed in. Access renews automatically; no need to reconnect."
    assert vault.open(tenant, email["items"][0]["id"])["refresh_token"] == "refresh-1"
    again = h.client.get("/api/oauth/callback", params={"code": "c1", "state": state}, follow_redirects=False)
    assert again.headers["location"].endswith("?signin=failed")  # a sign-in finishes once
    # rebuilt from its events, the mailbox is still there and signed in
    h.manager.evict(tenant)
    email2 = next(g for g in h.client.get("/api/sources", headers=H).json()["groups"] if g["id"] == "email")
    assert email2 == email


def test_mailbox_sign_in_for_a_named_address(tmp_path: Path) -> None:
    holder: dict = {}
    vault, authorizer, _ = _oauth(tmp_path, holder)
    h = harness(tmp_path, store=holder["store"], vault=vault, authorizer=authorizer)
    account = signup(h.client)
    H = bearer(account["token"])
    start = h.client.get("/api/oauth/start?provider=google&address=ana@padaria.pt", headers=H, follow_redirects=False)
    assert start.status_code == 302 and "login_hint=ana%40padaria.pt" in start.headers["location"]
    pending = next(g for g in h.client.get("/api/sources", headers=H).json()["groups"] if g["id"] == "email")
    assert pending["items"][0]["signIn"] == "Waiting for you to finish signing in."
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    h.client.get("/api/oauth/callback", params={"code": "c2", "state": state}, follow_redirects=False)
    done = next(g for g in h.client.get("/api/sources", headers=H).json()["groups"] if g["id"] == "email")
    assert done["items"][0]["status"] == "healthy" and done["items"][0]["id"] == "mail-ana-padaria-pt"
    assert h.client.get("/api/oauth/start?provider=yahoo", headers=H, follow_redirects=False).status_code == 400


def test_mailbox_sign_in_is_off_until_configured(tmp_path: Path) -> None:
    h = harness(tmp_path)
    H = bearer(signup(h.client)["token"])
    res = h.client.get("/api/oauth/start?provider=microsoft", headers=H, follow_redirects=False)
    assert res.status_code == 503 and res.json()["message"] == "Microsoft sign-in is not set up on this server yet."


# --------------------------------------------------------------------------- bank link (GoCardless)


class FakeGoCardless:
    def __init__(self) -> None:
        self.links: list[dict] = []

    def create_link(self, **kwargs: object) -> BankLink:
        self.links.append(dict(kwargs))
        return BankLink("req-1", "https://ob.gocardless.com/psd2/start/req-1", "agr-1")

    def consent(self, requisition_id: str) -> BankConsent:
        assert requisition_id == "req-1"
        return BankConsent("req-1", ConsentStatus.ACTIVE, ("acc-1",), "MILLENNIUMBCP_BCOMPTPL",
                           datetime(2027, 3, 29, tzinfo=timezone.utc), 90)

    def account(self, account_id: str) -> BankAccountInfo:
        return BankAccountInfo(account_id, iban="PT50000201231234567890154", currency="EUR")


def test_bank_link_through_gocardless(tmp_path: Path) -> None:
    vault = TokenVault(LocalKeyProvider(os.urandom(32)))
    fake = FakeGoCardless()
    h = harness(tmp_path, vault=vault, aggregator=lambda: fake)
    account = signup(h.client)
    H, tenant = bearer(account["token"]), account["tenant"]["id"]
    assert h.client.post("/api/connections/bank/start", json={"institutionId": "x y"}, headers=H).status_code == 400
    res = h.client.post("/api/connections/bank/start", json={"institutionId": "MILLENNIUMBCP_BCOMPTPL"}, headers=H)
    assert res.json() == {"redirectUrl": "https://ob.gocardless.com/psd2/start/req-1"}
    link = fake.links[0]
    assert link["redirect_url"].endswith("/api/connections/bank/callback")
    assert link["history_days"] == 90 and link["access_days"] == 180
    back = h.client.get("/api/connections/bank/callback", params={"ref": link["reference"]}, follow_redirects=False)
    assert back.status_code == 302 and back.headers["location"].endswith("/sources/?bank=done")
    banks = next(g for g in h.client.get("/api/sources", headers=H).json()["groups"] if g["id"] == "banks")
    assert banks["items"][0]["name"] == "Millenniumbcp •••• 0154" and banks["items"][0]["company"] == "Padaria Lda"
    assert vault.open(tenant, "bank-millenniumbcp")["requisition_id"] == "req-1"
    forged = h.client.get("/api/connections/bank/callback", params={"ref": link["reference"][:-3] + "abc"},
                          follow_redirects=False)
    assert forged.headers["location"].endswith("?bank=failed")


def test_bank_link_is_off_until_configured(tmp_path: Path) -> None:
    h = harness(tmp_path)
    H = bearer(signup(h.client)["token"])
    res = h.client.post("/api/connections/bank/start", json={"institutionId": "MILLENNIUMBCP_BCOMPTPL"}, headers=H)
    assert res.status_code == 503 and "not set up" in res.json()["message"]


def test_uploads_keep_the_mobile_contract(tmp_path: Path) -> None:
    h = harness(tmp_path)
    H = bearer(signup(h.client)["token"])
    data = b"%PDF-1.7\n%%EOF\n"
    bad = h.client.post("/api/evidence/upload", data={"sha256": "0" * 64, "source": "mobile_scan"},
                        files={"file": ("scan.pdf", data, "application/pdf")},
                        headers={**H, "Idempotency-Key": "scan-000000000009"})
    assert bad.status_code == 422 and bad.json()["error"] == "hash_mismatch"
    good = h.client.post("/api/evidence/upload", data={"sha256": sha(data), "source": "mobile_scan"},
                         files={"file": ("scan.pdf", data, "application/pdf")},
                         headers={**H, "Idempotency-Key": "scan-000000000010"})
    assert good.status_code == 200 and good.json()["sha256"] == sha(data) and good.json()["delete_local"] is True
    again = h.client.post("/api/evidence/upload", data={"sha256": sha(data), "source": "mobile_scan"},
                          files={"file": ("scan.pdf", data, "application/pdf")},
                          headers={**H, "Idempotency-Key": "scan-000000000010"})
    assert again.status_code == 200 and again.json()["evidence_id"] == good.json()["evidence_id"]  # idempotent


def test_time_is_real_in_production(tmp_path: Path) -> None:
    h = harness(tmp_path)
    H = bearer(signup(h.client)["token"])
    first = h.client.get("/api/activity", headers=H).json()["today"]
    h.clock.advance(days=20)
    h.client.get("/api/home", headers=H)
    h.clock.advance(days=20)
    home = h.client.get("/api/home", headers=H).json()
    assert h.client.get("/api/activity", headers=H).json()["today"] == (
        datetime.fromisoformat(first) + timedelta(days=40)).date().isoformat()
    assert home["currentMonth"]["key"] == "2026-10"  # the month being closed moved on with the calendar
