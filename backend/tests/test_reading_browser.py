"""The static demo reads photos and scanned PDFs in the visitor's browser (§13-19, backoffice.reading.browser).

The page reads the file (tesseract.js, a JavaScript QR decoder, pdf.js) and sends what it read with the
upload as a device reading. These tests drive the real ``POST /api/evidence`` of the demo with such readings,
in the wire format web/lib/ocr.ts sends, and check that the engine treats them exactly like any local OCR:
Stage 0 first, the reading as one source labelled with its engine, GREEN only with the fiscal QR code, the
retake task for a blurred photo, and never on the production server, which reads every file itself.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

from backoffice.domain.lifecycle import Stage
from backoffice.domain.models import BoundingBox, ExtractionMethod, Quality
from backoffice.ocr.rows import TextBox, join_rows, normalize_text
from backoffice.reading import BrowserReader, DeviceReading, DeviceReadingError, DocumentReader, StepState
from backoffice.reading.browser import BROWSER_OCR, BROWSER_QR
from backoffice.service import BackOfficeService

FIXTURES = Path(__file__).parent / "fixtures"
DOCUMENTS = FIXTURES / "documents"
PHOTOS = FIXTURES / "photos"
CENTRAL = (DOCUMENTS / "central-fs-cc2026-3317.txt").read_text(encoding="utf-8").splitlines()
CENTRAL_QR = ("A:516722344*B:516123459*C:PT*D:FS*E:N*F:20260929*G:FS CC2026/3317*H:CCQ7M2KP-3317*I1:PT*I5:21.15"
              "*I6:2.75*N:2.75*O:23.90*Q:Kx2e*R:0987")
BOLHAO = (PHOTOS / "fs-pb2026-0441.txt").read_text(encoding="utf-8").splitlines()
BOLHAO_QR = ("A:509882412*B:516123459*C:PT*D:FS*E:N*F:20260926*G:FS PB2026/441*H:KPB4M8XT-441*I1:PT*I5:7.43"
             "*I6:0.97*N:0.97*O:8.40*Q:Hq3z*R:1187")


def lines(texts: list[str], *, confidence: float = 0.93) -> list[dict]:
    """Lines as tesseract.js gives them: text, a 0-1 score and a pixel box, one 50-pixel row each."""
    return [{"text": t, "confidence": confidence, "box": [60, 60 + 50 * i, 60 + 18 * max(1, len(t)), 95 + 50 * i]}
            for i, t in enumerate(texts) if t.strip()]


def reading(texts: list[str] | None = None, *, qr: tuple[str, ...] = (), sharpness: float | None = 0.9,
            pages: list[list[str]] | None = None, **extra: object) -> dict:
    pages = pages if pages is not None else ([texts] if texts is not None else [])
    return {"method": "ocr_browser", "engine": "tesseract.js", "version": "7.0.0 por+eng",
            "pages": [{"number": n, "width": 900, "height": 1150, "lines": lines(p)} for n, p in enumerate(pages, 1)],
            "qr": list(qr), "sharpness": sharpness, "ms": 1800, **extra}


def demo(reader: object = "browser") -> BackOfficeService:
    svc = BackOfficeService.demo()
    svc.repo.reader = BrowserReader() if reader == "browser" else reader
    return svc


def upload(svc: BackOfficeService, path: Path, read: dict | None, content_type: str = "image/jpeg") -> dict:
    body = {"filename": path.name, "contentType": content_type,
            "dataBase64": base64.b64encode(path.read_bytes()).decode()}
    if read is not None:
        body["reading"] = read
    status, out = svc.dispatch("POST", "/api/evidence", json.dumps(body))
    assert status == 200, out
    return out


def only_document(svc: BackOfficeService, out: dict):  # type: ignore[no-untyped-def]
    assert len(out["documents"]) == 1, out
    return svc.repo.documents[out["documents"][0]["id"]]


# --------------------------------------------------------------------------- photos


def test_browser_reading_and_the_photos_qr_code_make_the_receipt_verified() -> None:
    svc = demo()
    out = upload(svc, DOCUMENTS / "central-fs-cc2026-3317.jpg", reading(CENTRAL, qr=(CENTRAL_QR,)))
    record = only_document(svc, out)
    ev = out["evidenceIds"][0]
    assert record.document.quality is Quality.GREEN
    assert record.document.supplier_name == "Café Central da Baixa"
    for field, value in (("gross_amount", "23.90"), ("supplier_tax_id", "516722344"),
                         ("invoice_number", "FS CC2026/3317"), ("issue_date", "2026-09-29")):
        check = record.checks[field]
        assert str(check.value) == value
        [ocr] = [o for o in check.observations if o.method is ExtractionMethod.OCR]
        assert ocr.source == f"{ev}@{BROWSER_OCR}" and isinstance(ocr.location, BoundingBox)  # §18 provenance
        assert any(o.method is ExtractionMethod.QR for o in check.observations)
    steps = {s.step: s for s in svc.repo.reads[ev].steps}
    assert steps["image_qr"].state is StepState.DONE and steps["image_qr"].engine == BROWSER_QR
    primary = steps["ocr_primary"]
    assert primary.state is StepState.DONE and primary.engine == BROWSER_OCR
    assert "external_ai" not in steps and steps["ocr_commercial"].state is not StepState.DONE


def test_browser_reading_alone_is_likely_never_verified() -> None:
    svc = demo()
    out = upload(svc, PHOTOS / "fs-pb2026-0441.jpg", reading(BOLHAO))
    record = only_document(svc, out)
    assert record.document.gross_amount is not None and str(record.document.gross_amount) == "8.40"
    assert record.document.quality is Quality.AMBER  # one reading of pixels (§57)
    assert svc.repo.items[record.item_id].stage is not Stage.CLOSED


def test_browser_reading_of_the_cafe_receipt_with_its_qr_closes_nothing_without_the_bank() -> None:
    svc = demo()
    out = upload(svc, PHOTOS / "fs-pb2026-0441.jpg", reading(BOLHAO, qr=(BOLHAO_QR,)))
    record = only_document(svc, out)
    assert record.document.quality is Quality.GREEN and not record.matched_tx_ids
    assert not svc.repo.items[record.item_id].is_done  # verified, but no payment yet: not closed (§3)


def test_badly_blurred_photo_read_in_the_browser_becomes_the_retake_task() -> None:
    svc = demo()
    out = upload(svc, PHOTOS / "fs-mr2026-0088-blurred.jpg", reading([], sharpness=0.18))
    assert out["documents"] == [] and out["storedOnly"] is True
    assert out["message"] == ("Got it, but I can't read it. This photo of a receipt is too blurred to read. "
                              "Take it again?")
    read = svc.repo.reads[out["evidenceIds"][0]]
    assert read.image_quality == ("blurry",) and read.needs_person
    assert any(n.kind == "retake" for n in svc.repo.needs.values())


def test_a_photo_the_browser_could_not_read_is_stored_and_says_so() -> None:
    svc = demo()
    out = upload(svc, PHOTOS / "fs-pb2026-0441.jpg", None)
    assert out["storedOnly"] is True and out["documents"] == []
    assert out["message"] == "Got it. I stored it, but this browser couldn't read it. Try sending it again."
    bad = dict(reading(BOLHAO), method="ocr")  # not the wire format: ignored as a whole, never half used
    out = upload(svc, PHOTOS / "fs-mr2026-0088.jpg", bad)
    assert out["message"] == "Got it. I stored it, but this browser couldn't read it. Try sending it again."


def test_the_server_never_takes_a_reading_sent_by_a_client() -> None:
    forged = reading(["Café Central da Baixa", "NIF: 516722344", "Total: 999,00 €"], qr=(CENTRAL_QR,))
    svc = demo(DocumentReader(qr_decoder=None))  # the server's reader, no OCR engine configured
    out = upload(svc, DOCUMENTS / "central-fs-cc2026-3317.jpg", forged)
    assert out["storedOnly"] is True and out["documents"] == []
    assert out["message"] == "Got it. I stored it. Reading photos is not set up here yet."
    svc = demo(None)  # no reader at all
    out = upload(svc, DOCUMENTS / "central-fs-cc2026-3317.jpg", forged)
    assert out["message"] == "Got it. I stored it. Reading photos and PDFs is switched off in this demo."


def test_a_reload_replays_the_reading_to_the_same_result() -> None:
    """The demo journals the upload with its reading and replays it after a reload: same document, no OCR."""
    first, second = demo(), demo()
    body = reading(CENTRAL, qr=(CENTRAL_QR,))
    a = upload(first, DOCUMENTS / "central-fs-cc2026-3317.jpg", body)
    b = upload(second, DOCUMENTS / "central-fs-cc2026-3317.jpg", json.loads(json.dumps(body)))
    assert a == b
    assert first.dispatch("GET", "/api/documents", None) == second.dispatch("GET", "/api/documents", None)


def test_the_browser_engine_runs_without_an_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "emscripten")  # Pyodide: the chain is stepped, never awaited on a loop
    svc = demo()
    out = upload(svc, DOCUMENTS / "central-fs-cc2026-3317.jpg", reading(CENTRAL, qr=(CENTRAL_QR,)))
    assert only_document(svc, out).document.quality is Quality.GREEN


# --------------------------------------------------------------------------- PDFs


def test_pdf_text_layer_read_in_the_browser_is_stage0_text() -> None:
    pypdf = pytest.importorskip("pypdf")  # only to produce what pdf.js would give the page
    import io

    data = (DOCUMENTS / "edp-ft-558120.pdf").read_bytes()
    pdf = pypdf.PdfReader(io.BytesIO(data))
    layer = [page.extract_text() for page in pdf.pages]
    svc = demo()
    out = upload(svc, DOCUMENTS / "edp-ft-558120.pdf", reading(pages=[], textLayer=layer,
                                                              metadata={"Producer": "ReportLab PDF Library"}),
                 content_type="application/pdf")
    record = only_document(svc, out)
    assert out["message"] == "Got it. It matches the €64.10 payment to EDP on 19 September."
    assert record.document.quality is Quality.GREEN and svc.repo.items[record.item_id].stage is Stage.CLOSED
    assert {ExtractionMethod.EMBEDDED_TEXT, ExtractionMethod.QR} <= {
        o.method for o in record.checks["gross_amount"].observations}
    steps = {s.step: s for s in svc.repo.reads[out["evidenceIds"][0]].steps}
    assert steps["pdf_text"].state is StepState.DONE and "read in the browser" in steps["pdf_text"].detail
    assert "ocr_primary" not in steps  # its own text and QR settle it: nothing is read from pixels


def test_qr_payload_in_pdf_metadata_read_in_the_browser_counts() -> None:
    pypdf = pytest.importorskip("pypdf")
    import io

    data = (DOCUMENTS / "moderna-ft-pm2026-88.pdf").read_bytes()
    pdf = pypdf.PdfReader(io.BytesIO(data))
    meta = {k.lstrip("/"): str(v) for k, v in dict(pdf.metadata).items()}
    svc = demo()
    out = upload(svc, DOCUMENTS / "moderna-ft-pm2026-88.pdf",
                 reading(pages=[], textLayer=[p.extract_text() for p in pdf.pages], metadata=meta),
                 content_type="application/pdf")
    assert only_document(svc, out).document.quality is Quality.GREEN


def test_scanned_pdf_read_in_the_browser_is_read_like_a_photo() -> None:
    svc = demo()
    out = upload(svc, DOCUMENTS / "central-fs-cc2026-3317-scan.pdf",
                 reading(CENTRAL, qr=(CENTRAL_QR,), textLayer=[""]), content_type="application/pdf")
    record = only_document(svc, out)
    assert record.document.quality is Quality.GREEN
    steps = {s.step: s for s in svc.repo.reads[out["evidenceIds"][0]].steps}
    assert steps["pdf_text"].state is StepState.NOTHING
    assert steps["pdf_qr_image"].state is StepState.DONE and steps["pdf_qr_image"].engine == BROWSER_QR
    assert steps["ocr_primary"].engine == BROWSER_OCR


# --------------------------------------------------------------------------- the wire format and line joining


def test_device_reading_wire_format_is_strict() -> None:
    ok = DeviceReading.from_json(reading(["Total: 8,40 EUR"], qr=(BOLHAO_QR,)))
    assert ok.engine == "tesseract.js" and ok.qr == (BOLHAO_QR,) and ok.pages[0].lines[0].text == "Total: 8,40 EUR"
    for bad in ({}, {"method": "ocr"}, dict(reading(["x"]), pages="no"), dict(reading(["x"]), sharpness=2),
                dict(reading(["x"]), qr=["a"] * 20), dict(reading(pages=[]), qr=[]),
                dict(reading(["x"]), pages=[{"number": 2, "lines": []}])):
        with pytest.raises(DeviceReadingError):
            DeviceReading.from_json(bad)
    odd = reading(["ok"])
    odd["pages"][0]["lines"].append({"text": "no box"})
    odd["pages"][0]["lines"].append({"text": "bad box", "box": [5, 5, 1, 1]})
    assert [ln.text for ln in DeviceReading.from_json(odd).pages[0].lines] == ["ok"]  # unusable lines are dropped


def test_join_rows_joins_the_boxes_of_a_tilted_line_and_keeps_lines_apart() -> None:
    tilt = 0.07  # about 4 degrees: the right-hand box sits 37 pixels lower
    boxes = [
        TextBox("Total:", 0.98, [[150, 800], [330, 800 + 180 * tilt], [330, 840 + 180 * tilt], [150, 840]]),
        TextBox("45,60 €", 0.97, [[680, 838], [880, 838 + 200 * tilt], [880, 878 + 200 * tilt], [680, 878]]),
        TextBox("IVA 13% incluído: 5,25", 0.96, [[150, 860], [700, 860 + 550 * tilt], [700, 900 + 550 * tilt],
                                                  [150, 900]]),
        TextBox("ATCUD：KPB4M8XT-441", 0.99, [[150, 700], [600, 700 + 450 * tilt], [600, 740 + 450 * tilt],
                                                   [150, 740]]),
    ]
    rows = join_rows(boxes)
    assert [r.text for r in rows] == ["ATCUD:KPB4M8XT-441", "Total: 45,60 €", "IVA 13% incluído: 5,25"]
    total = rows[1]
    assert total.parts == 2 and total.confidence == 0.97 and total.box == (150, 800, 880, 878 + 200 * tilt)
    assert normalize_text("  n.º　FS  1 ") == "n.º FS 1"  # "º" stays: only full-width ASCII is folded


def test_the_browser_engine_needs_none_of_the_server_readers(tmp_path: Path) -> None:
    """Pyodide has pydantic only: the demo engine, its BrowserReader and the whole reading chain import and run
    without httpx, numpy, Pillow, OpenCV, onnxruntime, RapidOCR, pypdf, pypdfium2 or zxing-cpp; and the browser
    bundle (backend/scripts/bundle_engine.py) carries no model or native file."""
    import subprocess
    import zipfile

    script = f"""
import builtins, sys
sys.path.insert(0, {str(Path(__file__).parents[1] / "src")!r})
blocked = {{"httpx", "numpy", "PIL", "cv2", "onnxruntime", "rapidocr", "pypdf", "pypdfium2", "zxingcpp", "pyzbar"}}
real = builtins.__import__
def guard(name, *args, **kwargs):
    if name.split(".")[0] in blocked:
        raise ImportError("not in the browser: " + name)
    return real(name, *args, **kwargs)
builtins.__import__ = guard
import base64, json
from backoffice.reading import BrowserReader
from backoffice.service import BackOfficeService
svc = BackOfficeService.demo()
svc.repo.reader = BrowserReader()
body = open({str(tmp_path / "body.json")!r}, encoding="utf-8").read()
status, out = svc.dispatch("POST", "/api/evidence", body)
print(status, svc.repo.documents[out["documents"][0]["id"]].document.quality.value)
"""
    (tmp_path / "body.json").write_text(json.dumps({
        "filename": "r.jpg", "contentType": "image/jpeg",
        "dataBase64": base64.b64encode((DOCUMENTS / "central-fs-cc2026-3317.jpg").read_bytes()).decode(),
        "reading": reading(CENTRAL, qr=(CENTRAL_QR,))}), encoding="utf-8")
    (tmp_path / "browser.py").write_text(script, encoding="utf-8")
    done = subprocess.run([sys.executable, "-I", str(tmp_path / "browser.py")], capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.split() == ["200", "verified"]

    from importlib import util

    spec = util.spec_from_file_location("bundle_engine", Path(__file__).parents[1] / "scripts" / "bundle_engine.py")
    assert spec is not None and spec.loader is not None
    bundle = util.module_from_spec(spec)
    spec.loader.exec_module(bundle)
    out = tmp_path / "backoffice.zip"
    bundle.bundle(out)
    names = zipfile.ZipFile(out).namelist()
    assert "backoffice/reading/browser.py" in names and "backoffice/ocr/providers/rapid.py" in names  # code only
    assert not [n for n in names if not n.endswith((".py", ".json", ".txt", ".eml", ".xml", ".csv", ".typed"))]
    assert not [n for n in names if n.split("/")[1] in ("rapidocr", "onnxruntime")]
    assert out.stat().st_size < 3 * 1024 * 1024
