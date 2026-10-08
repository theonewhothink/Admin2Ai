"""Sign-up, sign-in, sessions, CSRF guard, roles and rate limits of the production API (§52)."""

from __future__ import annotations

from pathlib import Path

import pytest
from _server_support import NIF_A, NIF_B, ORIGIN, PASSWORD, bearer, harness, signup

from backoffice.server import passwords
from backoffice.server.auth import COOKIE_NAME, hash_token
from backoffice.server.passwords import PasswordRejected, hash_password, verify_password

WRONG = {"error": "unauthorized", "message": "Email or password is not right."}


# --------------------------------------------------------------------------- passwords


def test_password_hash_is_scrypt_with_the_contract_parameters() -> None:
    stored = hash_password(PASSWORD)
    scheme, n, r, p, salt, digest = stored.split("$")
    assert (scheme, n, r, p) == ("scrypt", "32768", "8", "1")
    assert len(salt) == 22 and len(digest) == 43  # 16-byte salt, 32-byte key, base64url
    assert PASSWORD not in stored
    assert verify_password(PASSWORD, stored)
    assert not verify_password(PASSWORD + "x", stored)
    assert hash_password(PASSWORD) != stored  # a fresh salt every time


@pytest.mark.parametrize("bad", ["short", "", None, 12345, "x" * 257, "ten chars\x00"])
def test_password_rules(bad: object) -> None:
    with pytest.raises(PasswordRejected):
        hash_password(bad)  # type: ignore[arg-type]


def test_verify_never_raises_on_garbage() -> None:
    for stored in ("", "plain", "scrypt$x$y$z$a$b", "md5$1$1$1$a$b", "scrypt$99999999$8$1$AA$AA"):
        assert verify_password(PASSWORD, stored) is False
    assert verify_password(None, hash_password(PASSWORD)) is False


# --------------------------------------------------------------------------- sign-up


def test_signup_creates_user_business_company_and_session(tmp_path: Path) -> None:
    h = harness(tmp_path)
    res = h.client.post("/api/auth/signup", json={"email": " Ana@Example.PT ", "password": PASSWORD,
                                                  "name": "Ana Silva", "companyName": "Padaria Lda", "taxId": NIF_A})
    assert res.status_code == 201
    body = res.json()
    assert set(body) == {"user", "tenant", "token"}
    assert body["user"]["email"] == "ana@example.pt" and body["user"]["name"] == "Ana Silva"
    assert body["tenant"]["name"] == "Padaria Lda" and body["user"]["id"].startswith("usr_")
    cookie = res.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}={body['token']}")
    for flag in ("HttpOnly", "Secure", "Path=/", "SameSite=lax", "Max-Age=2592000"):
        assert flag in cookie
    session = h.store.session(hash_token(body["token"]))
    assert session is not None and session.tenant_id == body["tenant"]["id"]
    assert body["token"] not in repr(h.store._d)  # only the SHA-256 is kept
    me = h.client.get("/api/auth/me", headers=bearer(body["token"])).json()
    assert me == {"user": body["user"], "tenant": body["tenant"], "role": "owner"}
    company = h.client.get("/api/companies", headers=bearer(body["token"])).json()["companies"][0]
    assert company["taxId"] == NIF_A and company["name"] == "Padaria Lda"


def test_signup_without_a_tax_id_and_insecure_cookies_for_localhost(tmp_path: Path) -> None:
    from _server_support import config

    h = harness(tmp_path, cfg=config(insecure_cookies=True))
    res = h.client.post("/api/auth/signup", json={"email": "b@example.pt", "password": PASSWORD, "name": "B",
                                                  "companyName": "B Studio"})
    assert res.status_code == 201 and "Secure" not in res.headers["set-cookie"]


@pytest.mark.parametrize(("change", "message"), [
    ({"taxId": "516123450"}, "That NIF doesn't add up. Please check the digits."),
    ({"taxId": "12345"}, "That NIF has 5 digits. It needs 9."),
    ({"password": "short"}, "Use at least 10 characters for your password."),
    ({"email": "not-an-email"}, "That doesn't look like an email address."),
    ({"name": ""}, "What is your name?"),
    ({"companyName": " "}, "What is your company called?"),
])
def test_signup_refuses_bad_input_in_plain_words(tmp_path: Path, change: dict, message: str) -> None:
    h = harness(tmp_path)
    body = {"email": "c@example.pt", "password": PASSWORD, "name": "C", "companyName": "C Lda", "taxId": NIF_A,
            **change}
    res = h.client.post("/api/auth/signup", json=body)
    assert res.status_code == 400 and res.json() == {"error": "bad_request", "message": message}
    assert h.store.tenant_ids() == []


def test_signup_twice_with_one_email(tmp_path: Path) -> None:
    h = harness(tmp_path)
    signup(h.client)
    res = h.client.post("/api/auth/signup", json={"email": "ANA@example.pt", "password": PASSWORD, "name": "X",
                                                  "companyName": "Other", "taxId": NIF_B})
    assert res.status_code == 409 and "already an account" in res.json()["message"]


def test_listed_admin_email_gets_admin(tmp_path: Path) -> None:
    h = harness(tmp_path)
    account = signup(h.client, "admin@backoffice.test")
    assert h.client.get("/api/auth/me", headers=bearer(account["token"])).json()["role"] == "admin"
    other = signup(h.client, "someone@example.pt", tax_id=NIF_B)
    assert h.client.get("/api/auth/me", headers=bearer(other["token"])).json()["role"] == "owner"


def test_auth_endpoints_want_json(tmp_path: Path) -> None:
    h = harness(tmp_path)
    res = h.client.post("/api/auth/login", content=b'{"email":"a@b.pt","password":"xxxxxxxxxxxx"}',
                        headers={"content-type": "text/plain"})
    assert res.status_code == 415 and res.json()["message"] == "Send this as JSON."


# --------------------------------------------------------------------------- sign-in


def test_login_right_and_wrong(tmp_path: Path) -> None:
    h = harness(tmp_path)
    account = signup(h.client)
    res = h.client.post("/api/auth/login", json={"email": "ANA@example.pt", "password": PASSWORD})
    assert res.status_code == 200 and set(res.json()) == {"user", "tenant", "token"}
    assert res.json()["tenant"] == account["tenant"] and res.json()["token"] != account["token"]
    assert COOKIE_NAME in res.headers["set-cookie"]
    wrong = h.client.post("/api/auth/login", json={"email": "ana@example.pt", "password": "wrong password"})
    unknown = h.client.post("/api/auth/login", json={"email": "nobody@example.pt", "password": PASSWORD})
    assert (wrong.status_code, wrong.json()) == (401, WRONG)
    assert (unknown.status_code, unknown.json()) == (401, WRONG)


def test_unknown_email_spends_the_same_hashing_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = passwords.dummy_verify
    monkeypatch.setattr("backoffice.server.auth.dummy_verify", lambda: (calls.append(1), real())[1])
    h = harness(tmp_path)
    h.client.post("/api/auth/login", json={"email": "nobody@example.pt", "password": PASSWORD})
    assert calls == [1]


def test_login_rate_limit_per_email(tmp_path: Path) -> None:
    h = harness(tmp_path)
    signup(h.client)
    from fastapi.testclient import TestClient

    def from_ip(i: int) -> TestClient:  # a different address every time: only the email limit applies
        return TestClient(h.app, base_url="https://api.backoffice.test", client=(f"10.0.0.{i}", 50000))

    codes = [from_ip(i).post("/api/auth/login", json={"email": "ana@example.pt", "password": f"wrong-pass-{i}"}
                             ).status_code for i in range(11)]
    assert codes == [401] * 10 + [429]
    blocked = from_ip(99).post("/api/auth/login", json={"email": "ana@example.pt", "password": PASSWORD})
    assert blocked.status_code == 429
    assert blocked.json() == {"error": "too_many_attempts",
                              "message": "Too many attempts. Please wait 15 minutes and try again."}
    other_email = from_ip(99).post("/api/auth/login", json={"email": "someone@example.pt", "password": PASSWORD})
    assert other_email.status_code == 401  # other people are not locked out
    h.clock.advance(minutes=16)
    assert from_ip(99).post("/api/auth/login", json={"email": "ana@example.pt", "password": PASSWORD}).status_code == 200
    # the database only ever saw keyed hashes, never the email
    assert all("ana" not in subject for subject, _, _ in h.store.all_attempts())


def test_login_rate_limit_per_ip(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    h = harness(tmp_path)
    signup(h.client)
    codes = [h.client.post("/api/auth/login", json={"email": f"user{i}@example.pt", "password": PASSWORD}).status_code
             for i in range(11)]
    assert codes[-1] == 429 and set(codes[:10]) == {401}
    # the right password from the same address waits too; from another address it works
    assert h.client.post("/api/auth/login", json={"email": "ana@example.pt", "password": PASSWORD}).status_code == 429
    elsewhere = TestClient(h.app, base_url="https://api.backoffice.test", client=("192.0.2.7", 50000))
    assert elsewhere.post("/api/auth/login", json={"email": "ana@example.pt", "password": PASSWORD}).status_code == 200


# --------------------------------------------------------------------------- sessions


def test_bearer_and_cookie_sessions(tmp_path: Path) -> None:
    h = harness(tmp_path)
    account = signup(h.client)
    assert h.client.get("/api/home").status_code == 401
    assert h.client.get("/api/home", headers=bearer(account["token"])).status_code == 200
    h.client.cookies.set(COOKIE_NAME, account["token"])
    assert h.client.get("/api/home").status_code == 200
    assert h.client.get("/api/home", headers=bearer("x" * 43)).status_code == 401


def test_sessions_expire_after_30_days_and_slide_while_used(tmp_path: Path) -> None:
    h = harness(tmp_path)
    token = signup(h.client)["token"]
    for _ in range(3):  # used every 20 days: never expires
        h.clock.advance(days=20)
        assert h.client.get("/api/auth/me", headers=bearer(token)).status_code == 200
    session = h.store.session(hash_token(token))
    assert (session.expires_at - session.last_seen_at).days == 30
    h.clock.advance(days=31)
    res = h.client.get("/api/auth/me", headers=bearer(token))
    assert res.status_code == 401 and res.json()["message"] == "Please sign in again."


def test_sliding_session_refreshes_the_cookie(tmp_path: Path) -> None:
    h = harness(tmp_path)
    token = signup(h.client)["token"]
    h.client.cookies.set(COOKIE_NAME, token)
    assert "set-cookie" not in h.client.get("/api/home").headers  # used just now: nothing to refresh
    h.clock.advance(hours=2)
    assert h.client.get("/api/home").headers["set-cookie"].startswith(f"{COOKIE_NAME}={token}")


def test_logout_revokes_and_clears_the_cookie(tmp_path: Path) -> None:
    h = harness(tmp_path)
    token = signup(h.client)["token"]
    res = h.client.post("/api/auth/logout", headers=bearer(token))
    assert res.status_code == 204 and f'{COOKIE_NAME}=""' in res.headers["set-cookie"]
    assert h.store.session(hash_token(token)).revoked_at is not None
    assert h.client.get("/api/home", headers=bearer(token)).status_code == 401


# --------------------------------------------------------------------------- CSRF


def test_cookie_writes_need_the_csrf_header_bearer_writes_do_not(tmp_path: Path) -> None:
    h = harness(tmp_path)
    token = signup(h.client)["token"]
    h.client.cookies.set(COOKIE_NAME, token)
    res = h.client.post("/api/tasks", json={"title": "Call the bank"})
    assert res.status_code == 403 and res.json()["error"] == "forbidden"
    assert h.client.post("/api/tasks", json={"title": "x"}, headers={"X-Requested-With": "other"}).status_code == 403
    assert h.client.post("/api/auth/logout").status_code == 403
    ok = h.client.post("/api/tasks", json={"title": "Call the bank"}, headers={"X-Requested-With": "admin2ai"})
    assert ok.status_code == 200
    assert h.client.get("/api/tasks").status_code == 200  # reads need no header
    h.client.cookies.clear()
    assert h.client.post("/api/tasks", json={"title": "Bearer"}, headers=bearer(token)).status_code == 200
    titles = [t["title"] for t in h.client.get("/api/tasks", headers=bearer(token)).json()["tasks"]]
    assert titles == ["Call the bank", "Bearer"]


# --------------------------------------------------------------------------- roles


def test_accountant_reads_and_teaches_rules_only(tmp_path: Path) -> None:
    h = harness(tmp_path)
    owner = signup(h.client)
    accountant = signup(h.client, "marc@vidal.pt", company="Contabilidade Vidal", tax_id=NIF_B)
    h.store.add_membership(owner["tenant"]["id"], accountant["user"]["id"], "accountant")
    h.store._d.memberships.discard((accountant["tenant"]["id"], accountant["user"]["id"], "owner"))
    login = h.client.post("/api/auth/login", json={"email": "marc@vidal.pt", "password": PASSWORD}).json()
    A = bearer(login["token"])
    assert login["tenant"] == owner["tenant"]
    assert h.client.get("/api/auth/me", headers=A).json()["role"] == "accountant"
    assert h.client.get("/api/home", headers=A).status_code == 200
    assert h.client.get("/api/accountant/clients", headers=A).status_code == 200
    h.client.post("/api/onboarding/accountant", json={"email": "marc@vidal.pt"}, headers=bearer(owner["token"]))
    rule = h.client.post("/api/accountant/rules", json={"text": "Treat all Adobe subscriptions as Software"}, headers=A)
    assert rule.status_code == 200
    assert h.client.post("/api/ask", json={"question": "Is September complete?"}, headers=A).status_code == 200
    for path, body in (("/api/tasks", {"title": "x"}), ("/api/sources", {"kind": "supplier", "name": "X"}),
                       ("/api/onboarding/company", {"name": "X", "taxId": NIF_B}), ("/api/chat", {"message": "hi"}),
                       ("/api/account/delete", {"confirm": "DELETE", "password": PASSWORD}),
                       ("/api/accountant/api-keys", {"name": "x"})):
        res = h.client.post(path, json=body, headers=A)
        assert (res.status_code, res.json()["message"]) == (403, "You don't have access to that."), path
    assert h.client.get("/api/account/export", headers=A).status_code == 403


INTERNAL = ("/api/internal/overview", "/api/internal/operations?limit=5", "/api/internal/readiness")


def test_the_team_dashboard_is_for_admins_only(tmp_path: Path) -> None:
    from backoffice.internal import admin_only
    from backoffice.server.auth import Principal, permitted
    from backoffice.server.store import Tenant, User

    h = harness(tmp_path)
    owner = signup(h.client)
    other = signup(h.client, "rui@oficina.pt", company="Oficina Rui", tax_id=NIF_B, name="Rui")
    accountant = signup(h.client, "marc@vidal.pt", company="Contabilidade Vidal", tax_id=None)
    h.store.add_membership(owner["tenant"]["id"], accountant["user"]["id"], "accountant")
    h.store._d.memberships.discard((accountant["tenant"]["id"], accountant["user"]["id"], "owner"))
    A = bearer(h.client.post("/api/auth/login", json={"email": "marc@vidal.pt", "password": PASSWORD}).json()["token"])
    admin = signup(h.client, "admin@backoffice.test", company="Admin2Ai Lda", tax_id=None, name="Team")
    assert admin["user"] and h.client.get("/api/auth/me", headers=bearer(admin["token"])).json()["role"] == "admin"
    for who in (bearer(owner["token"]), A):
        for path in INTERNAL:
            res = h.client.get(path, headers=who)
            assert (res.status_code, res.json()) == (403, {"error": "forbidden",
                                                           "message": "This page is for the Admin2Ai team only."}), path
    assert h.client.get("/api/internal/overview").status_code == 401

    X = bearer(admin["token"])
    overview = h.client.get("/api/internal/overview", headers=X)
    assert overview.status_code == 200
    tenants = {t["id"] for t in overview.json()["tenants"]}
    assert tenants == {owner["tenant"]["id"], other["tenant"]["id"], accountant["tenant"]["id"],
                       admin["tenant"]["id"]}  # every business the service runs, read-only
    ops = h.client.get("/api/internal/operations?limit=5", headers=X).json()
    assert ops["audit"]["shown"] == min(5, ops["audit"]["records"]) and len(ops["audit"]["chains"]) == 4
    assert h.client.get("/api/internal/readiness", headers=X).status_code == 200
    assert h.client.get("/api/internal/nothing", headers=X).status_code == 404
    assert h.client.post("/api/internal/overview", json={}, headers=X).status_code == 405

    # The one rule, also where roles are checked: an owner may do everything in their business except this.
    principal = Principal(User("u", "ana@example.pt", "Ana"), Tenant("t", "Padaria"), frozenset({"owner"}), "", "bearer")
    assert admin_only("/api/internal/overview?x=1") and not permitted(principal, "GET", "/api/internal/overview")
    assert permitted(principal, "GET", "/api/home")


def test_origins_and_credentials(tmp_path: Path) -> None:
    h = harness(tmp_path)
    res = h.client.options("/api/auth/login", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST",
                                                       "Access-Control-Request-Headers": "content-type"})
    assert res.headers["access-control-allow-origin"] == ORIGIN
    assert res.headers["access-control-allow-credentials"] == "true"
    evil = h.client.options("/api/auth/login", headers={"Origin": "https://evil.example",
                                                        "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in evil.headers
