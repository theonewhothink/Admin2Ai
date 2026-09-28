"""PaddleX pipeline-serving envelope (shared by PP-OCR and PaddleOCR-VL adapters).

Assumed contract (PaddleX 3.x serving, as deployed with
``paddlex --serve --pipeline <pipeline>``)::

    POST <base>/<endpoint>
    {"file": "<base64>", "fileType": 0 (PDF) | 1 (image), ...pipeline options}

    200 {"logId": "...", "errorCode": 0, "errorMsg": "Success",
         "result": {"<pipeline>Results": [...one item per page...],
                    "dataInfo": {"type": "image", "width": W, "height": H}
                              | {"type": "pdf", "numPages": N,
                                 "pages": [{"width": W, "height": H}, ...]}}}

Parsing is tolerant: snake_case keys, a bare ``result`` object and missing
``dataInfo`` are accepted; a non-zero ``errorCode`` is an error.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from ..base import OCRResponseError

__all__ = ["first_key", "page_sizes", "positive_int", "pruned", "rotation", "unwrap"]


def unwrap(body: Any, *, engine: str) -> Mapping[str, Any]:
    """The ``result`` object of a PaddleX serving response."""
    if not isinstance(body, Mapping):
        raise OCRResponseError(engine, "response is not an object")
    code = body.get("errorCode", body.get("error_code", 0))
    if code not in (0, None, "0"):
        raise OCRResponseError(engine, f"errorCode {code}")
    result = body.get("result", body)
    if not isinstance(result, Mapping):
        raise OCRResponseError(engine, "result is not an object")
    return result


def first_key(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def positive_int(value: Any) -> int | None:
    """A positive finite number as int; None for anything else (NaN, Infinity, text)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return int(value) if value > 0 else None


def rotation(result: Mapping[str, Any]) -> int:
    """Orientation correction the pipeline applied (0/90/180/270), else 0."""
    pre = result.get("doc_preprocessor_res")
    angle = pre.get("angle", 0) if isinstance(pre, Mapping) else 0
    if isinstance(angle, bool) or not isinstance(angle, (int, float)):
        return 0
    return int(angle) if angle in (0, 90, 180, 270) else 0


def page_sizes(result: Mapping[str, Any]) -> list[tuple[int | None, int | None]]:
    """Per-page (width, height) from ``dataInfo``; empty when absent."""
    info = first_key(result, "dataInfo", "data_info")
    if not isinstance(info, Mapping):
        return []
    pages = info.get("pages")
    if isinstance(pages, list):
        return [_size(p) if isinstance(p, Mapping) else (None, None) for p in pages]
    return [_size(info)]


def _size(item: Mapping[str, Any]) -> tuple[int | None, int | None]:
    return positive_int(item.get("width")), positive_int(item.get("height"))


def pruned(item: Any, *, engine: str) -> Mapping[str, Any]:
    """The JSON result of one page (``prunedResult``), tolerating bare objects."""
    if not isinstance(item, Mapping):
        raise OCRResponseError(engine, "page result is not an object")
    for key in ("prunedResult", "pruned_result", "res"):
        value = item.get(key)
        if isinstance(value, Mapping):
            return value
    return item
