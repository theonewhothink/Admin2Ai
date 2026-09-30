"""Embedded PDF content as Stage 0 evidence (§13).

A born-digital PDF already contains its text, and hybrid e-invoices
(Factur-X / ZUGFeRD) embed a CII XML file. Both outrank OCR. Reading uses
the optional ``pypdf`` package, imported only when a PDF is actually read.

A text layer produced by a scanner's OCR is not "embedded text" in the
§13 sense: when the PDF metadata names a known OCR tool, text observations
are downgraded to method OCR and noted, so ranking stays honest. Either
way the observations are labelled here, with this evidence as source,
whatever the text extractor claimed: text never poses as XML or QR.
"""

from __future__ import annotations

import io
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from backoffice.domain.models import ExtractionMethod, FieldObservation

from ._optional import import_optional
from .einvoice import parse_einvoice
from .fields import FieldExtractor, Stage0Result, StructuredDataError

__all__ = [
    "MIN_TEXT_CHARS",
    "PdfContent",
    "extract_from_pdf",
    "read_pdf",
    "render_pdf_pages",
]

# Fewer non-blank characters than this per document means "no text layer".
MIN_TEXT_CHARS = 20

# Producer/creator names of tools that add an OCR text layer to scans.
_OCR_TOOLS = re.compile(
    r"ocrmypdf|tesseract|abbyy|finereader|paper capture|readiris|omnipage", re.IGNORECASE
)


class EncryptedPDFError(StructuredDataError):
    """The PDF is encrypted and cannot be opened without a password."""


@dataclass(frozen=True)
class PdfContent:
    """What a PDF carries besides pixels."""

    page_texts: tuple[str, ...]
    attachments: Mapping[str, bytes] = field(default_factory=lambda: MappingProxyType({}))
    metadata: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def text(self) -> str:
        return "\n\n".join(self.page_texts)

    @property
    def has_text_layer(self) -> bool:
        return sum(len("".join(t.split())) for t in self.page_texts) >= MIN_TEXT_CHARS

    @property
    def text_layer_from_ocr(self) -> bool:
        producer = " ".join(self.metadata.get(k, "") for k in ("Producer", "Creator"))
        return bool(_OCR_TOOLS.search(producer))


def read_pdf(data: bytes, *, max_pages: int | None = None) -> PdfContent:
    """Text per page, embedded files and metadata (needs ``pypdf``)."""
    pypdf = import_optional("pypdf", feature="Reading PDF text")
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False) and not reader.decrypt(""):
            raise EncryptedPDFError("pdf_encrypted")
        pages = list(reader.pages)[:max_pages]
        texts = tuple((page.extract_text() or "") for page in pages)
        attachments = _attachments(reader)
        metadata = _metadata(getattr(reader, "metadata", None))
    except StructuredDataError:
        raise
    except Exception as exc:  # pypdf raises many types for damaged files
        raise StructuredDataError("pdf_unreadable", type(exc).__name__) from None
    return PdfContent(texts, MappingProxyType(attachments), MappingProxyType(metadata))


def render_pdf_pages(data: bytes, *, max_pages: int, dpi: int = 150) -> tuple[tuple[bytes, int, int], ...]:
    """PNG images ``(png, width, height)`` of the first ``max_pages`` pages.

    Needs the optional ``pypdfium2`` and ``Pillow`` (MissingDependencyError
    otherwise); a PDF the renderer cannot open raises StructuredDataError.
    """
    pdfium = import_optional("pypdfium2", feature="Rendering PDF pages")
    import_optional("PIL.Image", feature="Rendering PDF pages", package="Pillow")
    try:
        document = pdfium.PdfDocument(data)
    except Exception as exc:  # pdfium raises its own error types for damaged or encrypted files
        raise StructuredDataError("pdf_unrenderable", type(exc).__name__) from None
    try:
        pages = []
        for index in range(min(len(document), max_pages)):
            image = document[index].render(scale=dpi / 72).to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            pages.append((buffer.getvalue(), image.width, image.height))
        return tuple(pages)
    finally:
        document.close()


def _attachments(reader: Any) -> dict[str, bytes]:
    raw = getattr(reader, "attachments", None) or {}
    found: dict[str, bytes] = {}
    for name, blobs in dict(raw).items():
        blob = blobs[0] if isinstance(blobs, (list, tuple)) and blobs else blobs
        if isinstance(blob, bytes):
            found[str(name)] = blob
    return found


def _metadata(raw: Any) -> dict[str, str]:
    if not raw:
        return {}
    return {str(k).lstrip("/"): str(v) for k, v in dict(raw).items() if v is not None}


def extract_from_pdf(
    data: bytes | PdfContent,
    *,
    source: str,
    text_extractor: FieldExtractor | None = None,
) -> tuple[Stage0Result, ...]:
    """Stage 0 results from a PDF: embedded e-invoice XML, then its text layer."""
    content = data if isinstance(data, PdfContent) else read_pdf(data)
    results: list[Stage0Result] = []
    for name, blob in sorted(content.attachments.items()):
        if not name.lower().endswith(".xml"):
            continue
        try:
            embedded = parse_einvoice(blob, source=source)
        except StructuredDataError as exc:
            results.append(Stage0Result.build("pdf_attachment", source, (), notes=[f"attachment_skipped:{exc.code}"]))
            continue
        results.append(
            Stage0Result.build(
                f"pdf_attachment_{embedded.kind}",
                source,
                ((f, o) for f, obs in embedded.fields.items() for o in obs),
                doc_type=embedded.doc_type,
                extras=embedded.extras,
                notes=(*embedded.notes, f"embedded_file:{name}"),
            )
        )
    if text_extractor is not None and content.has_text_layer:
        results.append(_text_result(content, source, text_extractor))
    return tuple(results)


def _text_result(content: PdfContent, source: str, extractor: FieldExtractor) -> Stage0Result:
    from_ocr = content.text_layer_from_ocr
    method = ExtractionMethod.OCR if from_ocr else ExtractionMethod.EMBEDDED_TEXT
    fields = extractor(content.text, source, method)
    pairs = [
        (f, _labelled(o, source, method))
        for f, observations in fields.items()
        for o in observations
        if isinstance(o, FieldObservation)
    ]
    notes = ["text_layer_from_ocr"] if from_ocr else []
    return Stage0Result.build("pdf_text", source, pairs, notes=notes)


def _labelled(observation: FieldObservation, source: str, method: ExtractionMethod) -> FieldObservation:
    """What the observation is: this PDF's text layer, born-digital or OCR'd (§13)."""
    if observation.source == source and observation.method is method:
        return observation
    return observation.model_copy(update={"source": source, "method": method})
