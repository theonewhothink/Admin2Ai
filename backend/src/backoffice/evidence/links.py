"""Link intelligence (§9): "Your invoice is ready — View Invoice".

1. find the URL (see :mod:`backoffice.evidence.email`);
2. check URL safety — http(s) only, no credentials, sane ports and lengths,
   no lookalike of a known supplier domain, and **after DNS resolution** no
   private, loopback, link-local, carrier-grade NAT or cloud-metadata address;
3. follow redirects by hand, re-validating every hop (``max_hops``), within
   one total deadline (a server dripping bytes cannot hold a worker);
4. open an isolated browser session only when the page needs JavaScript;
5. decide what is behind it: a file, a page, a login wall or a code prompt;
6. download the original when possible;
7-8. record the original URL, final URL, redirect chain and retrieval time;
9. preserve the rendered page when no file is behind the link;
10. hand the bytes to the evidence registry.

Connections are pinned to the address that passed the checks (SNI and the
``Host`` header keep the real name), so a DNS answer cannot change between
the check and the connection. Owner-facing text is plain (§48, §70).

A login-protected link can be opened with a stored authorised session
(:class:`SessionCookie`, §9 "Login required → stored authorized session");
each cookie is bound to its host and only ever sent over HTTPS.
"""

from __future__ import annotations

import ipaddress
import multiprocessing
import re
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from email.message import Message
from http.cookiejar import Cookie
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

try:  # httpx is only needed to fetch links; the browser build (Pyodide) has none.
    import httpx
except ImportError:  # pragma: no cover - exercised by the no-network import check
    httpx = None  # type: ignore[assignment]

from backoffice.domain.models import EvidenceFormat, SourceKind, utcnow

from .domains import LookalikeChecker, LookalikeFinding, display_name_for_host, registrable_domain, to_ascii_host
from .archive import ZipLimits, expand_zip, register_members
from .email import EmailIngestor, EmailIngestResult, EmailParseError, score_link
from .html_signals import HtmlSignals, analyze_html
from .sniff import sniff
from .store import EvidenceRegistry, Registration, sha256_hex

__all__ = [
    "BLOCKED_MESSAGE",
    "BrowserDownload",
    "BrowserError",
    "BrowserSession",
    "BrowserUnavailable",
    "FetchPolicy",
    "FetchRecord",
    "FetchResult",
    "LinkFetcher",
    "LinkOutcome",
    "PlaywrightBrowserSession",
    "PlaywrightConfig",
    "RedirectHop",
    "RenderedPage",
    "Resolver",
    "SessionCookie",
    "SystemResolver",
    "UnsafeReason",
    "UrlCheck",
    "UrlSafety",
    "UrlSafetyConfig",
    "authentication_message",
    "register_fetch",
    "sign_in_message",
]

BLOCKED_MESSAGE = "This link didn't look safe, so I didn't open it."


def authentication_message(supplier: str) -> str:
    """§9: the mobile prompt when a supplier asks for a one-time code."""
    return f"{supplier} needs authentication."


def sign_in_message(supplier: str) -> str:
    return f"{supplier} needs you to sign in."


class LinkOutcome(str, Enum):
    DOWNLOADED = "downloaded"  # a file (PDF, XML, image, ...) was behind the link
    RENDERED_PAGE = "rendered_page"  # no file: the page itself is preserved
    LOGIN_REQUIRED = "login_required"  # needs a stored authorised session (§9, §10)
    MFA_REQUIRED = "mfa_required"  # needs the owner's code; resumes afterwards
    BLOCKED_UNSAFE = "blocked_unsafe"  # never opened
    UNAVAILABLE = "unavailable"  # expired link, outage, too large; see ``retryable``


class UnsafeReason(str, Enum):
    SCHEME = "scheme"
    TOO_LONG = "too_long"
    MALFORMED = "malformed"
    CREDENTIALS = "credentials_in_url"
    PORT = "port"
    BLOCKED_HOST = "blocked_host"
    PRIVATE_ADDRESS = "private_address"
    METADATA_ADDRESS = "metadata_address"
    UNRESOLVABLE = "unresolvable"
    LOOKALIKE = "lookalike_domain"


# --------------------------------------------------------------------------- URL safety

_BLOCKED_HOSTS = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "metadata",
     "metadata.google.internal", "metadata.goog", "instance-data", "instance-data.ec2.internal"}
)  # fmt: skip
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home.arpa", ".localdomain")
# Cloud instance-metadata endpoints (AWS/GCP/Azure, AWS ECS, AWS IPv6, Alibaba, Oracle).
_METADATA_IPS = frozenset(
    ipaddress.ip_address(a)
    for a in ("169.254.169.254", "169.254.170.2", "fd00:ec2::254", "100.100.100.200", "192.0.0.192")
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]*|\d+))*\.?$", re.IGNORECASE)


@dataclass(frozen=True)
class UrlSafetyConfig:
    """Picklable settings (the browser worker rebuilds its checker from these)."""

    max_url_length: int = 2048
    allowed_ports: frozenset[int] = frozenset({80, 443, 8080, 8443})
    known_domains: tuple[str, ...] = ()  # supplier domains learned for this tenant
    allow_http: bool = True


@dataclass(frozen=True)
class UrlCheck:
    url: str
    safe: bool
    reason: UnsafeReason | None = None
    host: str | None = None  # ASCII (punycode) host
    port: int | None = None
    addresses: tuple[str, ...] = ()
    lookalike: LookalikeFinding | None = None
    host_is_ip: bool = False


@runtime_checkable
class Resolver(Protocol):
    def resolve(self, host: str, port: int) -> Sequence[str]:
        """All addresses ``host`` resolves to (empty when it does not resolve)."""
        ...


class SystemResolver:
    def resolve(self, host: str, port: int) -> list[str]:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError, OSError):
            return []
        seen: dict[str, None] = {}
        for info in infos:
            seen.setdefault(str(info[4][0]), None)
        return list(seen)


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _ip_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> UnsafeReason | None:
    if ip in _METADATA_IPS:
        return UnsafeReason.METADATA_ADDRESS
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved or not ip.is_global:
        return UnsafeReason.PRIVATE_ADDRESS
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_v4(ip)
        if inner is not None:
            return _ip_reason(inner)
    return None


def _parse_ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None


class UrlSafety:
    """§9 step 2. ``check`` resolves DNS; every redirect hop is checked again."""

    def __init__(self, config: UrlSafetyConfig | None = None, resolver: Resolver | None = None) -> None:
        self.config = config or UrlSafetyConfig()
        self._resolver = resolver or SystemResolver()
        self._lookalikes = LookalikeChecker(self.config.known_domains)

    def check(self, url: str) -> UrlCheck:
        static = self.check_static(url)
        if not static.safe or static.host_is_ip:
            return static
        assert static.host is not None and static.port is not None
        try:
            answers = list(self._resolver.resolve(static.host, static.port))
        except Exception:  # noqa: BLE001 - a failing resolver means "not resolvable"
            answers = []
        ips = [ip for ip in (_parse_ip(a) for a in answers) if ip is not None]
        if not ips:
            return _unsafe(url, UnsafeReason.UNRESOLVABLE, static)
        for ip in ips:  # one private answer poisons the name (DNS rebinding)
            reason = _ip_reason(ip)
            if reason is not None:
                return _unsafe(url, reason, static)
        return UrlCheck(url, True, None, static.host, static.port, tuple(str(ip) for ip in ips))

    def check_static(self, url: str) -> UrlCheck:
        """Every check that does not need the network."""
        cfg = self.config
        if not isinstance(url, str) or not url:
            return UrlCheck(str(url), False, UnsafeReason.MALFORMED)
        if len(url) > cfg.max_url_length:
            return UrlCheck(url, False, UnsafeReason.TOO_LONG)
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\" for c in url):
            return UrlCheck(url, False, UnsafeReason.MALFORMED)
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return UrlCheck(url, False, UnsafeReason.MALFORMED)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https") or (scheme == "http" and not cfg.allow_http):
            return UrlCheck(url, False, UnsafeReason.SCHEME)
        if "@" in parts.netloc:
            return UrlCheck(url, False, UnsafeReason.CREDENTIALS)
        host = parts.hostname
        if not host:
            return UrlCheck(url, False, UnsafeReason.MALFORMED)
        port = port or (443 if scheme == "https" else 80)
        if port not in cfg.allowed_ports:
            return UrlCheck(url, False, UnsafeReason.PORT, host=host, port=port)
        return self._check_host(url, host, port)

    def _check_host(self, url: str, host: str, port: int) -> UrlCheck:
        ip = _parse_ip(host)
        if ip is not None:
            reason = _ip_reason(ip)
            return UrlCheck(url, reason is None, reason, str(ip), port, (str(ip),), host_is_ip=True)
        if _NUMERIC_HOST.match(host):  # 2130706433, 0x7f.1, 017700000001: obfuscated IPs
            return UrlCheck(url, False, UnsafeReason.MALFORMED)
        ascii_host = to_ascii_host(host)
        if ascii_host is None:
            return UrlCheck(url, False, UnsafeReason.MALFORMED)
        if ascii_host in _BLOCKED_HOSTS or ascii_host.endswith(_BLOCKED_SUFFIXES) or "." not in ascii_host:
            return UrlCheck(url, False, UnsafeReason.BLOCKED_HOST, ascii_host, port)
        finding = self._lookalikes.check(ascii_host)
        if finding is not None:
            return UrlCheck(url, False, UnsafeReason.LOOKALIKE, ascii_host, port, lookalike=finding)
        return UrlCheck(url, True, None, ascii_host, port)


def _unsafe(url: str, reason: UnsafeReason, static: UrlCheck) -> UrlCheck:
    return UrlCheck(url, False, reason, static.host, static.port)


# --------------------------------------------------------------------------- browser


@dataclass(frozen=True)
class BrowserDownload:
    filename: str | None
    data: bytes = field(repr=False)
    content_type: str | None = None


@dataclass(frozen=True)
class RenderedPage:
    requested_url: str
    final_url: str
    html: str = field(repr=False)
    status: int | None = None
    downloads: tuple[BrowserDownload, ...] = ()
    screenshot_png: bytes | None = field(default=None, repr=False)


class BrowserError(RuntimeError):
    """Rendering failed (internal; never shown to owners)."""


class BrowserUnavailable(BrowserError):
    """No browser engine is installed or it could not start."""


@runtime_checkable
class BrowserSession(Protocol):
    """Renders a page in an isolated, throwaway session (§9 step 4, §44)."""

    def render(self, url: str, *, timeout_s: float) -> RenderedPage: ...


@dataclass(frozen=True)
class PlaywrightConfig:
    safety: UrlSafetyConfig = field(default_factory=UrlSafetyConfig)
    headless: bool = True
    locale: str = "pt-PT"
    user_agent: str | None = None
    max_download_bytes: int = 25 * 1024 * 1024
    screenshot: bool = True


class PlaywrightBrowserSession:
    """Chromium via Playwright, in a fresh context per render (§44 isolated workers).

    With ``isolate_process`` (default) each render runs in a separate spawned
    process that is killed on timeout, so a hostile page cannot outlive its
    job. Every request the page makes is checked by :class:`UrlSafety`.
    DNS inside Chromium is not pinned; run workers in a network namespace
    without access to private ranges as the second line of defence.
    """

    def __init__(
        self,
        config: PlaywrightConfig | None = None,
        *,
        isolate_process: bool = True,
        startup_grace_s: float = 10.0,
    ) -> None:
        self.config = config or PlaywrightConfig()
        self.isolate_process = isolate_process
        self.startup_grace_s = startup_grace_s  # process spawn + browser launch on top of the page timeout

    def render(self, url: str, *, timeout_s: float) -> RenderedPage:
        if not self.isolate_process:
            return _render_with_playwright(url, self.config, timeout_s)
        return _run_in_worker(_render_with_playwright, url, self.config, timeout_s, self.startup_grace_s)


RenderFn = Callable[[str, PlaywrightConfig, float], RenderedPage]


def _render_with_playwright(url: str, config: PlaywrightConfig, timeout_s: float) -> RenderedPage:
    try:
        from playwright.sync_api import Error as PlaywrightError  # optional dependency
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable("playwright is not installed") from exc
    safety = UrlSafety(config.safety)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=config.headless, args=["--disable-dev-shm-usage"])
        try:
            context = browser.new_context(
                accept_downloads=True, locale=config.locale, user_agent=config.user_agent,
                service_workers="block",
            )
            context.route("**/*", lambda route: _guard_route(route, safety))
            page = context.new_page()
            downloads: list[Any] = []
            page.on("download", downloads.append)
            status = None
            try:
                response = page.goto(url, wait_until="networkidle", timeout=timeout_s * 1000)
                status = response.status if response is not None else None
            except PlaywrightError as exc:
                if "Download is starting" not in str(exc):
                    raise BrowserError("navigation failed") from exc
            files = tuple(f for f in (_read_download(d, config.max_download_bytes) for d in downloads) if f)
            shot = page.screenshot(full_page=True, type="png") if config.screenshot else None
            return RenderedPage(url, page.url, page.content(), status, files, shot)
        finally:
            browser.close()


def _guard_route(route: Any, safety: UrlSafety) -> None:
    target = route.request.url
    if target.startswith(("data:", "blob:", "about:")) or safety.check(target).safe:
        route.continue_()
    else:
        route.abort("blockedbyclient")


def _read_download(download: Any, cap: int) -> BrowserDownload | None:
    path = download.path()
    if path is None or Path(path).stat().st_size > cap:
        return None
    return BrowserDownload(download.suggested_filename, Path(path).read_bytes())


def _worker_main(conn: Any, render: RenderFn, url: str, config: PlaywrightConfig, timeout_s: float) -> None:
    try:
        conn.send(("ok", render(url, config, timeout_s)))
    except BrowserUnavailable as exc:
        conn.send(("unavailable", str(exc)))
    except Exception as exc:  # noqa: BLE001 - report the kind only, never page content
        conn.send(("error", type(exc).__name__))
    finally:
        conn.close()


def _run_in_worker(
    render: RenderFn, url: str, config: PlaywrightConfig, timeout_s: float, grace_s: float = 10.0
) -> RenderedPage:
    """Run ``render`` in a spawned process; kill it if it overruns ``timeout_s + grace_s``."""
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_worker_main, args=(sender, render, url, config, timeout_s), daemon=True)
    proc.start()
    sender.close()
    try:
        if not receiver.poll(timeout_s + grace_s):
            raise BrowserError("render timed out")
        status, payload = receiver.recv()
    except EOFError:
        raise BrowserError("render worker exited") from None
    finally:
        proc.join(timeout=2)
        if proc.is_alive():
            proc.kill()
            proc.join()
        receiver.close()
    if status == "ok":
        return payload
    if status == "unavailable":
        raise BrowserUnavailable(payload)
    raise BrowserError(f"render failed: {payload}")


# --------------------------------------------------------------------------- fetching

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
# Identity providers and email click-tracking services seen between a link and
# the supplier; never used as the supplier's name in owner copy.
_INTERMEDIARIES = frozenset(
    {"microsoftonline.com", "live.com", "google.com", "auth0.com", "okta.com", "onelogin.com",
     "sendgrid.net", "list-manage.com", "mandrillapp.com", "mailgun.org", "awstrack.me", "rs6.net",
     "hubspotlinks.com", "mailjet.com", "sparkpostmail.com", "exacttarget.com",
     "outlook.com", "urldefense.com", "mimecast.com"}
)  # fmt: skip
_MFA_TEXT = re.compile(
    r"verification code|one[- ]time (pass)?code|two[- ]factor|2-step|two-step|authenticator app"
    r"|enter the code|we (have )?sent (you )?a code|security code"
    r"|c[oó]digo de (verifica[çc][ãa]o|seguran[çc]a|confirma[çc][ãa]o|acesso|autentica[çc][ãa]o)"
    r"|autentica[çc][ãa]o (de dois fatores|em dois passos|forte)|(introduza|insira) o c[oó]digo"
    r"|c[oó]digo (que )?envi[aá]mos|verificaci[oó]n en dos pasos|c[oó]digo de un solo uso",
    re.IGNORECASE,
)
_LOGIN_TEXT = re.compile(
    r"sign ?in|log ?in|iniciar sess[ãa]o|inicie sess[ãa]o|entrar|aceder [àa] (sua )?conta"
    r"|iniciar sesi[oó]n|acceder|se connecter|connexion|anmelden",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FetchPolicy:
    max_hops: int = 5  # redirects, meta refreshes and document links together
    max_bytes: int = 25 * 1024 * 1024
    timeout_s: float = 20.0  # per network operation (connect, each read)
    total_timeout_s: float = 120.0  # the whole retrieval, every hop included
    max_meta_refresh_delay_s: int = 10  # slower refreshes are session timeouts, not redirects
    user_agent: str = "Mozilla/5.0 (compatible; BackOfficeEvidence/1.0)"
    pin_addresses: bool = True
    follow_document_links: bool = True
    script_page_text_chars: int = 200  # below this, a page with scripts needs a browser


@dataclass(frozen=True)
class RedirectHop:
    url: str
    status: int | None
    kind: str  # "request", "http", "meta_refresh", "document_link", "browser"


@dataclass(frozen=True)
class FetchRecord:
    """Provenance of one retrieval (§9 steps 7-8, §55)."""

    original_url: str
    final_url: str
    redirect_chain: tuple[RedirectHop, ...]
    retrieved_at: datetime
    sha256: str | None
    content_type: str | None  # from the bytes
    declared_content_type: str | None  # from the server
    status_code: int | None
    size: int = 0

    def as_metadata(self) -> dict[str, Any]:
        return {
            "original_url": self.original_url,
            "final_url": self.final_url,
            "redirect_chain": [{"url": h.url, "status": h.status, "kind": h.kind} for h in self.redirect_chain],
            "retrieved_at": self.retrieved_at.isoformat(),
            "sha256": self.sha256,
            "content_type": self.content_type,
            "declared_content_type": self.declared_content_type,
            "status_code": self.status_code,
            "size": self.size,
        }


@dataclass(frozen=True)
class FetchResult:
    outcome: LinkOutcome
    record: FetchRecord
    content: bytes | None = field(default=None, repr=False)
    format: EvidenceFormat | None = None
    filename: str | None = None
    reason: str | None = None  # internal code, e.g. "private_address", "http_404"
    owner_message: str | None = None
    retryable: bool = False
    via_browser: bool = False
    snapshot_png: bytes | None = field(default=None, repr=False)
    supplier: str | None = None  # display name used in owner copy
    portal: str | None = None  # a supplier portal adapter (§10) handed the document over, not a web page

    @property
    def expired(self) -> bool:
        """The link can no longer give the document (gone, 404/410, refused for good): never retried."""
        return self.outcome is LinkOutcome.UNAVAILABLE and not self.retryable


@dataclass(frozen=True)
class SessionCookie:
    """One cookie of a stored authorised supplier session (§9, §10).

    Sent only to ``domain`` (and its subdomains) and only over HTTPS. The
    value comes from the secrets vault and is never shown or logged.
    """

    domain: str
    name: str
    value: str = field(repr=False)
    path: str = "/"


def _session_cookie(cookie: SessionCookie) -> Cookie | None:
    host = to_ascii_host(cookie.domain.lstrip("."))
    if host is None or not cookie.name:
        return None
    return Cookie(
        version=0, name=cookie.name, value=cookie.value, port=None, port_specified=False,
        domain=host, domain_specified=True, domain_initial_dot=False,
        path=cookie.path or "/", path_specified=True, secure=True, expires=None, discard=True,
        comment=None, comment_url=None, rest={},
    )


class _DeadlineExceeded(Exception):
    """The retrieval as a whole ran out of time (internal)."""


@dataclass
class _Run:
    original_url: str
    supplier: str | None
    deadline: float
    chain: list[RedirectHop] = field(default_factory=list)
    visited: dict[str, int] = field(default_factory=dict)
    cookies: httpx.Cookies = field(default_factory=lambda: httpx.Cookies())
    hops: int = 0
    followed_document: bool = False


@dataclass(frozen=True)
class _Response:
    url: str
    status: int
    headers: httpx.Headers
    body: bytes
    too_large: bool = False


@dataclass(frozen=True)
class _Next:
    url: str
    kind: str


class LinkFetcher:
    """Follows one link safely and says what is behind it (§9)."""

    def __init__(
        self,
        safety: UrlSafety,
        *,
        transport: httpx.BaseTransport | None = None,
        browser: BrowserSession | None = None,
        policy: FetchPolicy | None = None,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.safety = safety
        self.browser = browser
        self.policy = policy or FetchPolicy()
        self._transport = transport
        self._clock = clock
        self._monotonic = monotonic

    def fetch(
        self,
        url: str,
        *,
        supplier_name: str | None = None,
        session_cookies: Sequence[SessionCookie] = (),
    ) -> FetchResult:
        """Follow ``url`` and say what is behind it. Never raises for network problems."""
        run = _Run(url, supplier_name, deadline=self._monotonic() + self.policy.total_timeout_s)
        for stored in session_cookies:
            cookie = _session_cookie(stored)
            if cookie is not None:
                run.cookies.jar.set_cookie(cookie)
        with httpx.Client(
            transport=self._transport,
            timeout=httpx.Timeout(self.policy.timeout_s),
            # Pinned connections are pooled by IP; never reuse one, so every host
            # gets its own TLS handshake verified against its own name (SNI).
            limits=httpx.Limits(max_keepalive_connections=0),
            follow_redirects=False,
            trust_env=False,  # no ambient proxies: the checks above decide where we connect
            headers={"User-Agent": self.policy.user_agent,
                     "Accept": "application/pdf,application/xml,text/html;q=0.9,*/*;q=0.8"},
        ) as client:
            return self._follow(client, run, url)

    # ----------------------------------------------------------------- hop loop

    def _follow(self, client: httpx.Client, run: _Run, url: str) -> FetchResult:
        current, kind = url, "request"
        while True:
            if self._monotonic() > run.deadline:
                return self._unavailable(run, current, "timeout", retryable=True)
            check = self.safety.check(current)
            if not check.safe:
                return self._blocked(run, current, check)
            # A page may redirect to itself once after setting a cookie; twice is a loop.
            if run.visited.get(current, 0) >= 2:
                return self._unavailable(run, current, "redirect_loop")
            run.visited[current] = run.visited.get(current, 0) + 1
            try:
                response = self._get(client, current, check, run)
            except (httpx.TimeoutException, _DeadlineExceeded):
                return self._unavailable(run, current, "timeout", retryable=True)
            except httpx.HTTPError:
                return self._unavailable(run, current, "network", retryable=True)
            except (httpx.InvalidURL, ValueError):
                return self._unavailable(run, current, "malformed_url")
            run.chain.append(RedirectHop(current, response.status, kind))
            step = self._next_step(run, response)
            if isinstance(step, FetchResult):
                return step
            if run.hops >= self.policy.max_hops:
                return self._unavailable(run, current, "too_many_redirects")
            run.hops += 1
            current, kind = step.url, step.kind

    def _get(self, client: httpx.Client, url: str, check: UrlCheck, run: _Run) -> _Response:
        target, headers, extensions = self._pinned(url, check)
        cookie = _cookie_header(run.cookies, url)
        if cookie:
            headers["Cookie"] = cookie
        with client.stream("GET", target, headers=headers, extensions=extensions) as resp:
            _store_cookies(run.cookies, url, resp.headers)
            if resp.status_code in _REDIRECTS:
                return _Response(url, resp.status_code, resp.headers, b"")
            body = bytearray()
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > self.policy.max_bytes:
                    return _Response(url, resp.status_code, resp.headers, b"", too_large=True)
                if self._monotonic() > run.deadline:
                    raise _DeadlineExceeded
            return _Response(url, resp.status_code, resp.headers, bytes(body))

    def _pinned(self, url: str, check: UrlCheck) -> tuple[str, dict[str, str], dict[str, Any]]:
        if not self.policy.pin_addresses or check.host_is_ip or not check.addresses:
            return url, {}, {}
        parts = urlsplit(url)
        ip = check.addresses[0]
        default_port = 443 if parts.scheme.lower() == "https" else 80
        host_part = f"[{ip}]" if ":" in ip else ip
        port_part = "" if check.port == default_port else f":{check.port}"
        target = urlunsplit((parts.scheme, host_part + port_part, parts.path or "/", parts.query, ""))
        assert check.host is not None
        return target, {"Host": check.host + port_part}, {"sni_hostname": check.host}

    # ----------------------------------------------------------------- classification

    def _next_step(self, run: _Run, resp: _Response) -> FetchResult | _Next:
        if resp.status in _REDIRECTS:
            location = resp.headers.get("location")
            if not location:
                return self._unavailable(run, resp.url, "redirect_without_location")
            target = _join(resp.url, location.strip())
            if target is None:
                return self._unavailable(run, resp.url, "malformed_url", status=resp.status)
            return _Next(target, "http")
        if resp.too_large:
            return self._unavailable(run, resp.url, "too_large", status=resp.status)
        if resp.status == 401:
            return self._login(run, resp)
        if resp.status in (404, 410):
            return self._unavailable(run, resp.url, f"http_{resp.status}", status=resp.status)
        if resp.status == 429 or resp.status >= 500:
            return self._unavailable(run, resp.url, f"http_{resp.status}", retryable=True, status=resp.status)
        return self._content(run, resp)

    def _content(self, run: _Run, resp: _Response) -> FetchResult | _Next:
        declared = resp.headers.get("content-type")
        filename = _filename(resp.headers.get("content-disposition"), resp.url)
        found = sniff(resp.body, declared_type=declared, filename=filename)
        if resp.status >= 400 and found.format is not EvidenceFormat.HTML:
            return self._unavailable(run, resp.url, f"http_{resp.status}", status=resp.status)
        if found.format is None:
            return self._unavailable(run, resp.url, "unsupported_content", status=resp.status)
        if found.format is not EvidenceFormat.HTML:
            return self._result(run, LinkOutcome.DOWNLOADED, resp.url, resp.status, resp.body,
                                found.format, found.mime_type, declared, filename)
        return self._page(run, resp, declared)

    def _page(self, run: _Run, resp: _Response, declared: str | None) -> FetchResult | _Next:
        html = _decode_html(resp.body, declared)
        signals = analyze_html(html)
        refresh = _join(resp.url, signals.meta_refresh) if signals.meta_refresh else None
        if refresh is not None and signals.meta_refresh_delay <= self.policy.max_meta_refresh_delay_s:
            return _Next(refresh, "meta_refresh")
        # A code prompt or password field is a wall; an email box and a "Sign in"
        # link are not, when the page also offers the document itself.
        wall = self._gate(run, resp.url, resp.status, signals, include_weak=False)
        if wall is not None:
            return wall
        document = self._document_link(run, resp.url, signals) if resp.status < 400 else None
        if document is not None:
            return _Next(document, "document_link")
        gate = self._gate(run, resp.url, resp.status, signals)
        if gate is not None:
            return gate
        if resp.status >= 400:
            return self._unavailable(run, resp.url, f"http_{resp.status}", status=resp.status)
        budget = min(self.policy.timeout_s * 2, run.deadline - self._monotonic())
        if self.browser is not None and budget > 0 and self._needs_script(signals):
            rendered = self._render(run, resp.url, budget)
            if rendered is not None:
                return rendered
        return self._result(run, LinkOutcome.RENDERED_PAGE, resp.url, resp.status, resp.body,
                            EvidenceFormat.HTML, "text/html", declared, None)

    def _gate(self, run: _Run, url: str, status: int | None, signals: HtmlSignals, *,
              include_weak: bool = True) -> FetchResult | None:
        """Code prompts and login walls (§9: stored session or owner authentication).

        Strong signals: a one-time-code field (or code wording with a form) and a
        password field. Weak: an email/user field plus sign-in wording, which
        newsletter boxes and site headers also produce.
        """
        words = f"{signals.title} {signals.text[:5000]}"
        if signals.has_otp_input or (_MFA_TEXT.search(words) and signals.form_actions):
            return self._needs_owner(run, url, status, LinkOutcome.MFA_REQUIRED)
        if signals.has_password_input:
            return self._needs_owner(run, url, status, LinkOutcome.LOGIN_REQUIRED)
        if include_weak and signals.has_login_input and _LOGIN_TEXT.search(words):
            return self._needs_owner(run, url, status, LinkOutcome.LOGIN_REQUIRED)
        return None

    def _document_link(self, run: _Run, page_url: str, signals: HtmlSignals) -> str | None:
        if not self.policy.follow_document_links or run.followed_document:
            return None
        base = (_join(page_url, signals.base_href) if signals.base_href else None) or page_url
        best: tuple[int, int, str] | None = None
        for anchor in signals.anchors:
            target = _join(base, anchor.href)
            if target is None or not target.lower().startswith(("http://", "https://")) or target in run.visited:
                continue
            score, reasons = score_link(target, anchor.text, button_like=anchor.button_like)
            if "negative" in reasons or ("url:pdf" not in reasons and score < 55):
                continue
            if best is None or (score, -anchor.order) > (best[0], best[1]):
                best = (score, -anchor.order, target)
        if best is None:
            return None
        run.followed_document = True
        return best[2]

    def _needs_script(self, signals: HtmlSignals) -> bool:
        if len(signals.text) >= self.policy.script_page_text_chars:
            return False
        return signals.script_count > 0 or "javascript" in signals.noscript_text.lower()

    def _render(self, run: _Run, url: str, timeout_s: float) -> FetchResult | None:
        """Isolated browser for script-only pages, within what is left of the total deadline."""
        assert self.browser is not None
        try:
            page = self.browser.render(url, timeout_s=timeout_s)
        except BrowserError:
            return None  # keep what plain HTTP fetched
        if page.final_url != url:
            run.chain.append(RedirectHop(page.final_url, page.status, "browser"))
        final_check = self.safety.check(page.final_url)
        if not final_check.safe:
            return self._blocked(run, page.final_url, final_check)
        for download in page.downloads:
            found = sniff(download.data, declared_type=download.content_type, filename=download.filename)
            if found.format is not None and found.format is not EvidenceFormat.HTML:
                return self._result(run, LinkOutcome.DOWNLOADED, page.final_url, page.status, download.data,
                                    found.format, found.mime_type, download.content_type,
                                    _clean_name(download.filename), via_browser=True)
        gate = self._gate(run, page.final_url, page.status, analyze_html(page.html))
        if gate is not None:
            return gate
        return self._result(run, LinkOutcome.RENDERED_PAGE, page.final_url, page.status,
                            page.html.encode("utf-8"), EvidenceFormat.HTML, "text/html", "text/html", None,
                            via_browser=True, snapshot=page.screenshot_png)

    # ----------------------------------------------------------------- results

    def _record(self, run: _Run, final_url: str, status: int | None, body: bytes | None,
                mime: str | None, declared: str | None) -> FetchRecord:
        return FetchRecord(
            original_url=run.original_url,
            final_url=final_url,
            redirect_chain=tuple(run.chain),
            retrieved_at=self._clock(),
            sha256=sha256_hex(body) if body else None,
            content_type=mime,
            declared_content_type=declared,
            status_code=status,
            size=len(body or b""),
        )

    def _supplier(self, run: _Run, url: str) -> str:
        """Caller's name for the supplier, else the last host on the way that is not
        a sign-in or click-tracking service ("Vodafone", not "Microsoftonline")."""
        if run.supplier:
            return run.supplier
        for candidate in [url, *(hop.url for hop in reversed(run.chain)), run.original_url]:
            host = urlsplit(candidate).hostname
            if host and registrable_domain(host) not in _INTERMEDIARIES:
                return display_name_for_host(host)
        return "The supplier"

    def _result(self, run: _Run, outcome: LinkOutcome, url: str, status: int | None, body: bytes,
                fmt: EvidenceFormat, mime: str, declared: str | None, filename: str | None,
                *, via_browser: bool = False, snapshot: bytes | None = None) -> FetchResult:
        return FetchResult(outcome, self._record(run, url, status, body, mime, declared), body, fmt,
                           filename, via_browser=via_browser, snapshot_png=snapshot,
                           supplier=self._supplier(run, url))

    def _needs_owner(self, run: _Run, url: str, status: int | None, outcome: LinkOutcome) -> FetchResult:
        supplier = self._supplier(run, url)
        message = authentication_message(supplier) if outcome is LinkOutcome.MFA_REQUIRED else sign_in_message(supplier)
        return FetchResult(outcome, self._record(run, url, status, None, None, None), reason=outcome.value,
                           owner_message=message, retryable=True, supplier=supplier)

    def _login(self, run: _Run, resp: _Response) -> FetchResult:
        return self._needs_owner(run, resp.url, resp.status, LinkOutcome.LOGIN_REQUIRED)

    def _blocked(self, run: _Run, url: str, check: UrlCheck) -> FetchResult:
        reason = check.reason.value if check.reason else "unsafe"
        return FetchResult(LinkOutcome.BLOCKED_UNSAFE, self._record(run, url, None, None, None, None),
                           reason=reason, owner_message=BLOCKED_MESSAGE)

    def _unavailable(self, run: _Run, url: str, reason: str, *, retryable: bool = False,
                     status: int | None = None) -> FetchResult:
        return FetchResult(LinkOutcome.UNAVAILABLE, self._record(run, url, status, None, None, None),
                           reason=reason, retryable=retryable)


# --------------------------------------------------------------------------- helpers


def _join(base: str, ref: str) -> str | None:
    """``urljoin`` that answers ``None`` for a hostile reference instead of raising."""
    try:
        joined = urljoin(base, ref)
        _ = urlsplit(joined).port  # validates brackets and the port
    except ValueError:
        return None
    return joined


def _cookie_header(cookies: httpx.Cookies, url: str) -> str | None:
    probe = httpx.Request("GET", url)
    cookies.set_cookie_header(probe)
    return probe.headers.get("cookie")


def _store_cookies(cookies: httpx.Cookies, url: str, headers: httpx.Headers) -> None:
    """Cookies are keyed by the real host name, not the pinned address."""
    if "set-cookie" in headers:
        cookies.extract_cookies(httpx.Response(200, headers=headers, request=httpx.Request("GET", url)))


def _clean_name(name: str | None) -> str | None:
    if not name:
        return None
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c.isprintable()).strip()
    return name[:255] or None


def _filename(disposition: str | None, url: str) -> str | None:
    if disposition:
        msg = Message()
        msg["content-disposition"] = disposition
        name = _clean_name(msg.get_filename())
        if name:
            return name
    last = PurePosixPath(unquote(urlsplit(url).path)).name
    return _clean_name(last) if "." in last else None


def _decode_html(body: bytes, declared: str | None) -> str:
    charset = None
    if declared:
        m = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", declared, re.IGNORECASE)
        charset = m.group(1) if m else None
    if charset is None:
        m = re.search(rb"<meta[^>]+charset\s*=\s*['\"]?([\w.-]+)", body[:4096], re.IGNORECASE)
        charset = m.group(1).decode("ascii", "ignore") if m else None
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def register_fetch(
    result: FetchResult,
    registry: EvidenceRegistry,
    *,
    tenant_id: str,
    source_kind: SourceKind = SourceKind.EMAIL,
    context: Mapping[str, Any] | None = None,
    zip_limits: ZipLimits | None = None,
) -> list[Registration]:
    """§9 step 10: store what the link gave us, with its provenance.

    The downloaded original comes first. A ZIP bundle (PDF + XML invoice) is
    expanded and a downloaded email is read like any email, so the documents
    inside become evidence too, each pointing back at the download.
    Returns nothing for outcomes without content (login, code prompt, blocked,
    unavailable): those are not evidence, they are work still to do.
    """
    if result.content is None or result.format is None:
        return []
    record = result.record
    metadata = {"fetch": record.as_metadata(), "outcome": result.outcome.value, "via_browser": result.via_browser}
    regs = [
        registry.register(
            result.content, tenant_id=tenant_id, source_kind=source_kind, format=result.format,
            mime_type=record.content_type or "application/octet-stream", filename=result.filename,
            original_url=record.original_url, retrieved_at=record.retrieved_at, metadata=metadata,
            context=dict(context or {}),
        )
    ]
    regs += _register_contents(result, regs[0], registry, source_kind=source_kind, context=context or {},
                               zip_limits=zip_limits)
    if result.snapshot_png:
        regs.append(
            registry.register(
                result.snapshot_png, tenant_id=tenant_id, source_kind=source_kind,
                format=EvidenceFormat.SCREENSHOT, mime_type="image/png", filename=None,
                original_url=record.original_url, retrieved_at=record.retrieved_at,
                metadata={"fetch": record.as_metadata(), "snapshot_of": regs[0].evidence.id},
                context=dict(context or {}),
            )
        )
    return regs


def _register_contents(
    result: FetchResult,
    original: Registration,
    registry: EvidenceRegistry,
    *,
    source_kind: SourceKind,
    context: Mapping[str, Any],
    zip_limits: ZipLimits | None,
) -> list[Registration]:
    """Evidence inside a downloaded container (ZIP bundle or email file)."""
    assert result.content is not None
    record = result.record
    if result.format is EvidenceFormat.ZIP:
        members, _ = register_members(registry, expand_zip(result.content, zip_limits), original,
                                      source_kind=source_kind, at=record.retrieved_at, context=context,
                                      original_url=record.original_url)
        return members
    if result.format is EvidenceFormat.EML:
        try:
            ingested = EmailIngestor(registry, zip_limits=zip_limits).ingest(
                result.content, tenant_id=original.evidence.tenant_id, source_kind=source_kind,
                message_format=EvidenceFormat.EML, received_at=record.retrieved_at, filename=result.filename,
                context={**context, "downloaded_evidence_id": original.evidence.id},
            )
        except EmailParseError:
            return []
        return _email_file_registrations(ingested)
    return []


def _email_file_registrations(result: EmailIngestResult) -> list[Registration]:
    regs = [f.registration for f in result.files]
    for nested in result.attached_emails:
        regs += [nested.message, *_email_file_registrations(nested)]
    return regs
