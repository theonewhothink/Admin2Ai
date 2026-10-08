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

The sidecar contract is PaddleX pipeline serving (see
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


def _endpoint(env: Mapping[str, str], prefix: str):  # type: ignore[no-untyped-def]
    from backoffice.ocr import EndpointConfig

    return EndpointConfig(
        base_url=env[f"{prefix}_URL"].strip(),
        timeout_seconds=float(env.get(f"{prefix}_TIMEOUT") or 60),
        api_key=env.get(f"{prefix}_API_KEY") or None,
    )


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
