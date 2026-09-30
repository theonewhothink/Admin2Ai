"""The reading machinery (§13-17, §53): Stage 0 steps, the OCR sidecar over real HTTP,
the Claude vision provider's wire format, redaction, routing and configuration."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import struct
import subprocess
import sys
import threading
import zlib
from collections.abc import Iterator
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from backoffice.domain.models import BoundingBox, CriticalField, ExtractionMethod, FieldObservation
from backoffice.ocr import (
    COMMERCIAL,
    PP_OCR_V6_MEDIUM,
    ClaudeVisionConfig,
    ClaudeVisionProvider,
    EngineRegistry,
    FakeOCRProvider,
    OCRHints,
    OCRLine,
    OCRPage,
    OCRResponseError,
    OCRResult,
    OCRRouter,
    OCRUnavailable,
    PageImage,
    ReasonCode,
    RouterConfig,
    RouteStage,
    RoutingRequest,
    StepStatus,
    locate,
)
from backoffice.ocr.providers.claude import TOOL_NAME, parse_tool_answer, tool_definition
from backoffice.ocr.redact import mask_personal_details, strip_metadata, vision_redactor
from backoffice.reading import (
    DocumentReader,
    ReadRequest,
    StepState,
    fiscal_qr_payloads,
    read_image_stage0,
    read_pdf_stage0,
    reader_from_env,
    run_sync,
)
from backoffice.service import BackOfficeService

FIXTURES = Path(__file__).parent / "fixtures" / "documents"
PHOTO = (FIXTURES / "central-fs-cc2026-3317.jpg").read_bytes()
TRANSCRIPTION = (FIXTURES / "central-fs-cc2026-3317.txt").read_text(encoding="utf-8")
BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


# --------------------------------------------------------------------------- Stage 0


def test_fiscal_qr_payloads_are_found_line_by_line_and_validated() -> None:
    good = "A:501000100*B:516123459*C:PT*D:FT*E:N*F:20260918*G:FT X/1*H:0*I1:PT*I7:1.00*I8:0.23*N:0.23*O:1.23*Q:abcd*R:1"
    text = f"Fatura\nCódigo QR: {good}\nA:123*B:not a qr\n{good}\n"
    assert fiscal_qr_payloads(text) == [good]
    assert fiscal_qr_payloads("") == []


def test_pdf_stage0_reports_a_missing_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    from backoffice.extraction import pdf as pdf_module
    from backoffice.extraction._optional import MissingDependencyError

    def missing(module: str, **_: object) -> None:
        raise MissingDependencyError(module, "Reading PDF text")

    monkeypatch.setattr(pdf_module, "import_optional", missing)
    stage0 = read_pdf_stage0((FIXTURES / "edp-ft-558120.pdf").read_bytes())
    assert stage0.text == "" and [(s.step, s.state) for s in stage0.steps] == [("pdf_text", StepState.NOT_AVAILABLE)]


def test_pdf_stage0_damaged_file_fails_cleanly() -> None:
    pytest.importorskip("pypdf")
    stage0 = read_pdf_stage0(b"%PDF-1.7\n this is not a pdf body")
    assert stage0.steps[0].state is StepState.FAILED and stage0.text == ""


def test_image_stage0_without_a_decoder_says_so_and_a_broken_decoder_does_not_crash() -> None:
    assert read_image_stage0(PHOTO, None).steps[0].state is StepState.NOT_AVAILABLE

    class Broken:
        name = "broken"

        def decode(self, image: bytes) -> tuple[str, ...]:
            raise RuntimeError("boom")

    stage0 = read_image_stage0(PHOTO, Broken())
    assert stage0.steps[0].state is StepState.FAILED and stage0.text == ""

    class OtherCodes:
        name = "other"

        def decode(self, image: bytes) -> tuple[str, ...]:
            return ("https://example.com/menu",)

    assert read_image_stage0(PHOTO, OtherCodes()).steps[0].state is StepState.NOTHING


def test_reader_skips_files_that_are_not_pdfs_or_images() -> None:
    outcome = DocumentReader(qr_decoder=None).read(ReadRequest(tenant_id="t", evidence_id="ev_1", data=b"hello"))
    assert not outcome.found_anything and outcome.steps[0].state is StepState.SKIPPED


# --------------------------------------------------------------------------- redaction (§53)


def test_strip_metadata_removes_exif_from_jpeg_and_keeps_the_pixels() -> None:
    from PIL import Image

    page = PageImage(data=PHOTO, mime_type="image/jpeg")
    clean = strip_metadata(page)
    assert b"Exif" in PHOTO[:2048] and b"Exif" not in clean.data and b"Fictional Phone" not in clean.data
    with Image.open(io.BytesIO(PHOTO)) as a, Image.open(io.BytesIO(clean.data)) as b:
        assert a.size == b.size and a.tobytes() == b.tobytes()
        assert not b.getexif()


def test_strip_metadata_removes_png_text_chunks_and_leaves_unknown_bytes_alone() -> None:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buffer, format="PNG")
    raw = buffer.getvalue()
    tagged = raw[:33] + png_chunk(b"tEXt", b"Author\x00Laura, Rua da Rosa 57") + raw[33:]
    clean = strip_metadata(PageImage(data=tagged, mime_type="image/png"))
    assert b"Rua da Rosa" not in clean.data and clean.data == raw
    junk = PageImage(data=b"\xff\xd8\xff\x00garbage", mime_type="image/jpeg")
    assert strip_metadata(junk) is junk


def test_mask_personal_details_paints_over_lines_with_contact_details_only() -> None:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (200, 100), (255, 255, 255)).save(buffer, format="PNG")
    page = PageImage(data=buffer.getvalue(), mime_type="image/png", width=200, height=100)
    prior = OCRPage(number=1, width=400, height=200, lines=(
        OCRLine(text="Total: 23,90 €", bbox=BoundingBox(page=1, x0=0, y0=0, x1=200, y1=40)),
        OCRLine(text="Tel. 213 400 912", bbox=BoundingBox(page=1, x0=0, y0=100, x1=200, y1=140)),
    ))
    masked, count = mask_personal_details(page, prior)
    assert count == 1
    with Image.open(io.BytesIO(masked.data)) as img:
        assert img.getpixel((50, 60)) == (0, 0, 0)  # the phone line (scaled by half)
        assert img.getpixel((50, 10)) == (255, 255, 255)  # the total stays readable


def test_vision_redactor_renders_pdfs_or_says_it_could_not_mask_them() -> None:
    pdf = PageImage(data=(FIXTURES / "edp-ft-558120.pdf").read_bytes(), mime_type="application/pdf", page_count=1)
    unmasked = vision_redactor(render_pdfs=False)((pdf,), OCRHints())
    assert unmasked.pages == (pdf,) and "pdf_not_masked" in unmasked.notes
    pytest.importorskip("pypdfium2")
    rendered = vision_redactor()((pdf,), OCRHints())
    assert "pdf_rendered" in rendered.notes and rendered.pages[0].mime_type == "image/png"


# --------------------------------------------------------------------------- Claude vision


def test_claude_config_requires_https_and_a_key() -> None:
    with pytest.raises(ValueError):
        ClaudeVisionConfig(api_key="k", base_url="http://api.anthropic.com")
    with pytest.raises(ValueError):
        ClaudeVisionConfig(api_key=" ")
    assert ClaudeVisionConfig(api_key="k").model == "claude-sonnet-5-5"
    assert "secret-key-123" not in repr(ClaudeVisionConfig(api_key="secret-key-123"))


def test_tool_asks_for_every_critical_field_of_section_18() -> None:
    tool = tool_definition()
    assert tool["name"] == TOOL_NAME
    assert set(tool["input_schema"]["properties"]["fields"]["properties"]) == {f.value for f in CriticalField}


def test_parse_tool_answer_is_tolerant_but_never_invents() -> None:
    body = {"stop_reason": "max_tokens", "content": [
        {"type": "text", "text": "Here you go"},
        {"type": "tool_use", "name": TOOL_NAME, "input": {
            "supplier_name": "  Café  Central ", "document_type": "made-up",
            "fields": {"gross_amount": {"value": "23.90", "page": 9}, "vat_amount": "2.75",
                       "iban": {"value": ""}, "unknown_field": {"value": "x"}}}}]}
    readings, supplier, doc_type, stop = parse_tool_answer(body, engine="c", pages=[1], field_confidence=0.6)
    assert [(r.field, r.value, r.page) for r in readings] == [
        (CriticalField.GROSS_AMOUNT, "23.90", None), (CriticalField.VAT_AMOUNT, "2.75", None)]
    assert supplier == "Café Central" and doc_type is None and stop == "max_tokens"
    with pytest.raises(OCRResponseError):
        parse_tool_answer({"content": [{"type": "text", "text": "no"}]}, engine="c", pages=[1], field_confidence=0.6)


def _provider(handler) -> ClaudeVisionProvider:  # type: ignore[no-untyped-def]
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ClaudeVisionProvider(ClaudeVisionConfig(api_key="sk-test", model="claude-test"), client=client)


def test_claude_sends_pdfs_as_documents_and_returns_fields() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"stop_reason": "tool_use", "content": [{
            "type": "tool_use", "name": TOOL_NAME,
            "input": {"fields": {"gross_amount": {"value": "64.10", "page": 1}}}}]})

    pdf = PageImage(data=(FIXTURES / "edp-ft-558120.pdf").read_bytes(), mime_type="application/pdf", page_count=1)
    provider = ClaudeVisionProvider(ClaudeVisionConfig(api_key="sk", cost_per_page=Decimal("0.03")),
                                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                    redactor=vision_redactor(render_pdfs=False))
    result = asyncio.run(provider.recognize([pdf], OCRHints(page_count=1)))
    block = seen[0]["messages"][0]["content"][0]
    assert block["type"] == "document" and block["source"]["media_type"] == "application/pdf"
    assert result.fields[0].field is CriticalField.GROSS_AMOUNT and result.cost == Decimal("0.03")
    assert result.method is ExtractionMethod.VLM and "redaction:pdf_not_masked" in result.warnings


@pytest.mark.parametrize("status, error", [(529, OCRUnavailable), (500, OCRUnavailable)])
def test_claude_overload_is_retryable_not_fatal(status: int, error: type) -> None:
    provider = _provider(lambda request: httpx.Response(status, json={"error": {"type": "overloaded_error"}}))
    with pytest.raises(error):
        asyncio.run(provider.recognize([PageImage(data=PHOTO, mime_type="image/jpeg")]))


# --------------------------------------------------------------------------- the router


def _structured(evidence: str, *pairs: tuple[CriticalField, object, ExtractionMethod]) -> dict:
    out: dict = {}
    for f, value, method in pairs:
        out.setdefault(f, []).append(FieldObservation(value=value, source=evidence, method=method, confidence=0.95,
                                                      location="qr:x"))
    return out


def test_router_corroborates_a_single_source_locally_but_never_pays_for_it() -> None:
    engine = FakeOCRProvider(PP_OCR_V6_MEDIUM, texts=["Total: 23,90 €"])
    paid = FakeOCRProvider(COMMERCIAL, cost_per_page=Decimal("1"))
    registry = EngineRegistry([engine, paid])
    structured = _structured("ev", (CriticalField.GROSS_AMOUNT, Decimal("23.90"), ExtractionMethod.QR))
    request = RoutingRequest(tenant_id="t", evidence_id="ev", pages=[PHOTO], structured=structured,
                             required_fields={CriticalField.GROSS_AMOUNT})
    off = asyncio.run(OCRRouter(registry, lambda t, s, m: {}).route(request))
    assert not off.ocr_used  # default: Stage 0 settles, OCR skipped (unchanged behaviour)
    on = asyncio.run(OCRRouter(registry, lambda t, s, m: {}, config=RouterConfig(corroborate_single_source=True))
                     .route(request))
    primary = on.step(RouteStage.PRIMARY)
    assert primary is not None and primary.status is StepStatus.RAN and primary.has(ReasonCode.SINGLE_SOURCE)
    assert on.step(RouteStage.COMMERCIAL).status is StepStatus.SKIPPED and paid.calls == []


def test_router_uses_fields_an_engine_states_instead_of_parsing_its_text() -> None:
    from backoffice.ocr import OCRFieldReading

    class Stating(FakeOCRProvider):
        async def recognize(self, pages, hints=None):  # type: ignore[no-untyped-def]
            base = await super().recognize(pages, hints)
            return base.model_copy(update={"fields": (
                OCRFieldReading(field=CriticalField.GROSS_AMOUNT, value="23.90", page=1, printed="Total 23,90"),
                OCRFieldReading(field=CriticalField.ISSUE_DATE, value="about last week"))})

    engine = Stating(PP_OCR_V6_MEDIUM, method=ExtractionMethod.VLM, texts=["ignored text Total: 99,99 €"])
    outcome = asyncio.run(OCRRouter(EngineRegistry([engine]), lambda t, s, m: pytest.fail("text was parsed"))
                          .route(RoutingRequest(tenant_id="t", evidence_id="ev_9", pages=[PHOTO])))
    gross = outcome.fields[CriticalField.GROSS_AMOUNT].observations[0]
    assert gross.value == Decimal("23.90") and gross.source == f"ev_9@{PP_OCR_V6_MEDIUM}"
    assert gross.method is ExtractionMethod.VLM and gross.location == "vlm:page 1 · printed “Total 23,90”"
    assert outcome.fields[CriticalField.ISSUE_DATE].observations[0].value == "about last week"  # kept as dissent


def test_locate_turns_a_named_text_line_into_the_engine_box() -> None:
    lines = (OCRLine(text="NIF: 516722344", bbox=BoundingBox(page=1, x0=1, y0=2, x1=3, y1=4), confidence=0.4),
             OCRLine(text="Total: 23,90 €", bbox=BoundingBox(page=1, x0=5, y0=6, x1=7, y1=8), confidence=0.99))
    result = OCRResult(engine="e", model_version="1", method=ExtractionMethod.OCR,
                       pages=(OCRPage(number=1, lines=lines),))
    obs = FieldObservation(value=Decimal("23.90"), source="s", method=ExtractionMethod.OCR, confidence=0.5,
                           location="text:line 2")
    assert locate(obs, CriticalField.GROSS_AMOUNT, result).location == lines[1].bbox
    wrong_line = obs.model_copy(update={"location": "text:line 1"})
    assert locate(wrong_line, CriticalField.GROSS_AMOUNT, result).location == "text:line 1"
    nif = FieldObservation(value="516722344", source="s", method=ExtractionMethod.OCR, confidence=0.65,
                           location="text:line 1")
    boxed = locate(nif, CriticalField.SUPPLIER_TAX_ID, result)
    assert boxed.location == lines[0].bbox and boxed.confidence == 0.4  # the shaky line caps confidence


# --------------------------------------------------------------------------- the PP-OCRv6 sidecar over HTTP


class _PaddleX(BaseHTTPRequestHandler):
    """A PaddleX OCR-pipeline server: POST /ocr {file, fileType} -> ocrResults (the documented contract)."""

    received: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append({"path": self.path, **body})
        lines = [line for line in TRANSCRIPTION.splitlines() if line.strip()]
        result = {"errorCode": 0, "errorMsg": "Success", "logId": "x", "result": {
            "ocrResults": [{"prunedResult": {
                "rec_texts": lines,
                "rec_scores": [0.97] * len(lines),
                "rec_polys": [[[60, 60 + 50 * i], [700, 60 + 50 * i], [700, 95 + 50 * i], [60, 95 + 50 * i]]
                              for i in range(len(lines))],
            }}],
            "dataInfo": {"type": "image", "width": 900, "height": 1150}}}
        data = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        return None


@pytest.fixture
def paddlex() -> Iterator[str]:
    _PaddleX.received = []
    server = HTTPServer(("127.0.0.1", 0), _PaddleX)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_sidecar_configured_by_env_reads_an_uploaded_photo(paddlex: str) -> None:
    reader = reader_from_env({"BACKOFFICE_OCR_URL": paddlex})
    assert reader is not None and reader.engines() == (PP_OCR_V6_MEDIUM,) and not reader.external_ai
    svc = BackOfficeService.demo()
    svc.repo.reader = reader
    body = {"filename": "lunch.jpg", "contentType": "image/jpeg", "dataBase64": base64.b64encode(PHOTO).decode()}
    status, out = svc.dispatch("POST", "/api/evidence", json.dumps(body))
    assert status == 200 and len(out["documents"]) == 1
    [call] = _PaddleX.received
    assert call["path"] == "/ocr" and call["fileType"] == 1 and base64.b64decode(call["file"]) == PHOTO
    record = svc.repo.documents[out["documents"][0]["id"]]
    ev = out["evidenceIds"][0]
    gross = [o for o in record.checks["gross_amount"].observations if o.method is ExtractionMethod.OCR]
    assert gross and gross[0].source == f"{ev}@{PP_OCR_V6_MEDIUM}" and gross[0].value == Decimal("23.90")
    row = [line for line in TRANSCRIPTION.splitlines() if line.strip()].index("Total: 23,90 €")
    assert gross[0].location == BoundingBox(page=1, x0=60, y0=60 + 50 * row, x1=700, y1=95 + 50 * row)
    assert record.document.supplier_name == "Café Central da Baixa"


def test_sidecar_down_is_recorded_and_the_upload_is_kept() -> None:
    reader = reader_from_env({"BACKOFFICE_OCR_URL": "http://127.0.0.1:9", "BACKOFFICE_OCR_TIMEOUT": "2"})
    svc = BackOfficeService.demo()
    svc.repo.reader = reader
    body = {"filename": "lunch.jpg", "contentType": "image/jpeg", "dataBase64": base64.b64encode(PHOTO).decode()}
    status, out = svc.dispatch("POST", "/api/evidence", json.dumps(body))
    assert status == 200 and out["evidenceIds"]
    steps = {s.step: s for s in svc.repo.reads[out["evidenceIds"][0]].steps}
    assert steps["ocr_primary"].state is StepState.FAILED and "engine_unavailable" in steps["ocr_primary"].detail


# --------------------------------------------------------------------------- configuration and plumbing


def test_reader_from_env() -> None:
    assert reader_from_env({"BACKOFFICE_DOCUMENT_READING": "off"}) is None
    plain = reader_from_env({})
    assert plain is not None and plain.engines() == () and not plain.external_ai
    no_key = reader_from_env({"BACKOFFICE_EXTERNAL_AI": "on"})
    assert no_key is not None and not no_key.external_ai and no_key.engines() == ()
    on = reader_from_env({"BACKOFFICE_EXTERNAL_AI": "on", "ANTHROPIC_API_KEY": "sk-x",
                          "BACKOFFICE_VISION_COST_PER_PAGE": "0.05", "BACKOFFICE_OCR_MONTHLY_BUDGET": "2.50"})
    assert on is not None and on.external_ai and on.engines() == (COMMERCIAL,)
    provider = on._registry.find(COMMERCIAL)
    assert isinstance(provider, ClaudeVisionProvider) and provider.version == "claude-sonnet-5-5"
    assert provider.cost_per_page == Decimal("0.05") and on._budget.ceiling("any") == Decimal("2.50")
    with pytest.raises(ValueError):
        reader_from_env({"BACKOFFICE_EXTERNAL_AI": "on", "ANTHROPIC_API_KEY": "k",
                         "BACKOFFICE_OCR_MONTHLY_BUDGET": "-1"})


def test_run_sync_works_inside_a_running_event_loop() -> None:
    async def value() -> int:
        await asyncio.sleep(0)
        return 42

    async def handler() -> int:  # like a FastAPI async handler calling the synchronous service
        return run_sync(value)

    assert run_sync(value) == 42
    assert asyncio.run(handler()) == 42


_BROWSER = """
import base64, json, sys
BLOCKED = {"httpx", "fastapi", "starlette", "uvicorn", "cryptography", "pypdf", "PIL", "zxingcpp", "pyzbar",
           "pypdfium2", "anthropic", "psycopg", "temporalio", "boto3", "reportlab"}

class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"{name} is not available in the browser")
        return None

sys.meta_path.insert(0, Block())
import backoffice.reading
from backoffice.service import BackOfficeService

svc = BackOfficeService.demo()
body = {"filename": "scan.pdf", "contentType": "application/pdf", "dataBase64": base64.b64encode(sys.argv[1].encode()).decode()}
status, out = svc.dispatch("POST", "/api/evidence", json.dumps(body))
assert "backoffice.ocr" not in sys.modules, "the OCR package was imported"
print(status, out["message"], out["storedOnly"])
"""


def test_the_browser_engine_boots_and_stores_uploads_with_only_pydantic() -> None:
    """The Pyodide bundle has only pydantic: nothing optional may be needed to boot or to take an upload."""
    done = subprocess.run([sys.executable, "-c", _BROWSER, "%PDF-1.7\n%%EOF\n"],
                          env={"PYTHONPATH": str(BACKEND_SRC), "PATH": ""}, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == "200 Got it. I stored it. Reading photos and PDFs is switched off in this demo. True"
