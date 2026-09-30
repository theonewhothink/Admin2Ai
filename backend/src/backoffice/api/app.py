"""HTTP API: a thin FastAPI wrapper over :class:`backoffice.service.BackOfficeService`.

Every route hands the request to ``BackOfficeService.dispatch``; the only work
done here is HTTP plumbing (CORS, reading multipart uploads with the standard
library, status codes). The same service runs in the browser without this file.

    uvicorn backoffice.api.app:app --port 8000

Handlers are ``async`` on purpose: they run on the event loop one at a time,
so the in-memory service is never touched by two threads at once.
"""

from __future__ import annotations

import base64
import json
import os
from email import policy
from email.parser import BytesParser
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backoffice.service import BackOfficeService

__all__ = ["ALLOWED_ORIGINS", "MAX_UPLOAD_BYTES", "app", "create_app"]

ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000", "https://theonewhothink.github.io"]
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _json(status: int, body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=status, content=body)


def _multipart(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, Any] | None]:
    """Fields and the (first) file of a multipart/form-data body.

    Parts are cut at the boundary by hand so a file's bytes arrive exactly as sent
    (the §43 receipt hashes them); only each part's headers go through the stdlib parser.
    """
    header = BytesParser(policy=policy.HTTP).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode("latin-1"))
    boundary = header.get_param("boundary")
    if not isinstance(boundary, str) or not boundary:
        return {}, None
    fields: dict[str, str] = {}
    file_part: dict[str, Any] | None = None
    delimiter = b"--" + boundary.encode("latin-1")
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):
            break  # closing delimiter
        chunk = chunk[2:] if chunk.startswith(b"\r\n") else chunk
        head, sep, payload = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        payload = payload[:-2] if payload.endswith(b"\r\n") else payload
        part = BytesParser(policy=policy.HTTP).parsebytes(head + b"\r\n\r\n")
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        if filename is not None or name == "file":
            if file_part is None:
                file_part = {"filename": filename, "content_type": part.get_content_type(), "data": payload}
        elif name:
            fields[str(name)] = payload.decode("utf-8", errors="replace")
    return fields, file_part


def _service_from_env() -> BackOfficeService:
    """Demo data plus real Google/Microsoft sign-in when their OAuth apps are configured."""
    svc = BackOfficeService.demo()
    from backoffice.connectors.authorize import OAuthAuthorizer, app_from_env

    apps = {p: a for p in ("google", "microsoft") if (a := app_from_env(p)) is not None}
    key = os.environ.get("BACKOFFICE_STATE_KEY", "")
    base = os.environ.get("BACKOFFICE_API_URL", "http://localhost:8000").rstrip("/")
    if apps and svc.vault is not None and len(key) >= 32:
        svc.authorizer = OAuthAuthorizer(apps, svc.vault, redirect_uri=f"{base}/api/oauth/callback",
                                         state_key=key.encode())
    from backoffice.mailer import mailer_from_env

    svc.mailer = mailer_from_env()
    from backoffice.reading import reader_from_env

    # Uploaded PDFs and photos are read (Stage 0, OCR sidecar, Claude vision only if switched on).
    svc.repo.reader = reader_from_env()
    if os.environ.get("ANTHROPIC_API_KEY"):
        from backoffice.assistant import ClaudeBrain

        svc.brain = ClaudeBrain(svc.assistant)
    return svc


def create_app(service: BackOfficeService | None = None) -> FastAPI:
    """The demo app for ``service`` (or the demo tenant); the production app when BACKOFFICE_MODE=production.

    ``BACKOFFICE_MODE=demo`` (the default) serves exactly the one frozen demo
    tenant, without sign-in, as the static site and the tests expect.
    """
    if service is None:
        from backoffice.server.config import PRODUCTION, mode_from_env

        if mode_from_env() == PRODUCTION:
            from backoffice.server.config import ServerConfig
            from backoffice.server.http import build_production_app

            return build_production_app(ServerConfig.from_env())
    svc = service if service is not None else _service_from_env()
    app = FastAPI(title="Back Office", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.service = svc
    app.add_middleware(
        CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"],
    )

    async def _body(request: Request) -> bytes | None:
        data = await request.body()
        if len(data) > MAX_UPLOAD_BYTES + 1024 * 1024:
            return None
        return data

    @app.get("/api/oauth/callback")
    async def oauth_callback(code: str = "", state: str = "", error: str = "") -> Any:
        """Where Google/Microsoft send the owner back after consent (connectors/authorize.py)."""
        from fastapi.responses import RedirectResponse

        web = os.environ.get("BACKOFFICE_WEB_URL", "http://localhost:3000").rstrip("/")
        if error or svc.authorizer is None:
            return RedirectResponse(f"{web}/sources/?signin=failed")
        try:
            done = svc.authorizer.complete(code, state)
        except Exception:
            return RedirectResponse(f"{web}/sources/?signin=failed")
        svc.finish_sign_in(done["connection_id"])
        return RedirectResponse(f"{web}/sources/?signin=done")

    def _bearer(request: Request) -> bool:
        auth = request.headers.get("authorization", "")
        return auth.lower().startswith("bearer ") and svc.api_authorize(auth[7:].strip())

    @app.get("/api/v1/documents")
    async def v1_documents(request: Request) -> JSONResponse:
        """Accountant API (§28): list documents. Query: company, from, to, supplier, q."""
        if not _bearer(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(svc.documents_list(dict(request.query_params)))

    @app.get("/api/v1/documents/{document_id}/file")
    async def v1_document_file(document_id: str, request: Request) -> Any:
        from fastapi.responses import Response

        if not _bearer(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        f = svc.assistant.document_file(document_id)
        if f is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return Response(f[2], media_type=f[1], headers={"Content-Disposition": f'attachment; filename="{f[0]}"'})

    @app.get("/api/v1/export")
    async def v1_export(request: Request) -> Any:
        from fastapi.responses import Response

        if not _bearer(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        q = dict(request.query_params)
        try:
            name, data, _ = svc.assistant.export_zip(company_id=q.get("company", ""),
                                                     date_from=svc._date_arg(q, "from"), date_to=svc._date_arg(q, "to"))
        except Exception:
            return JSONResponse({"error": "bad_request", "message": "Use dates like 2026-09-30."}, status_code=400)
        return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    async def _upload(request: Request, path: str) -> JSONResponse:
        data = await _body(request)
        if data is None:
            return _json(413, {"error": "too_large", "message": "This file is too large to send."})
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("multipart/form-data"):
            status, body = svc.dispatch("POST", path, data)
            return _json(status, body)
        fields, file_part = _multipart(data, content_type)
        if file_part is None:
            return _json(400, {"error": "bad_request", "message": "There was nothing to save."})
        payload: dict[str, Any] = {
            **fields,
            "filename": file_part["filename"],
            "contentType": file_part["content_type"],
            "dataBase64": base64.b64encode(file_part["data"]).decode("ascii"),
        }
        key = request.headers.get("idempotency-key")
        if key and "client_item_id" not in payload:
            payload["client_item_id"] = key
        status, body = svc.dispatch("POST", path, payload)
        return _json(status, body)

    @app.post("/api/evidence")
    async def upload_evidence(request: Request) -> JSONResponse:
        return await _upload(request, "/api/evidence")

    @app.post("/api/evidence/upload")
    async def upload_receipt(request: Request) -> JSONResponse:
        return await _upload(request, "/api/evidence/upload")

    @app.api_route("/api/{path:path}", methods=["GET", "POST"])
    async def api(path: str, request: Request) -> JSONResponse:
        data = await _body(request)
        if data is None:
            return _json(413, {"error": "too_large", "message": "This request is too large."})
        target = "/api/" + path
        if request.url.query:
            target += "?" + request.url.query
        try:
            status, body = svc.dispatch(request.method, target, data or None)
        except (json.JSONDecodeError, UnicodeDecodeError):
            status, body = 400, {"error": "bad_request", "message": "I couldn't read that request."}
        return _json(status, body)

    return app


app = create_app()
