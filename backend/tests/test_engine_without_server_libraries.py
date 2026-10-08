"""The engine runs in the browser with pydantic only (the live demo: Pyodide, web/public/engine/worker.js).

Every place the owner can add from Sources, and every first-load page, must work there: nothing on those paths may
import a server-only library (httpx, cryptography, a database driver, a browser). Each check runs in a fresh
process where those libraries cannot be imported, as in the browser.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
SERVER_ONLY = ("httpx", "cryptography", "psycopg", "boto3", "playwright", "rapidocr", "anthropic")

_SCRIPT = r"""
import json, sys
sys.path.insert(0, %(src)r)
for name in %(blocked)r:
    sys.modules[name] = None  # importing it fails, as in the browser
from backoffice.service import BackOfficeService

s = BackOfficeService.demo()
company = next(iter(s.repo.companies))
adds = [
    {"kind": "files", "provider": "google", "address": "laura@hazeltree.pt",
     "folder": "https://drive.google.com/drive/folders/1AbCdEfGhIjK", "companyId": company},
    {"kind": "files", "provider": "microsoft", "address": "laura@hazeltree.pt", "companyId": company},
    {"kind": "accounting", "provider": "toconline", "clientId": "id", "clientSecret": "secret",
     "oauthUrl": "https://app.toconline.pt/oauth", "apiUrl": "https://api.toconline.pt", "companyId": company},
    {"kind": "accounting", "provider": "invoicexpress", "account": "hazel", "apiKey": "key", "companyId": company},
    {"kind": "email", "provider": "microsoft", "address": "shared@hazeltree.pt", "mailbox": "shared"},
    {"kind": "portal", "supplier": "EDP", "username": "laura", "password": "pass"},
]
out = {"adds": [s.dispatch("POST", "/api/sources", body)[0] for body in adds]}
pages = ["/api/home", "/api/needs-you", "/api/activity", "/api/companies", "/api/sources", "/api/connections",
         "/api/documents", "/api/obligations", "/api/billing", "/api/settings/automation", "/api/settings/reading",
         "/api/employees", "/api/expense-claims", "/api/internal/overview", "/api/internal/acceptance",
         "/api/sources/mbcp-ht/payments", "/api/sources/card-4817/payments"]
out["pages"] = {p: s.dispatch("GET", p, None)[0] for p in pages}
# "Something missing?" on Sources: understood in the browser too (an email, an IBAN, a card, a link, a name).
said = ["invoices@hazeltree.pt", "PT50 0033 0000 4532 8817 1026 5", "my Revolut card ending 4821",
        "https://drive.google.com/drive/folders/1AbCdEfGhIjK", "Moloni", "the EDP website", "my lawyer"]
out["understood"] = [s.dispatch("POST", "/api/sources/understand", {"text": t})[0] for t in said]
out["leaked"] = sorted(n for n in %(blocked)r if sys.modules.get(n) is not None)
print(json.dumps(out))
"""


def test_adding_every_kind_of_source_and_the_first_pages_need_no_server_library() -> None:
    run = subprocess.run([sys.executable, "-I", "-c", _SCRIPT % {"blocked": SERVER_ONLY, "src": str(SRC)}],
                         capture_output=True, text=True, timeout=300, check=False)
    assert run.returncode == 0, run.stderr[-3000:]
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["adds"] == [200] * 6, out["adds"]
    assert all(status == 200 for status in out["pages"].values()), out["pages"]
    assert out["understood"] == [200] * 7, out["understood"]
    assert out["leaked"] == []
