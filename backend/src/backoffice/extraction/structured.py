"""Stage 0: structured evidence first, before any OCR (§13, §17, §18).

Structured evidence outranks OCR. Each extractor turns one kind of
structured content into :class:`Stage0Result` observations whose method
(and therefore rank, via the domain ``METHOD_RANK``) says how it was read:

* UBL 2.1 Invoice / CreditNote and UN/CEFACT CII (Factur-X, ZUGFeRD):
  :func:`parse_einvoice`, method STRUCTURED_XML;
* schema.org Invoice / Order in JSON-LD or microdata:
  :func:`extract_html_structured`, method HTML_STRUCTURED;
* JSON API payloads: :class:`JsonFieldMapper`, method API;
* PDFs: :func:`extract_from_pdf`, embedded e-invoice XML (STRUCTURED_XML) and
  the text layer (EMBEDDED_TEXT, or OCR when a scanner made it);
* QR payloads: :class:`QRHook`, method QR.

:func:`extract_structured` runs whatever applies to one piece of evidence.
When its combined results cover every required field without conflict,
OCR is skipped entirely (see ``backoffice.ocr.router``).
"""

from __future__ import annotations

from collections.abc import Iterable

from ._optional import MissingDependencyError
from .einvoice import StructuredFormatError, parse_einvoice
from .fields import FieldExtractor, Stage0Result, StructuredDataError, combine
from .htmldata import extract_html_structured
from .jsonmap import FieldPath, JsonFieldMapper
from .media import MIME_HTML, MIME_JSON, MIME_PDF, MIME_XML, sniff_mime, sniff_text_format
from .pdf import PdfContent, extract_from_pdf, read_pdf
from .qr import DecodedCode, QRHook, ZXingDecoder, fiscal_qr_handler, parse_epc_qr
from .safexml import UnsafeXMLError, XMLSyntaxError, parse_xml

__all__ = [
    "DecodedCode",
    "FieldPath",
    "JsonFieldMapper",
    "PdfContent",
    "QRHook",
    "Stage0Result",
    "StructuredDataError",
    "StructuredFormatError",
    "UnsafeXMLError",
    "XMLSyntaxError",
    "ZXingDecoder",
    "combine",
    "extract_from_pdf",
    "extract_html_structured",
    "extract_structured",
    "fiscal_qr_handler",
    "parse_einvoice",
    "parse_epc_qr",
    "parse_xml",
    "read_pdf",
]


def extract_structured(
    data: bytes,
    *,
    source: str,
    mime_type: str | None = None,
    text_extractor: FieldExtractor | None = None,
    json_mapper: JsonFieldMapper | None = None,
    qr_payloads: Iterable[str] = (),
    qr_hook: QRHook | None = None,
) -> tuple[Stage0Result, ...]:
    """Every Stage 0 result for one piece of evidence; empty when none applies.

    The content decides the format (``mime_type`` is only a fallback).
    Hostile XML raises :class:`UnsafeXMLError`, a security signal the caller
    must see. Anything else that cannot be read here (an encrypted or
    damaged PDF, a missing optional reader, broken or oversized JSON/HTML)
    yields a noted, empty result so the evidence simply continues to OCR.
    """
    kind = sniff_mime(data) or sniff_text_format(data) or mime_type
    results: list[Stage0Result] = []
    if kind == MIME_PDF:
        try:
            results += extract_from_pdf(data, source=source, text_extractor=text_extractor)
        except MissingDependencyError:
            results.append(Stage0Result.build("pdf", source, (), notes=["pdf_reader_unavailable"]))
        except StructuredDataError as exc:
            results.append(Stage0Result.build("pdf", source, (), notes=[exc.code]))
    elif kind == MIME_XML:
        try:
            results.append(parse_einvoice(data, source=source))
        except (StructuredFormatError, XMLSyntaxError) as exc:
            results.append(Stage0Result.build("xml", source, (), notes=[exc.code]))
    elif kind == MIME_HTML:
        try:
            results += extract_html_structured(data, source=source)
        except StructuredDataError as exc:
            results.append(Stage0Result.build("html", source, (), notes=[exc.code]))
    elif kind == MIME_JSON and json_mapper is not None:
        try:
            results.append(json_mapper.extract(data, source=source))
        except StructuredDataError as exc:
            results.append(Stage0Result.build("json_api", source, (), notes=[exc.code]))
    if qr_payloads:
        results += (qr_hook or QRHook()).extract(qr_payloads, source=source)
    return tuple(results)
