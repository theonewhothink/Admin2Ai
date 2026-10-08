"""The document reader configured from the environment (server only).

=====================================  ==========================================================
Variable                               Meaning
=====================================  ==========================================================
``BACKOFFICE_DOCUMENT_READING``        ``off`` stores uploads without reading them (default on)
``BACKOFFICE_OCR_URL``                 PP-OCRv6 sidecar (PaddleX OCR-pipeline serving), e.g.
                                       ``http://ppocr:8080``; ``BACKOFFICE_OCR_PATH`` (``/ocr``),
                                       ``BACKOFFICE_OCR_API_KEY``, ``BACKOFFICE_OCR_TIMEOUT`` (s)
``BACKOFFICE_OCR_LOCAL``               ``auto`` (default): when no ``BACKOFFICE_OCR_URL`` is set, PP-OCRv6
                                       runs in this process (RapidOCR + onnxruntime, the ``ocr-local``
                                       extra: free, no network, nothing leaves); ``off`` never
``BACKOFFICE_OCR_VL_URL``              PaddleOCR-VL sidecar (PaddleX layout-parsing serving);
                                       ``BACKOFFICE_OCR_VL_PATH`` (``/layout-parsing``),
                                       ``BACKOFFICE_OCR_VL_API_KEY``
``BACKOFFICE_OCR_UNLIMITED_URL``       Unlimited-OCR for long documents (§16): an OpenAI-compatible
                                       server such as ``vllm serve``, e.g. ``http://unlimited-ocr:8000``;
                                       ``_MODEL`` (served model name, ``Unlimited-OCR``), ``_PATH``
                                       (``/v1/chat/completions``), ``_API_KEY``, ``_TIMEOUT`` (s, per
                                       request, default 300), ``_MAX_PAGES`` (longer documents are never
                                       sent, default 200), ``_PAGES_PER_REQUEST`` (8), ``_MAX_TOKENS``
                                       (8192), ``_DPI`` (PDF pages rendered as images, 150)
``BACKOFFICE_EXTERNAL_AI``             ``on`` allows the Claude vision fallback; anything else
                                       (the default) means no file ever leaves (§53)
``ANTHROPIC_API_KEY``                  needed for the fallback
``BACKOFFICE_VISION_MODEL``            default ``claude-sonnet-5-5``
``BACKOFFICE_VISION_URL``              default ``https://api.anthropic.com``
``BACKOFFICE_VISION_COST_PER_PAGE``    budget estimate per page in EUR, default ``0.02``
``BACKOFFICE_VISION_MAX_PAGES``        default ``10``
``BACKOFFICE_OCR_MONTHLY_BUDGET``      per-tenant ceiling for paid reading, EUR per month,
                                       default ``5.00`` (§17: paid OCR is the exception)
=====================================  ==========================================================

Unlimited-OCR runs only where it belongs in the chain (``backoffice.ocr.router``): a document of
``RouterConfig.long_document_pages`` pages or more whose fields are still missing or disputed after the
local engines, or as the fallback when the layout engine gave nothing. Its transcription is one more
reading (source ``<evidence>@unlimited-ocr``, method VLM): it never settles a field on its own, a field
needs an independent source to become verified, and it can confirm the document's own structured data,
never overrule it (§17-19). A PDF's pages are sent as PNG images when pypdfium2 is installed (the
production image has it), ``_PAGES_PER_REQUEST`` at a time, else the PDF whole as a file part.

The PP-OCRv6 sidecar contract is PaddleX pipeline serving (see
``backoffice.ocr.providers._paddlex``)::

    POST <BACKOFFICE_OCR_URL>/ocr
    {"file": "<base64 of the PDF or image>", "fileType": 0 (PDF) | 1 (image), "visualize": false}

    200 {"errorCode": 0, "errorMsg": "Success",
         "result": {"ocrResults": [{"prunedResult": {"rec_texts": ["..."], "rec_scores": [0.98],
                                                      "rec_polys": [[[x, y], [x, y], [x, y], [x, y]]]}}],
                    "dataInfo": {"type": "image", "width": W, "height": H}}}
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from .reader import DocumentReader

__all__ = ["external_ai_enabled", "reader_from_env"]

_OFF = {"off", "0", "false", "no", "disabled"}
_ON = {"on", "1", "true", "yes", "enabled"}


def _decimal(value: str | None, default: str) -> Decimal:
    try:
        out = Decimal((value or default).strip())
    except InvalidOperation:
        raise ValueError(f"not a number: {value!r}") from None
    if out < 0:
        raise ValueError("amounts cannot be negative")
    return out


def _endpoint(env: Mapping[str, str], prefix: str, *, timeout: float = 60):  # type: ignore[no-untyped-def]
    from backoffice.ocr import EndpointConfig

    return EndpointConfig(
        base_url=env[f"{prefix}_URL"].strip(),
        timeout_seconds=float(env.get(f"{prefix}_TIMEOUT") or timeout),
        api_key=env.get(f"{prefix}_API_KEY") or None,
    )


def _positive(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name} must be a whole number: {raw!r}") from None
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def _unlimited_ocr(env: Mapping[str, str]):  # type: ignore[no-untyped-def]
    """Unlimited-OCR behind its OpenAI-compatible server, with timeouts and page limits (table above)."""
    from backoffice.extraction._optional import MissingDependencyError, import_optional
    from backoffice.ocr import UNLIMITED_OCR, PdfPageRasterizer, UnlimitedOCRConfig, UnlimitedOCRProvider

    prefix = "BACKOFFICE_OCR_UNLIMITED"
    max_pages = _positive(env, f"{prefix}_MAX_PAGES", 200)
    config = UnlimitedOCRConfig(
        endpoint=_endpoint(env, prefix, timeout=300),
        model=(env.get(f"{prefix}_MODEL") or "Unlimited-OCR").strip(),
        path=(env.get(f"{prefix}_PATH") or "/v1/chat/completions").strip(),
        name=UNLIMITED_OCR,
        max_pages=max_pages,
        pages_per_request=_positive(env, f"{prefix}_PAGES_PER_REQUEST", 8),
        max_tokens=_positive(env, f"{prefix}_MAX_TOKENS", 8192),
    )
    try:  # PDF pages as images when the renderer is installed; else the PDF is sent whole
        import_optional("pypdfium2", feature="Rendering PDF pages")
        import_optional("PIL.Image", feature="Rendering PDF pages", package="Pillow")
        rasterizer = PdfPageRasterizer(max_pages=max_pages, dpi=_positive(env, f"{prefix}_DPI", 150))
    except (MissingDependencyError, OSError):
        rasterizer = None
    return UnlimitedOCRProvider(config, rasterizer=rasterizer)


def external_ai_enabled(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("BACKOFFICE_EXTERNAL_AI") or "").strip().lower() in _ON


def reader_from_env(env: Mapping[str, str] | None = None) -> DocumentReader | None:
    """A reader wired from the environment (table above); None when reading is switched off."""
    env = os.environ if env is None else env
    if (env.get("BACKOFFICE_DOCUMENT_READING") or "").strip().lower() in _OFF:
        return None
    from backoffice.ocr import (
        COMMERCIAL,
        ClaudeVisionConfig,
        ClaudeVisionProvider,
        EngineRegistry,
        InMemoryBudgetLedger,
        LocalOCRProvider,
        PaddleOCRVLConfig,
        PaddleOCRVLProvider,
        PPOCRConfig,
        PPOCRv6Provider,
        local_ocr_available,
    )
    from backoffice.ocr.providers.claude import DEFAULT_VISION_MODEL

    registry = EngineRegistry()
    if env.get("BACKOFFICE_OCR_URL"):
        registry.register(PPOCRv6Provider(PPOCRConfig(
            endpoint=_endpoint(env, "BACKOFFICE_OCR"), path=env.get("BACKOFFICE_OCR_PATH") or "/ocr")))
    elif (env.get("BACKOFFICE_OCR_LOCAL") or "auto").strip().lower() not in _OFF and local_ocr_available():
        registry.register(LocalOCRProvider())  # the primary engine when no sidecar is configured (§14)
    if env.get("BACKOFFICE_OCR_VL_URL"):
        registry.register(PaddleOCRVLProvider(PaddleOCRVLConfig(
            endpoint=_endpoint(env, "BACKOFFICE_OCR_VL"), path=env.get("BACKOFFICE_OCR_VL_PATH") or "/layout-parsing")))
    if (env.get("BACKOFFICE_OCR_UNLIMITED_URL") or "").strip():
        registry.register(_unlimited_ocr(env))  # long documents whose fields are still unsettled (§16)
    external = external_ai_enabled(env) and bool((env.get("ANTHROPIC_API_KEY") or "").strip())
    budget = None
    if external:
        registry.register(ClaudeVisionProvider(ClaudeVisionConfig(
            api_key=env["ANTHROPIC_API_KEY"].strip(),
            model=(env.get("BACKOFFICE_VISION_MODEL") or DEFAULT_VISION_MODEL).strip(),
            base_url=(env.get("BACKOFFICE_VISION_URL") or "https://api.anthropic.com").strip(),
            cost_per_page=_decimal(env.get("BACKOFFICE_VISION_COST_PER_PAGE"), "0.02"),
            max_pages=int(env.get("BACKOFFICE_VISION_MAX_PAGES") or 10),
        )), name=COMMERCIAL)
        budget = InMemoryBudgetLedger(default_ceiling=_decimal(env.get("BACKOFFICE_OCR_MONTHLY_BUDGET"), "5.00"))
    return DocumentReader(registry=registry, budget=budget, external_ai=external)
