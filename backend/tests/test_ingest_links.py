"""Link intelligence (§9): URL safety, redirects, content, login/MFA walls, browser."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from backoffice.domain.models import EvidenceFormat
from backoffice.evidence.domains import (
    LookalikeChecker,
    LookalikeKind,
    display_name_for_host,
    registrable_domain,
    to_ascii_host,
)
from backoffice.evidence.links import (
    BLOCKED_MESSAGE,
    BrowserDownload,
    BrowserError,
    BrowserUnavailable,
    FetchPolicy,
    LinkFetcher,
    LinkOutcome,
    PlaywrightBrowserSession,
    PlaywrightConfig,
    RenderedPage,
    UnsafeReason,
    UrlSafety,
    UrlSafetyConfig,
    register_fetch,
)
from backoffice.evidence.store import EvidenceRegistry, LocalObjectStore

T0 = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n"
PUBLIC = {
    "click.mailer.example": ["93.184.216.34"],
    "u123.ct.sendgrid.net": ["93.184.216.41"],
    "login.microsoftonline.com": ["93.184.216.42"],
    "minha.vodafone.pt": ["2a00:1450:4003:80e::2003", "93.184.216.35"],
    "www.vodafone.pt": ["93.184.216.35"],
    "billing.acme-cloud.com": ["93.184.216.36"],
    "files.acme-cloud.com": ["93.184.216.37"],
    "rebind.example": ["93.184.216.38", "10.0.0.5"],
    "internal.example": ["192.168.1.10"],
    "meta.example": ["169.254.169.254"],
    "cgnat.example": ["100.64.1.1"],
    "ula.example": ["fd12:3456::1"],
    "mapped.example": ["::ffff:127.0.0.1"],
    "sixtofour.example": ["2002:c0a8:0101::1"],
}


class FakeResolver:
    def __init__(self, table=None):
        self.table = dict(PUBLIC if table is None else table)
        self.calls: list[str] = []

    def resolve(self, host, port):
        self.calls.append(host)
        return self.table.get(host, [])


def safety(**kw) -> UrlSafety:
    config = UrlSafetyConfig(known_domains=("vodafone.pt", "acme-cloud.com"), **kw)
    return UrlSafety(config, FakeResolver())


# --------------------------------------------------------------------------- domains


def test_registrable_domain_and_display_names():
    assert registrable_domain("my.vodafone.pt") == "vodafone.pt"
    assert registrable_domain("a.b.example.co.uk") == "example.co.uk"
    assert registrable_domain("faturas.edp.com.pt") == "edp.com.pt"
    assert display_name_for_host("faturas.vodafone.pt") == "Vodafone"
    assert display_name_for_host("www.acme-cloud.com") == "Acme Cloud"


@pytest.mark.parametrize(
    ("host", "kind", "imitates"),
    [
        ("vodafοne.pt", LookalikeKind.HOMOGLYPH, "vodafone.pt"),  # Greek omicron
        ("vodаfone.pt", LookalikeKind.HOMOGLYPH, "vodafone.pt"),  # Cyrillic a inside Latin
        ("shоp-billing.com", LookalikeKind.MIXED_SCRIPT, None),  # Cyrillic o, imitates nothing known
        ("vodaf0ne.pt", LookalikeKind.HOMOGLYPH, "vodafone.pt"),
        ("vodafome.pt", LookalikeKind.TYPO, "vodafone.pt"),
        ("vodaofne.pt", LookalikeKind.TYPO, "vodafone.pt"),
        ("vodafone.pt.secure-billing.com", LookalikeKind.EMBEDDED, "vodafone.pt"),
        ("arnazon.es", LookalikeKind.HOMOGLYPH, "amazon.es"),  # rn -> m
    ],
)
def test_lookalikes_are_caught(host, kind, imitates):
    checker = LookalikeChecker(["vodafone.pt", "amazon.es", "edp.pt"])
    finding = checker.check(host)
    assert finding is not None and finding.kind is kind and finding.imitates == imitates


@pytest.mark.parametrize("host", ["vodafone.pt", "minha.vodafone.pt", "unrelated.org", "edf.pt", "münchen.de"])
def test_legitimate_hosts_pass(host):
    assert LookalikeChecker(["vodafone.pt", "edp.pt"]).check(host) is None  # short names: no typo check


def test_punycode_hosts_are_decoded_before_comparison():
    punycode = to_ascii_host("vodafοne.pt")
    assert punycode.startswith("xn--")
    assert LookalikeChecker(["vodafone.pt"]).check(punycode).kind is LookalikeKind.HOMOGLYPH


# --------------------------------------------------------------------------- URL safety


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://minha.vodafone.pt/f.pdf", UnsafeReason.SCHEME),
        ("javascript:alert(1)", UnsafeReason.SCHEME),
        ("file:///etc/passwd", UnsafeReason.SCHEME),
        ("https://minha.vodafone.pt/" + "a" * 3000, UnsafeReason.TOO_LONG),
        ("https://vodafone.pt@evil.example/", UnsafeReason.CREDENTIALS),
        ("https://minha.vodafone.pt:22/", UnsafeReason.PORT),
        ("https://minha.vodafone.pt:99999/", UnsafeReason.MALFORMED),
        ("https://evil.example\\@minha.vodafone.pt/", UnsafeReason.MALFORMED),
        ("https://minha.vodafone.pt/a b", UnsafeReason.MALFORMED),
        ("http://127.0.0.1/", UnsafeReason.PRIVATE_ADDRESS),
        ("http://[::1]/", UnsafeReason.PRIVATE_ADDRESS),
        ("http://169.254.169.254/latest/meta-data/", UnsafeReason.METADATA_ADDRESS),
        ("http://2130706433/", UnsafeReason.MALFORMED),
        ("http://0x7f.1/", UnsafeReason.MALFORMED),
        ("http://localhost/", UnsafeReason.BLOCKED_HOST),
        ("http://metadata.google.internal/", UnsafeReason.BLOCKED_HOST),
        ("http://printer.local/", UnsafeReason.BLOCKED_HOST),
        ("http://intranet/", UnsafeReason.BLOCKED_HOST),
        ("https://internal.example/", UnsafeReason.PRIVATE_ADDRESS),
        ("https://meta.example/", UnsafeReason.METADATA_ADDRESS),
        ("https://cgnat.example/", UnsafeReason.PRIVATE_ADDRESS),
        ("https://ula.example/", UnsafeReason.PRIVATE_ADDRESS),
        ("https://mapped.example/", UnsafeReason.PRIVATE_ADDRESS),
        ("https://sixtofour.example/", UnsafeReason.PRIVATE_ADDRESS),
        ("https://rebind.example/", UnsafeReason.PRIVATE_ADDRESS),  # one private answer poisons it
        ("https://nowhere.example/", UnsafeReason.UNRESOLVABLE),
        ("https://vodafοne.pt/fatura", UnsafeReason.LOOKALIKE),
    ],
)
def test_unsafe_urls_are_blocked(url, reason):
    check = safety().check(url)
    assert not check.safe and check.reason is reason


def test_safe_url_reports_validated_addresses():
    check = safety().check("https://minha.vodafone.pt/faturas/1")
    assert check.safe and check.host == "minha.vodafone.pt" and check.port == 443
    assert check.addresses == ("2a00:1450:4003:80e::2003", "93.184.216.35")


def test_http_can_be_disallowed_and_resolver_errors_are_unresolvable():
    assert safety(allow_http=False).check("http://minha.vodafone.pt/").reason is UnsafeReason.SCHEME

    class Exploding:
        def resolve(self, host, port):
            raise OSError("dns down")

    assert UrlSafety(resolver=Exploding()).check("https://a.example/").reason is UnsafeReason.UNRESOLVABLE


def test_public_ip_literal_needs_no_dns():
    resolver = FakeResolver({})
    check = UrlSafety(resolver=resolver).check("https://93.184.216.34/x")
    assert check.safe and check.host_is_ip and resolver.calls == []


# --------------------------------------------------------------------------- fetching


class Site:
    """Routes MockTransport requests by the Host header (connections are pinned to IPs)."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.headers["host"], request.url.path)
        handler = self.routes.get(key)
        if handler is None:
            return httpx.Response(404, text="not found")
        return handler(request) if callable(handler) else handler


def fetcher(routes, *, browser=None, policy=None, config=None) -> tuple[LinkFetcher, Site]:
    site = Site(routes)
    safe = UrlSafety(config or UrlSafetyConfig(known_domains=("vodafone.pt", "acme-cloud.com")), FakeResolver())
    return LinkFetcher(safe, transport=httpx.MockTransport(site), browser=browser, policy=policy,
                       clock=lambda: T0), site


def pdf_response(name: str = "Fatura FT 1.pdf") -> httpx.Response:
    return httpx.Response(200, content=PDF, headers={
        "content-type": "application/pdf", "content-disposition": f"attachment; filename*=UTF-8''{name.replace(' ', '%20')}"})


def test_tracked_link_redirects_to_pdf_with_full_provenance():
    f, site = fetcher({
        ("click.mailer.example", "/c"): httpx.Response(302, headers={"location": "https://minha.vodafone.pt/f/1"}),
        ("minha.vodafone.pt", "/f/1"): httpx.Response(303, headers={"location": "/f/1.pdf", "set-cookie": "s=1; Path=/"}),
        ("minha.vodafone.pt", "/f/1.pdf"): pdf_response(),
    })
    result = f.fetch("https://click.mailer.example/c?id=9")
    assert result.outcome is LinkOutcome.DOWNLOADED
    assert result.content == PDF and result.format is EvidenceFormat.PDF
    assert result.filename == "Fatura FT 1.pdf"
    record = result.record
    assert record.original_url == "https://click.mailer.example/c?id=9"
    assert record.final_url == "https://minha.vodafone.pt/f/1.pdf"
    assert [(h.status, h.kind) for h in record.redirect_chain] == [(302, "request"), (303, "http"), (200, "http")]
    assert record.retrieved_at == T0 and record.sha256 and record.content_type == "application/pdf"
    # Connections go to the validated address, with the real name for Host and TLS SNI.
    last = site.requests[-1]
    assert last.url.host == "2a00:1450:4003:80e::2003"
    assert last.headers["host"] == "minha.vodafone.pt"
    assert last.extensions["sni_hostname"] == "minha.vodafone.pt"
    assert last.headers["cookie"] == "s=1"  # cookies follow the real host, not the IP


def test_every_redirect_hop_is_revalidated():
    f, site = fetcher({
        ("click.mailer.example", "/c"): httpx.Response(302, headers={"location": "http://169.254.169.254/latest"}),
    })
    result = f.fetch("https://click.mailer.example/c")
    assert result.outcome is LinkOutcome.BLOCKED_UNSAFE
    assert result.reason == "metadata_address" and result.owner_message == BLOCKED_MESSAGE
    assert len(site.requests) == 1  # the metadata address was never contacted


def test_redirect_to_lookalike_domain_is_blocked():
    f, _ = fetcher({("click.mailer.example", "/c"): httpx.Response(
        302, headers={"location": "https://vodafone.pt.secure-billing.com/f"})})
    assert f.fetch("https://click.mailer.example/c").reason == "lookalike_domain"


def test_too_many_redirects_and_loops():
    hop = lambda n: httpx.Response(302, headers={"location": f"/r{n + 1}"})  # noqa: E731
    routes = {("www.vodafone.pt", f"/r{n}"): hop(n) for n in range(10)}
    f, _ = fetcher(routes, policy=FetchPolicy(max_hops=3))
    result = f.fetch("https://www.vodafone.pt/r0")
    assert result.outcome is LinkOutcome.UNAVAILABLE and result.reason == "too_many_redirects"
    loop, _ = fetcher({("www.vodafone.pt", "/a"): httpx.Response(302, headers={"location": "/a"})})
    assert loop.fetch("https://www.vodafone.pt/a").reason == "redirect_loop"


def test_meta_refresh_is_followed():
    f, _ = fetcher({
        ("billing.acme-cloud.com", "/i/5521"): httpx.Response(
            200, html='<meta http-equiv="refresh" content="0; url=https://files.acme-cloud.com/5521.pdf">'),
        ("files.acme-cloud.com", "/5521.pdf"): pdf_response("INV-5521.pdf"),
    })
    result = f.fetch("https://billing.acme-cloud.com/i/5521")
    assert result.outcome is LinkOutcome.DOWNLOADED
    assert result.record.redirect_chain[-1].kind == "meta_refresh"


def test_landing_page_download_link_is_followed_once():
    page = ('<html><body><h1>Invoice INV-5521</h1><p>' + "Details of your invoice. " * 20 +
            '</p><a href="/help">Help</a><a class="btn" href="/i/5521/download.pdf">Download PDF</a></body></html>')
    f, _ = fetcher({
        ("billing.acme-cloud.com", "/i/5521"): httpx.Response(200, html=page),
        ("billing.acme-cloud.com", "/i/5521/download.pdf"): pdf_response("INV-5521.pdf"),
    })
    result = f.fetch("https://billing.acme-cloud.com/i/5521")
    assert result.outcome is LinkOutcome.DOWNLOADED and result.filename == "INV-5521.pdf"
    assert result.record.redirect_chain[-1].kind == "document_link"


def test_page_without_a_file_is_preserved_as_rendered_evidence():
    page = "<html><head><title>Receipt</title></head><body>" + "Order 7781 total 39,00 EUR. " * 20 + "</body></html>"
    f, _ = fetcher({("billing.acme-cloud.com", "/r/7781"): httpx.Response(
        200, content=page.encode(), headers={"content-type": "text/html; charset=utf-8"})})
    result = f.fetch("https://billing.acme-cloud.com/r/7781")
    assert result.outcome is LinkOutcome.RENDERED_PAGE
    assert result.format is EvidenceFormat.HTML and result.content == page.encode()
    assert result.owner_message is None


def test_login_wall_asks_for_a_stored_session():
    login = ('<html><title>Entrar</title><form action="/login"><input type="email" name="email">'
             '<input type="password" name="password"></form></html>')
    f, _ = fetcher({("minha.vodafone.pt", "/f/9"): httpx.Response(200, html=login)})
    result = f.fetch("https://minha.vodafone.pt/f/9")
    assert result.outcome is LinkOutcome.LOGIN_REQUIRED and result.retryable
    assert result.owner_message == "Vodafone needs you to sign in."
    assert result.content is None


def test_mfa_prompt_uses_the_spec_wording_and_supplier_name():
    mfa = ('<form action="/verify"><label>Introduza o código que enviámos por SMS</label>'
           '<input name="otp" autocomplete="one-time-code" inputmode="numeric"></form>')
    f, _ = fetcher({("minha.vodafone.pt", "/f/9"): httpx.Response(200, html=mfa)})
    result = f.fetch("https://minha.vodafone.pt/f/9")
    assert result.outcome is LinkOutcome.MFA_REQUIRED
    assert result.owner_message == "Vodafone needs authentication."
    named = f.fetch("https://minha.vodafone.pt/f/9", supplier_name="Vodafone Portugal")
    assert named.owner_message == "Vodafone Portugal needs authentication."


def test_supplier_name_skips_tracking_and_sign_in_hosts():
    login = '<form action="/login"><input type="password"></form>'
    f, _ = fetcher({
        ("u123.ct.sendgrid.net", "/ls/click"): httpx.Response(302, headers={"location": "https://minha.vodafone.pt/f"}),
        ("minha.vodafone.pt", "/f"): httpx.Response(302, headers={"location": "https://login.microsoftonline.com/o"}),
        ("login.microsoftonline.com", "/o"): httpx.Response(200, html=login),
    })
    result = f.fetch("https://u123.ct.sendgrid.net/ls/click?upn=x")
    assert result.outcome is LinkOutcome.LOGIN_REQUIRED
    assert result.owner_message == "Vodafone needs you to sign in."


def test_http_statuses_map_to_outcomes():
    f, _ = fetcher({
        ("www.vodafone.pt", "/401"): httpx.Response(401, headers={"www-authenticate": "Basic"}),
        ("www.vodafone.pt", "/410"): httpx.Response(410),
        ("www.vodafone.pt", "/503"): httpx.Response(503),
        ("www.vodafone.pt", "/429"): httpx.Response(429),
        ("www.vodafone.pt", "/403"): httpx.Response(403, content=b"%PDF-no"),
        ("www.vodafone.pt", "/bin"): httpx.Response(200, content=bytes(range(256)) * 4),
    })
    assert f.fetch("https://www.vodafone.pt/401").outcome is LinkOutcome.LOGIN_REQUIRED
    gone = f.fetch("https://www.vodafone.pt/410")
    assert (gone.outcome, gone.retryable, gone.reason) == (LinkOutcome.UNAVAILABLE, False, "http_410")
    assert f.fetch("https://www.vodafone.pt/503").retryable
    assert f.fetch("https://www.vodafone.pt/429").retryable
    assert f.fetch("https://www.vodafone.pt/403").reason == "http_403"
    assert f.fetch("https://www.vodafone.pt/bin").reason == "unsupported_content"


def test_oversized_downloads_are_abandoned():
    f, _ = fetcher({("www.vodafone.pt", "/big.pdf"): httpx.Response(200, content=PDF * 1000)},
                   policy=FetchPolicy(max_bytes=1024))
    result = f.fetch("https://www.vodafone.pt/big.pdf")
    assert result.outcome is LinkOutcome.UNAVAILABLE and result.reason == "too_large"


def test_network_errors_are_retryable_and_never_raw():
    def boom(request):
        raise httpx.ConnectError("connection refused")

    def slow(request):
        raise httpx.ReadTimeout("slow")

    f, _ = fetcher({("www.vodafone.pt", "/x"): boom, ("www.vodafone.pt", "/t"): slow})
    down = f.fetch("https://www.vodafone.pt/x")
    assert (down.outcome, down.reason, down.retryable, down.owner_message) == (
        LinkOutcome.UNAVAILABLE, "network", True, None)
    assert f.fetch("https://www.vodafone.pt/t").reason == "timeout"


def test_pinning_can_be_disabled():
    f, site = fetcher({("www.vodafone.pt", "/f.pdf"): pdf_response()}, policy=FetchPolicy(pin_addresses=False))
    assert f.fetch("https://www.vodafone.pt/f.pdf").outcome is LinkOutcome.DOWNLOADED
    assert site.requests[0].url.host == "www.vodafone.pt"


# --------------------------------------------------------------------------- browser


SCRIPT_PAGE = '<html><body><div id="app"></div><script src="/app.js"></script><noscript>Enable JavaScript</noscript></body></html>'


class FakeBrowser:
    def __init__(self, page=None, error=None):
        self.page, self.error, self.calls = page, error, []

    def render(self, url, *, timeout_s):
        self.calls.append((url, timeout_s))
        if self.error:
            raise self.error
        return self.page


def test_script_page_is_rendered_in_the_browser_and_download_captured():
    browser = FakeBrowser(RenderedPage("https://billing.acme-cloud.com/app", "https://billing.acme-cloud.com/app#done",
                                       "<html>ok</html>", 200, (BrowserDownload("INV.pdf", PDF),), b"\x89PNG"))
    f, _ = fetcher({("billing.acme-cloud.com", "/app"): httpx.Response(200, html=SCRIPT_PAGE)}, browser=browser)
    result = f.fetch("https://billing.acme-cloud.com/app")
    assert result.outcome is LinkOutcome.DOWNLOADED and result.via_browser
    assert result.content == PDF and result.filename == "INV.pdf"
    assert result.record.redirect_chain[-1].kind == "browser"
    assert browser.calls[0][0] == "https://billing.acme-cloud.com/app"


def test_rendered_page_keeps_html_and_screenshot():
    browser = FakeBrowser(RenderedPage("u", "https://billing.acme-cloud.com/app", "<html>Total 39,00</html>", 200,
                                       (), b"\x89PNGshot"))
    f, _ = fetcher({("billing.acme-cloud.com", "/app"): httpx.Response(200, html=SCRIPT_PAGE)}, browser=browser)
    result = f.fetch("https://billing.acme-cloud.com/app")
    assert result.outcome is LinkOutcome.RENDERED_PAGE and result.via_browser
    assert result.content == b"<html>Total 39,00</html>" and result.snapshot_png == b"\x89PNGshot"


def test_browser_that_lands_somewhere_unsafe_is_blocked():
    browser = FakeBrowser(RenderedPage("u", "http://192.168.1.1/admin", "<html></html>"))
    f, _ = fetcher({("billing.acme-cloud.com", "/app"): httpx.Response(200, html=SCRIPT_PAGE)}, browser=browser)
    assert f.fetch("https://billing.acme-cloud.com/app").outcome is LinkOutcome.BLOCKED_UNSAFE


def test_browser_login_wall_is_detected():
    browser = FakeBrowser(RenderedPage("u", "https://billing.acme-cloud.com/login",
                                       '<form><input type="password"></form>'))
    f, _ = fetcher({("billing.acme-cloud.com", "/app"): httpx.Response(200, html=SCRIPT_PAGE)}, browser=browser)
    result = f.fetch("https://billing.acme-cloud.com/app")
    assert result.outcome is LinkOutcome.LOGIN_REQUIRED and result.owner_message == "Acme Cloud needs you to sign in."


@pytest.mark.parametrize("error", [BrowserUnavailable("no playwright"), BrowserError("crash")])
def test_browser_failure_falls_back_to_the_fetched_page(error):
    f, _ = fetcher({("billing.acme-cloud.com", "/app"): httpx.Response(200, html=SCRIPT_PAGE)},
                   browser=FakeBrowser(error=error))
    result = f.fetch("https://billing.acme-cloud.com/app")
    assert result.outcome is LinkOutcome.RENDERED_PAGE and not result.via_browser


def test_playwright_adapter_is_lazy_and_reports_absence():
    session = PlaywrightBrowserSession(PlaywrightConfig(), isolate_process=False)
    try:
        import playwright  # noqa: F401
    except ImportError:
        with pytest.raises(BrowserUnavailable):
            session.render("https://example.com", timeout_s=1)
    else:  # pragma: no cover - only where playwright is installed
        pytest.skip("playwright installed; covered by integration tests")


def _render_ok(url, config, timeout_s):  # module level: picklable for the spawned worker
    return RenderedPage(url, url + "#rendered", "<html>ok</html>", 200)


def _render_hangs(url, config, timeout_s):
    import time

    time.sleep(60)


def _render_missing(url, config, timeout_s):
    raise BrowserUnavailable("playwright is not installed")


def _render_crashes(url, config, timeout_s):
    raise RuntimeError("page crashed with secrets in the message")


def test_isolated_worker_returns_the_page():
    from backoffice.evidence.links import _run_in_worker

    page = _run_in_worker(_render_ok, "https://a.example/x", PlaywrightConfig(), 5, 20)
    assert page.final_url == "https://a.example/x#rendered" and page.html == "<html>ok</html>"


def test_isolated_worker_is_killed_on_timeout_and_errors_are_typed():
    from backoffice.evidence.links import _run_in_worker

    with pytest.raises(BrowserError, match="timed out"):
        _run_in_worker(_render_hangs, "https://a.example/x", PlaywrightConfig(), 0.5, 1.5)
    with pytest.raises(BrowserUnavailable):
        _run_in_worker(_render_missing, "https://a.example/x", PlaywrightConfig(), 5, 20)
    with pytest.raises(BrowserError) as info:
        _run_in_worker(_render_crashes, "https://a.example/x", PlaywrightConfig(), 5, 20)
    assert "secrets" not in str(info.value)  # only the error kind crosses the process boundary


# --------------------------------------------------------------------------- registration


def test_register_fetch_stores_original_url_and_retrieval_time(tmp_path):
    reg = EvidenceRegistry(LocalObjectStore(tmp_path))
    f, _ = fetcher({
        ("click.mailer.example", "/c"): httpx.Response(302, headers={"location": "https://minha.vodafone.pt/f.pdf"}),
        ("minha.vodafone.pt", "/f.pdf"): pdf_response(),
    })
    result = f.fetch("https://click.mailer.example/c")
    (registration,) = register_fetch(result, reg, tenant_id="t1", context={"message_evidence_id": "ev_m"})
    ev = registration.evidence
    assert ev.original_url == "https://click.mailer.example/c" and ev.retrieved_at == T0
    assert ev.metadata["fetch"]["final_url"] == "https://minha.vodafone.pt/f.pdf"
    assert len(ev.metadata["fetch"]["redirect_chain"]) == 2
    assert registration.sighting.context["message_evidence_id"] == "ev_m"


def test_register_fetch_ignores_outcomes_without_content(tmp_path):
    reg = EvidenceRegistry(LocalObjectStore(tmp_path))
    f, _ = fetcher({})
    assert register_fetch(f.fetch("http://localhost/"), reg, tenant_id="t1") == []


def test_register_fetch_stores_snapshot_alongside_page(tmp_path):
    reg = EvidenceRegistry(LocalObjectStore(tmp_path))
    browser = FakeBrowser(RenderedPage("u", "https://billing.acme-cloud.com/app", "<html>x</html>", 200, (),
                                       b"\x89PNG\r\n\x1a\nshot"))
    f, _ = fetcher({("billing.acme-cloud.com", "/app"): httpx.Response(200, html=SCRIPT_PAGE)}, browser=browser)
    page, shot = register_fetch(f.fetch("https://billing.acme-cloud.com/app"), reg, tenant_id="t1")
    assert page.evidence.format is EvidenceFormat.HTML
    assert shot.evidence.format is EvidenceFormat.SCREENSHOT
    assert shot.evidence.metadata["snapshot_of"] == page.evidence.id
