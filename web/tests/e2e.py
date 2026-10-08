"""Browser checks for the web app's production mode and the static demo.

    python tests/e2e.py production   # builds with sign-in on, runs against a mock API
    python tests/e2e.py demo         # needs `npm run build:pages` first (out/)
    python tests/e2e.py ocr          # the same build: a photographed receipt read in the browser

production: builds the app with NEXT_PUBLIC_REQUIRE_SIGNIN=1 against a tiny
mock of the production API contract written here (cookie session, CSRF
header, 401s), then drives Chromium through sign-in, sign-up, onboarding,
Settings → Account and sign-out. Data endpoints are answered by the real
demo engine (backend/src), so the pages render real shapes.

demo: serves the static export under /Admin2Ai and checks that no page
redirects to sign-in or calls an auth endpoint.

ocr: serves the same static export and uploads a generated photo of a café
receipt (backend/tests/fixtures/photos/fs-pb2026-0441.jpg) on Scan. The page
reads it in the browser (tesseract.js, jsQR; lib/ocr.ts) with nothing but the
site's own files, the engine verifies it against its fiscal QR code, and
Documents shows its supplier and total, also after a reload (the journal
replays the reading; the photo is not read again). Then a scanned PDF
(pdf.js renders it, tesseract reads it) and a badly blurred photo (the retake
task). The OCR files are loaded only once a photo is uploaded.

Needs: Python Playwright with Chromium (`pip install playwright` and
`python -m playwright install chromium`). Screenshots go to $SCREENSHOT_DIR
when it is set.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Iterator

WEB = Path(__file__).resolve().parents[1]
BACKEND_SRC = WEB.parent / "backend" / "src"
HOST = "127.0.0.1"
SUPPORT = "help@admin2ai.app"
EMAIL = "laura@example.pt"
PASSWORD = "correct horse battery"

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok:
        failures.append(what)


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return int(s.getsockname()[1])


def wait_for(url: str, timeout: float = 60) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except Exception:  # noqa: BLE001 - still starting
            time.sleep(0.3)
    raise RuntimeError(f"{url} did not start")


# --------------------------------------------------------------------------- mock API


class MockApi:
    """The production API contract, just enough for the web app: cookie sessions, CSRF, 401s."""

    def __init__(self, web_origin: str) -> None:
        sys.path.insert(0, str(BACKEND_SRC))
        from backoffice.service import BackOfficeService

        self.engine = BackOfficeService.demo()
        self.web_origin = web_origin
        self.sessions: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.users = {EMAIL: {"id": "u1", "email": EMAIL, "name": "Laura Martins", "password": PASSWORD}}
        self.lock = threading.Lock()
        self.reconnects: list[str] = []
        self.checkouts: list[dict[str, Any]] = []
        self.dead_letters: list[dict[str, Any]] = []  # the server's parked jobs, as GET /api/internal/overview lists them
        self.portal_codes: dict[str, str] = {}  # supplier website → the code it sent the owner

    def session_for(self, headers: Any) -> dict[str, Any] | None:
        cookie = SimpleCookie(headers.get("Cookie") or "")
        token = cookie["a2a_session"].value if "a2a_session" in cookie else ""
        return self.sessions.get(token)

    def new_session(self, user: dict[str, Any], tenant: str, fresh: bool) -> str:
        token = secrets.token_urlsafe(32)
        self.sessions[token] = {"user": user, "tenant": tenant, "fresh": fresh, "companies": [], "bank": False}
        return token


def make_handler(api: MockApi) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # quiet
            pass

        def cors(self) -> None:
            if self.headers.get("Origin") == api.web_origin:
                self.send_header("Access-Control-Allow-Origin", api.web_origin)
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.send_header("Vary", "Origin")

        def reply(self, status: int, body: Any = None, cookie: str | None = None, raw: bytes | None = None, ctype: str = "application/json") -> None:
            data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
            self.send_response(status)
            self.cors()
            if data:
                self.send_header("Content-Type", ctype)
            if raw is not None and ctype == "application/zip":
                self.send_header("Content-Disposition", 'attachment; filename="admin2ai-export.zip"')
            if cookie is not None:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the page navigated away (e.g. to sign-in) before the reply arrived

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Requested-With, Accept, Authorization")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            self.handle_any("GET")

        def do_POST(self) -> None:  # noqa: N802
            self.handle_any("POST")

        def handle_any(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            path, _, query = self.path.partition("?")
            with api.lock:
                session = api.session_for(self.headers)
                api.requests.append({
                    "method": method, "path": path, "query": query,
                    "csrf": self.headers.get("X-Requested-With"),
                    "cookie": session is not None,
                    "body": raw.decode("utf-8", "replace"),
                })
                self.route(method, path, query, raw, session)

        def route(self, method: str, path: str, query: str, raw: bytes, session: dict[str, Any] | None) -> None:
            body: dict[str, Any] = {}
            if raw and (self.headers.get("Content-Type") or "").startswith("application/json"):
                body = json.loads(raw)
            # The fake bank consent page: straight back to the app, like GoCardless does.
            if path == "/fake-bank":
                for s in api.sessions.values():
                    s["bank"] = True
                self.send_response(302)
                self.send_header("Location", f"{api.web_origin}/sources?ref=req-1")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # The fake Google sign-in page a reconnect sends the owner to.
            if path == "/fake-google":
                return self.reply(200, raw=b"<!doctype html><title>Google sign-in</title><p>Sign in again</p>",
                                  ctype="text/html")
            # The fake Stripe payment page a plan change sends the owner to.
            if path == "/fake-stripe":
                return self.reply(200, raw=b"<!doctype html><title>Stripe checkout</title><p>Pay</p>",
                                  ctype="text/html")
            if method == "POST" and self.headers.get("X-Requested-With") != "admin2ai":
                return self.reply(403, {"error": "csrf", "message": "Refresh the page and try again."})
            reconnect = re.fullmatch(r"/api/connections/([^/]+)/reconnect", path)
            if reconnect and session is not None:
                # Production contract: a Google mailbox answers with the sign-in page, never "connected".
                api.reconnects.append(reconnect.group(1))
                port = self.server.server_address[1]
                return self.reply(200, {"ok": True, "message": "Sign in to Gmail again to reconnect.",
                                        "authorizeUrl": f"http://{HOST}:{port}/fake-google?c={reconnect.group(1)}"})
            if path == "/api/auth/login":
                user = api.users.get(str(body.get("email", "")).lower())
                if not user or user["password"] != body.get("password"):
                    return self.reply(401, {"error": "unauthorized", "message": "Email or password is not right."})
                token = api.new_session(user, "Hazel Tree", fresh=False)
                return self.reply(200, self.who(api.sessions[token]) | {"token": token}, cookie=f"a2a_session={token}; HttpOnly; SameSite=Lax; Path=/")
            if path == "/api/auth/signup":
                if len(str(body.get("password", ""))) < 10:
                    return self.reply(422, {"error": "weak_password", "message": "Use at least 10 characters for the password."})
                if str(body.get("email", "")).lower() in api.users:
                    return self.reply(409, {"error": "email_taken", "message": "There is already an account with this email. Sign in instead."})
                user = {"id": "u2", "email": body["email"], "name": body.get("name", ""), "password": body["password"]}
                api.users[user["email"].lower()] = user
                token = api.new_session(user, body.get("companyName", ""), fresh=True)
                api.sessions[token]["signup"] = body
                return self.reply(201, self.who(api.sessions[token]) | {"token": token}, cookie=f"a2a_session={token}; HttpOnly; SameSite=Lax; Path=/")
            if session is None:
                return self.reply(401, {"error": "unauthorized", "message": "Sign in to continue."})
            if path == "/api/auth/me":
                return self.reply(200, self.who(session))
            if path == "/api/auth/logout":
                api.sessions = {k: v for k, v in api.sessions.items() if v is not session}
                return self.reply(204, None, cookie="a2a_session=; Max-Age=0; Path=/")
            if path == "/api/account/export":
                return self.reply(200, raw=b"PK\x05\x06" + b"\x00" * 18, ctype="application/zip")
            if path == "/api/account/delete":
                if body.get("confirm") != "DELETE" or body.get("password") != session["user"]["password"]:
                    return self.reply(403, {"error": "forbidden", "message": "That password is not right."})
                return self.reply(202, {"ok": True})
            if path == "/api/onboarding/company":
                session["companies"].append(body)
                return self.reply(200, {"ok": True, "id": f"c{len(session['companies'])}"})
            if path == "/api/onboarding/accountant":
                session["accountant"] = body
                return self.reply(200, {"ok": True})
            if path == "/api/billing" and method == "GET":
                # Production contract for a paying business (the demo engine's own business is never billed):
                # the Free plan, payments set up on the server, no paid plan yet.
                status, data = api.engine.dispatch("GET", path, None)
                free = next(p for p in data["plans"] if p["id"] == "free")
                return self.reply(status, data | {
                    "plan": free | {"status": "active", "renewsOn": None}, "demo": False, "limits": free["limits"],
                    "canUpgrade": True, "canManage": False, "message": None})
            if path == "/api/billing/checkout":
                api.checkouts.append(body)
                port = self.server.server_address[1]
                return self.reply(200, {"url": f"http://{HOST}:{port}/fake-stripe?plan={body.get('plan')}",
                                        "via": "checkout", "plan": body.get("plan")})
            if path == "/api/connections/bank/start":
                return self.reply(200, {"redirectUrl": f"http://{HOST}:{self.server.server_address[1]}/fake-bank?institution={body.get('institutionId')}"})
            code = re.fullmatch(r"/api/portals/([^/]+)/code", path)
            if code and method == "POST":
                # Production contract (server/portals.py): the website signs in with the code and the invoices are
                # fetched before the answer; a wrong code leaves it waiting.
                cid, sent = code.group(1), re.sub(r"[\s-]", "", str(body.get("code") or ""))
                if cid not in api.portal_codes:
                    return self.reply(404, {"error": "not_found", "message": "Vodafone isn't waiting for a code right now."})
                if sent != api.portal_codes[cid]:
                    return self.reply(400, {"error": "bad_request", "message": "That code didn't work. Check it and try again."})
                del api.portal_codes[cid]
                api.engine.portal_retrieved(cid, [], None)
                return self.reply(200, {"ok": True, "documents": 2,
                                        "message": "Done. I signed in to Vodafone and fetched 2 invoices."})
            export = re.fullmatch(r"/api/accounting/([^/]+)/export", path)
            if export and method == "GET":  # the month as the accounting software has it: a ZIP (server/http.py)
                if not re.fullmatch(r"month=\d{4}-\d{2}", query):
                    return self.reply(400, {"error": "bad_request", "message": "Choose a month like 2026-09."})
                return self.reply(200, raw=b"PK\x05\x06" + b"\x00" * 18, ctype="application/zip")
            status, data = api.engine.dispatch(method, path + (f"?{query}" if query else ""), body or None)
            if path == "/api/internal/overview" and status == 200:  # the production server adds its parked jobs
                data = data | {"deadLetters": api.dead_letters}
            if path == "/api/home":  # production labels a stale Google mailbox's button like this
                data = data | {"connections": [c | ({"action": f"Sign in to {c['name']} again"}
                                                    if c["status"] == "stale" and c["kind"] == "email" else {})
                                               for c in data["connections"]]}
            if session["fresh"] and path == "/api/home":
                data = data | {"connections": [c for c in data["connections"] if c["kind"] == "bank"] if session["bank"] else []}
            if session["fresh"] and path == "/api/companies":
                data = {"companies": [{**data["companies"][0], "id": "first", "name": session["tenant"]}]}
            return self.reply(status, data)

        @staticmethod
        def who(session: dict[str, Any]) -> dict[str, Any]:
            u = session["user"]
            return {"user": {"id": u["id"], "email": u["email"], "name": u["name"]}, "tenant": {"id": "t1", "name": session["tenant"]},
                    "role": session.get("role", "owner")}

    return Handler


@contextmanager
def serve(handler: type[http.server.BaseHTTPRequestHandler], port: int) -> Iterator[None]:
    server = http.server.ThreadingHTTPServer((HOST, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()


@contextmanager
def next_server(port: int, env: dict[str, str]) -> Iterator[None]:
    proc = subprocess.Popen(["npx", "next", "start", "-H", HOST, "-p", str(port)], cwd=WEB, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        wait_for(f"http://{HOST}:{port}/signin")
        yield
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=20)


# --------------------------------------------------------------------------- engine fixtures


def nif(prefix8: str) -> str:
    """A Portuguese tax number with a valid check digit."""
    total = sum(int(d) * w for d, w in zip(prefix8, range(9, 1, -1), strict=True))
    check_digit = 11 - total % 11
    return prefix8 + str(0 if check_digit >= 10 else check_digit)


def cafe_receipt() -> bytes:
    """A till receipt (fatura simplificada) to Hazel Tree with its fiscal QR code: €19.02 + €4.38 VAT."""
    from backoffice.demo.evidence import qr_payload

    shop = nif("51234568")
    qr = qr_payload(A=shop, B="516123459", C="PT", D="FS", E="N", F="20260930", G="FS 2026/12", H="CSDF7T5H-12",
                    I1="PT", I7="19.02", I8="4.38", N="4.38", O="23.40", Q="e1Dk", R="1422")
    return "\n".join(["Café Central", f"NIF: {shop}", "Fatura simplificada n.º FS 2026/12", "ATCUD: CSDF7T5H-12",
                      "Data: 30/09/2026", "NIF cliente: 516123459", "Base tributável (23%): 19,02", "IVA 23%: 4,38",
                      "Total: 23,40 €", f"Código QR: {qr}", ""]).encode()


# A letter from the bank asking Hazel Tree for proof of address by 20 October: a deadline only the owner can close.
BANK_LETTER = ("Caro cliente,\nNo âmbito da atualização de dados da conta da Hazel Tree Interiores, Lda., NIF 516 123 459, "
               "solicitamos o envio do comprovativo de morada até 20/10/2026.\nSem estes documentos poderemos bloquear "
               "a conta.\n")


def posts(api: MockApi, pattern: str) -> list[dict[str, Any]]:
    return [r for r in api.requests if r["method"] == "POST" and re.fullmatch(pattern, r["path"])]


# --------------------------------------------------------------------------- production


def shots(page: Any, name: str) -> None:
    out = os.environ.get("SCREENSHOT_DIR")
    if not out:
        return
    Path(out).mkdir(parents=True, exist_ok=True)
    for width, height in ((1440, 900), (390, 844)):
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(250)
        page.screenshot(path=str(Path(out) / f"{name}-{width}.png"), full_page=True)
    page.set_viewport_size({"width": 1280, "height": 900})


def no_sideways_scroll(page: Any, name: str) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(200)
    wide = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
    check(not wide, f"{name}: no horizontal scroll at 390px")
    page.set_viewport_size({"width": 1280, "height": 900})


def production() -> None:
    from playwright.sync_api import expect, sync_playwright

    # Fixed ports: NEXT_PUBLIC_API_URL is baked into the build, so --no-build reuses them.
    api_port = int(os.environ.get("E2E_API_PORT", "8765"))
    web_port = int(os.environ.get("E2E_WEB_PORT", "3765"))
    web_origin = f"http://{HOST}:{web_port}"
    api_url = f"http://{HOST}:{api_port}"
    env = os.environ | {
        "NEXT_PUBLIC_API_URL": api_url,
        "NEXT_PUBLIC_REQUIRE_SIGNIN": "1",
        "NEXT_PUBLIC_SUPPORT_EMAIL": SUPPORT,
        "NEXT_TELEMETRY_DISABLED": "1",
    }
    env.pop("NEXT_PUBLIC_ENGINE", None)
    if "--no-build" not in sys.argv:
        print("building (production mode)…")
        subprocess.run(["npx", "next", "build"], cwd=WEB, env=env, check=True, stdout=subprocess.DEVNULL)

    api = MockApi(web_origin)
    with serve(make_handler(api), api_port), next_server(web_port, env), sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(base_url=web_origin, viewport={"width": 1280, "height": 900}, accept_downloads=True)
        page = context.new_page()
        console_errors: list[str] = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(str(e)))

        print("security headers")
        res = page.goto("/signin")
        h = res.all_headers() if res else {}
        csp = h.get("content-security-policy", "")
        check("frame-ancestors 'none'" in csp, "CSP forbids framing")
        check(f"connect-src 'self' {api_url}" in csp, "CSP allows the API origin for fetch")
        check("object-src 'none'" in csp and "base-uri 'self'" in csp, "CSP locks objects and base URI")
        check(h.get("strict-transport-security", "").startswith("max-age="), "HSTS set")
        check(h.get("x-frame-options") == "DENY", "X-Frame-Options DENY")
        check(h.get("referrer-policy") == "strict-origin-when-cross-origin", "Referrer-Policy set")
        check("camera=()" in h.get("permissions-policy", ""), "Permissions-Policy set")
        check(h.get("x-content-type-options") == "nosniff", "nosniff set")

        print("signed out: pages send the owner to sign in")
        page.goto("/settings")
        page.wait_for_url(re.compile(r"/signin\?next=%2Fsettings$"), timeout=15000)
        check(True, "/settings → /signin?next=%2Fsettings")
        page.goto("/")
        page.wait_for_url(re.compile(r"/signin$"), timeout=15000)
        check(True, "/ → /signin")

        print("sign in: accessible form, plain errors")
        page.goto("/signin?next=%2Fsettings")
        email = page.get_by_label("Email")
        password = page.get_by_label("Password", exact=True)
        check(email.get_attribute("autocomplete") == "username", "email field autocomplete=username")
        check(password.get_attribute("autocomplete") == "current-password", "password autocomplete=current-password")
        page.get_by_role("button", name="Sign in").click()
        expect(page.get_by_text("Enter your email address.")).to_be_visible()
        check(email.get_attribute("aria-invalid") == "true", "empty email marked invalid")
        check("signin-email-error" in (email.get_attribute("aria-describedby") or ""), "email error tied with aria-describedby")
        check(page.evaluate("document.activeElement.id") == "signin-email", "focus moves to the first problem")
        shots(page, "signin-errors")

        email.fill(EMAIL)
        password.fill("wrong password!")
        page.get_by_role("button", name="Sign in").click()
        expect(page.locator("#signin-error[role=alert]")).to_contain_text("Email or password is not right.")
        login = [r for r in api.requests if r["path"] == "/api/auth/login"]
        check(bool(login) and login[-1]["csrf"] == "admin2ai", "login sends X-Requested-With: admin2ai")
        check(page.evaluate("document.activeElement.id") == "signin-password", "focus returns to the password")
        check("signin-error" in (password.get_attribute("aria-describedby") or ""), "form error tied to the password field")

        page.locator("summary", has_text="Forgot password?").click()
        expect(page.get_by_text(f"Ask support at {SUPPORT} to reset it", exact=False)).to_be_visible()
        check(page.locator(f'a[href^="mailto:{SUPPORT}"]').count() == 1, "support email is a mailto link")
        page.get_by_role("button", name="Show password").click()
        check(password.get_attribute("type") == "text", "Show reveals the password")
        page.get_by_role("button", name="Hide password").click()
        shots(page, "signin")
        no_sideways_scroll(page, "/signin")

        password.fill(PASSWORD)
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url(re.compile(r"/settings$"), timeout=15000)
        check(True, "signed in, back to /settings (?next=)")

        print("settings: signed-in owner and account section")
        expect(page.get_by_text(f"Laura Martins · {EMAIL}")).to_be_visible()
        expect(page.get_by_text("Signed in as")).to_be_visible()
        check(page.get_by_text("Things I remember").count() == 0, "no sample rules in production")
        me = [r for r in api.requests if r["path"] == "/api/auth/me"]
        check(any(r["cookie"] for r in me), "GET /api/auth/me carries the session cookie")
        check(page.locator("header").get_by_text("LM").count() >= 1, "header avatar shows the owner's initials")
        with page.expect_download() as dl:
            page.get_by_role("button", name="Download").click()
        check(dl.value.suggested_filename == "admin2ai-export.zip", "Download my data saves the zip")

        page.get_by_role("button", name="Delete account…").click()
        check(page.evaluate("document.activeElement.id") == "delete-confirm", "delete panel takes focus")
        page.get_by_role("button", name="Delete my account").click()
        expect(page.get_by_text("Type DELETE in capital letters to confirm.")).to_be_visible()
        page.get_by_label("Type DELETE to confirm").fill("DELETE")
        page.get_by_label("Your password").fill("not my password")
        page.get_by_role("button", name="Delete my account").click()
        expect(page.get_by_text("That password is not right.")).to_be_visible()
        check(page.url.endswith("/settings"), "a wrong password keeps the owner signed in")
        shots(page, "settings-account")
        page.get_by_role("button", name="Keep my account").click()
        check(page.evaluate("document.activeElement.textContent").strip() == "Delete account…", "focus returns to the button")

        print("home: never all clear with a dead mailbox; reconnect means signing in again")
        with api.lock:
            api.engine.mark_connection_stale("gmail")
            headline = api.engine.home()["headline"]
        page.goto("/")
        expect(page.get_by_text("Gmail needs reconnecting.")).to_be_visible(timeout=15000)
        check(page.get_by_text("Everything is under control.").count() == 0, "a stale mailbox is never 'under control'")
        expect(page.locator("main").get_by_text(headline, exact=True)).to_be_visible()
        check(True, f"Home shows the engine's own headline ({headline})")
        tile = page.locator("a.tile", has_text="Needs you")
        check(tile.locator(".dot-attention").count() == 1 and tile.locator(".dot-good").count() == 0,
              "Needs you tile is not green")
        check(page.locator(".progress-attention").count() >= 1 and page.locator(".progress-good").count() == 0,
              "the month bar is amber below 100%")
        page.get_by_role("button", name="Sign in to Gmail again").click()
        page.wait_for_url(re.compile(r"/fake-google\?c=gmail$"), timeout=15000)
        check(api.reconnects == ["gmail"], "Reconnect asks the API and goes to the provider's sign-in page")
        with api.lock:
            api.engine.reconnect("gmail")
        page.goto("/")
        expect(page.get_by_role("heading", name=re.compile("^Good"))).to_be_visible(timeout=15000)
        check(page.get_by_text("connected again").count() == 0, "never 'connected again' without a sync")

        print("accountant: rules and export come from the engine")
        page.goto("/accountant/hazel-tree")
        page.get_by_role("textbox", name="Rule").fill("Treat all Adobe subscriptions as Software")
        page.get_by_role("button", name="Teach").click()
        expect(page.get_by_text(re.compile(r"It applies to \d+ payments? so far\."))).to_be_visible(timeout=15000)
        # The client's own rules link when the engine gives one (one company's accountant), else the general route.
        taught = [r for r in api.requests
                  if re.fullmatch(r"/api/accountant/(clients/[^/]+/)?rules", r["path"].split("?", 1)[0])]
        check(bool(taught) and json.loads(taught[-1]["body"]) == {"text": "Treat all Adobe subscriptions as Software",
                                                                   "scope": "client"} and taught[-1]["csrf"] == "admin2ai",
              "Teach posts the rule to the client's rules route")
        check(page.get_by_text("61 past transactions").count() == 0, "no made-up rule results")
        with page.expect_download() as dl:
            page.get_by_role("button", name=re.compile("^Download")).click()
        check(dl.value.suggested_filename.startswith("documents_Hazel-Tree_2026-09-01_2026-09-30"),
              "Export downloads the engine's ZIP for the month")
        expect(page.get_by_text(re.compile(r"^Downloaded documents_Hazel-Tree.*\d+ documents?, with the ledger"))).to_be_visible()
        page.goto("/settings")  # back where the next steps start
        expect(page.get_by_text("Signed in as")).to_be_visible(timeout=15000)

        owner_screens(page, api, expect)
        sources_and_codes(page, api, expect)
        team_dashboard(page, api, expect)

        print("sign out")
        page.goto("/settings")
        expect(page.get_by_text("Signed in as")).to_be_visible(timeout=15000)
        page.get_by_role("button", name="Account and settings").click()
        page.get_by_role("button", name="Sign out").click()
        page.wait_for_url(re.compile(r"/signin$"), timeout=15000)
        logout = [r for r in api.requests if r["path"] == "/api/auth/logout"]
        check(bool(logout) and logout[-1]["csrf"] == "admin2ai" and logout[-1]["cookie"], "sign-out ends the session on the server")
        page.goto("/activity")
        page.wait_for_url(re.compile(r"/signin\?next=%2Factivity$"), timeout=15000)
        check(True, "after sign-out, pages ask to sign in again")

        print("sign up")
        page.goto("/signup")
        check(page.get_by_label("Password", exact=True).get_attribute("autocomplete") == "new-password", "new-password autocomplete")
        page.get_by_label("Your name").fill("Rui Costa")
        page.get_by_label("Work email").fill("rui@example.pt")
        page.get_by_label("Password", exact=True).fill("short")
        rule = page.locator("#signup-password-rule")
        check(rule.get_attribute("data-met") == "false", "10-character rule shown, not met")
        page.get_by_label("Company name").fill("Costa Lda")
        tax = page.get_by_label("Company tax number (NIF)")
        tax.fill("123456780")
        tax.blur()
        expect(page.get_by_text("That NIF doesn’t add up. Please check the digits.")).to_be_visible()
        check("signup-taxid-error" in (tax.get_attribute("aria-describedby") or ""), "NIF error tied to its field")
        page.get_by_role("button", name="Create account").click()
        expect(page.get_by_text("Use at least 10 characters. This one has 5.")).to_be_visible()
        check(page.evaluate("document.activeElement.id") == "signup-password", "focus on the first problem (password)")
        shots(page, "signup-errors")
        page.get_by_label("Password", exact=True).fill("a long enough pass")
        check(rule.get_attribute("data-met") == "true", "rule ticked at 10+ characters")
        tax.fill("PT 123 456 789")
        tax.blur()
        check(page.get_by_text("That NIF doesn’t add up").count() == 0, "valid NIF clears the message")
        page.get_by_label("Password", exact=True).fill("")
        page.get_by_label("Password", exact=True).fill("a long enough pass")
        shots(page, "signup")
        no_sideways_scroll(page, "/signup")
        page.get_by_role("button", name="Create account").click()
        page.wait_for_url(re.compile(r"/onboarding$"), timeout=15000)
        signup = [r for r in api.requests if r["path"] == "/api/auth/signup"][-1]
        sent = json.loads(signup["body"])
        check(sent == {"email": "rui@example.pt", "password": "a long enough pass", "name": "Rui Costa", "companyName": "Costa Lda", "taxId": "123456789"},
              "sign-up posts the contract body (NIF normalised)")
        check(signup["csrf"] == "admin2ai", "sign-up sends the CSRF header")

        print("onboarding against the API")
        expect(page.get_by_role("heading", name="Your companies")).to_be_visible()
        expect(page.get_by_text("Costa Lda")).to_be_visible()
        page.get_by_role("button", name="Add another company").click()
        page.get_by_label("Company name").fill("Costa Imóveis")
        page.get_by_label("Tax number (NIF)").fill("123456789")
        page.get_by_role("button", name="Add company").click()
        expect(page.get_by_text("Costa Imóveis")).to_be_visible()
        company = [r for r in api.requests if r["path"] == "/api/onboarding/company"][-1]
        check(json.loads(company["body"]) == {"name": "Costa Imóveis", "taxId": "123456789"}, "POST /api/onboarding/company")
        shots(page, "onboarding-company")
        page.get_by_role("button", name="Continue").click()
        expect(page.get_by_role("heading", name="Connect your email")).to_be_visible()
        href_ok = page.evaluate("document.activeElement.textContent") == "Connect your email"
        check(href_ok, "focus moves to the new step's heading")
        ninety = page.get_by_role("radio", name=re.compile("^Last 90 days"))
        twelve = page.get_by_role("radio", name=re.compile("^Last 12 months"))
        expect(ninety).to_be_checked(timeout=15000)
        check(not twelve.is_checked(), "the first read is the last 90 days unless the owner chooses more")
        page.locator("label.choice", has_text="Last 12 months").click()
        expect(twelve).to_be_checked()
        expect(page.locator("label.choice", has_text="Last 12 months")).to_be_visible()
        chose = posts(api, r"/api/settings/reading")
        check(bool(chose) and json.loads(chose[-1]["body"]) == {"history": "12m"} and chose[-1]["csrf"] == "admin2ai",
              "choosing 12 months before connecting posts history: 12m")
        shots(page, "onboarding-email-history")
        no_sideways_scroll(page, "/onboarding (email)")
        page.get_by_role("button", name="I’ll do this later").click()
        expect(page.get_by_role("heading", name="Connect your bank")).to_be_visible()
        page.get_by_role("button", name="Millennium bcp").click()
        page.wait_for_url(re.compile(r"/onboarding\?returned=bank&result=back$"), timeout=20000)
        start = [r for r in api.requests if r["path"] == "/api/connections/bank/start"][-1]
        check(json.loads(start["body"]) == {"institutionId": "MILLENNIUMBCP_BCOMPTPL"}, "POST /api/connections/bank/start")
        expect(page.get_by_role("heading", name="Connect your bank")).to_be_visible()
        expect(page.get_by_text("Millennium BCP connected")).to_be_visible()
        check(True, "back from the bank, onboarding resumes at the bank step")
        page.get_by_role("button", name="Continue").click()
        page.get_by_role("button", name="Invite them by email").click()
        page.get_by_label("Their email").fill("contas@vidal.pt")
        page.get_by_role("button", name="Save accountant").click()
        expect(page.get_by_text("contas@vidal.pt is your accountant.")).to_be_visible()
        acc = [r for r in api.requests if r["path"] == "/api/onboarding/accountant"][-1]
        check(json.loads(acc["body"]) == {"email": "contas@vidal.pt"}, "POST /api/onboarding/accountant")
        page.get_by_role("button", name="Continue").click()
        page.get_by_role("button", name="Start").click()
        page.wait_for_url(re.compile(rf"{re.escape(web_origin)}/$"), timeout=15000)
        check(True, "Start goes Home")

        print("email connect link")
        page.goto("/onboarding")
        page.get_by_role("button", name="Continue").click()
        with page.expect_request(re.compile(r"/api/oauth/start\?provider=google$")) as req:
            page.get_by_role("button", name=re.compile("^Google")).click()
        check(req.value.url == f"{api_url}/api/oauth/start?provider=google", "Google goes to /api/oauth/start?provider=google")

        blocked = [e for e in console_errors if "Content Security Policy" in e or "Refused to" in e]
        check(not blocked, f"nothing blocked by the Content-Security-Policy {blocked[:2]}")
        crashed = [e for e in console_errors if "Uncaught" in e or "Hydration" in e or "hydrat" in e]
        check(not crashed, f"no uncaught errors or hydration mismatches {crashed[:2]}")

        browser.close()


def owner_screens(page: Any, api: MockApi, expect: Any) -> None:
    """The owner's screens beyond the five places: navigation, cost centers, people, plan, automation, deadlines."""
    print("navigation: five places on top, the rest in the profile menu")
    page.goto("/")
    expect(page.get_by_role("heading", name=re.compile("^Good"))).to_be_visible(timeout=15000)
    def labels(links: Any) -> list[str]:  # the Needs You badge ("2 waiting") is not part of the place's name
        return links.evaluate_all(
            "els => els.map(e => { const c = e.cloneNode(true); c.querySelectorAll('[class*=badge]').forEach(b => b.remove());"
            " return c.textContent.replace(/\\s+/g, ' ').trim(); })")

    names = labels(page.locator("header nav[aria-label=Main] a"))
    check(names == ["Home", "Needs You", "Companies", "Sources", "Activity"], f"the header has exactly the five places {names}")
    page.locator("header nav[aria-label=Main]").get_by_role("link", name="Sources", exact=True).click()
    page.wait_for_url(re.compile(r"/sources$"), timeout=15000)
    expect(page.get_by_role("heading", name="Sources", level=1)).to_be_visible(timeout=15000)
    check(True, "Sources is one tap away, on top")
    for name, url in (("Documents", "/documents"), ("Ask", "/ask"), ("Diagram", "/diagram")):
        page.get_by_role("button", name="Account and settings").click()
        page.locator("header").get_by_role("link", name=name, exact=True).click()
        page.wait_for_url(re.compile(rf"{url}$"), timeout=15000)
        check(True, f"profile menu → {name} (two taps)")
    page.get_by_role("button", name="Account and settings").click()
    check(page.locator("header").get_by_role("link", name="Sources", exact=True).count() == 1,
          "Sources is not repeated in the profile menu")
    page.keyboard.press("Escape")
    page.goto("/")
    expect(page.get_by_role("heading", name=re.compile("^Good"))).to_be_visible(timeout=15000)
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(200)
    bottom = labels(page.locator("nav[aria-label=Main]:not(header nav) a"))
    check(bottom == ["Home", "Needs You", "Scan a receipt", "Sources", "Activity"], f"the phone bar has its five {bottom}")
    chat = page.get_by_role("button", name="Chat", exact=True)
    expect(chat).to_be_visible()
    header_box = page.locator("header").first.bounding_box()
    box = chat.bounding_box()
    check(bool(header_box and box and box["y"] >= header_box["y"] and box["y"] + box["height"] <= header_box["y"] + header_box["height"]),
          "on a phone the Chat button sits in the header, never over the page")
    chat.click()
    expect(page.get_by_role("dialog", name="Ask")).to_be_visible()
    check(True, "Ask is one tap away on a phone (the Chat button)")
    page.get_by_role("dialog", name="Ask").get_by_role("button", name="Close").click()
    page.set_viewport_size({"width": 1280, "height": 900})

    print("cost centers: in the company's own words, a page each, an owner statement, a split")
    page.goto("/companies/hazel-tree")
    expect(page.get_by_role("heading", name="Jobs")).to_be_visible(timeout=15000)
    page.get_by_role("button", name="Add a job").click()
    form = page.locator("form#cc-add")  # its name follows the company's own word, which changes below
    form.get_by_label("Name").fill("Rua das Flores 12")
    form.get_by_role("button", name="Add", exact=True).click()
    expect(form.get_by_text("Done. Job Rua das Flores 12 is set up.", exact=False)).to_be_visible(timeout=15000)
    sent = posts(api, r"/api/companies/hazel-tree/cost-centers")
    check(bool(sent) and json.loads(sent[-1]["body"]) == {"name": "Rua das Flores 12", "kind": "Job"}
          and sent[-1]["csrf"] == "admin2ai", "Add posts the job to the company's cost centers")
    form.get_by_label("Name").fill("2B")
    form.get_by_label("Kind").fill("Apartment")
    form.get_by_label("Owner").fill("Marta Gonçalves")
    form.get_by_label("Management fee, %").fill("10")
    form.get_by_role("button", name="Add", exact=True).click()
    expect(form.get_by_text("Done. Apartment 2B is set up.", exact=False)).to_be_visible(timeout=15000)
    check(json.loads(posts(api, r"/api/companies/hazel-tree/cost-centers")[-1]["body"]) == {
        "name": "2B", "kind": "Apartment", "owner": "Marta Gonçalves", "managementFee": {"percent": "10"}},
        "a property is added with its owner and management fee")
    expect(page.get_by_text(re.compile(r"I need you to tell me which .* payments? (is|are) for\."))).to_be_visible()
    shots(page, "company-cost-centers")
    page.get_by_role("link", name=re.compile("^Apartment 2B")).click()
    page.wait_for_url(re.compile(r"/companies/cost-center\?id=cc-2b$"), timeout=15000)
    expect(page.get_by_role("heading", name="Apartment 2B", level=1)).to_be_visible(timeout=15000)
    expect(page.get_by_role("heading", name="Owner statement", exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile(r"^Owner statement · Apartment 2B · "))).to_be_visible(timeout=15000)
    check(any(r["path"] == "/api/cost-centers/cc-2b/statement" and r["query"].startswith("month=")
              for r in api.requests), "the owner statement is read for one month")
    shots(page, "cost-center-property")
    no_sideways_scroll(page, "/companies/cost-center")

    page.goto("/needs-you")
    card = page.locator("article", has=page.get_by_role("heading", name="EDP"))
    expect(card).to_be_visible(timeout=15000)
    card.get_by_text("Split it between several").click()
    card.get_by_role("textbox", name="Apartment 2B").fill("30")
    card.get_by_role("textbox", name="Job Rua das Flores 12").fill("30")
    card.get_by_role("button", name="Confirm").click()
    expect(card.get_by_role("alert")).to_contain_text("They must match to the cent.", timeout=15000)
    shots(page, "needs-split")
    check(True, "a split that does not add up shows the engine's plain words")
    card.get_by_role("textbox", name="Job Rua das Flores 12").fill("34.10")
    card.get_by_role("button", name="Confirm").click()
    expect(page.get_by_text(re.compile(r"^Done\. The EDP payment is now split"))).to_be_visible(timeout=15000)
    split = json.loads(posts(api, r"/api/needs-you/[^/]+/answer")[-1]["body"])
    check(split["option_id"] == "split" and split["split"] == [
        {"costCenterId": "cc-2b", "amount": "30"}, {"costCenterId": "cc-rua-das-flores-12", "amount": "34.10"}],
        "the split is sent as amounts per job")

    print("cost center: rename and archive")
    page.goto("/companies/cost-center?id=cc-rua-das-flores-12")
    expect(page.get_by_text("its part of €64.10")).to_be_visible(timeout=15000)
    page.get_by_role("button", name="Rename").click()
    page.get_by_role("form", name=re.compile("^Change")).get_by_label("Name").fill("Rua das Flores 14")
    page.get_by_role("button", name="Save").click()
    expect(page.get_by_text("Done. It is now called Job Rua das Flores 14.")).to_be_visible(timeout=15000)
    page.get_by_role("button", name="Archive", exact=True).click()
    page.get_by_role("button", name="Archive it").click()
    expect(page.get_by_text("Done. Job Rua das Flores 14 is archived. Its past costs stay on it.")).to_be_visible(timeout=15000)
    check(json.loads(posts(api, r"/api/cost-centers/cc-rua-das-flores-12")[-1]["body"]) == {"active": False},
          "Archive posts active: false")

    print("payment and document detail")
    edp = next(r.id for r in api.engine.repo.transactions.values() if r.tx.counterparty == "EDP COMERCIAL")
    page.goto(f"/payments/detail?id={edp}")
    expect(page.get_by_role("heading", name="EDP", level=1)).to_be_visible(timeout=15000)
    shots(page, "payment-detail")
    page.get_by_role("button", name="It always has one").click()
    expect(page.get_by_text(re.compile(r"^Done\. .*I will remember this\."))).to_be_visible(timeout=15000)
    check(json.loads(posts(api, rf"/api/transactions/{edp}/evidence")[-1]["body"]) == {"need": "invoice"},
          "It always has one posts need: invoice")
    page.goto("/documents")
    page.get_by_role("link", name=re.compile("^Marta Gonçalves")).first.click()
    page.wait_for_url(re.compile(r"/documents/detail\?id="), timeout=15000)
    expect(page.get_by_role("heading", name="Proof")).to_be_visible(timeout=15000)
    page.get_by_role("button", name="Mark as sensitive").click()
    expect(page.get_by_text(re.compile(r"^Done\. Only you and the company’?'?s accountant"))).to_be_visible(timeout=15000)
    expect(page.get_by_role("button", name="It is not sensitive")).to_be_visible()
    shots(page, "document-detail")
    no_sideways_scroll(page, "/documents/detail")

    print("people and expense claims: one tap to pay someone back")
    page.goto("/settings/people")
    expect(page.get_by_role("heading", name="People and expenses")).to_be_visible(timeout=15000)
    page.get_by_role("button", name="Add someone").click()
    person = page.get_by_role("form", name="Add someone")
    person.get_by_label("Name").fill("Rui Costa")
    person.get_by_label("Email").fill("rui@hazeltree.pt")
    person.get_by_label("Company cards").fill("5521")
    person.get_by_label("Works for").select_option(label="Hazel Tree")
    person.get_by_role("button", name="Add", exact=True).click()
    expect(person.get_by_text("Done. When a payment on card •••• 5521 misses its receipt, I will ask Rui, not you.")).to_be_visible(timeout=15000)
    check(json.loads(posts(api, r"/api/employees")[-1]["body"]) == {
        "name": "Rui Costa", "email": "rui@hazeltree.pt", "cards": ["5521"], "companyId": "hazel-tree"},
        "Add posts the person with their card")
    page.get_by_role("button", name="Send a receipt for someone").click()
    claim_form = page.get_by_role("form", name="Send a receipt for someone")
    claim_form.get_by_label("The receipt").set_input_files(
        files=[{"name": "cafe.txt", "mimeType": "text/plain", "buffer": cafe_receipt()}])
    claim_form.get_by_role("button", name="Send").click()
    expect(claim_form.get_by_text("Got it. Rui's €23.40 receipt from Café Central is waiting for your OK to pay it back."))\
        .to_be_visible(timeout=15000)
    claim = page.get_by_role("listitem", name="Rui Costa, Café Central")
    expect(claim.get_by_text("Waiting for approval")).to_be_visible(timeout=15000)
    claim.get_by_role("button", name="Yes, pay Rui back").click()
    expect(claim.get_by_text("Done. When you pay Rui back €23.40, I will match the transfer and close it.")).to_be_visible(timeout=15000)
    expect(claim.get_by_text("Approved, to be paid back")).to_be_visible(timeout=15000)
    answered = posts(api, r"/api/needs-you/nd_claim_rui_23/answer")
    check(bool(answered) and json.loads(answered[-1]["body"])["option_id"] == "approve" and answered[-1]["csrf"] == "admin2ai",
          "approving the claim answers its Needs You question")
    shots(page, "people")
    no_sideways_scroll(page, "/settings/people")

    print("plan: usage, limits, upgrade goes to Stripe's page")
    page.goto("/settings/plan")
    expect(page.get_by_role("heading", name="Your plan")).to_be_visible(timeout=15000)
    expect(page.locator("p.lead")).to_have_text("Free")
    expect(page.get_by_text("Documents this month")).to_be_visible()
    shots(page, "plan")
    no_sideways_scroll(page, "/settings/plan")
    page.get_by_role("button", name="Choose Solo").click()
    page.wait_for_url(re.compile(r"/fake-stripe\?plan=solo$"), timeout=15000)
    check(api.checkouts == [{"plan": "solo"}], "Choose Solo asks the API for the payment page and goes there")
    check(posts(api, r"/api/billing/checkout")[-1]["csrf"] == "admin2ai", "checkout sends the CSRF header")
    page.goto("/settings?billing=cancelled")
    expect(page.get_by_text("Nothing was charged. Your plan is as it was.")).to_be_visible(timeout=15000)

    print("automation: a switch, in plain words")
    switch = page.get_by_role("switch", name="Send each month to your accountant")
    expect(switch).to_have_attribute("aria-checked", "false", timeout=15000)
    switch.click()
    expect(page.get_by_text("Done. I will send each month to your accountant.")).to_be_visible(timeout=15000)
    expect(switch).to_have_attribute("aria-checked", "true")
    shots(page, "settings-automation")
    toggled = posts(api, r"/api/settings/automation")
    check(bool(toggled) and json.loads(toggled[-1]["body"]) == {"monthlyPackage": True} and toggled[-1]["csrf"] == "admin2ai",
          "the switch posts monthlyPackage: true")

    print("reading: spam only when allowed, and how far back, in plain words")
    spam = page.get_by_role("switch", name="Also look in spam for invoices")
    expect(spam).to_have_attribute("aria-checked", "false", timeout=15000)
    expect(page.get_by_text("Sometimes an invoice lands in spam. I look there too, but never in the trash.")).to_be_visible()
    spam.click()
    expect(page.get_by_text("Done. I will also look in spam for invoices.")).to_be_visible(timeout=15000)
    expect(spam).to_have_attribute("aria-checked", "true")
    toggled = posts(api, r"/api/settings/reading")
    check(bool(toggled) and json.loads(toggled[-1]["body"]) == {"lookInSpam": True} and toggled[-1]["csrf"] == "admin2ai",
          "the spam switch posts lookInSpam: true")
    section = page.locator("section#reading")
    expect(section.get_by_role("radio", name=re.compile("^Last 90 days"))).to_be_checked()
    section.locator("label.choice", has_text="Last 12 months").click()
    expect(page.get_by_text("Done. I will read the last 12 months of what you connect.")).to_be_visible(timeout=15000)
    expect(section.get_by_role("radio", name=re.compile("^Last 12 months"))).to_be_checked()
    check(json.loads(posts(api, r"/api/settings/reading")[-1]["body"]) == {"history": "12m"},
          "choosing 12 months in Settings posts history: 12m")
    shots(page, "settings-reading")
    no_sideways_scroll(page, "/settings (reading)")
    section.locator("label.choice", has_text="Last 90 days").click()  # back to the default for what follows
    expect(page.get_by_text("Done. I will read the last 90 days of what you connect.")).to_be_visible(timeout=15000)

    print("deadlines: who does it, what proves it done, and It is done")
    with api.lock:
        letter = api.engine.dispatch("POST", "/api/evidence", {
            "filename": "carta.txt", "contentType": "text/plain",
            "dataBase64": base64.b64encode(BANK_LETTER.encode()).decode()})
    check(letter[0] == 200, "the bank's letter becomes a deadline")
    page.goto("/deadlines")
    expect(page.get_by_role("heading", name="Deadlines", level=1)).to_be_visible(timeout=15000)
    due = page.get_by_role("listitem").filter(has=page.get_by_text("Your bank needs updated details", exact=True))
    expect(due.get_by_text("What proves it done")).to_be_visible()
    expect(due.get_by_text("A copy of the reply that was sent by 20 October.")).to_be_visible()
    expect(due.get_by_text("Who does it")).to_be_visible()
    shots(page, "deadlines")
    no_sideways_scroll(page, "/deadlines")
    due.get_by_role("button", name="It is done").click()
    due.get_by_text("I sent what they asked for").click()
    due.get_by_role("button", name="Confirm").click()
    expect(page.get_by_text("Done. Your bank needs updated details is closed with your confirmation.")).to_be_visible(timeout=15000)
    done = posts(api, r"/api/obligations/[^/]+/done")
    check(bool(done) and json.loads(done[-1]["body"]) == {"outcome": "sent"} and done[-1]["csrf"] == "admin2ai",
          "It is done posts the confirmation the API offers")


def sources_and_codes(page: Any, api: MockApi, expect: Any) -> None:
    """Supplier websites, cloud storage and accounting software added from Sources; a website's sign-in code entered
    in Needs you; a month catching up on missed email; the EU VAT register's details used in one tap."""
    print("sources: what I read, how every payment stands, and what is missing")
    page.goto("/sources")
    expect(page.get_by_role("heading", name="Sources", level=1)).to_be_visible(timeout=15000)
    with api.lock:
        engine = api.engine.sources()
    summary = engine["summary"]
    expect(page.locator("p.lead")).to_have_text(summary["text"], timeout=15000)
    verdict = page.locator(f"[role=status][data-tone={summary['tone']}]", has_text=summary["coverage"])
    expect(verdict).to_be_visible()
    check(summary["tone"] == "attention" and verdict.locator(".dot-good").count() == 0,
          f"the coverage line is the engine's and is not green while something is open ({summary['coverage'][:60]}…)")
    check(re.match(r"I read \d+ mailbox", summary["text"]) is not None, f"the summary says what I read ({summary['text']})")
    read = page.locator("section#read")
    for group, item in (("email", "gmail"), ("banks", "mbcp-ht"), ("cards", "card-5530")):
        line = next(i for g in engine["groups"] if g["id"] == group for i in g["items"] if i["id"] == item)["coverage"]["text"]
        expect(read.locator(f"li[data-source={item}]").get_by_text(line, exact=True)).to_be_visible()
    check(True, "each mailbox, bank account and card has its own coverage line")

    print("sources: a card's payments, each with where it stands")
    ikea = read.locator("li[data-source=card-4817]")
    ikea.get_by_role("button", name="Show payments").click()
    with api.lock:
        rows = api.engine.source_payments("card-4817")["items"]
    listed = ikea.get_by_role("list", name="Payments of Card •••• 4817")
    expect(listed.get_by_role("link", name="IKEA")).to_be_visible(timeout=15000)
    first = rows[0]
    if first["state"] == "needs_you":
        expect(listed.get_by_role("link", name="Needs your answer")).to_have_attribute("href", re.compile(r"/needs-you#nd_ikea_418$"))
    else:
        expect(listed.get_by_text(first["stateText"], exact=True)).to_be_visible()
    check(listed.get_by_role("link", name="IKEA").get_attribute("href").endswith(f"/payments/detail?id={first['id']}"),
          "a payment row opens the payment's own page")
    check(any(r["method"] == "GET" and r["path"] == "/api/sources/card-4817/payments" for r in api.requests),
          "expanding a card reads its payments")
    card = read.locator("li[data-source=card-5530]")
    card.get_by_role("button", name="Show payments").click()
    expect(card.get_by_text("Receipt found", exact=True)).to_be_visible(timeout=15000)
    bank = read.locator("li[data-source=mbcp-ht]")
    bank.get_by_role("button", name="Show payments").click()
    with api.lock:
        bank_rows = api.engine.source_payments("mbcp-ht")["items"]
    for row in bank_rows:
        expect(bank.get_by_text(row["stateText"], exact=True).first).to_be_visible(timeout=15000)
    check(True, "every payment of the account says where it stands (invoice found, no invoice needed, looking)")
    shots(page, "sources-expanded")
    no_sideways_scroll(page, "/sources (a card open)")
    bank.get_by_role("button", name="Hide payments").click()

    print("sources: the companies I cover")
    companies = page.locator("section#companies")
    hazel = companies.locator("li[data-company=hazel-tree]")
    expect(hazel.get_by_text("Hazel Tree", exact=True)).to_be_visible()
    expect(hazel.get_by_text(re.compile(r"NIF 516123459"))).to_be_visible()
    expect(hazel.get_by_text(re.compile(r"^laura@hazeltree\.pt, Millennium BCP •••• 0265, Card •••• 5530"))).to_be_visible()
    check(True, "each company with its tax number and the sources that feed it")

    print("sources: Something missing? understands, opens the form filled in, adds nothing by itself")
    box = page.get_by_role("textbox", name="What I’m not reading yet")
    check("an email address, a bank, a card" in (box.get_attribute("placeholder") or ""), "the box says what it takes")
    added_before = len(posts(api, r"/api/sources"))
    box.fill("billing@hazeltree.pt")
    page.get_by_role("button", name="Add it").click()
    expect(page.get_by_text("hazeltree.pt is on Google, like laura@hazeltree.pt. Sign in once and I read it.")).to_be_visible(timeout=15000)
    form = page.get_by_role("form", name="Add a source")
    expect(form.get_by_label("Email address")).to_have_value("billing@hazeltree.pt")
    expect(form.get_by_label("Provider")).to_have_value("google")
    understood = posts(api, r"/api/sources/understand")
    check(bool(understood) and json.loads(understood[-1]["body"]) == {"text": "billing@hazeltree.pt"}
          and understood[-1]["csrf"] == "admin2ai", "the text goes to /api/sources/understand")
    form.get_by_role("button", name="Cancel").click()
    box.fill("PT76 0007 0000 0012 3456 7892 3")
    page.get_by_role("button", name="Add it").click()
    expect(page.get_by_text("PT76 •••• 8923 is a Novo Banco account. Check the company and tap Add.")).to_be_visible(timeout=15000)
    expect(form.get_by_label("Bank")).to_have_value("Novo Banco")
    expect(form.get_by_label("IBAN (optional)")).to_have_value("PT76000700000012345678923")
    check(len(posts(api, r"/api/sources")) == added_before, "understanding adds nothing: only the form's Add does")
    shots(page, "sources-missing-iban")
    no_sideways_scroll(page, "/sources (an IBAN understood)")
    form.get_by_role("button", name="Cancel").click()
    box.fill("my lawyer's invoices")
    page.get_by_role("button", name="Add it").click()
    expect(page.get_by_text("I'll ask Claude to help with that.")).to_be_visible(timeout=15000)
    chat = page.get_by_role("dialog", name="Ask")
    expect(chat).to_be_visible()
    expect(chat.get_by_role("textbox", name="Message")).to_have_value("my lawyer's invoices")
    check(True, "anything else opens the chat with the text ready to send")
    chat.get_by_role("button", name="Close").click()

    print("sources: supplier websites, cloud storage and accounting software, one tap each")
    page.get_by_role("button", name="Supplier website").click()
    form = page.get_by_role("form", name="Add a source")
    form.get_by_label("Supplier").fill("Vodafone")
    form.get_by_label("Your username on their website").fill("laura@hazeltree.pt")
    form.get_by_label("Password").fill("portal pass 1 ")
    form.get_by_role("button", name="Add", exact=True).click()
    expect(page.get_by_text("Done. I will sign in to Vodafone and fetch your invoices from there.")).to_be_visible(timeout=15000)
    sent = posts(api, r"/api/sources")
    check(bool(sent) and json.loads(sent[-1]["body"]) == {
        "kind": "portal", "supplier": "Vodafone", "username": "laura@hazeltree.pt", "password": "portal pass 1 "}
        and sent[-1]["csrf"] == "admin2ai", "a supplier website is posted with its sign-in (the password as typed)")
    portals = page.locator("#portals")
    expect(portals.get_by_text("Vodafone", exact=True)).to_be_visible()
    expect(portals.get_by_text(re.compile(r"I sign in when an invoice is missing"))).to_be_visible()

    page.get_by_role("button", name="Cloud storage").click()
    form = page.get_by_role("form", name="Add a source")
    form.get_by_label("Where your files are").select_option("microsoft")
    form.get_by_label("Microsoft account").fill("laura@hazeltree.pt")
    form.get_by_label("Folder to watch (optional)").fill("/Invoices/2026")
    form.get_by_role("button", name="Sign in and connect").click()
    expect(page.get_by_text("Done. I will search your OneDrive for missing invoices.")).to_be_visible(timeout=15000)
    check(json.loads(posts(api, r"/api/sources")[-1]["body"]) == {
        "kind": "files", "provider": "microsoft", "address": "laura@hazeltree.pt", "folder": "/Invoices/2026"},
        "cloud storage is posted with its provider, account and folder")

    page.get_by_role("button", name="Accounting software").click()
    form = page.get_by_role("form", name="Add a source")
    form.get_by_label("Client identifier").fill("typed-for-toconline")
    form.get_by_label("Accounting software").select_option("invoicexpress")
    form.get_by_label("Account name").fill("hazeltree")
    form.get_by_label("Access key").fill("secret-key")
    form.get_by_role("button", name="Add", exact=True).click()
    expect(page.get_by_text("Done. I will read Hazel Tree's documents in InvoiceXpress.")).to_be_visible(timeout=15000)
    check(json.loads(posts(api, r"/api/sources")[-1]["body"]) == {
        "kind": "accounting", "provider": "invoicexpress", "account": "hazeltree", "apiKey": "secret-key",
        "companyId": "hazel-tree"}, "accounting software is posted with only the chosen program's fields")
    accounting = page.locator("#accounting")
    shots(page, "sources")
    no_sideways_scroll(page, "/sources")
    with page.expect_download() as dl:
        accounting.get_by_role("button", name=re.compile(r"^Download \w+ from InvoiceXpress$")).click()
    check(re.fullmatch(r"InvoiceXpress-\d{4}-\d{2}\.zip", dl.value.suggested_filename) is not None,
          f"the accounting software's month downloads as a ZIP ({dl.value.suggested_filename})")
    asked = [r for r in api.requests if re.fullmatch(r"/api/accounting/accounting-invoicexpress-hazel-tree/export", r["path"])]
    check(bool(asked) and re.fullmatch(r"month=\d{4}-\d{2}", asked[-1]["query"]) is not None, "the export asks for one month")

    print("sources: what I learned stays one tap away, collapsed")
    learned = page.locator("section#learned")
    suppliers = learned.locator("details#suppliers")
    check(suppliers.get_attribute("open") is None, "what I learned is collapsed")
    suppliers.locator("summary").click()
    expect(suppliers.get_by_text("EDP", exact=True)).to_be_visible()
    expect(suppliers.get_by_role("button", name="Remove EDP")).to_be_visible()

    print("sources: a connection that stopped is said, never green, with Reconnect")
    with api.lock:
        api.engine.mark_connection_stale("gmail")
        stale = api.engine.sources()["summary"]["coverage"]
    page.reload()
    expect(page.locator("[role=status][data-tone=attention]", has_text=stale)).to_be_visible(timeout=15000)
    check(stale.startswith("Gmail has not been read since"), f"the line says which connection stopped ({stale[:48]}…)")
    gmail = page.locator("section#read li[data-source=gmail]")
    expect(gmail.get_by_text("Needs reconnecting")).to_be_visible()
    gmail.get_by_role("button", name="Reconnect").click()
    page.wait_for_url(re.compile(r"/fake-google\?c=gmail$"), timeout=15000)
    check(api.reconnects[-1] == "gmail", "Reconnect on Sources goes to the provider's sign-in")
    with api.lock:
        api.engine.reconnect("gmail")

    print("needs you: a supplier website's sign-in code")
    with api.lock:
        api.portal_codes["portal-vodafone"] = "000001"
        api.engine.portal_code_needed("portal-vodafone", channel="sms",
                                      expires_at=datetime.now(timezone.utc) + timedelta(minutes=10), state=None)
    page.goto("/needs-you")
    card = page.locator("article", has_text="Sign-in code")
    expect(card.get_by_role("heading", name="Vodafone", exact=True)).to_be_visible(timeout=15000)
    expect(card.get_by_text("Vodafone sent you a sign-in code to your phone. Enter it so I can fetch your invoices.")) \
        .to_be_visible(timeout=15000)
    expect(card.get_by_text(re.compile(r"^The code works until \d{2}:\d{2}\.$"))).to_be_visible()
    box = card.get_by_role("textbox", name="Sign-in code")
    check(box.get_attribute("autocomplete") == "one-time-code", "the code box offers the code the phone received")
    box.fill("999999")
    card.get_by_role("button", name="Confirm").click()
    expect(card.get_by_role("alert")).to_have_text("That code didn't work. Check it and try again.", timeout=15000)
    shots(page, "needs-code")
    no_sideways_scroll(page, "/needs-you")
    box.fill("000 001")
    card.get_by_role("button", name="Confirm").click()
    expect(page.get_by_text("Done. I signed in to Vodafone and fetched 2 invoices.")).to_be_visible(timeout=15000)
    sent = posts(api, r"/api/portals/portal-vodafone/code")
    check([json.loads(r["body"]) for r in sent] == [{"code": "999999"}, {"code": "000 001"}]
          and all(r["csrf"] == "admin2ai" for r in sent), "the code is posted to the item's own address")
    expect(card).to_have_count(0, timeout=15000)
    since = api.requests.index(sent[-1])
    for _ in range(50):  # the list is read again once the card has folded away
        if any(r["method"] == "GET" and r["path"] == "/api/needs-you" for r in api.requests[since:]):
            break
        page.wait_for_timeout(100)
    check(any(r["method"] == "GET" and r["path"] == "/api/needs-you" for r in api.requests[since:]),
          "the list is read again once the website signed in")

    print("month: catching up on email it missed")
    with api.lock:
        start = datetime(2026, 9, 10, tzinfo=timezone.utc)
        api.engine.repo.connectors["gmail"].gaps = ((start, start + timedelta(days=3), True),)
        api.engine.orchestrator.run()
    page.goto("/companies/hazel-tree")
    expect(page.get_by_text("Catching up on 3 days of email from laura@hazeltree.pt.")).to_be_visible(timeout=15000)
    check(True, "the month says it is catching up on the email it missed")
    with api.lock:
        api.engine.repo.connectors["gmail"].gaps = ()
        api.engine.orchestrator.run()

    print("company: the EU VAT register's details in one tap")
    with api.lock:
        company_b = api.engine.repo.companies["company-b"]
        api.engine.repo.identity_suggestions["company-b"] = {
            "status": "found", "country": "PT", "number": company_b.tax_id, "legal_name": "COMPANY B LDA",
            "address": "RUA DE SANTA CATARINA 112, 4000-447 PORTO", "source": "VIES"}
        said = next(c for c in api.engine.companies()["companies"] if c["id"] == "company-b")["identityCheck"]["message"]
    page.goto("/companies/company-b")
    expect(page.get_by_text(said, exact=True)).to_be_visible(timeout=15000)
    page.get_by_role("button", name="Use these details").click()
    expect(page.get_by_text("Done. Company B now has the legal name and address from the EU VAT register.")) \
        .to_be_visible(timeout=15000)
    used = posts(api, r"/api/companies/company-b/identity")
    check(bool(used) and json.loads(used[-1]["body"]) == {"use": True} and used[-1]["csrf"] == "admin2ai",
          "Use these details posts use: true")


def team_dashboard(page: Any, api: MockApi, expect: Any) -> None:
    """The team's Command Center lists background jobs that kept failing (the server's dead letters)."""
    print("admin: failed background jobs")
    with api.lock:
        for s in api.sessions.values():
            s["role"] = "admin"
    page.goto("/internal")
    expect(page.get_by_role("heading", name="Failed jobs")).to_be_visible(timeout=30000)
    expect(page.get_by_text("No failed jobs.")).to_be_visible()
    with api.lock:
        api.dead_letters = [{"id": "job:41", "tenant": "t1", "kind": "sync.connection",
                             "label": "Reading a mailbox after a push notification", "connection": "gmail",
                             "attempts": 5, "lastError": "Gmail did not answer", "since": "2026-10-02T08:10:00+00:00"}]
    page.get_by_role("button", name="Refresh").click()
    jobs = page.get_by_role("region", name=re.compile(r"^Failed jobs"))
    expect(jobs.get_by_text("Reading a mailbox after a push notification")).to_be_visible(timeout=15000)
    expect(jobs.get_by_text("Gmail did not answer")).to_be_visible()
    expect(jobs.get_by_text(re.compile(r"Failed 5 times · parked .* · tenant t1 · gmail"))).to_be_visible()
    shots(page, "internal-failed-jobs")
    with api.lock:
        for s in api.sessions.values():
            s["role"] = "owner"
        api.dead_letters = []


# --------------------------------------------------------------------------- demo


def demo_sources(page: Any, port: int, expect: Any) -> None:
    """Sources on the static demo: the engine in the browser says what it reads, how every payment stands, and
    understands what is missing."""
    sys.path.insert(0, str(BACKEND_SRC))
    from backoffice.service import BackOfficeService

    engine = BackOfficeService.demo()
    summary = engine.sources()["summary"]
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"http://{HOST}:{port}/Admin2Ai/sources/")
    expect(page.get_by_role("heading", name="Sources", level=1)).to_be_visible(timeout=60000)
    expect(page.locator("p.lead")).to_have_text(summary["text"], timeout=60000)
    expect(page.locator("[role=status][data-tone=attention]", has_text=summary["coverage"])).to_be_visible()
    check(True, f"demo /sources: {summary['text']} {summary['coverage']}")
    read = page.locator("section#read")
    for item in ("gmail", "mbcp-ht", "card-4817"):
        line = next(i for g in engine.sources()["groups"] for i in g["items"] if i["id"] == item)["coverage"]["text"]
        expect(read.locator(f"li[data-source={item}]").get_by_text(line, exact=True)).to_be_visible()
    shots(page, "demo-sources")
    card = read.locator("li[data-source=card-4817]")
    card.get_by_role("button", name="Show payments").click()
    expect(card.get_by_role("link", name="Needs your answer")).to_have_attribute(
        "href", re.compile(r"/needs-you/?#nd_ikea_418$"), timeout=180000)
    bank = read.locator("li[data-source=mbcp-ht]")
    bank.get_by_role("button", name="Show payments").click()
    for row in engine.source_payments("mbcp-ht")["items"]:
        expect(bank.get_by_text(row["stateText"], exact=True).first).to_be_visible(timeout=180000)
    check(True, "demo: a card and an account open to their payments, each with where it stands")
    bank.scroll_into_view_if_needed()
    shots(page, "demo-sources-expanded")
    no_sideways_scroll(page, "demo /sources (a card open)")
    hazel = page.locator("section#companies li[data-company=hazel-tree]")
    expect(hazel.get_by_text("laura@hazeltree.pt, Millennium BCP •••• 0265, Card •••• 5530", exact=True)).to_be_visible()
    box = page.get_by_role("textbox", name="What I’m not reading yet")
    box.fill("ana@gmail.com")
    page.get_by_role("button", name="Add it").click()
    expect(page.get_by_text("ana@gmail.com is a Gmail address. Sign in with Google once and I read it.")).to_be_visible(timeout=180000)
    form = page.get_by_role("form", name="Add a source")
    expect(form.get_by_label("Email address")).to_have_value("ana@gmail.com")
    box.fill("my Revolut card ending 4821")
    page.get_by_role("button", name="Add it").click()
    expect(page.get_by_text("Card •••• 4821 from Revolut. Check the company and tap Add.")).to_be_visible(timeout=60000)
    expect(form.get_by_label("Last 4 digits")).to_have_value("4821")
    expect(form.get_by_label("Issued by")).to_have_value("Revolut")
    check(True, "demo: Something missing? understands an email address and a card in the browser")
    shots(page, "demo-sources-missing")
    no_sideways_scroll(page, "demo /sources (Something missing?)")
    form.get_by_role("button", name="Cancel").click()
    # The phone's "Add it on the web" arrives with what was typed: understood at once, the form filled in.
    page.goto(f"http://{HOST}:{port}/Admin2Ai/sources/?missing=PT76%200007%200000%200012%203456%207892%203")
    expect(page.get_by_text("PT76 •••• 8923 is a Novo Banco account. Check the company and tap Add.")).to_be_visible(timeout=180000)
    form = page.get_by_role("form", name="Add a source")
    expect(form.get_by_label("Bank")).to_have_value("Novo Banco")
    expect(form.get_by_label("IBAN (optional)")).to_have_value("PT76000700000012345678923")
    check(True, "demo: /sources?missing=<an IBAN> opens the bank form filled in")


def demo() -> None:
    from playwright.sync_api import expect, sync_playwright

    out = WEB / "out"
    if not (out / "index.html").exists():
        raise SystemExit("Build the static demo first: npm run build:pages")
    root = Path(tempfile.mkdtemp())
    try:
        (root / "Admin2Ai").symlink_to(out)
        port = free_port()

        class Static(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a: Any, **kw: Any) -> None:
                super().__init__(*a, directory=str(root), **kw)

            def log_message(self, *args: Any) -> None:
                pass

            def copyfile(self, source: Any, outputfile: Any) -> None:
                try:
                    super().copyfile(source, outputfile)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the browser moved on (e.g. Pyodide downloads cancelled between pages)

        with serve(Static, port), sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            seen: list[str] = []
            page.on("request", lambda r: seen.append(r.url))
            for path in ["/", "/settings/", "/needs-you/", "/onboarding/", "/companies/hazel-tree/", "/accountant/",
                         "/deadlines/", "/settings/people/", "/documents/detail/?id=doc_796709c2823e768a"]:
                page.goto(f"http://{HOST}:{port}/Admin2Ai{path}")
                page.wait_for_timeout(1500)
                check("/signin" not in page.url, f"demo {path}: no redirect to sign-in")
                check(len(page.locator("main").first.inner_text().strip()) > 20, f"demo {path}: the page rendered")
            check(not [u for u in seen if "/api/auth" in u or "/signin" in u or "/signup" in u], "demo: no auth request, no sign-in page loaded")
            # The plan page says it is the demo; choosing a plan explains that, and never opens a payment page.
            page.goto(f"http://{HOST}:{port}/Admin2Ai/settings/plan/")
            expect(page.locator("main").get_by_text("Demo", exact=True).first).to_be_visible(timeout=180000)
            page.get_by_role("button", name="Choose Solo").click()
            expect(page.get_by_text(re.compile(r"This is the demo, so nothing is billed"))).to_be_visible()
            check(page.url.endswith("/Admin2Ai/settings/plan/"), "demo: choosing a plan stays on the page")
            check(not [u for u in seen if "stripe" in u or "/api/billing" in u], "demo: no payment provider is called")
            top = page.locator("header nav[aria-label=Main] a").evaluate_all(
                "els => els.map(e => { const c = e.cloneNode(true); c.querySelectorAll('[class*=badge]').forEach(b => b.remove());"
                " return c.textContent.replace(/\\s+/g, ' ').trim(); })")
            check(top == ["Home", "Needs You", "Companies", "Sources", "Activity"], f"demo: the header has the five places {top}")
            demo_sources(page, port, expect)
            for path in ["/signin/", "/signup/"]:
                r = urllib.request.Request(f"http://{HOST}:{port}/Admin2Ai{path}")
                try:
                    urllib.request.urlopen(r, timeout=5)
                    check(False, f"demo {path} does not exist")
                except Exception:  # noqa: BLE001 - 404 is the point
                    check(True, f"demo {path} does not exist")
            browser.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- reading a photo in the browser

RECEIPT = WEB.parent / "backend" / "tests" / "fixtures" / "photos" / "fs-pb2026-0441.jpg"
BLURRED = RECEIPT.with_name("fs-mr2026-0088-blurred.jpg")
SCAN = WEB.parent / "backend" / "tests" / "fixtures" / "documents" / "central-fs-cc2026-3317-scan.pdf"


@contextmanager
def static_site() -> Iterator[int]:
    """The static export served under /Admin2Ai, as GitHub Pages serves it."""
    out = WEB / "out"
    if not (out / "index.html").exists():
        raise SystemExit("Build the static demo first: npm run build:pages")
    if not (out / "ocr" / "tesseract" / "worker.min.js").exists():
        raise SystemExit("The static build has no OCR files (npm run engine:ocr is part of build:pages)")
    root = Path(tempfile.mkdtemp())
    try:
        (root / "Admin2Ai").symlink_to(out)
        port = free_port()

        class Static(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a: Any, **kw: Any) -> None:
                super().__init__(*a, directory=str(root), **kw)

            def log_message(self, *args: Any) -> None:
                pass

            def copyfile(self, source: Any, outputfile: Any) -> None:
                try:
                    super().copyfile(source, outputfile)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        with serve(Static, port):
            yield port
    finally:
        shutil.rmtree(root, ignore_errors=True)


def ocr() -> None:
    from playwright.sync_api import expect, sync_playwright

    with static_site() as port, sync_playwright() as pw:
        site = f"http://{HOST}:{port}/Admin2Ai"
        browser = pw.chromium.launch()
        page = browser.new_page()
        seen: list[str] = []
        page.on("request", lambda r: seen.append(r.url))
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        print("first load")
        page.goto(f"{site}/scan/")
        expect(page.get_by_text("They are read right here in your browser")).to_be_visible(timeout=30000)
        page.wait_for_timeout(1500)
        check(not [u for u in seen if "/ocr/" in u], "the OCR files are not loaded until a photo is uploaded")

        print("a photographed café receipt")
        started = time.time()
        page.locator("input[type=file][multiple]").set_input_files(str(RECEIPT))
        row = page.locator("li", has_text=RECEIPT.name)
        expect(row.get_by_text("Got it.")).to_be_visible(timeout=240000)
        took = time.time() - started
        print(f"  read and filed in {took:.1f} s (OCR engine and models loaded on demand)")
        ocr_bytes = sum(1 for u in seen if "/ocr/" in u)
        check(ocr_bytes > 0, "the OCR files were loaded for the photo")
        check(any(u.endswith("/ocr/tesseract/lang/por.traineddata.gz") for u in seen), "the Portuguese model is self-hosted")
        foreign = sorted({u for u in seen if not u.startswith(f"http://{HOST}:{port}/") and not u.startswith(("data:", "blob:"))})
        check(not foreign, f"nothing is fetched from anywhere but the site itself {foreign[:3]}")

        def documents_show_the_receipt(when: str) -> None:
            page.goto(f"{site}/documents/")
            listed = page.locator("li", has_text="Pastelaria")
            expect(listed.first).to_be_visible(timeout=120000)
            text = listed.first.inner_text()
            check("PB2026/441" in text, f"{when}: Documents lists the receipt's number")
            check("8.40" in text, f"{when}: Documents shows its total, €8.40")

        journal = json.loads(page.evaluate("sessionStorage.getItem('admin2ai:engine-journal')") or "[]")
        sent = [e["body"] for e in journal if e["path"] == "/api/evidence"]
        reading = (sent[-1] or {}).get("reading") if sent else None
        check(bool(reading) and reading.get("method") == "ocr_browser", "the upload carries the browser's reading (journaled)")
        if reading:
            text = "\n".join(line["text"] for page_ in reading["pages"] for line in page_["lines"])
            print(f"  OCR in the browser: {len(text.splitlines())} lines in {reading['ms'] / 1000:.1f} s, sharpness {reading['sharpness']}")
            check("PASTELARIA" in text.upper() and "8,40" in text, "tesseract.js read the supplier and the total")
            check(any(q.startswith("A:509882412*B:516123459*") for q in reading["qr"]), "jsQR decoded the fiscal QR code on the photo")
            check((reading.get("sharpness") or 0) > 0.32, "the photo measured sharp: nobody is asked to retake it")
            # The same upload through the same engine code, natively: the reading and the QR code agree (GREEN).
            sys.path.insert(0, str(BACKEND_SRC))
            from backoffice.reading import BrowserReader
            from backoffice.service import BackOfficeService

            engine = BackOfficeService.demo()
            engine.repo.reader = BrowserReader()
            status, out = engine.dispatch("POST", "/api/evidence", json.dumps(sent[-1]))
            docs = [engine.repo.documents[d["id"]].document for d in out.get("documents", [])] if status == 200 else []
            check(len(docs) == 1 and docs[0].quality.value == "verified" and str(docs[0].gross_amount) == "8.40",
                  "the engine verified it: what the browser read agrees with the fiscal QR code")
        documents_show_the_receipt("after the upload")
        reads = sum(1 for u in seen if u.endswith("/ocr/tesseract/worker.min.js"))
        page.reload()
        documents_show_the_receipt("after a reload")
        check(sum(1 for u in seen if u.endswith("/ocr/tesseract/worker.min.js")) == reads, "a reload replays the reading: the photo is not read again")

        print("a scanned PDF and a blurred photo")
        page.goto(f"{site}/scan/")
        upload = page.locator("input[type=file][multiple]")
        upload.set_input_files(str(SCAN))
        expect(page.locator("li", has_text=SCAN.name).get_by_text("Got it.")).to_be_visible(timeout=240000)
        journal = json.loads(page.evaluate("sessionStorage.getItem('admin2ai:engine-journal')") or "[]")
        scan = [e["body"]["reading"] for e in journal if e["path"] == "/api/evidence" and e["body"].get("filename") == SCAN.name]
        check(bool(scan) and scan[0]["textLayer"] == [""] and len(scan[0]["pages"]) == 1, "pdf.js found no text layer and the page was rendered and read")
        check(bool(scan) and any(q.startswith("A:516722344*") for q in scan[0]["qr"]), "the QR code was decoded on the rendered page")
        upload.set_input_files(str(BLURRED))
        expect(page.locator("li", has_text=BLURRED.name).get_by_text("Take it again?")).to_be_visible(timeout=240000)
        check(True, "a badly blurred photo becomes the plain retake task")
        page.goto(f"{site}/documents/")
        scanned = page.locator("li", has_text="CC2026/3317")
        expect(scanned.first).to_be_visible(timeout=120000)
        check("23.90" in scanned.first.inner_text(), "Documents shows the scanned receipt's total, €23.90")
        crashed = [e for e in errors if "Uncaught" in e]
        check(not crashed, f"no uncaught errors {crashed[:2]}")
        browser.close()


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "production"
    {"production": production, "demo": demo, "ocr": ocr}[which]()
    print(f"\n{len(failures)} failed" if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)
