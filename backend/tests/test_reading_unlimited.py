"""Unlimited-OCR on the server, for long documents (§16, §17, QA E5).

The server registers Unlimited-OCR when ``BACKOFFICE_OCR_UNLIMITED_URL`` points at its OpenAI-compatible
server (``vllm serve``); these tests run a stand-in of that server over real HTTP on this machine (the real
model has not run here). The stand-in answers the documented contract: chat completions with the page images,
a transcription split per page by ``=== PAGE n ===`` markers.

Proven here: the server registers it when configured, with its timeout, page limit and model; a long scanned
PDF (12 pages, beyond the local engine's 10) whose fields are still missing after Stage 0 goes to it, page
images in batches; what it reads is one more reading, cross-checked like any other (it confirms the PDF's
own text, never overrules it, and never verifies a field on its own); a document longer than its page limit
is never sent; an engine that is down or too slow is recorded and the upload is kept; and when the layout
engine is down, it is the fallback for a photo nobody else could read.
"""

from __future__ import annotations

import base64
import io
import json
import re
import threading
import time
import zlib
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from backoffice.domain.models import ExtractionMethod, Quality
from backoffice.ocr import UNLIMITED_OCR, PdfPageRasterizer, UnlimitedOCRProvider
from backoffice.reading import StepState, reader_from_env
from backoffice.service import BackOfficeService

pytest.importorskip("pypdf")  # Stage 0 reads the PDF's text layer
pytest.importorskip("pypdfium2")  # the pages are sent as images
pytest.importorskip("PIL")

PHOTOS = Path(__file__).parent / "fixtures" / "photos"
SUPPLIER_NIF = "508732140"
HAZEL_NIF = "516123459"

# Page 1 has a text layer (the PDF's own words); pages 2 to 12 are scanned images.
FRONT = [
    "Telecomunicacoes Atlantico, S.A.",
    f"NIF: {SUPPLIER_NIF}",
    "Fatura n.º FT TA2026/9031",
    "ATCUD: TA7Q2K9M-9031",
    "Data de emissão: 05/09/2026",
    "Data de vencimento: 25/09/2026",
    "Cliente: Hazel Tree Interiores, Lda.",
    f"NIF: {HAZEL_NIF}",
    "Detalhe de chamadas e serviços nas páginas seguintes.",
]
DETAIL = ["Detalhe de chamadas - pagina {n}", "02/08 10:14  912 345 678  00:03:12  0,00",
          "03/08 16:40  226 091 344  00:01:05  0,00"]
SUMMARY = ["Resumo da fatura", "Base tributável (23%): 182,40", "IVA 23%: 41,95", "Total: 224,35 €"]


def transcription(page: int, *, invoice: str = "FT TA2026/9031") -> str:
    """What the stand-in Unlimited-OCR reads on each page."""
    if page == 1:
        return "\n".join(line.replace("FT TA2026/9031", invoice) for line in FRONT)
    if page == 12:
        return "\n".join(SUMMARY)
    return "\n".join(line.format(n=page) for line in DETAIL)


# --------------------------------------------------------------------------- a long scanned PDF


def _scan(lines: list[str]) -> bytes:
    from PIL import Image, ImageDraw

    page = Image.new("RGB", (620, 877), (250, 250, 247))
    draw = ImageDraw.Draw(page)
    for i, line in enumerate(lines):
        draw.text((40, 60 + 22 * i), line, fill=(20, 20, 24))
    out = io.BytesIO()
    page.save(out, format="JPEG", quality=80)
    return out.getvalue()


def long_pdf(pages: int = 12) -> bytes:
    """A PDF whose first page is text (Helvetica, WinAnsi) and whose other pages are JPEG scans."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    page_ids: list[int] = []
    bodies: list[tuple[bytes, bytes | None]] = []
    for number in range(1, pages + 1):
        if number == 1:
            text = b"BT /F1 12 Tf 50 790 Td 16 TL " + b" ".join(
                b"(" + line.encode("cp1252").replace(b"(", b"\\(").replace(b")", b"\\)") + b") '" for line in FRONT
            ) + b" ET"
            bodies.append((text, None))
        else:
            lines = SUMMARY if number == pages else [line.format(n=number) for line in DETAIL]
            bodies.append((b"q 595 0 0 842 0 0 cm /Im1 Do Q", _scan(lines)))
    for content, jpeg in bodies:
        stream = zlib.compress(content)
        content_id = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream")
        resources = b"/Font << /F1 %d 0 R >>" % font
        if jpeg is not None:
            image_id = add(b"<< /Type /XObject /Subtype /Image /Width 620 /Height 877 /ColorSpace /DeviceRGB "
                           b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n" % len(jpeg)
                           + jpeg + b"\nendstream")
            resources += b" /XObject << /Im1 %d 0 R >>" % image_id
        page_ids.append(add(b"<< /Type /Page /Parent PAGES 0 R /MediaBox [0 0 595 842] /Resources << "
                            + resources + b" >> /Contents %d 0 R >>" % content_id))
    tree = add(b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % i for i in page_ids) + b"] /Count %d >>" % pages)
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % tree)
    objects = [o.replace(b"PAGES 0 R", b"%d 0 R" % tree) for o in objects]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref))
    return out.getvalue()


# --------------------------------------------------------------------------- the stand-in server


class UnlimitedServer(BaseHTTPRequestHandler):
    """vLLM's OpenAI-compatible chat completions, answering like Unlimited-OCR (module docstring)."""

    calls: list[dict] = []
    mode = "ok"  # "ok" | "down" | "slow"
    invoice = "FT TA2026/9031"
    photo: str | None = None  # the transcription of a one-page photo, when one is sent

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cls = type(self)
        content = body["messages"][0]["content"]
        prompt = next(p["text"] for p in content if p["type"] == "text")
        images = [p["image_url"]["url"] for p in content if p["type"] == "image_url"]
        numbers = [int(n) for n in re.search(r"Page numbers: ([\d, ]+)\.", prompt)[1].split(",")]
        cls.calls.append({"path": self.path, "model": body["model"], "max_tokens": body["max_tokens"],
                          "auth": self.headers.get("Authorization"), "images": images, "pages": numbers})
        if cls.mode == "slow":
            time.sleep(3)
        if cls.mode == "down":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        text = "\n".join(f"=== PAGE {n} ===\n{cls.photo or transcription(n, invoice=cls.invoice)}" for n in numbers)
        data = json.dumps({"id": "cmpl-1", "object": "chat.completion", "model": body["model"], "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}]}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):  # the reader gave up waiting (the "slow" case)
            pass

    def log_message(self, *args: object) -> None:
        return None


@pytest.fixture
def unlimited() -> Iterator[str]:
    UnlimitedServer.calls, UnlimitedServer.mode, UnlimitedServer.invoice = [], "ok", "FT TA2026/9031"
    UnlimitedServer.photo = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), UnlimitedServer)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def env(url: str, **extra: str) -> dict[str, str]:
    """The server's settings: Unlimited-OCR on, the local engine off (it takes at most 10 pages anyway)."""
    return {"BACKOFFICE_OCR_LOCAL": "off", "BACKOFFICE_OCR_UNLIMITED_URL": url,
            "BACKOFFICE_OCR_UNLIMITED_MODEL": "baidu/Unlimited-OCR", "BACKOFFICE_OCR_UNLIMITED_API_KEY": "local-key",
            "BACKOFFICE_OCR_UNLIMITED_DPI": "60", **extra}


def upload(settings: dict[str, str], data: bytes, filename: str = "fatura-setembro.pdf"):  # type: ignore[no-untyped-def]
    svc = BackOfficeService.demo()
    svc.repo.reader = reader_from_env(settings)
    body = {"filename": filename, "contentType": None, "dataBase64": base64.b64encode(data).decode()}
    status, out = svc.dispatch("POST", "/api/evidence", json.dumps(body))
    assert status == 200 and out["evidenceIds"], out
    evidence_id = out["evidenceIds"][0]
    steps = {s.step: s for s in svc.repo.reads[evidence_id].steps}
    record = svc.repo.documents[out["documents"][0]["id"]] if out["documents"] else None
    return svc, evidence_id, steps, record


# --------------------------------------------------------------------------- registration


def test_the_server_registers_unlimited_ocr_only_when_configured() -> None:
    assert UNLIMITED_OCR not in reader_from_env({"BACKOFFICE_OCR_LOCAL": "off"}).engines()
    reader = reader_from_env({
        "BACKOFFICE_OCR_LOCAL": "off", "BACKOFFICE_OCR_UNLIMITED_URL": "http://unlimited-ocr:8000",
        "BACKOFFICE_OCR_UNLIMITED_MODEL": "baidu/Unlimited-OCR", "BACKOFFICE_OCR_UNLIMITED_API_KEY": "k",
        "BACKOFFICE_OCR_UNLIMITED_TIMEOUT": "120", "BACKOFFICE_OCR_UNLIMITED_MAX_PAGES": "60",
        "BACKOFFICE_OCR_UNLIMITED_PAGES_PER_REQUEST": "4"})
    assert reader.engines() == (UNLIMITED_OCR,) and not reader.external_ai
    engine = reader._registry.find(UNLIMITED_OCR)
    assert isinstance(engine, UnlimitedOCRProvider) and engine.version == "baidu/Unlimited-OCR"
    config = engine._config
    assert config.endpoint.url(config.path) == "http://unlimited-ocr:8000/v1/chat/completions"
    assert config.endpoint.timeout_seconds == 120 and config.endpoint.request_headers()["Authorization"] == "Bearer k"
    assert (config.max_pages, config.pages_per_request) == (60, 4)
    caps = engine.capabilities
    assert caps.max_pages == 60 and caps.handles_long_docs and caps.accepts_pdf and not caps.external  # ours
    assert engine.cost_per_page == 0 and engine.method is ExtractionMethod.VLM  # a reading, never an anchor
    assert isinstance(engine._rasterizer, PdfPageRasterizer) and engine._rasterizer.max_pages == 60
    defaults = reader_from_env({"BACKOFFICE_OCR_LOCAL": "off", "BACKOFFICE_OCR_UNLIMITED_URL": "http://u:8000"})
    config = defaults._registry.find(UNLIMITED_OCR)._config
    assert (config.model, config.endpoint.timeout_seconds, config.max_pages) == ("Unlimited-OCR", 300, 200)
    for bad in ({"BACKOFFICE_OCR_UNLIMITED_MAX_PAGES": "0"}, {"BACKOFFICE_OCR_UNLIMITED_PAGES_PER_REQUEST": "x"},
                {"BACKOFFICE_OCR_UNLIMITED_URL": "unlimited-ocr:8000"}):
        with pytest.raises(ValueError):
            reader_from_env({"BACKOFFICE_OCR_UNLIMITED_URL": "http://u:8000", **bad})


# --------------------------------------------------------------------------- a long document


def test_a_long_scanned_invoice_goes_to_unlimited_ocr_and_its_reading_is_cross_checked(unlimited) -> None:
    svc, ev, steps, record = upload(env(unlimited), long_pdf())

    assert steps["pdf_text"].state is StepState.DONE  # page 1's own words: Stage 0
    assert steps["ocr_primary"].state is StepState.NOT_AVAILABLE  # no local engine (and 12 pages is beyond it)
    long_step = steps["ocr_long_document"]
    assert long_step.state is StepState.DONE and long_step.engine == UNLIMITED_OCR
    assert long_step.detail.startswith("long_document:12")
    # The 12 pages went as images, 8 and 4 (two requests at a time), to the configured model with the key.
    assert sorted(c["pages"] for c in UnlimitedServer.calls) == [list(range(1, 9)), list(range(9, 13))]
    for call in UnlimitedServer.calls:
        assert call["path"] == "/v1/chat/completions" and call["model"] == "baidu/Unlimited-OCR"
        assert call["auth"] == "Bearer local-key" and call["max_tokens"] == 8192
        assert len(call["images"]) == len(call["pages"])
        assert all(url.startswith("data:image/png;base64,") for url in call["images"])

    # Its reading is one more source, labelled as what it is.
    gross = record.checks["gross_amount"]
    [vlm] = [o for o in gross.observations if o.method is ExtractionMethod.VLM]
    assert vlm.source == f"{ev}@{UNLIMITED_OCR}" and str(vlm.value) == "224.35"
    # Where it agrees with the PDF's own text, the field is verified (two independent sources) ...
    number = record.checks["invoice_number"]
    assert number.quality is Quality.GREEN and number.value == "FT TA2026/9031"
    assert {o.method for o in number.observations} >= {ExtractionMethod.EMBEDDED_TEXT, ExtractionMethod.VLM}
    assert record.checks["supplier_tax_id"].quality is Quality.GREEN
    # ... and what only it read stays likely: never verified on its own (§17, §57).
    for field in ("gross_amount", "net_amount", "vat_amount"):
        assert record.checks[field].quality is Quality.AMBER, field
    assert record.document.quality is not Quality.GREEN
    assert str(record.document.gross_amount) == "224.35" and str(record.document.net_amount) == "182.40"


def test_unlimited_ocr_never_overrules_the_documents_own_text(unlimited) -> None:
    UnlimitedServer.invoice = "FT TA2026/9037"  # a misreading of the invoice number
    svc, ev, steps, record = upload(env(unlimited), long_pdf())
    assert steps["ocr_long_document"].state is StepState.DONE
    number = record.checks["invoice_number"]
    assert number.quality is Quality.RED  # the PDF's text says 9031, the reading 9037: nobody guesses
    assert record.document.quality is Quality.RED


def test_a_document_longer_than_the_page_limit_is_never_sent(unlimited) -> None:
    svc, ev, steps, record = upload(env(unlimited, BACKOFFICE_OCR_UNLIMITED_MAX_PAGES="10"), long_pdf())
    assert UnlimitedServer.calls == []
    long_step = steps["ocr_long_document"]
    assert long_step.state is StepState.SKIPPED and "too_many_pages" in long_step.detail
    assert svc.repo.reads[ev].needs_person  # the totals are still missing: one plain question


# --------------------------------------------------------------------------- failures


@pytest.mark.parametrize("mode", ["down", "slow"])
def test_unlimited_ocr_down_or_too_slow_is_recorded_and_the_upload_is_kept(unlimited, mode) -> None:
    UnlimitedServer.mode = mode
    started = time.monotonic()
    svc, ev, steps, record = upload(env(unlimited, BACKOFFICE_OCR_UNLIMITED_TIMEOUT="1"), long_pdf())
    assert time.monotonic() - started < 20  # a slow engine is cut off at its timeout
    failed = steps["ocr_long_document"]
    assert failed.state is StepState.FAILED and "engine_unavailable" in failed.detail
    assert steps["ocr_human"].state is StepState.NEEDS_PERSON
    assert svc.repo.evidence(ev) is not None
    assert record is None or record.checks["invoice_number"].value == "FT TA2026/9031"  # Stage 0 is kept


def test_unlimited_ocr_is_the_fallback_when_the_layout_engine_is_down(unlimited) -> None:
    """A photo nobody else could read: the layout engine is down, so the long-document engine reads it."""
    UnlimitedServer.photo = (PHOTOS / "fs-mr2026-0088.txt").read_text(encoding="utf-8")
    settings = env(unlimited, BACKOFFICE_OCR_VL_URL="http://127.0.0.1:9", BACKOFFICE_OCR_VL_TIMEOUT="2")
    svc, ev, steps, record = upload(settings, (PHOTOS / "fs-mr2026-0088-blurred.jpg").read_bytes(), "talao.jpg")
    assert steps["ocr_complex_layout"].state is StepState.FAILED
    fallback = steps["ocr_long_document"]
    assert fallback.state is StepState.DONE and "complex_fallback" in fallback.detail
    [call] = UnlimitedServer.calls
    assert call["pages"] == [1] and call["images"][0].startswith("data:image/jpeg;base64,")
    assert record is not None and str(record.checks["gross_amount"].value) == "45.60"
    assert record.checks["gross_amount"].quality is Quality.AMBER  # one reading of pixels: likely, not verified
