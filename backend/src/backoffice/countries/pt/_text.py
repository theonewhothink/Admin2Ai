"""Small text helpers shared by the Portugal pack (internal)."""

from __future__ import annotations

import unicodedata
from functools import lru_cache

# Space-like and dash-like characters that OCR and PDFs emit in place of the
# plain ASCII ones. Mapping them keeps string indexes aligned.
_CHAR_MAP = str.maketrans(
    {
        "\u00a0": " ",  # no-break space
        "\u202f": " ",  # narrow no-break space (thousands separator)
        "\u2009": " ",  # thin space
        "\u2007": " ",  # figure space
        "\t": " ",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",  # en dash (ATCUD is often typeset with it)
        "\u2014": "-",
        "\u2212": "-",  # minus sign
    }
)


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
    """Lower-case, accent-free copy of ``text`` with identical length.

    Index-aligned, so a match found in the folded text can be cut from the
    original text with the same span.
    """
    return "".join(_fold_char(c) for c in text)


def fold_label(text: str) -> str:
    """Fold and collapse whitespace; strip surrounding punctuation."""
    return " ".join(fold(clean(text)).split()).strip(" :.-")
