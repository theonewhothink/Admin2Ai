"""Adding and removing sources, and the encrypted sign-in vault (§4, §47, §52)."""
import os

import httpx
import pytest

from backoffice.connectors.authorize import AuthorizationError, OAuthApp, OAuthAuthorizer, PROVIDERS
from backoffice.connectors.vault import LocalKeyProvider, TokenVault, VaultError
from backoffice.service import BackOfficeService

IBAN = "PT76000700000012345678923"


@pytest.fixture()
def svc():
    return BackOfficeService.demo()


def names(svc, group):
    _, body = svc.dispatch("GET", "/api/sources", None)
    return {i["name"]: i for g in body["groups"] if g["id"] == group for i in g["items"]}


def test_add_every_kind_shows_up(svc):
    adds = [
        ({"kind": "email", "provider": "microsoft", "address": "office@hazeltree.pt"}, "email", "office@hazeltree.pt"),
        ({"kind": "bank", "bank": "Novo Banco", "companyId": "company-b", "iban": IBAN}, "banks", "Novo Banco •••• 8923"),
        ({"kind": "card", "last4": "9911", "bank": "Revolut", "companyId": "hazel-tree"}, "cards", "Card •••• 9911"),
        ({"kind": "supplier", "name": "Worten", "email": "faturas@worten.pt"}, "suppliers", "Worten"),
        ({"kind": "insurance", "name": "Tranquilidade", "companyId": "hazel-tree", "renewsOn": "2027-02-01"},
         "insurance", "Tranquilidade"),
        ({"kind": "investment", "name": "Alpha Fund", "companyId": "company-c"}, "investments", "Alpha Fund"),
        ({"kind": "loan", "name": "CGD mortgage", "companyId": "company-b"}, "lenders", "CGD mortgage"),
        ({"kind": "government", "name": "Câmara Municipal", "companyId": "company-b"}, "government", "Câmara Municipal"),
    ]
    for body, group, name in adds:
        status, reply = svc.dispatch("POST", "/api/sources", body)
        assert status == 200, reply
        assert name in names(svc, group)


def test_bank_consent_is_shown_and_imap_secret_is_sealed(svc):
    svc.dispatch("POST", "/api/sources", {"kind": "bank", "bank": "Novo Banco", "companyId": "company-b", "iban": IBAN})
    assert "180 days" in names(svc, "banks")["Novo Banco •••• 8923"]["signIn"]
    body = {"kind": "email", "provider": "imap", "address": "inv@hazeltree.pt", "host": "imap.x.pt", "password": "pw"}
    status, reply = svc.dispatch("POST", "/api/sources", body)
    assert status == 200
    assert svc.vault.open(svc.repo.tenant_id, reply["id"])["password"] == "pw"
    assert "pw" not in str(svc.dispatch("GET", "/api/sources", None))  # never returned
    assert "renews automatically" in names(svc, "email")["inv@hazeltree.pt"]["signIn"]
    svc.dispatch("POST", f"/api/sources/{reply['id']}/remove", None)
    assert not svc.vault.has(svc.repo.tenant_id, reply["id"])
    assert "inv@hazeltree.pt" not in names(svc, "email")


@pytest.mark.parametrize("body", [
    {"kind": "bank", "bank": "X", "companyId": "company-b", "iban": "PT50 1234"},
    {"kind": "bank", "bank": "X"},
    {"kind": "email", "address": "not-an-email"},
    {"kind": "email", "provider": "imap", "address": "a@b.pt", "host": "imap.b.pt"},
    {"kind": "card", "last4": "12a4", "bank": "X", "companyId": "hazel-tree"},
    {"kind": "insurance", "name": "Y", "companyId": "hazel-tree", "renewsOn": "soon"},
    {"kind": "rocket"},
])
def test_bad_input_is_refused_plainly(svc, body):
    status, reply = svc.dispatch("POST", "/api/sources", body)
    assert status == 400 and reply["message"]


def test_duplicates_and_unknown_removal(svc):
    assert svc.dispatch("POST", "/api/sources", {"kind": "email", "address": "laura@hazeltree.pt"})[0] == 409
    assert svc.dispatch("POST", "/api/sources/nope/remove", None)[0] == 404


def test_vault_binds_records_to_tenant_and_connection():
    vault = TokenVault(LocalKeyProvider(os.urandom(32)))
    rec = vault.store("t1", "c1", "google", {"refresh_token": "r1"})
    assert "r1" not in repr(rec)
    assert vault.open("t1", "c1") == {"refresh_token": "r1"}
    vault._store.put(rec.__class__(**{**rec.__dict__, "tenant_id": "t2"}))  # copied to another tenant
    with pytest.raises(VaultError):
        vault.open("t2", "c1")
    with pytest.raises(VaultError):
        TokenVault(LocalKeyProvider(os.urandom(32)), vault._store).open("t1", "c1")  # wrong master key


def test_rotated_refresh_token_is_saved():
    from datetime import datetime, timedelta, timezone

    from backoffice.connectors.oauth import OAuthToken

    vault = TokenVault(LocalKeyProvider(os.urandom(32)))
    vault.store("t", "c", "microsoft", {"refresh_token": "old"})

    class Refresher:
        def refresh(self, token):
            assert token == "old"
            return OAuthToken("access", datetime.now(timezone.utc) + timedelta(hours=1), refresh_token="new")

    assert vault.token_provider("t", "c", Refresher()).access_token() == "access"
    assert vault.open("t", "c")["refresh_token"] == "new"


def test_oauth_code_flow_stores_refresh_token_once():
    vault = TokenVault(LocalKeyProvider(os.urandom(32)))
    seen = {}

    def handler(request):
        seen["form"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 3600})

    app = OAuthApp(provider="google", client_id="cid", client_secret="sec", **PROVIDERS["google"])
    auth = OAuthAuthorizer({"google": app}, vault, redirect_uri="https://x/cb", state_key=b"k" * 32,
                           http=httpx.Client(transport=httpx.MockTransport(handler)))
    url = auth.begin("google", "t", "gmail-2", login_hint="a@b.pt")
    assert "access_type=offline" in url and "code_challenge_method=S256" in url
    state = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))["state"]
    from urllib.parse import unquote

    state = unquote(state)
    assert auth.complete("code1", state)["connection_id"] == "gmail-2"
    assert "code_verifier=" in seen["form"]
    assert vault.open("t", "gmail-2")["refresh_token"] == "r"
    with pytest.raises(AuthorizationError):
        auth.complete("code1", state)  # replay
    with pytest.raises(AuthorizationError):
        auth.complete("code1", state[:-2] + "xx")  # tampered
