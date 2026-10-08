"""Stage 0 for uploaded PDFs and photos: structured data before any OCR (§13, §19).

* **PDF text layer** with the optional ``pypdf`` (``backoffice.extraction.read_pdf``).
  A born-digital PDF already carries its text; a layer added by a scanner's
  OCR tool is labelled OCR, not embedded text, so ranking stays honest.
* **Portuguese invoice QR payloads** (``A:<NIF>*B:...``) found in the PDF's
  text or metadata, and decoded from photos when a QR decoder is installed
  (``zxing-cpp`` or ``pyzbar``, each with ``Pillow``). A scanned PDF with no
  payload in its text or metadata has its first pages rendered (optional
  ``pypdfium2``) and the QR code image decoded the same way.
* **Embedded e-invoice XML** (Factur-X / ZUGFeRD / UBL attached to a PDF).

Nothing here interprets fields: the caller's country pack reads the text and
the QR payloads. Every step reports what it did, including "not available"
when its optional package is missing, so a missing reader is visible instead
of silently reading nothing.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from backoffice.domain.models import ExtractionMethod
from backoffice.extraction._optional import MissingDependencyError, import_optional
from backoffice.extraction.fields import StructuredDataError
from backoffice.extraction.pdf import read_pdf

__all__ = [
    "QRDecoder",
    "PyzbarQRDecoder",
    "ReadStep",
    "Stage0",
    "StepState",
    "ZXingQRDecoder",
    "find_qr_decoder",
    "fiscal_qr_payloads",
    "read_image_stage0",
    "read_pdf_stage0",
]


class StepState(str, Enum):
    DONE = "done"  # ran and found something
    NOTHING = "nothing_found"  # ran, found nothing
    NOT_AVAILABLE = "not_available"  # its package or engine is not installed / configured
    OFF = "switched_off"  # deliberately disabled (e.g. external AI)
    SKIPPED = "skipped"  # not needed
    FAILED = "failed"  # ran and failed (unreadable file, engine error)
    NEEDS_PERSON = "needs_a_person"  # every engine tried and fields are still missing or disagree (§17 human stage)


@dataclass(frozen=True)
class ReadStep:
    """One step of reading a file, for the audit log and the owner's "why" (§54-55)."""

    step: str
    state: StepState
    detail: str = ""
    engine: str | None = None
    cost: Decimal = Decimal("0")

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"step": self.step, "state": self.state.value}
        if self.detail:
            out["detail"] = self.detail
        if self.engine:
            out["engine"] = self.engine
        if self.cost:
            out["cost"] = str(self.cost)
        return out


@dataclass(frozen=True)
class Stage0:
    """What a file says about itself before any OCR."""

    text: str = ""  # text layer, plus QR payload lines found outside it
    method: ExtractionMethod = ExtractionMethod.EMBEDDED_TEXT  # how ``text`` was produced
    qr_payloads: tuple[str, ...] = ()
    embedded_xml: tuple[bytes, ...] = ()
    page_count: int | None = None
    steps: tuple[ReadStep, ...] = field(default=())


# --------------------------------------------------------------------------- QR payloads


def fiscal_qr_payloads(text: str) -> list[str]:
    """Fiscal invoice QR payloads in ``text`` (one per line; a payload never spans lines), as the country packs of
    the countries a company can run in recognise them (§49: Portugal's AT code, Spain's Verifactu and TicketBAI
    codes). The core names no country: it asks each pack (backoffice.countries registry)."""
    from backoffice.countries import company_countries, company_pack

    packs = [company_pack(country) for country in company_countries()]
    found: list[str] = []
    for line in (text or "").splitlines():
        for pack in packs:
            payload = pack.find_fiscal_qr(line)
            if payload:
                if payload not in found:
                    found.append(payload)
                break
    return found


@runtime_checkable
class QRDecoder(Protocol):
    """Turns an image into the strings of the QR codes on it."""

    name: str

    def decode(self, image: bytes) -> tuple[str, ...]: ...


class ZXingQRDecoder:
    """QR codes with ``zxing-cpp`` and ``Pillow`` (``backoffice.extraction.ZXingDecoder``)."""

    name = "zxing-cpp"

    def decode(self, image: bytes) -> tuple[str, ...]:
        from backoffice.extraction.qr import ZXingDecoder

        return tuple(code.text for code in ZXingDecoder(qr_only=True).decode(image))


class PyzbarQRDecoder:
    """QR codes with ``pyzbar`` (the zbar library) and ``Pillow``."""

    name = "pyzbar"

    def decode(self, image: bytes) -> tuple[str, ...]:
        pyzbar = import_optional("pyzbar.pyzbar", feature="Reading QR codes from images", package="pyzbar")
        pil_image = import_optional("PIL.Image", feature="Reading QR codes from images", package="Pillow")
        with pil_image.open(io.BytesIO(image)) as img:
            found = pyzbar.decode(img)
        texts = []
        for item in found:
            if str(getattr(item, "type", "")).upper() != "QRCODE":
                continue
            data = getattr(item, "data", b"")
            try:
                texts.append(data.decode("utf-8"))
            except (AttributeError, UnicodeDecodeError):
                continue
        return tuple(texts)


def find_qr_decoder() -> QRDecoder | None:
    """The first installed QR decoder (zxing-cpp, then pyzbar), or None."""
    for module, decoder in (("zxingcpp", ZXingQRDecoder), ("pyzbar.pyzbar", PyzbarQRDecoder)):
        try:
            import_optional(module, feature="Reading QR codes from images")
            import_optional("PIL.Image", feature="Reading QR codes from images", package="Pillow")
        except (MissingDependencyError, OSError):  # pyzbar raises OSError when libzbar is missing
            continue
        return decoder()
    return None


# --------------------------------------------------------------------------- PDFs


QR_RENDER_PAGES = 2  # a fiscal QR code sits on the first page (or the last of a two-page invoice)
QR_RENDER_DPI = 200


def read_pdf_stage0(data: bytes, decoder: QRDecoder | None = None, *, content: Any = None) -> Stage0:
    """Text layer, fiscal QR payloads in text or metadata, and embedded e-invoice XML of a PDF.

    When neither the text nor the metadata carries a QR payload (a scan) and
    a QR ``decoder`` is given, the first pages are rendered (``pypdfium2``)
    and the QR code image is decoded. ``content`` is the PDF's text layer and
    metadata when something else already read them
    (:class:`~backoffice.extraction.pdf.PdfContent`: pdf.js in the browser demo).
    """
    if content is None:
        try:
            content = read_pdf(data)
        except MissingDependencyError:
            return Stage0(steps=(ReadStep("pdf_text", StepState.NOT_AVAILABLE, "pypdf is not installed"),))
        except StructuredDataError as exc:
            return Stage0(steps=(ReadStep("pdf_text", StepState.FAILED, exc.code),))
    steps: list[ReadStep] = []
    pages = len(content.page_texts)
    text = content.text if content.has_text_layer else ""
    method = ExtractionMethod.OCR if content.text_layer_from_ocr else ExtractionMethod.EMBEDDED_TEXT
    if text:
        made_by = "made by a scanner's OCR" if method is ExtractionMethod.OCR else "born-digital"
        steps.append(ReadStep("pdf_text", StepState.DONE, f"{pages} page(s), text layer {made_by}"))
    else:
        steps.append(ReadStep("pdf_text", StepState.NOTHING, f"{pages} page(s), no text layer (a scan)"))

    in_text = fiscal_qr_payloads(text)
    in_metadata = list(dict.fromkeys(p for value in content.metadata.values() for p in fiscal_qr_payloads(str(value))
                                     if p not in in_text))
    payloads = [*in_text, *in_metadata]
    if payloads:
        where = " and ".join(w for w, found in (("text", in_text), ("metadata", in_metadata)) if found)
        steps.append(ReadStep("pdf_qr", StepState.DONE, f"{len(payloads)} invoice QR payload(s) in the {where}"))
    else:
        steps.append(ReadStep("pdf_qr", StepState.NOTHING, "no invoice QR payload in the text or metadata"))
        in_image, step = _qr_from_pages(data, decoder)
        steps.append(step)
        in_metadata += in_image  # joins the text below, like a payload found in the metadata
        payloads += in_image

    xml = tuple(blob for name, blob in sorted(content.attachments.items()) if name.lower().endswith(".xml"))
    if xml:
        steps.append(ReadStep("pdf_xml", StepState.DONE, f"{len(xml)} embedded XML file(s)"))
    if in_metadata:
        text = "\n".join([text, *in_metadata]).strip()
    return Stage0(text=text, method=method, qr_payloads=tuple(payloads), embedded_xml=xml,
                  page_count=pages or None, steps=tuple(steps))


def _qr_from_pages(data: bytes, decoder: QRDecoder | None) -> tuple[list[str], ReadStep]:
    """Fiscal QR payloads decoded from the rendered first pages of a PDF."""
    from backoffice.extraction.pdf import render_pdf_pages

    if decoder is None:
        return [], ReadStep("pdf_qr_image", StepState.NOT_AVAILABLE, "no QR decoder is installed (zxing-cpp or pyzbar)")
    if getattr(decoder, "reads_whole_files", False):
        pages: Sequence[tuple[bytes, int, int]] = ((data, 0, 0),)  # it already knows the codes on the file's pages
    else:
        try:
            pages = render_pdf_pages(data, max_pages=QR_RENDER_PAGES, dpi=QR_RENDER_DPI)
        except MissingDependencyError:
            return [], ReadStep("pdf_qr_image", StepState.NOT_AVAILABLE,
                                "no PDF page renderer is installed (pypdfium2)")
        except StructuredDataError as exc:
            return [], ReadStep("pdf_qr_image", StepState.FAILED, exc.code)
    found: list[str] = []
    for png, _, _ in pages:
        try:
            decoded = decoder.decode(png)
        except Exception:  # one unreadable page must not stop the others
            continue
        found += [p for p in _fiscal_only(decoded) if p not in found]
    if not found:
        return [], ReadStep("pdf_qr_image", StepState.NOTHING, "no invoice QR code on the page images",
                            engine=decoder.name)
    return found, ReadStep("pdf_qr_image", StepState.DONE, f"{len(found)} invoice QR code(s) on the page images",
                           engine=decoder.name)


# --------------------------------------------------------------------------- photos


def read_image_stage0(data: bytes, decoder: QRDecoder | None) -> Stage0:
    """Fiscal QR payloads decoded from a photo or screenshot, when a decoder is installed."""
    if decoder is None:
        step = ReadStep("image_qr", StepState.NOT_AVAILABLE, "no QR decoder is installed (zxing-cpp or pyzbar)")
        return Stage0(page_count=1, steps=(step,))
    try:
        decoded = decoder.decode(data)
    except MissingDependencyError:
        return Stage0(page_count=1, steps=(ReadStep("image_qr", StepState.NOT_AVAILABLE, decoder.name),))
    except Exception as exc:  # a damaged image must not stop the reading; OCR may still work
        return Stage0(page_count=1, steps=(ReadStep("image_qr", StepState.FAILED, type(exc).__name__,
                                                    engine=decoder.name),))
    payloads = _fiscal_only(decoded)
    if not payloads:
        step = ReadStep("image_qr", StepState.NOTHING,
                        f"{len(decoded)} QR code(s), none an invoice QR" if decoded else "no QR code found",
                        engine=decoder.name)
        return Stage0(page_count=1, steps=(step,))
    step = ReadStep("image_qr", StepState.DONE, f"{len(payloads)} invoice QR payload(s)", engine=decoder.name)
    return Stage0(text="\n".join(payloads), method=ExtractionMethod.QR, qr_payloads=tuple(payloads), page_count=1,
                  steps=(step,))


def _fiscal_only(decoded: Sequence[str]) -> list[str]:
    out: list[str] = []
    for text in decoded:
        for payload in fiscal_qr_payloads(text):
            if payload not in out:
                out.append(payload)
    return out
