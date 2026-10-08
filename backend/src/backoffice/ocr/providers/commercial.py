"""Independent commercial OCR / multimodal engine: the paid exception (§17, §53).

Only uncertain evidence leaves our infrastructure, and only redacted (§53).
The provider cannot be built without a :data:`Redactor`, and it refuses to
send when:

* a page it would send is byte-identical to an input page (an unredacted
  image slipping through, e.g. an identity redactor);
* the redacted text fails the configured ``is_clean`` check (plug in
  ``backoffice.policy.privacy.is_clean`` at integration time);
* nothing is left to send.

Tokens such as ``[IBAN_1]`` in the answer are restored locally through the
redaction's ``restore``; the token map never leaves.

Independence (§17, §19): when locally read text is sent (a text-only
redactor, or masked pages plus text), the answer is marked
``from_prior_text`` and the router counts it as a re-reading of the local
engine's text, not as an independent vote. Only masked page images without
text give an independent reading that can break a tie.

Transport (§52): the endpoint must be HTTPS with certificate verification,
since document content and an API key travel over the public internet. A
PDF whose page count is unknown is refused: it could not be billed or held
to ``max_pages`` honestly.

Wire formats:

* ``OPENAI_CHAT`` (default): OpenAI-compatible chat completions, the common
  denominator of commercial multimodal APIs (shape in :mod:`._chat`).
* ``GENERIC_JSON``::

      POST <base><path or "/ocr">
      {"model": m, "pages": [{"number": n, "mime_type": t, "data": "<b64>"}],
       "text": "<redacted text or null>", "languages": [...], "instructions": "..."}

      200 {"pages": [{"number"|"page": n, "text": "...",
                      "lines": [{"text": s, "confidence": c,
                                 "bbox": [x0, y0, x1, y1] | {"x0",...}}]}]}
          or {"text": "..."}, optionally wrapped in {"result": ...}

  ``confidence_scale`` converts vendor scores to 0-1 (100 for percentages).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from backoffice.domain.models import ExtractionMethod

from ..base import (
    ANY_LANGUAGE,
    OCRCapabilities,
    OCRHints,
    OCRInputError,
    OCRLine,
    OCRPage,
    OCRResponseError,
    OCRResult,
    PageImage,
    RedactionError,
    as_pages,
    ensure_supported,
)
from ..layout import geometry, to_bbox
from ._chat import DEFAULT_TRANSCRIBE_PROMPT, chat_payload, chat_text, file_part, image_part, split_pages
from ._common import DEFAULT_CLOCK, Clock, build_result, confidence, text_lines
from ._http import EndpointConfig, JSONEndpoint, b64

if TYPE_CHECKING:  # only for annotations: httpx is imported where a request is made
    import httpx

__all__ = [
    "CommercialOCRConfig",
    "CommercialOCRProvider",
    "CommercialWireFormat",
    "RedactedInput",
    "Redactor",
    "TextRedaction",
    "masked_pages_redactor",
    "parse_generic_response",
    "text_only_redactor",
]


def _identity(text: str) -> str:
    return text


@dataclass(frozen=True)
class RedactedInput:
    """What may leave: masked page images and/or redacted text, plus a local restorer."""

    pages: tuple[PageImage, ...] = ()
    text: str | None = field(default=None, repr=False)
    restore: Callable[[str], str] = field(default=_identity, repr=False)


Redactor = Callable[[tuple[PageImage, ...], OCRHints], RedactedInput]


class TextRedaction(Protocol):
    """Result of a text redactor (``backoffice.policy.privacy.redact`` fits)."""

    @property
    def text(self) -> str: ...

    def restore(self, external_text: str) -> str: ...


def text_only_redactor(redact_text: Callable[[str], TextRedaction]) -> Redactor:
    """Send no images, only the locally read text (``hints.prior_text``), redacted.

    The fallback when no image masker is deployed: the external engine then
    re-reads our transcription (fixing structure and labels), not the pixels.
    """

    def redactor(pages: tuple[PageImage, ...], hints: OCRHints) -> RedactedInput:
        if not hints.prior_text or not hints.prior_text.strip():
            raise RedactionError("redactor", "no local text to redact")
        redaction = redact_text(hints.prior_text)
        return RedactedInput(text=redaction.text, restore=redaction.restore)

    return redactor


def masked_pages_redactor(
    mask_page: Callable[[PageImage], PageImage],
    redact_text: Callable[[str], TextRedaction] | None = None,
) -> Redactor:
    """Send page images with sensitive regions masked, plus redacted text if any.

    ``mask_page`` is the image worker that blacks out bank accounts,
    addresses and unrelated personal data (for example using the local OCR
    boxes); it must return new image bytes. Passing ``redact_text`` also
    sends the local transcription, which makes the answer a re-reading of
    it: it can fill gaps but no longer counts as an independent vote.
    """

    def redactor(pages: tuple[PageImage, ...], hints: OCRHints) -> RedactedInput:
        masked = tuple(mask_page(p) for p in pages)
        if redact_text is None or not hints.prior_text:
            return RedactedInput(pages=masked)
        redaction = redact_text(hints.prior_text)
        return RedactedInput(pages=masked, text=redaction.text, restore=redaction.restore)

    return redactor


class CommercialWireFormat(str, Enum):
    OPENAI_CHAT = "openai_chat"
    GENERIC_JSON = "generic_json"


@dataclass(frozen=True)
class CommercialOCRConfig:
    endpoint: EndpointConfig
    model: str
    cost_per_page: Decimal  # the vendor's price per page: always explicit
    name: str = "commercial"
    wire_format: CommercialWireFormat = CommercialWireFormat.OPENAI_CHAT
    path: str | None = None
    method: ExtractionMethod = ExtractionMethod.VLM
    languages: frozenset[str] = frozenset({ANY_LANGUAGE})
    max_pages: int | None = 20
    max_tokens: int = 4096
    prompt: str = DEFAULT_TRANSCRIBE_PROMPT
    confidence_scale: float = 1.0
    is_clean: Callable[[str], bool] | None = None

    def __post_init__(self) -> None:
        if self.cost_per_page < 0:
            raise ValueError("cost_per_page cannot be negative")
        if not self.model.strip():
            raise ValueError("model is required")
        if not self.endpoint.base_url.lower().startswith("https://") or not self.endpoint.verify_tls:
            raise ValueError("an external engine must be reached over verified HTTPS (§52)")

    @property
    def request_path(self) -> str:
        if self.path:
            return self.path
        return "/v1/chat/completions" if self.wire_format is CommercialWireFormat.OPENAI_CHAT else "/ocr"


def parse_generic_response(body: Any, *, first_page: int, engine: str, scale: float = 1.0) -> list[OCRPage]:
    """Pages from the generic JSON wire format."""
    root = body.get("result", body) if isinstance(body, Mapping) else None
    if not isinstance(root, Mapping):
        raise OCRResponseError(engine, "response is not an object")
    raw_pages = root.get("pages")
    if isinstance(raw_pages, list):
        return [_generic_page(p, first_page + i, scale) for i, p in enumerate(raw_pages) if isinstance(p, Mapping)]
    if isinstance(root.get("text"), str):
        text = root["text"]
        return [OCRPage(number=first_page, lines=text_lines(text), markdown=text)]
    raise OCRResponseError(engine, "no pages or text in response")


def _generic_page(raw: Mapping[str, Any], default_number: int, scale: float) -> OCRPage:
    number = raw.get("number", raw.get("page"))
    number = number if isinstance(number, int) and not isinstance(number, bool) and number >= 1 else default_number
    lines = []
    raw_lines = raw.get("lines")
    for line in raw_lines if isinstance(raw_lines, list) else []:
        if not isinstance(line, Mapping) or not str(line.get("text", "")).strip():
            continue
        bbox = line.get("bbox")
        if isinstance(bbox, Mapping):
            bbox = [bbox.get(k) for k in ("x0", "y0", "x1", "y1")]
        box, _ = geometry(bbox)
        lines.append(
            OCRLine(
                text=str(line["text"]).strip(),
                bbox=to_bbox(box, number),
                confidence=confidence(line.get("confidence"), scale),
            )
        )
    text = raw.get("text")
    text = text if isinstance(text, str) else None
    return OCRPage(number=number, lines=tuple(lines) or text_lines(text or ""), markdown=text)


def _restored(page: OCRPage, restore: Callable[[str], str]) -> OCRPage:
    return page.model_copy(
        update={
            "lines": tuple(line.model_copy(update={"text": restore(line.text)}) for line in page.lines),
            "markdown": restore(page.markdown) if page.markdown is not None else None,
        }
    )


class CommercialOCRProvider:
    """A paid external engine behind :class:`~backoffice.ocr.base.OCRProviderInterface`."""

    def __init__(
        self,
        config: CommercialOCRConfig,
        *,
        redactor: Redactor,
        client: httpx.AsyncClient | None = None,
        clock: Clock = DEFAULT_CLOCK,
    ) -> None:
        if not callable(redactor):
            raise TypeError("a redactor is required: nothing leaves unredacted (§53)")
        self._config = config
        self._redactor = redactor
        self._http = JSONEndpoint(config.endpoint, engine=config.name, client=client)
        self._clock = clock

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
        return self._config.method

    @property
    def capabilities(self) -> OCRCapabilities:
        return OCRCapabilities(
            languages=self._config.languages,
            max_pages=self._config.max_pages,
            handles_tables=True,
            accepts_pdf=True,
            returns_boxes=self._config.wire_format is CommercialWireFormat.GENERIC_JSON,
            external=True,
        )

    async def recognize(self, pages: Sequence[PageImage | bytes], hints: OCRHints | None = None) -> OCRResult:
        started = self._clock()
        items = as_pages(pages)
        ensure_supported(items, engine=self.name, max_pages=self._config.max_pages)
        payload = self._redactor(items, hints or OCRHints())
        self._check(items, payload)
        first = payload.pages[0].number if payload.pages else items[0].number
        if self._config.wire_format is CommercialWireFormat.OPENAI_CHAT:
            result_pages, warnings = await self._chat(payload, first)
        else:
            result_pages, warnings = await self._generic(payload, hints or OCRHints(), first), []
        billed = sum(p.pages or 1 for p in payload.pages) or 1
        return build_result(
            engine=self.name,
            version=self.version,
            method=self.method,
            pages=[_restored(p, payload.restore) for p in result_pages],
            cost_per_page=self.cost_per_page,
            pages_billed=billed,
            started=started,
            clock=self._clock,
            warnings=warnings,
            from_prior_text=bool(payload.text and payload.text.strip()),
        )

    def _check(self, originals: Sequence[PageImage], payload: RedactedInput) -> None:
        """Refuse anything that could leave unredacted (§53) or be billed blindly."""
        digests = {hashlib.sha256(p.data).digest() for p in originals}
        if any(hashlib.sha256(p.data).digest() in digests for p in payload.pages):
            raise RedactionError(self.name, "a page would leave unredacted")
        has_text = bool(payload.text and payload.text.strip())
        if not payload.pages and not has_text:
            raise RedactionError(self.name, "nothing left to send")
        if any(p.pages is None for p in payload.pages):
            raise OCRInputError(self.name, "page count unknown")
        if payload.pages:
            ensure_supported(payload.pages, engine=self.name, max_pages=self._config.max_pages)
        if has_text and self._config.is_clean is not None and not self._config.is_clean(payload.text or ""):
            raise RedactionError(self.name, "text still contains sensitive data")

    async def _chat(self, payload: RedactedInput, first: int) -> tuple[list[OCRPage], list[str]]:
        parts = [file_part(p) if p.is_pdf else image_part(p) for p in payload.pages]
        prompt = self._config.prompt
        numbers = [p.number for p in payload.pages] or [first]
        if payload.pages:
            prompt += f"\nPage numbers: {', '.join(map(str, numbers))}."
        if payload.text:
            prompt += "\nText already read from the document (sensitive values replaced by tokens):\n" + payload.text
        body = await self._http.post(
            self._config.request_path,
            chat_payload(self._config.model, parts, prompt, max_tokens=self._config.max_tokens),
        )
        text, finish = chat_text(body, engine=self.name)
        by_page, marked = split_pages(text, numbers)
        warnings = ["truncated"] if finish == "length" else []
        if not marked and len(numbers) > 1:
            warnings.append("page_boundaries_unknown")
        pages = [OCRPage(number=n, lines=text_lines(t), markdown=t) for n, t in sorted(by_page.items())]
        return pages, warnings

    async def _generic(self, payload: RedactedInput, hints: OCRHints, first: int) -> list[OCRPage]:
        request = {
            "model": self._config.model,
            "pages": [{"number": p.number, "mime_type": p.mime_type, "data": b64(p.data)} for p in payload.pages],
            "text": payload.text,
            "languages": list(hints.languages),
            "instructions": self._config.prompt,
        }
        body = await self._http.post(self._config.request_path, request)
        return parse_generic_response(body, first_page=first, engine=self.name, scale=self._config.confidence_scale)

    async def aclose(self) -> None:
        await self._http.aclose()
