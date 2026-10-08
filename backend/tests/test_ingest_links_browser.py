"""Isolated browser sessions with a real Chromium (§9 step 4, §44, QA C9).

A script-only invoice page is opened in Chromium through Playwright, exactly as the server does
(``backoffice.server.links.link_fetcher_from_env``): one fresh browser and one empty context per link,
in a separate worker process that is killed when it overruns. These tests drive a real Chromium against a
local test web server that stands in for the internet: Chromium reaches it as an HTTP proxy, so the pages
have ordinary public names (``supplier-a.example``), and the request guard sees them at public addresses
(a fixed DNS answer). Anything private (the loopback address the test server really listens on, a
``10.x`` address, the cloud metadata service, a name that resolves to a private address, ``file:``) is
what the guard must refuse, and the server records every request that reaches it, so a leak would show.

Proven here: a cookie, local storage, session storage or cache entry made while opening one link is not
visible when opening the next (the same session object, two businesses); a page cannot reach a private
address or a local file; a download the page starts is captured and registered as evidence with its
provenance; a page that hangs is stopped at its deadline.

The tests skip only when Chromium cannot start here (``pip install playwright`` and
``python -m playwright install chromium``). CI installs both and sets ``BACKOFFICE_REQUIRE_BROWSER=1``, so
there a missing browser fails instead of skipping (``test_infra_deploy_files.py`` keeps CI that way).
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import pytest

from backoffice.domain.models import EvidenceFormat
from backoffice.evidence.links import (
    BrowserError,
    LinkFetcher,
    LinkOutcome,
    PlaywrightBrowserSession,
    PlaywrightConfig,
    UrlSafety,
    UrlSafetyConfig,
    register_fetch,
)
from backoffice.evidence.store import EvidenceRegistry, LocalObjectStore

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
REQUIRE = bool(os.environ.get("BACKOFFICE_REQUIRE_BROWSER"))


def _chromium_problem() -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "playwright is not installed"
    try:
        with sync_playwright() as pw:
            pw.chromium.launch().close()
    except Exception as exc:  # noqa: BLE001 - any failure to start means no browser here
        return f"Chromium cannot start here ({type(exc).__name__})"
    return None


@pytest.fixture(scope="module", autouse=True)
def chromium() -> None:
    problem = _chromium_problem()
    if problem is not None:
        if REQUIRE:
            pytest.fail(f"a real Chromium must run here: {problem}", pytrace=False)
        pytest.skip(problem)


# --------------------------------------------------------------------------- the stand-in internet


@dataclass(frozen=True)
class FixedDNS:
    """Picklable DNS for the request guard (the worker process rebuilds it): fixed answers, nothing else."""

    answers: tuple[tuple[str, str], ...]

    def resolve(self, host: str, port: int) -> list[str]:
        return [ip for name, ip in self.answers if name == host]


DNS = FixedDNS((
    ("supplier-a.example", "93.184.215.14"),
    ("supplier-b.example", "93.184.215.15"),
    ("billing.supplier-b.example", "93.184.215.15"),
    ("rebind.example", "192.168.1.20"),  # a public-looking name that resolves to a private address
))

PAGES = {
    # Link A: the supplier's page sets a session cookie (header and script), local and session storage, and
    # loads a cacheable script.
    ("supplier-a.example", "/invoice"): (200, "text/html", {"Set-Cookie": "sid=tenant-a-secret; Path=/"}, """
<html><head><title>Invoice</title><script src="/app.js"></script></head><body><div id="app"></div><script>
document.cookie = "basket=tenant-a; path=/";
localStorage.setItem("tenant", "a");
sessionStorage.setItem("tenant", "a");
document.getElementById("app").textContent = "stored";
</script></body></html>"""),
    # Link B, the same supplier for another business: it shows what the browser still knows.
    ("supplier-a.example", "/statement"): (200, "text/html", {}, """
<html><head><title>Statement</title><script src="/app.js"></script></head><body><pre id="state"></pre><script>
document.getElementById("state").textContent =
  "cookies=[" + document.cookie + "] local=" + localStorage.length + " session=" + sessionStorage.length;
</script></body></html>"""),
    ("supplier-a.example", "/app.js"): (200, "application/javascript", {"Cache-Control": "public, max-age=3600"},
                                        "window.appLoaded = true;"),
    # A hostile page: it tries the loopback address the test server really listens on, private and metadata
    # addresses, a name that resolves to a private address and a local file; then one public request, which
    # must arrive (the page's script did run).
    ("supplier-a.example", "/hostile"): (200, "text/html", {}, """
<html><body><pre id="out"></pre><script>
const out = document.getElementById("out");
const note = (name, p) => p.then(() => out.textContent += name + "=reached ").catch(() => out.textContent += name + "=refused ");
note("loopback", fetch("http://127.0.0.1:__PORT__/secret"));
note("private", fetch("http://10.0.0.7/admin"));
note("metadata", fetch("http://169.254.169.254/latest/meta-data/"));
note("metadata-name", fetch("http://metadata.google.internal/computeMetadata/v1/"));
note("rebind", fetch("http://rebind.example/x"));
note("file", fetch("file:///etc/passwd"));
const frame = document.createElement("iframe"); frame.src = "file:///etc/passwd"; document.body.appendChild(frame);
const img = new Image(); img.src = "http://127.0.0.1:__PORT__/pixel.gif";
note("public", fetch("http://supplier-b.example/ping", {mode: "no-cors"}));
</script></body></html>"""),
    ("supplier-a.example", "/to-file"): (200, "text/html", {}, """
<html><body>Redirecting<script>location.href = "file:///etc/passwd";</script></body></html>"""),
    ("supplier-a.example", "/to-loopback"): (200, "text/html", {}, """
<html><body>Redirecting<script>location.href = "http://127.0.0.1:__PORT__/secret";</script></body></html>"""),
    ("supplier-b.example", "/ping"): (200, "text/plain", {}, "pong"),
    # A billing portal that only works with JavaScript: the script starts the invoice download.
    ("billing.supplier-b.example", "/portal"): (200, "text/html", {}, """
<html><head><title>Billing</title></head><body><div id="root"></div><script src="/portal.js"></script>
<noscript>Enable JavaScript to see your invoice</noscript></body></html>"""),
    ("billing.supplier-b.example", "/portal.js"): (200, "application/javascript", {}, """
const a = document.createElement("a"); a.href = "/files/INV-2026-0042.pdf"; a.download = "INV-2026-0042.pdf";
document.body.appendChild(a); a.click();"""),
    ("billing.supplier-b.example", "/files/INV-2026-0042.pdf"): (
        200, "application/pdf", {"Content-Disposition": 'attachment; filename="INV-2026-0042.pdf"'}, PDF),
}
SLOW = {("supplier-a.example", "/hangs")}  # never answers within the test


@dataclass(frozen=True)
class Seen:
    host: str
    path: str
    cookie: str | None
    via_proxy: bool


class Internet(BaseHTTPRequestHandler):
    """The test web server: an HTTP proxy for the public names, and a plain server on its real address."""

    seen: list[Seen] = []
    port = 0

    def do_GET(self) -> None:  # noqa: N802
        via_proxy = self.path.startswith("http://")
        parts = urlsplit(self.path) if via_proxy else None
        host = (self.headers.get("Host") or (parts.hostname if parts else "") or "").split(":")[0]
        path = (parts.path or "/") if parts else self.path.split("?")[0]
        type(self).seen.append(Seen(host, path, self.headers.get("Cookie"), via_proxy))
        if (host, path) in SLOW:
            time.sleep(30)
            return
        status, kind, headers, body = PAGES.get((host, path), (404, "text/plain", {}, "not found"))
        data = body if isinstance(body, bytes) else body.replace("__PORT__", str(type(self).port)).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        return None


@pytest.fixture
def internet() -> Iterator[ThreadingHTTPServer]:
    Internet.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Internet)
    server.daemon_threads = True
    Internet.port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def config(server: ThreadingHTTPServer) -> PlaywrightConfig:
    return PlaywrightConfig(resolver=DNS, proxy=f"http://127.0.0.1:{server.server_port}")


def requests_to(host: str, path: str | None = None) -> list[Seen]:
    return [s for s in Internet.seen if s.host == host and (path is None or s.path == path)]


# --------------------------------------------------------------------------- isolation


@pytest.mark.parametrize("isolate_process", [True, False], ids=["worker-process", "same-process"])
def test_nothing_one_link_stores_is_visible_when_the_next_link_is_opened(internet, isolate_process) -> None:
    session = PlaywrightBrowserSession(config(internet), isolate_process=isolate_process)  # shared by businesses

    first = session.render("http://supplier-a.example/invoice", timeout_s=15)  # business A's link
    assert "stored" in first.html  # the page ran: it stored a cookie, local and session storage
    second = session.render("http://supplier-a.example/statement", timeout_s=15)  # business B's link

    assert "cookies=[] local=0 session=0" in second.html
    [statement] = requests_to("supplier-a.example", "/statement")
    assert statement.cookie is None  # the server set sid=..., the script set basket=...: neither is sent back
    assert len(requests_to("supplier-a.example", "/app.js")) == 2  # no shared cache either
    assert all(s.via_proxy for s in Internet.seen)


# --------------------------------------------------------------------------- no local network, no local files


def test_a_page_cannot_reach_private_addresses_or_local_files(internet) -> None:
    page = PlaywrightBrowserSession(config(internet)).render("http://supplier-a.example/hostile", timeout_s=15)

    for name in ("loopback", "private", "metadata", "metadata-name", "rebind", "file"):
        assert f"{name}=refused" in page.html, name
    assert "public=reached" in page.html  # the script ran and public requests still work
    assert requests_to("supplier-b.example", "/ping")
    hosts = {s.host for s in Internet.seen}
    assert hosts == {"supplier-a.example", "supplier-b.example"}  # nothing else was asked for
    assert not [s for s in Internet.seen if not s.via_proxy]  # and nothing reached the test server directly
    assert "root:" not in page.html


@pytest.mark.parametrize("path", ["/to-file", "/to-loopback"])
def test_a_page_that_navigates_to_a_local_file_or_address_goes_nowhere(internet, path) -> None:
    page = PlaywrightBrowserSession(config(internet)).render(f"http://supplier-a.example{path}", timeout_s=15)
    assert not page.final_url.startswith(("file:", "http://127.")) and "root:" not in page.html
    assert {s.host for s in Internet.seen} == {"supplier-a.example"}
    # Where it ended (the page itself, or the browser's error page) is checked again by the link fetcher:
    assert page.final_url == f"http://supplier-a.example{path}" or not UrlSafety().check(page.final_url).safe


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/", "http://10.0.0.7/", "http://rebind.example/"])
def test_unsafe_links_are_refused_before_any_browser_starts(internet, url) -> None:
    started = time.monotonic()
    with pytest.raises(BrowserError, match="unsafe"):
        PlaywrightBrowserSession(config(internet), isolate_process=False).render(url, timeout_s=15)
    assert time.monotonic() - started < 2 and not Internet.seen


# --------------------------------------------------------------------------- downloads become evidence


def test_a_download_started_by_a_script_page_is_captured_and_registered_as_evidence(internet, tmp_path) -> None:
    proxy = f"http://127.0.0.1:{internet.server_port}"
    fetcher = LinkFetcher(UrlSafety(UrlSafetyConfig(), resolver=DNS), transport=httpx.HTTPTransport(proxy=proxy),
                          browser=PlaywrightBrowserSession(config(internet)))

    result = fetcher.fetch("http://billing.supplier-b.example/portal")

    assert result.outcome is LinkOutcome.DOWNLOADED and result.via_browser
    assert result.content == PDF and result.filename == "INV-2026-0042.pdf" and result.format is EvidenceFormat.PDF
    assert requests_to("billing.supplier-b.example", "/files/INV-2026-0042.pdf")
    registry = EvidenceRegistry(LocalObjectStore(tmp_path))
    [registration] = register_fetch(result, registry, tenant_id="t1")
    evidence = registration.evidence
    assert evidence.format is EvidenceFormat.PDF and registry.open("t1", evidence.id) == PDF
    assert evidence.original_url == "http://billing.supplier-b.example/portal" and evidence.retrieved_at is not None


# --------------------------------------------------------------------------- deadlines


def test_a_page_that_never_answers_is_stopped_at_its_deadline(internet) -> None:
    session = PlaywrightBrowserSession(config(internet), startup_grace_s=20)
    started = time.monotonic()
    with pytest.raises(BrowserError):
        session.render("http://supplier-a.example/hangs", timeout_s=2)
    assert time.monotonic() - started < 2 + 20


def test_a_page_that_hangs_the_browser_is_stopped_at_its_deadline_and_leaves_nothing_running(internet) -> None:
    PAGES[("supplier-a.example", "/spin")] = (200, "text/html", {}, "<html><body><script>while (true) {}</script>")
    try:
        session = PlaywrightBrowserSession(config(internet), startup_grace_s=6)
        started = time.monotonic()
        with pytest.raises(BrowserError):
            session.render("http://supplier-a.example/spin", timeout_s=2)
        assert time.monotonic() - started < 2 + 6 + 3  # past this the worker is killed (test_ingest_links.py)
    finally:
        del PAGES[("supplier-a.example", "/spin")]
    assert not multiprocessing.active_children()  # the worker process is gone, and its browser with it


# --------------------------------------------------------------------------- the server


def test_the_server_opens_script_pages_in_the_isolated_browser_unless_switched_off(monkeypatch) -> None:
    from backoffice.server.links import link_fetcher_from_env

    monkeypatch.delenv("BACKOFFICE_LINK_BROWSER", raising=False)
    monkeypatch.delenv("BACKOFFICE_LINK_FETCHING", raising=False)
    fetcher = link_fetcher_from_env()
    assert isinstance(fetcher.browser, PlaywrightBrowserSession) and fetcher.browser.isolate_process
    monkeypatch.setenv("BACKOFFICE_LINK_BROWSER", "off")
    assert link_fetcher_from_env().browser is None

