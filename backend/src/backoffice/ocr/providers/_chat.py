"""OpenAI-compatible chat-completions helpers (vLLM, commercial multimodal APIs).

Request shape (``POST /v1/chat/completions``)::

    {"model": "...", "temperature": 0, "max_tokens": N,
     "messages": [{"role": "user", "content": [
         {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
         {"type": "file", "file": {"filename": "document.pdf",
                                   "file_data": "data:application/pdf;base64,..."}},
         {"type": "text", "text": "<instructions>"}]}]}

Response shape: ``choices[0].message.content`` as a string or a list of
``{"type": "text", "text": ...}`` parts, plus ``choices[0].finish_reason``.

Engines are asked to start each page with ``=== PAGE n ===``; the output is
split on those markers, tolerating missing markers (then the whole text is
attributed to the first page of the request and a warning is recorded).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from ..base import OCRResponseError, PageImage
from ._http import b64

__all__ = [
    "DEFAULT_TRANSCRIBE_PROMPT",
    "chat_payload",
    "chat_text",
    "file_part",
    "image_part",
    "split_pages",
    "strip_code_fence",
]

DEFAULT_TRANSCRIBE_PROMPT = (
    "Transcribe all text on these document pages exactly as printed, in reading order. "
    "Keep numbers, dates, codes and punctuation unchanged. Write each table row on one line "
    "with cells separated by ' | '. Start every page with a line '=== PAGE n ===' using the "
    "page number given below. Do not summarise, translate or add comments."
)

_PAGE_MARKER = re.compile(r"^[ \t]*=+[ \t]*page[ \t]+(\d+)[ \t]*=+[ \t]*$", re.IGNORECASE | re.MULTILINE)
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


def image_part(page: PageImage) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": f"data:{page.mime_type};base64,{b64(page.data)}"}}


def file_part(page: PageImage) -> dict[str, Any]:
    return {
        "type": "file",
        "file": {
            "filename": f"document-{page.number}.pdf",
            "file_data": f"data:{page.mime_type};base64,{b64(page.data)}",
        },
    }


def chat_payload(
    model: str, parts: Sequence[dict[str, Any]], prompt: str, *, max_tokens: int
) -> dict[str, Any]:
    return {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": [*parts, {"type": "text", "text": prompt}]}],
    }


def chat_text(body: Any, *, engine: str) -> tuple[str, str | None]:
    """Content text and finish reason of the first choice."""
    try:
        choice = body["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise OCRResponseError(engine, "no choices in response") from None
    if isinstance(content, list):
        content = "\n".join(
            str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") in (None, "text")
        )
    if not isinstance(content, str):
        raise OCRResponseError(engine, "content is not text")
    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    return strip_code_fence(content), finish if isinstance(finish, str) else None


def strip_code_fence(text: str) -> str:
    m = _FENCE.match(text)
    return m[1] if m else text


def split_pages(text: str, numbers: Sequence[int]) -> tuple[dict[int, str], bool]:
    """Page texts keyed by page number, and whether markers were found.

    Markers naming pages outside ``numbers`` are ignored (their text stays
    with the previous page); without usable markers everything goes to the
    first expected page.
    """
    expected = set(numbers)
    first = numbers[0] if numbers else 1
    marks = [(m.start(), m.end(), int(m[1])) for m in _PAGE_MARKER.finditer(text) if int(m[1]) in expected]
    if not marks:
        return {first: text.strip()}, False
    pages: dict[int, list[str]] = {}
    preamble = text[: marks[0][0]].strip()
    if preamble:
        pages.setdefault(marks[0][2], []).append(preamble)
    for i, (_, end, number) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        chunk = text[end:stop].strip()
        if chunk:
            pages.setdefault(number, []).append(chunk)
    return {n: "\n".join(parts) for n, parts in pages.items()}, True
