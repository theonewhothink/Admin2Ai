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


def create_app(service: BackOfficeService | None = None) -> FastAPI:
    svc = service if service is not None else BackOfficeService.demo()
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
