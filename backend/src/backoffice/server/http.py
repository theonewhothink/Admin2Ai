"""The production FastAPI application (``BACKOFFICE_MODE=production``).

Every ``/api/*`` call needs a signed-in person, except ``/api/auth/*``,
``/api/oauth/callback``, the bank callback, ``/api/v1/*`` (accountant API
keys), ``/healthz`` and ``/readyz``. All other routes keep exactly the shapes
the demo serves; they act on the signed-in person's business.

Hardening: security headers on every response (HSTS, nosniff, a CSP that
allows nothing, no framing, no referrer, no caching of API data), CORS only for
BACKOFFICE_ALLOWED_ORIGINS (with credentials), request size limits, one JSON
log line per request without personal data, plain-language errors and never a
stack trace.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import logging
import re
import secrets
import sys
import time
import traceback
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from backoffice.internal import admin_only

from .account import erase_account, export_account
from .auth import COOKIE_NAME, CSRF_HEADER, CSRF_VALUE, SESSION_DAYS, AuthError, AuthService, Principal, permitted
from .config import ServerConfig
from .erasure import purger_from_config
from .runtime import READ_ONLY_POSTS, ReplayDiverged, TenantManager, TenantNotFound
from .store import Device, Store, StoreError, StoreUnavailable

__all__ = ["JsonLogFormatter", "StoreNonces", "build_manager", "build_production_app", "configure_logging",
           "production_services"]

log = logging.getLogger("backoffice.http")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
UPLOAD_PATHS = frozenset({"/api/evidence", "/api/evidence/upload", "/api/receipts", "/api/share"})
# base64 in JSON grows a file by a third; a little room for the other fields.
UPLOAD_LIMIT = MAX_UPLOAD_BYTES * 4 // 3 + 1024 * 1024
PUBLIC_API = frozenset({"/api/auth/signup", "/api/auth/login", "/api/oauth/callback",
                        "/api/connections/bank/callback"})
SECURITY_HEADERS = (
    (b"strict-transport-security", b"max-age=63072000; includeSubDomains"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
)
_EXPO_TOKEN = re.compile(r"^Expo(nent)?PushToken\[[A-Za-z0-9_-]{8,200}\]$")
_INSTITUTION = re.compile(r"^[A-Z0-9_]{3,64}$")
_ROUTE_WORDS = frozenset(
    "api home needs-you answer activity companies months ask evidence upload receipts share sources chat tools "
    "tool tasks done outbox send reports file documents export settings report accountant api-keys revoke remove "
    "connections stale reconnect clients rules audit pipeline auth signup login logout me account delete "
    "onboarding company oauth start callback bank devices v1 healthz readyz internal overview operations "
    "readiness acceptance cost-centers allocate obligations transactions expected-invoices not-coming".split())

UNAVAILABLE = "I can't reach your data right now. Please try again in a minute."
DIVERGED = "Your data is safe, but I can't open it right now. The team has been alerted."
FORBIDDEN = "You don't have access to that."
ADMIN_ONLY = "This page is for the Admin2Ai team only."
INTERNAL_VIEWS = frozenset({"overview", "operations", "readiness", "acceptance"})
CSRF_BLOCKED = "This request was blocked for your safety. Please reload the page and try again."


def _error(status: int, error: str, message: str, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, status_code=status, headers=dict(headers or {}))


# --------------------------------------------------------------------------- logging


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line. Only fields this module chooses; never request bodies or addresses."""

    FIELDS = ("request_id", "method", "route", "status", "ms", "tenant", "role", "seq", "reason", "exc_type",
              "tenants", "connections", "messages", "rows")

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for name in self.FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                out[name] = value
        if record.exc_info and record.exc_info[0] is not None:
            out["exc_type"] = record.exc_info[0].__name__
            # Where it failed (file:line function), without the exception's text, which may hold data.
            out["where"] = [f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno} {f.name}"
                            for f in traceback.extract_tb(record.exc_info[2])][-6:]
        return json.dumps(out, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """JSON lines on stdout for everything the back office logs (once per process)."""
    ours = logging.getLogger("backoffice")
    if any(isinstance(h.formatter, JsonLogFormatter) for h in ours.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    ours.addHandler(handler)
    ours.setLevel(getattr(logging, level.upper(), logging.INFO))


def route_template(path: str) -> str:
    """``/api/documents/doc_1234/file`` -> ``/api/documents/{id}/file``: ids and emails never reach the log."""
    return "/".join(p if (p in _ROUTE_WORDS or not p) else "{id}" for p in path.split("/"))


# --------------------------------------------------------------------------- ASGI guard


class _Guard:
    """Request ids, size limits, security headers, one log line per request, JSON 500s."""

    def __init__(self, app: Any, *, max_json_bytes: int) -> None:
        self.app = app
        self.max_json = max_json_bytes

    async def __call__(self, scope: dict[str, Any], receive: Callable[[], Awaitable[dict[str, Any]]],
                       send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        request_id = secrets.token_hex(8)
        scope.setdefault("state", {})["request_id"] = request_id
        path = scope.get("path", "")
        limit = UPLOAD_LIMIT if path in UPLOAD_PATHS else self.max_json
        status_box = {"status": 500, "sent": False}

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_box["status"] = message["status"]
                status_box["sent"] = True
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in dict(SECURITY_HEADERS)]
                headers += list(SECURITY_HEADERS)
                names = {k.lower() for k, _ in headers}
                if b"cache-control" not in names:
                    headers.append((b"cache-control", b"no-store"))
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        declared = dict(scope.get("headers") or []).get(b"content-length")
        too_large = False
        if declared is not None:
            try:
                too_large = int(declared) > limit
            except ValueError:
                too_large = True
        if too_large:
            await _plain(send_with_headers, 413, "too_large", "This request is too large.")
        else:
            received = 0

            async def limited_receive() -> dict[str, Any]:
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > limit:
                        raise _TooLarge()
                return message

            try:
                await self.app(scope, limited_receive, send_with_headers)
            except _TooLarge:
                if not status_box["sent"]:
                    await _plain(send_with_headers, 413, "too_large", "This request is too large.")
            except Exception:
                log.exception("unhandled", extra={"request_id": request_id, "route": route_template(path)})
                if not status_box["sent"]:
                    await _plain(send_with_headers, 500, "server_error",
                                 "Something went wrong on our side. Please try again.")
        state = scope.get("state") or {}
        log.info("request", extra={
            "request_id": request_id, "method": scope.get("method"), "route": route_template(path),
            "status": status_box["status"], "ms": round((time.perf_counter() - started) * 1000, 1),
            "tenant": state.get("log_tenant"), "role": state.get("log_role"),
        })


class _TooLarge(Exception):
    pass


async def _plain(send: Callable[[dict[str, Any]], Awaitable[None]], status: int, error: str, message: str) -> None:
    body = json.dumps({"error": error, "message": message}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


# --------------------------------------------------------------------------- sign-in nonces


class StoreNonces:
    """:class:`~backoffice.connectors.authorize.NonceStore` on the shared store (any API process can finish)."""

    def __init__(self, store: Store, now: Callable[[], datetime]) -> None:
        self.store = store
        self.now = now

    def save(self, tenant_id: str, nonce: str, expires_at: float) -> None:
        self.store.save_nonce(tenant_id, nonce, datetime.fromtimestamp(expires_at, tz=timezone.utc))

    def take(self, tenant_id: str, nonce: str) -> bool:
        return self.store.take_nonce(tenant_id, nonce, self.now())


# --------------------------------------------------------------------------- wiring


def _default_services(config: ServerConfig) -> dict[str, Any]:
    """Real store, object store, vault, OAuth, mailer, chat model and push, from the configuration."""
    from concurrent.futures import ThreadPoolExecutor

    from backoffice.connectors.authorize import OAuthAuthorizer, app_from_env
    from backoffice.connectors.vault import AwsKmsKeyProvider, LocalKeyProvider, TokenVault
    from backoffice.evidence.store import LocalObjectStore, S3ObjectStore
    from backoffice.mailer import mailer_from_env
    from backoffice.reading import reader_from_env

    from .notify import ExpoPushClient, PushNotifier
    from .postgres import PostgresCredentialStore, PostgresStore

    config.require_production()
    store = PostgresStore(config.database_url, pool_size=config.db_pool_size)
    if config.s3_bucket:
        objects: Any = S3ObjectStore(config.s3_bucket, region=config.aws_region,
                                     kms_key_id=config.evidence_kms_key_id or None,
                                     endpoint_url=config.s3_endpoint_url or None, prefix=config.s3_prefix)
    else:
        objects = LocalObjectStore(config.object_dir)
    vault = None
    if config.vault_kms_key_id:
        vault = TokenVault(AwsKmsKeyProvider(config.vault_kms_key_id, region=config.aws_region),
                           PostgresCredentialStore(store))
    elif config.vault_key:
        vault = TokenVault(LocalKeyProvider.from_env(), PostgresCredentialStore(store))
    else:
        log.warning("vault_not_configured")
    now = lambda: datetime.now(timezone.utc)  # noqa: E731
    authorizer = None
    apps = {p: a for p in ("google", "microsoft") if (a := app_from_env(p)) is not None}
    if apps and vault is not None and len(config.state_key) >= 32:
        authorizer = OAuthAuthorizer(apps, vault, redirect_uri=f"{config.api_url}/api/oauth/callback",
                                     state_key=config.state_key, nonces=StoreNonces(store, now))
    brain_factory = None
    if config.anthropic_enabled:
        shared: dict[str, Any] = {}

        def brain_factory(svc: Any) -> Any:
            from backoffice.assistant import ClaudeBrain

            if "client" not in shared:
                import anthropic  # lazy: server-only dependency

                shared["client"] = anthropic.Anthropic()
            return ClaudeBrain(svc.assistant, client=shared["client"])

    notifier = None
    if config.push_enabled:
        notifier = PushNotifier(store, ExpoPushClient(access_token=config.expo_access_token),
                                executor=ThreadPoolExecutor(max_workers=2, thread_name_prefix="push"))
    aggregator = None
    if config.gocardless_configured:
        def aggregator() -> Any:
            from backoffice.connectors.open_banking import GOCARDLESS_API, GoCardlessBankAccountData

            return GoCardlessBankAccountData(config.gocardless_secret_id, config.gocardless_secret_key,
                                             base_url=config.gocardless_api_url or GOCARDLESS_API)
    return {"store": store, "objects": objects, "vault": vault, "authorizer": authorizer,
            "mailer": mailer_from_env(), "brain_factory": brain_factory, "notifier": notifier,
            "aggregator": aggregator, "reader": reader_from_env(), "now": now}


def production_services(config: ServerConfig, overrides: Mapping[str, Any]) -> dict[str, Any]:
    """The configured services, or (tests) exactly the ones given when a ``store`` is among them."""
    return dict(overrides) if "store" in overrides else {**_default_services(config), **overrides}


def build_manager(config: ServerConfig, services: Mapping[str, Any]) -> TenantManager:
    """The tenant manager the API and the sync worker share (same store, vault, reader, mailer, push)."""
    now: Callable[[], datetime] = services.get("now") or (lambda: datetime.now(timezone.utc))
    return TenantManager(
        services["store"], services["objects"], now=now, vault=services.get("vault"),
        authorizer=services.get("authorizer"), mailer=services.get("mailer"),
        brain_factory=services.get("brain_factory"), notifier=services.get("notifier"),
        reader=services.get("reader"), cache_size=config.tenant_cache_size,
        strict_reads=bool(services.get("strict_reads", config.strict_reads)))


def build_production_app(config: ServerConfig, **overrides: Any) -> FastAPI:
    """The production app. Tests pass ``store``, ``objects``, ``now``, fakes for OAuth, GoCardless, push."""
    configure_logging(config.log_level)
    services = production_services(config, overrides)
    store: Store = services["store"]
    now: Callable[[], datetime] = services.get("now") or (lambda: datetime.now(timezone.utc))
    manager = build_manager(config, services)
    auth = AuthService(store, manager, rate_key=config.state_key, admin_emails=config.admin_emails, now=now)
    purger = services.get("purger") or purger_from_config(config, services["objects"])
    aggregator_factory = services.get("aggregator")

    app = FastAPI(title="Back Office", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.manager = manager
    app.state.auth = auth
    app.state.store = store
    app.add_middleware(
        CORSMiddleware, allow_origins=list(config.allowed_origins), allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Requested-With", "Idempotency-Key"],
        max_age=600,
    )
    app.add_middleware(_Guard, max_json_bytes=config.max_json_bytes)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return _error(404, "not_found", "I can't find that.")
        if exc.status_code == 405:
            return _error(405, "method_not_allowed", "That is not something I can do here.")
        return _error(exc.status_code, "error", "That did not work.")

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled", exc_info=exc, extra={"route": route_template(request.url.path)})
        return _error(500, "server_error", "Something went wrong on our side. Please try again.")

    # ----------------------------------------------------------------- helpers

    def _ip(request: Request) -> str | None:
        return request.client.host if request.client else None

    def _client(request: Request) -> str:
        return "web" if request.headers.get("origin") else "mobile"

    def _set_cookie(response: Response, token: str) -> None:
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_DAYS * 86400, httponly=True,
                            secure=config.secure_cookies, samesite="lax", path="/")

    def _clear_cookie(response: Response) -> None:
        response.delete_cookie(COOKIE_NAME, path="/", secure=config.secure_cookies, httponly=True, samesite="lax")

    async def _json(request: Request, *, required: bool = False) -> dict[str, Any]:
        raw = await request.body()
        if not raw:
            if required:
                raise AuthError(400, "bad_request", "I couldn't read that request.")
            return {}
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AuthError(400, "bad_request", "I couldn't read that request.") from None
        if not isinstance(body, dict):
            raise AuthError(400, "bad_request", "I couldn't read that request.")
        return body

    def _token(request: Request) -> tuple[str | None, str]:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip(), "bearer"
        return request.cookies.get(COOKIE_NAME), "cookie"

    async def _signed_in(request: Request, *, owner: bool = False) -> tuple[Principal, bool]:
        token, via = _token(request)
        principal, extended = await run_in_threadpool(auth.authenticate, token, via)
        if via == "cookie" and request.method not in ("GET", "HEAD", "OPTIONS") and \
                request.headers.get(CSRF_HEADER, "") != CSRF_VALUE:
            raise AuthError(403, "forbidden", CSRF_BLOCKED)
        if admin_only(request.url.path) and not principal.is_admin:
            raise AuthError(403, "forbidden", ADMIN_ONLY)
        if not permitted(principal, request.method, request.url.path) or (owner and not principal.is_owner):
            raise AuthError(403, "forbidden", FORBIDDEN)
        request.state.log_tenant = principal.tenant.id  # read by the request log line (_Guard)
        request.state.log_role = principal.role
        return principal, extended and via == "cookie"

    def _reply(status: int, body: Any, *, refresh: tuple[bool, str | None] = (False, None)) -> Response:
        response = JSONResponse(body, status_code=status)
        if refresh[0] and refresh[1]:
            _set_cookie(response, refresh[1])
        return response

    def _guarded(handler: Callable[..., Awaitable[Response]]) -> Callable[..., Awaitable[Response]]:
        @functools.wraps(handler)
        async def run(*args: Any, **kwargs: Any) -> Response:
            try:
                return await handler(*args, **kwargs)
            except AuthError as exc:
                return _error(exc.status, exc.error, exc.message)
            except StoreUnavailable:
                log.warning("store_unavailable")
                return _error(503, "unavailable", UNAVAILABLE, {"Retry-After": "30"})
            except ReplayDiverged:
                return _error(503, "unavailable", DIVERGED)
            except TenantNotFound:
                return _error(401, "unauthorized", "Please sign in again.")

        return run

    def _require_json(request: Request) -> None:
        if not request.headers.get("content-type", "").split(";")[0].strip().lower() == "application/json":
            raise AuthError(415, "unsupported", "Send this as JSON.")

    # ----------------------------------------------------------------- health

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        ok, _detail = await run_in_threadpool(store.schema_status)
        if ok:
            return JSONResponse({"ok": True})
        return _error(503, "not_ready", "Not ready yet: the database is unreachable or not migrated.")

    # ----------------------------------------------------------------- sign-in

    @app.post("/api/auth/signup")
    @_guarded
    async def signup(request: Request) -> Response:
        _require_json(request)
        body = await _json(request, required=True)
        issued = await run_in_threadpool(auth.signup, body, ip=_ip(request), client=_client(request))
        out = issued.principal.public()
        response = JSONResponse({"user": out["user"], "tenant": out["tenant"], "token": issued.token},
                                status_code=201)
        _set_cookie(response, issued.token)
        return response

    @app.post("/api/auth/login")
    @_guarded
    async def login(request: Request) -> Response:
        _require_json(request)
        body = await _json(request, required=True)
        issued = await run_in_threadpool(auth.login, body, ip=_ip(request), client=_client(request))
        out = issued.principal.public()
        response = JSONResponse({"user": out["user"], "tenant": out["tenant"], "token": issued.token})
        _set_cookie(response, issued.token)
        return response

    @app.post("/api/auth/logout")
    @_guarded
    async def logout(request: Request) -> Response:
        principal, _ = await _signed_in(request)
        await run_in_threadpool(auth.logout, principal)
        response = Response(status_code=204)
        _clear_cookie(response)
        return response

    @app.get("/api/auth/me")
    @_guarded
    async def me(request: Request) -> Response:
        principal, extended = await _signed_in(request)
        return _reply(200, principal.public(), refresh=(extended, _token(request)[0]))

    # ----------------------------------------------------------------- account (GDPR)

    @app.get("/api/account/export")
    @_guarded
    async def account_export(request: Request) -> Response:
        principal, _ = await _signed_in(request, owner=True)
        filename, data = await run_in_threadpool(export_account, manager, principal)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.post("/api/account/delete")
    @_guarded
    async def account_delete(request: Request) -> Response:
        principal, _ = await _signed_in(request, owner=True)
        body = await _json(request, required=True)
        if body.get("confirm") != "DELETE":
            raise AuthError(400, "bad_request", "Type DELETE to confirm.")
        await run_in_threadpool(auth.reauthenticate, principal, body.get("password"), ip=_ip(request))
        await run_in_threadpool(erase_account, manager, principal, purger=purger)
        log.info("account_erased", extra={"tenant": principal.tenant.id})
        response = JSONResponse({"ok": True, "message": "Your account is being deleted. Your data is gone "
                                 "from the back office and your files are being removed."}, status_code=202)
        _clear_cookie(response)
        return response

    # ----------------------------------------------------------------- onboarding

    @app.post("/api/onboarding/company")
    @_guarded
    async def onboarding_company(request: Request) -> Response:
        principal, refresh = await _signed_in(request, owner=True)
        body = await _json(request)
        if not str(body.get("taxId") or "").strip():
            raise AuthError(400, "bad_request", "I need the company's NIF. It has 9 digits.")
        status, out = await run_in_threadpool(manager.add_company, principal.tenant.id, principal.user.id, body)
        return _reply(status, out, refresh=(refresh, _token(request)[0]))

    @app.post("/api/onboarding/accountant")
    @_guarded
    async def onboarding_accountant(request: Request) -> Response:
        principal, refresh = await _signed_in(request, owner=True)
        body = await _json(request)
        status, out = await run_in_threadpool(manager.set_accountant, principal.tenant.id, principal.user.id, body)
        return _reply(status, out, refresh=(refresh, _token(request)[0]))

    # ----------------------------------------------------------------- mailbox sign-in (Google, Microsoft)

    @app.get("/api/oauth/start")
    @_guarded
    async def oauth_start(request: Request) -> Response:
        principal, _ = await _signed_in(request, owner=True)
        provider = request.query_params.get("provider", "")
        address = request.query_params.get("address", "").strip().lower()
        web = config.web_url
        authorizer = manager.authorizer
        if provider not in ("google", "microsoft"):
            raise AuthError(400, "bad_request", "Choose Google or Microsoft.")
        if authorizer is None or provider not in getattr(authorizer, "providers", ()):
            label = "Google" if provider == "google" else "Microsoft"
            raise AuthError(503, "unavailable", f"{label} sign-in is not set up on this server yet.")
        if address:
            status, out = await run_in_threadpool(manager.command, principal.tenant.id, principal.user.id, "POST",
                                                  "/api/sources", {"kind": "email", "provider": provider,
                                                                   "address": address})
            url = out.get("authorizeUrl") if status == 200 else None
            return RedirectResponse(url or f"{web}/sources/?signin=failed", status_code=302)
        pending = f"mail-{provider}-{secrets.token_hex(6)}"
        url = await run_in_threadpool(authorizer.begin, provider, principal.tenant.id, pending)
        return RedirectResponse(url, status_code=302)

    @app.get("/api/oauth/callback")
    @_guarded
    async def oauth_callback(request: Request) -> Response:
        web = config.web_url
        code, state = request.query_params.get("code", ""), request.query_params.get("state", "")
        if request.query_params.get("error") or manager.authorizer is None or not code or not state:
            return RedirectResponse(f"{web}/sources/?signin=failed", status_code=302)
        try:
            done = await run_in_threadpool(manager.authorizer.complete, code, state)
        except Exception:
            log.warning("oauth_callback_refused")
            return RedirectResponse(f"{web}/sources/?signin=failed", status_code=302)
        status, _ = await run_in_threadpool(manager.finish_sign_in, done["tenant_id"], done["connection_id"],
                                            done["provider"], done.get("email"))
        return RedirectResponse(f"{web}/sources/?signin={'done' if status == 200 else 'failed'}", status_code=302)

    # ----------------------------------------------------------------- bank link (GoCardless, PSD2)

    def _sign(payload: dict[str, Any]) -> str:
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
        sig = hmac.new(config.state_key, body.encode(), hashlib.sha256).digest()[:18]
        return f"{body}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"

    def _unsign(value: str) -> dict[str, Any] | None:
        try:
            body, sig = value.split(".", 1)
            expected = base64.urlsafe_b64encode(
                hmac.new(config.state_key, body.encode(), hashlib.sha256).digest()[:18]).rstrip(b"=").decode()
            if not hmac.compare_digest(sig, expected):
                return None
            payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or int(payload.get("e", 0)) < now().timestamp():
            return None
        return payload

    @app.post("/api/connections/bank/start")
    @_guarded
    async def bank_start(request: Request) -> Response:
        principal, _ = await _signed_in(request, owner=True)
        body = await _json(request)
        institution = str(body.get("institutionId") or "").strip().upper()
        if not _INSTITUTION.match(institution):
            raise AuthError(400, "bad_request", "Choose your bank from the list.")
        if aggregator_factory is None or manager.vault is None or len(config.state_key) < 32:
            raise AuthError(503, "unavailable", "Bank connections are not set up on this server yet.")
        companies = await run_in_threadpool(manager.read, principal.tenant.id, lambda svc: list(svc.repo.companies))
        company = str(body.get("companyId") or (companies[0] if companies else ""))
        if company not in companies:
            raise AuthError(400, "bad_request", "Choose which company this bank account belongs to.")
        nonce = secrets.token_hex(6)
        pending = f"bank-pending-{nonce}"
        reference = _sign({"t": principal.tenant.id, "c": company, "i": institution, "n": nonce,
                           "e": int(now().timestamp()) + 3600})

        def start() -> str:
            aggregator = aggregator_factory()
            link = aggregator.create_link(institution_id=institution,
                                          redirect_url=f"{config.api_url}/api/connections/bank/callback",
                                          reference=reference, history_days=90, access_days=180)
            manager.vault.store(principal.tenant.id, pending, "open_banking",
                                {"requisition_id": link.requisition_id, "agreement_id": link.agreement_id or "",
                                 "institution_id": institution})
            return link.link

        try:
            url = await run_in_threadpool(start)
        except (AuthError, StoreUnavailable):
            raise
        except Exception:
            log.warning("bank_link_failed", extra={"tenant": principal.tenant.id})
            raise AuthError(502, "bank_unavailable", "Your bank's connection service did not answer. "
                                                     "Please try again in a few minutes.") from None
        return JSONResponse({"redirectUrl": url})

    @app.get("/api/connections/bank/callback")
    @_guarded
    async def bank_callback(request: Request) -> Response:
        web = config.web_url
        payload = _unsign(request.query_params.get("ref", ""))
        if payload is None or aggregator_factory is None or manager.vault is None:
            return RedirectResponse(f"{web}/sources/?bank=failed", status_code=302)
        tenant_id, pending = str(payload["t"]), f"bank-pending-{payload['n']}"

        def finish() -> bool:
            from backoffice.connectors.open_banking import ConsentStatus

            secret = manager.vault.open(tenant_id, pending)
            aggregator = aggregator_factory()
            consent = aggregator.consent(secret["requisition_id"])
            if consent.status is not ConsentStatus.ACTIVE:
                return False
            infos = [aggregator.account(a) for a in consent.account_ids]
            ibans = [info.iban for info in infos if info.iban]
            # Which bank account is which: the sync worker files each payment under the right account.
            secret = {**secret, "accounts": {info.account_id: info.iban for info in infos if info.iban}}
            institution = str(payload["i"])
            bank = institution.split("_")[0].title()
            until = consent.expires_at.date() if consent.expires_at is not None else None
            status, out = manager.bank_linked(tenant_id, bank=bank, company_id=str(payload["c"]), ibans=ibans,
                                              consent_until=until)
            if status != 200:
                return False
            connector = manager.read(tenant_id, lambda svc: next(
                (c.id for c in svc.repo.connectors.values() if c.kind == "bank" and c.name == bank), None))
            if connector:
                manager.vault.store(tenant_id, connector, "open_banking", secret, expires_at=consent.expires_at)
                manager.vault.delete(tenant_id, pending)
            return True

        try:
            ok = await run_in_threadpool(finish)
        except Exception:
            log.warning("bank_callback_failed", extra={"tenant": tenant_id})
            ok = False
        return RedirectResponse(f"{web}/sources/?bank={'done' if ok else 'failed'}", status_code=302)

    # ----------------------------------------------------------------- devices

    @app.post("/api/devices")
    @_guarded
    async def devices_add(request: Request) -> Response:
        principal, _ = await _signed_in(request)
        body = await _json(request)
        token, platform = str(body.get("expoPushToken") or ""), body.get("platform")
        if not _EXPO_TOKEN.match(token) or platform not in ("ios", "android"):
            raise AuthError(400, "bad_request", "That device could not be registered.")
        at = now()
        await run_in_threadpool(store.save_device, Device(principal.tenant.id, token, principal.user.id,
                                                          str(platform), at, at))
        return Response(status_code=204)

    @app.post("/api/devices/remove")
    @_guarded
    async def devices_remove(request: Request) -> Response:
        principal, _ = await _signed_in(request)
        body = await _json(request)
        token = str(body.get("expoPushToken") or "")
        if not _EXPO_TOKEN.match(token):
            raise AuthError(400, "bad_request", "That device could not be found.")
        await run_in_threadpool(store.remove_device, principal.tenant.id, principal.user.id, token)
        return Response(status_code=204)

    # ----------------------------------------------------------------- accountant API (§28), by key

    async def _key_tenant(request: Request) -> tuple[str, str] | None:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return None
        secret = header[7:].strip()
        if not secret.startswith("bo_live_") or len(secret) > 200:
            return None
        tenant_id = await run_in_threadpool(store.api_key_tenant, hashlib.sha256(secret.encode()).hexdigest())
        if tenant_id is None:
            return None
        ok = await run_in_threadpool(manager.read, tenant_id, lambda svc: svc.api_authorize(secret))
        return (tenant_id, secret) if ok else None

    @app.get("/api/v1/documents")
    @_guarded
    async def v1_documents(request: Request) -> Response:
        found = await _key_tenant(request)
        if found is None:
            return _error(401, "unauthorized", "This key is not valid.")
        query = dict(request.query_params)
        out = await run_in_threadpool(manager.read, found[0], lambda svc: svc.documents_list(query))
        return JSONResponse(out)

    @app.get("/api/v1/documents/{document_id}/file")
    @_guarded
    async def v1_document_file(document_id: str, request: Request) -> Response:
        found = await _key_tenant(request)
        if found is None:
            return _error(401, "unauthorized", "This key is not valid.")
        f = await run_in_threadpool(manager.read, found[0], lambda svc: svc.assistant.document_file(document_id))
        if f is None:
            return _error(404, "not_found", "I can't find that document.")
        return Response(f[2], media_type=f[1], headers={"Content-Disposition": f'attachment; filename="{f[0]}"'})

    @app.get("/api/v1/export")
    @_guarded
    async def v1_export(request: Request) -> Response:
        found = await _key_tenant(request)
        if found is None:
            return _error(401, "unauthorized", "This key is not valid.")
        q = dict(request.query_params)

        def build(svc: Any) -> tuple[str, bytes, int]:
            return svc.assistant.export_zip(company_id=q.get("company", ""), date_from=svc._date_arg(q, "from"),
                                            date_to=svc._date_arg(q, "to"))

        try:
            name, data, _ = await run_in_threadpool(manager.read, found[0], build)
        except Exception:
            return _error(400, "bad_request", "Use dates like 2026-09-30.")
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ----------------------------------------------------------------- uploads

    async def _upload(request: Request, path: str) -> Response:
        from backoffice.api.app import _multipart

        principal, refresh = await _signed_in(request)
        data = await request.body()
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("multipart/form-data"):
            body = await _json(request)
        else:
            fields, file_part = _multipart(data, content_type)
            if file_part is None:
                return _error(400, "bad_request", "There was nothing to save.")
            body = {**fields, "filename": file_part["filename"], "contentType": file_part["content_type"],
                    "dataBase64": base64.b64encode(file_part["data"]).decode("ascii")}
            key = request.headers.get("idempotency-key")
            if key and "client_item_id" not in body:
                body["client_item_id"] = key
        status, out = await run_in_threadpool(manager.command, principal.tenant.id, principal.user.id, "POST",
                                              path, body)
        return _reply(status, out, refresh=(refresh, _token(request)[0]))

    @app.post("/api/evidence")
    @_guarded
    async def upload_evidence(request: Request) -> Response:
        return await _upload(request, "/api/evidence")

    @app.post("/api/evidence/upload")
    @_guarded
    async def upload_receipt(request: Request) -> Response:
        return await _upload(request, "/api/evidence/upload")

    # ----------------------------------------------------------------- the team's dashboard (admins only)

    def _internal(principal: Principal, target: str, query: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """``/api/internal/<view>`` over every tenant (read-only), for an admin."""
        from backoffice import internal

        view = target.rstrip("/").rsplit("/", 1)[-1]
        if view not in INTERNAL_VIEWS:
            return 404, {"error": "not_found", "message": "I can't find that."}
        try:
            tenant_ids = store.tenant_ids()
        except StoreUnavailable:
            raise
        except StoreError:  # the login may not list tenants (not in backoffice_scheduler): own business only
            log.warning("tenant_list_unavailable")
            tenant_ids = []
        if principal.tenant.id not in tenant_ids:
            tenant_ids = [*tenant_ids, principal.tenant.id]
        return 200, manager.read_many(tenant_ids, lambda services: internal.handle(view, services, query),
                                      what=f"GET {route_template(target)}")

    # ----------------------------------------------------------------- everything else the demo serves

    @app.api_route("/api/{path:path}", methods=["GET", "POST"])
    @_guarded
    async def api(path: str, request: Request) -> Response:
        target = "/api/" + path
        if target.startswith("/api/v1/") or target in PUBLIC_API:
            return _error(404, "not_found", "I can't find that.")
        principal, refresh = await _signed_in(request)
        tenant, actor = principal.tenant.id, principal.user.id
        if admin_only(target):
            if request.method != "GET":
                return _error(405, "method_not_allowed", "That is not something I can do here.")
            status, out = await run_in_threadpool(_internal, principal, target, dict(request.query_params))
        elif request.method == "GET":
            query = ("?" + request.url.query) if request.url.query else ""
            status, out = await run_in_threadpool(manager.view, tenant, "GET", target + query, None)
        else:
            body = await _json(request)
            if target in READ_ONLY_POSTS:
                status, out = await run_in_threadpool(manager.view, tenant, "POST", target, body)
            elif target == "/api/chat":
                status, out = await run_in_threadpool(manager.chat, tenant, actor, body)
            elif target == "/api/chat/tool":
                status, out = await run_in_threadpool(manager.browser_tool, tenant, actor, body)
            else:
                status, out = await run_in_threadpool(manager.command, tenant, actor, "POST", target, body)
        return _reply(status, out, refresh=(refresh, _token(request)[0]))

    return app
