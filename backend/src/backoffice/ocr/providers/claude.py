"""Claude vision: the multimodal fallback engine (§17, §18, §53).

It runs only in the router's *commercial* role, so only when structured
extraction and the local engines leave a critical field missing or in
dispute, within the tenant's OCR budget, and only when external AI is
switched on (``BACKOFFICE_EXTERNAL_AI=on``; see ``backoffice.reading``).
Its answer is one more *reading* (method VLM): it can confirm a value or
expose a disagreement, never overrule the document's own statements (§19).

What leaves (§53): page images only, never our local transcription (so the
answer stays an independent reading), after the redactor has removed image
metadata and masked lines with personal details where feasible
(:mod:`backoffice.ocr.redact`). The model is asked for the §18 critical
fields only, not a transcription.

Wire format: Anthropic Messages API, plain HTTPS through ``httpx``::

    POST <base_url>/v1/messages
    x-api-key: <key>
    anthropic-version: 2023-06-01

    {"model": "claude-sonnet-5-5", "max_tokens": 1024, "temperature": 0,
     "system": "...",
     "tools": [{"name": "record_document_fields", "input_schema": {...}}],
     "tool_choice": {"type": "tool", "name": "record_document_fields"},
     "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}},
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "..."}},
        {"type": "text", "text": "..."}]}]}

    200 {"content": [{"type": "tool_use", "name": "record_document_fields",
                      "input": {"document_type": "invoice", "supplier_name": "...",
                                "fields": {"gross_amount": {"value": "483.60", "page": 1,
                                                            "printed": "Total 483,60 €"}, ...}}}],
         "stop_reason": "tool_use", "usage": {...}}

The forced tool call makes the answer structured JSON; the fields come back
as :class:`~backoffice.ocr.base.OCRFieldReading` on the result, which the
router types and labels (source ``<evidence>@claude-vision``, method VLM).
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from backoffice.domain.models import CriticalField, ExtractionMethod
from backoffice.extraction._optional import MissingDependencyError, import_optional
from backoffice.extraction.media import MIME_GIF, MIME_JPEG, MIME_PDF, MIME_PNG, MIME_WEBP

from ..base import (
    ANY_LANGUAGE,
    OCRCapabilities,
    OCRFieldReading,
    OCRHints,
    OCRInputError,
    OCRPage,
    OCRResponseError,
    OCRResult,
    PageImage,
    RedactionError,
    as_pages,
    ensure_supported,
)
from ..redact import RedactedPages, VisionRedactor, vision_redactor
from ._common import DEFAULT_CLOCK, Clock, build_result, confidence, text_lines
from ._http import EndpointConfig, JSONEndpoint, b64

if TYPE_CHECKING:  # only for annotations: httpx is imported where a request is made
    import httpx

__all__ = [
    "CLAUDE_VISION",
    "DEFAULT_VISION_MODEL",
    "TOOL_NAME",
    "ClaudeVisionConfig",
    "ClaudeVisionProvider",
    "parse_tool_answer",
    "tool_definition",
]

CLAUDE_VISION = "claude-vision"
DEFAULT_VISION_MODEL = "claude-sonnet-5-5"
TOOL_NAME = "record_document_fields"
ANTHROPIC_VERSION = "2023-06-01"

# Media types the Messages API accepts as images; anything else is converted (Pillow) or refused.
_API_IMAGES = frozenset({MIME_PNG, MIME_JPEG, MIME_GIF, MIME_WEBP})
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_EDGE = 2000

_FIELD_HELP: Mapping[CriticalField, str] = {
    CriticalField.INVOICE_NUMBER: "Document number exactly as printed, e.g. 'FT 2026/183'.",
    CriticalField.SUPPLIER_TAX_ID: "Issuer's VAT/tax number (NIF), digits with any country prefix as printed.",
    CriticalField.CUSTOMER_TAX_ID: "Customer's VAT/tax number as printed, or omit when none is shown.",
    CriticalField.GROSS_AMOUNT: "Total including VAT, as a plain number with a dot for decimals, e.g. '483.60'.",
    CriticalField.NET_AMOUNT: "Total before VAT (taxable base), plain number with a dot for decimals.",
    CriticalField.VAT_AMOUNT: "Total VAT, plain number with a dot for decimals.",
    CriticalField.CURRENCY: "ISO 4217 code of the amounts, e.g. 'EUR'.",
    CriticalField.ISSUE_DATE: "Issue date as YYYY-MM-DD.",
    CriticalField.DUE_DATE: "Payment due date as YYYY-MM-DD, or omit.",
    CriticalField.IBAN: "IBAN to pay into, if printed (it may be masked; then omit).",
    CriticalField.PAYMENT_REFERENCE: "Payment reference (e.g. Multibanco entity/reference or RF...), or omit.",
}

_DOCUMENT_TYPES = ("invoice", "invoice_receipt", "simplified_invoice", "receipt", "credit_note", "debit_note",
                   "other")

SYSTEM_PROMPT = (
    "You read business documents (invoices, receipts, credit notes) for an accounting back office. "
    "Report only what is printed on the pages. Never compute, infer or complete a value: when a field is "
    "not clearly printed, or is covered by a black box, leave it out. Copy numbers exactly; convert only "
    "the format as each field's description says. Black boxes hide personal details on purpose."
)
USER_PROMPT = (
    "Record the fields printed on this document with the {tool} tool. "
    "For every field you report, give the page number and the text exactly as printed."
)


def tool_definition() -> dict[str, Any]:
    """The forced tool whose input is the structured answer (JSON Schema)."""
    reading = {
        "type": "object",
        "properties": {
            "value": {"type": "string"},
            "page": {"type": "integer", "minimum": 1},
            "printed": {"type": "string"},
        },
        "required": ["value"],
        "additionalProperties": False,
    }
    fields = {f.value: {**reading, "description": _FIELD_HELP[f]} for f in CriticalField}
    return {
        "name": TOOL_NAME,
        "description": "Record the critical fields printed on the document.",
        "input_schema": {
            "type": "object",
            "properties": {
                "document_type": {"type": "string", "enum": list(_DOCUMENT_TYPES)},
                "supplier_name": {"type": "string", "description": "Issuer's name as printed."},
                "fields": {"type": "object", "properties": fields, "additionalProperties": False},
            },
            "required": ["fields"],
            "additionalProperties": False,
        },
    }


@dataclass(frozen=True)
class ClaudeVisionConfig:
    """Where and how to call Claude. ``cost_per_page`` is our estimate for budgeting (§17)."""

    api_key: str = field(repr=False)
    model: str = DEFAULT_VISION_MODEL
    base_url: str = "https://api.anthropic.com"
    path: str = "/v1/messages"
    api_version: str = ANTHROPIC_VERSION
    cost_per_page: Decimal = Decimal("0.02")
    max_pages: int = 10
    max_tokens: int = 1024
    timeout_seconds: float = 60.0
    name: str = CLAUDE_VISION
    field_confidence: float = 0.6

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("an API key is required")
        if not self.model.strip():
            raise ValueError("model is required")
        if not self.base_url.lower().startswith("https://"):
            raise ValueError("an external engine must be reached over verified HTTPS (§52)")
        if self.cost_per_page < 0:
            raise ValueError("cost_per_page cannot be negative")
        if self.max_pages < 1 or self.max_tokens < 1:
            raise ValueError("max_pages and max_tokens must be positive")
        if not 0.0 < self.field_confidence <= 1.0:
            raise ValueError("field_confidence must be in (0, 1]")

    def endpoint(self) -> EndpointConfig:
        return EndpointConfig(
            base_url=self.base_url, timeout_seconds=self.timeout_seconds, api_key=self.api_key,
            api_key_header="x-api-key", headers={"anthropic-version": self.api_version},
        )


# --------------------------------------------------------------------------- answer


def parse_tool_answer(
    body: Any, *, engine: str, pages: Sequence[int], field_confidence: float
) -> tuple[tuple[OCRFieldReading, ...], str | None, str | None, str | None]:
    """(fields, supplier name, document type, stop reason) from a Messages API response."""
    if not isinstance(body, Mapping):
        raise OCRResponseError(engine, "response is not an object")
    blocks = body.get("content")
    call = next(
        (b for b in blocks if isinstance(b, Mapping) and b.get("type") == "tool_use" and b.get("name") == TOOL_NAME),
        None,
    ) if isinstance(blocks, list) else None
    if call is None or not isinstance(call.get("input"), Mapping):
        raise OCRResponseError(engine, "no structured answer in response")
    answer = call["input"]
    raw_fields = answer.get("fields")
    if not isinstance(raw_fields, Mapping):
        raise OCRResponseError(engine, "answer has no fields object")
    known = set(pages)
    readings: list[OCRFieldReading] = []
    for name in CriticalField:
        item = raw_fields.get(name.value)
        if isinstance(item, str):
            item = {"value": item}
        if not isinstance(item, Mapping):
            continue
        value = item.get("value")
        if not isinstance(value, str) or not value.strip():
            continue
        page = item.get("page")
        page = page if isinstance(page, int) and not isinstance(page, bool) and page in known else None
        printed = item.get("printed")
        readings.append(OCRFieldReading(
            field=name, value=value.strip()[:120], page=page,
            printed=printed.strip()[:200] if isinstance(printed, str) and printed.strip() else None,
            confidence=confidence(item.get("confidence")) or field_confidence,
        ))
    supplier = answer.get("supplier_name")
    doc_type = answer.get("document_type")
    stop = body.get("stop_reason")
    return (
        tuple(readings),
        " ".join(supplier.split())[:120] if isinstance(supplier, str) and supplier.strip() else None,
        doc_type if isinstance(doc_type, str) and doc_type in _DOCUMENT_TYPES else None,
        stop if isinstance(stop, str) else None,
    )


def _summary(readings: Sequence[OCRFieldReading], supplier: str | None) -> str:
    """A short, readable page text for the result (display only: fields are used directly)."""
    lines = [supplier] if supplier else []
    lines += [f"{r.field.value}: {r.value}" for r in readings]
    return "\n".join(lines)


# --------------------------------------------------------------------------- provider


def _api_image(page: PageImage, engine: str) -> PageImage:
    """The page as an image the API accepts (converted or shrunk with Pillow when needed)."""
    if page.mime_type in _API_IMAGES and len(page.data) <= _MAX_IMAGE_BYTES:
        return page
    try:
        image_module = import_optional("PIL.Image", feature="Preparing images for Claude", package="Pillow")
    except MissingDependencyError:
        raise OCRInputError(engine, f"cannot send {page.mime_type} of {len(page.data)} bytes") from None
    with image_module.open(io.BytesIO(page.data)) as opened:
        image = opened.convert("RGB")
    image.thumbnail((_MAX_EDGE, _MAX_EDGE))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return PageImage(data=buffer.getvalue(), mime_type=MIME_JPEG, number=page.number, width=image.width,
                     height=image.height)


def _block(page: PageImage) -> dict[str, Any]:
    kind = "document" if page.mime_type == MIME_PDF else "image"
    return {"type": kind, "source": {"type": "base64", "media_type": page.mime_type, "data": b64(page.data)}}


class ClaudeVisionProvider:
    """Claude vision behind :class:`~backoffice.ocr.base.OCRProviderInterface` (external, paid)."""

    def __init__(
        self,
        config: ClaudeVisionConfig,
        *,
        redactor: VisionRedactor | None = None,
        client: httpx.AsyncClient | None = None,
        clock: Clock = DEFAULT_CLOCK,
    ) -> None:
        self._config = config
        self._redactor = redactor or vision_redactor(max_pages=config.max_pages)
        self._http = JSONEndpoint(config.endpoint(), engine=config.name, client=client)
        self._clock = clock
        self.last_redaction: RedactedPages | None = None

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def version(self) -> str:
        return self._config.model

    @property
    def cost_per_page(self) -> Decimal:
        return self._config.cost_per_page

    @property
    def method(self) -> ExtractionMethod:
        return ExtractionMethod.VLM

    @property
    def capabilities(self) -> OCRCapabilities:
        return OCRCapabilities(
            languages=frozenset({ANY_LANGUAGE}),
            max_pages=self._config.max_pages,
            handles_tables=True,
            accepts_pdf=True,
            returns_boxes=False,
            external=True,
        )

    async def recognize(self, pages: Sequence[PageImage | bytes], hints: OCRHints | None = None) -> OCRResult:
        started = self._clock()
        hints = hints or OCRHints()
        items = as_pages(pages)
        ensure_supported(items, engine=self.name, max_pages=self._config.max_pages)
        redacted = self._redactor(items, hints)
        self.last_redaction = redacted
        if not redacted.pages:
            raise RedactionError(self.name, "nothing left to send")
        sent = [p if p.mime_type == MIME_PDF else _api_image(p, self.name) for p in redacted.pages]
        numbers = sorted({p.number + i for p in sent for i in range(p.pages or hints.page_count or 1)})
        payload = {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,
            "temperature": 0,
            "system": SYSTEM_PROMPT,
            "tools": [tool_definition()],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
            "messages": [{"role": "user", "content": [
                *(_block(p) for p in sent),
                {"type": "text", "text": USER_PROMPT.format(tool=TOOL_NAME)
                 + f" Page numbers: {', '.join(map(str, numbers))}."},
            ]}],
        }
        body = await self._http.post(self._config.path, payload)
        readings, supplier, doc_type, stop = parse_tool_answer(
            body, engine=self.name, pages=numbers, field_confidence=self._config.field_confidence)
        warnings = [f"redaction:{note}" for note in redacted.notes]
        if stop == "max_tokens":
            warnings.append("truncated")
        text = _summary(readings, supplier)
        first = numbers[0] if numbers else 1
        result = build_result(
            engine=self.name,
            version=self.version,
            method=self.method,
            pages=[OCRPage(number=first, lines=text_lines(text), markdown=text)],
            cost_per_page=self.cost_per_page,
            pages_billed=max(1, len(numbers)),
            started=started,
            clock=self._clock,
            warnings=warnings,
        )
        return result.model_copy(update={"fields": readings, "supplier_name": supplier, "document_type": doc_type})

    async def aclose(self) -> None:
        await self._http.aclose()
