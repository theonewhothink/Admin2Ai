"""Small text helpers shared by the Spain pack (internal)."""

from __future__ import annotations

import unicodedata
from functools import lru_cache

# Space-like and dash-like characters that OCR and PDFs emit in place of the plain ASCII ones.
_CHAR_MAP = str.maketrans({
    " ": " ", " ": " ", " ": " ", " ": " ", "\t": " ",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
})  # fmt: skip


def clean(text: str) -> str:
    """NFC-normalise, unify spaces/dashes and line endings."""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.translate(_CHAR_MAP)


@lru_cache(maxsize=4096)
def _fold_char(char: str) -> str:
    base = unicodedata.normalize("NFD", char)[:1] or char
    lowered = base.lower()
    return lowered if len(lowered) == 1 else base


def fold(text: str) -> str:
    """Lower-case, accent-free copy of ``text`` with identical length (index-aligned)."""
    return "".join(_fold_char(c) for c in text)
