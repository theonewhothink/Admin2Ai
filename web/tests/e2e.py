"""Browser checks for the web app's production mode and the static demo.

    python tests/e2e.py production   # builds with sign-in on, runs against a mock API
    python tests/e2e.py demo         # needs `npm run build:pages` first (out/)

production: builds the app with NEXT_PUBLIC_REQUIRE_SIGNIN=1 against a tiny
mock of the production API contract written here (cookie session, CSRF
header, 401s), then drives Chromium through sign-in, sign-up, onboarding,
Settings → Account and sign-out. Data endpoints are answered by the real
demo engine (backend/src), so the pages render real shapes.

demo: serves the static export under /Admin2Ai and checks that no page
redirects to sign-in or calls an auth endpoint.

Needs: Python Playwright with Chromium (`pip install playwright` and
`python -m playwright install chromium`). Screenshots go to $SCREENSHOT_DIR
when it is set.
"""

from __future__ import annotations

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
            if method == "POST" and self.headers.get("X-Requested-With") != "admin2ai":
                return self.reply(403, {"error": "csrf", "message": "Refresh the page and try again."})
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
            if path == "/api/connections/bank/start":
                return self.reply(200, {"redirectUrl": f"http://{HOST}:{self.server.server_address[1]}/fake-bank?institution={body.get('institutionId')}"})
            status, data = api.engine.dispatch(method, path + (f"?{query}" if query else ""), body or None)
            if session["fresh"] and path == "/api/home":
                data = data | {"connections": [c for c in data["connections"] if c["kind"] == "bank"] if session["bank"] else []}
            if session["fresh"] and path == "/api/companies":
                data = {"companies": [{**data["companies"][0], "id": "first", "name": session["tenant"]}]}
            return self.reply(status, data)

        @staticmethod
        def who(session: dict[str, Any]) -> dict[str, Any]:
            u = session["user"]
            return {"user": {"id": u["id"], "email": u["email"], "name": u["name"]}, "tenant": {"id": "t1", "name": session["tenant"]}, "role": "owner"}

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

        print("sign out")
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


# --------------------------------------------------------------------------- demo


def demo() -> None:
    from playwright.sync_api import sync_playwright

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
            for path in ["/", "/settings/", "/needs-you/", "/onboarding/", "/companies/hazel-tree/", "/accountant/"]:
                page.goto(f"http://{HOST}:{port}/Admin2Ai{path}")
                page.wait_for_timeout(1500)
                check("/signin" not in page.url, f"demo {path}: no redirect to sign-in")
                check(len(page.locator("main").first.inner_text().strip()) > 20, f"demo {path}: the page rendered")
            check(not [u for u in seen if "/api/auth" in u or "/signin" in u or "/signup" in u], "demo: no auth request, no sign-in page loaded")
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


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "production"
    {"production": production, "demo": demo}[which]()
    print(f"\n{len(failures)} failed" if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)
