"""Where each word sits on a photographed or scanned page (§13, QA E8).

The local engine (PP-OCRv6 through RapidOCR) returns one box per run of text, and the reader joins the boxes
of one printed line (``backoffice.ocr.rows``) while keeping each one as a word of that line. A table's columns
can only be read from where those words sit: "4  un  62,50  0%  23%  250,00" says nothing about which number
is the quantity until each is placed under its column's heading. This module keeps exactly that, compactly,
with the reading of a file (``ReadOutcome.word_rows``): every printed line with its words left to right, in
the page's own frame (the photo's tilt undone, so a column stays one column from the top of the table to its
bottom), in whole pixels.

What the columns mean is decided elsewhere, against the invoice's own arithmetic
(``backoffice.line_prices.read_table_lines``). Pure Python: the browser build imports it too.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

__all__ = ["Word", "WordRow", "word_rows"]


@dataclass(frozen=True)
class Word:
    """One run of text the engine found: what it says and where it starts and ends along the line."""

    text: str
    x0: int
    x1: int


@dataclass(frozen=True)
class WordRow:
    """One printed line of one page: its words left to right, and its top and bottom (page frame, pixels)."""

    page: int
    top: int
    bottom: int
    words: tuple[Word, ...]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def height(self) -> int:
        return max(1, self.bottom - self.top)


def word_rows(pages: Iterable[Any]) -> tuple[WordRow, ...]:
    """The printed lines of ``pages`` (``OCRPage``) whose words the engine located, top to bottom per page.

    Coordinates are turned by the page's median tilt, so a photo taken a few degrees askew keeps its columns
    straight; each word's own width and height are recovered from its axis-aligned box.
    """
    out: list[WordRow] = []
    for page in pages:
        lines = [line for line in page.lines if line.words and line.bbox is not None]
        angles = [line.angle for line in lines if line.angle is not None]
        tilt = math.radians(statistics.median(angles)) if angles else 0.0
        cos, sin = math.cos(tilt), math.sin(tilt)
        rows: list[tuple[float, WordRow]] = []
        for line in lines:
            placed = [_place(word, cos, sin) for word in line.words if word.bbox is not None and word.text.strip()]
            if not placed:
                continue
            placed.sort(key=lambda p: p[0])
            centre = statistics.median(p[2] for p in placed)
            height = statistics.median(p[3] for p in placed)
            words = tuple(Word(text=text, x0=round(x0), x1=round(x1)) for x0, x1, _, _, text in placed)
            rows.append((centre, WordRow(page=page.number, top=round(centre - height / 2),
                                         bottom=round(centre + height / 2), words=words)))
        out += [row for _, row in sorted(rows, key=lambda r: r[0])]
    return tuple(out)


def _place(word: Any, cos: float, sin: float) -> tuple[float, float, float, float, str]:
    """(left, right, vertical centre, height, text) of one word in the page's frame."""
    box = word.bbox
    cx, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
    along, down = cx * cos + cy * sin, -cx * sin + cy * cos
    width_box, height_box = box.x1 - box.x0, box.y1 - box.y0
    lean = abs(sin)  # the axis-aligned box of a tilted word: width*cos + height*sin wide, and the reverse high
    det = cos * cos - lean * lean
    width = (width_box * cos - height_box * lean) / det if det > 0.5 else width_box
    height = (height_box * cos - width_box * lean) / det if det > 0.5 else height_box
    width = width if width > 0.5 * width_box else width_box  # a degenerate box: keep what was measured
    height = height if height > 0.25 * height_box else height_box
    return along - width / 2, along + width / 2, down, height, " ".join(word.text.split())
