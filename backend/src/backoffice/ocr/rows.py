"""Text boxes joined into printed lines (§14, §18).

PP-OCR's detector returns one box per run of text, so a receipt line such as
``TOTAL            8,40 EUR`` comes back as two boxes, ``TOTAL`` and
``8,40 EUR``. Field readers work on lines ("a total is the amount on the line
that says total"), so the boxes of one printed line are joined left to right
before any field is read. The page may be photographed at a slight angle:
rows are found after undoing the page's median tilt, so a 4-degree photo
keeps "Total:" and "45,60 €" on one line.

Each joined line keeps the union of its boxes as its location and the lowest
score of its parts as its confidence (a line is only as sure as its weakest
part), and its parts themselves, left to right (``Row.members``): a table's
columns are read from where each part sits (``backoffice.reading.tables``).
Pure Python: the browser engine uses it too.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from .layout import Box, geometry

__all__ = ["Member", "Row", "TextBox", "join_rows", "normalize_text"]

# Full-width ASCII punctuation a multilingual recogniser sometimes returns ("ATCUD：..."), mapped back to ASCII.
# Only this block: a general NFKC fold would also turn "n.º" into "n.o".
_FULL_WIDTH = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_FULL_WIDTH[0x3000] = 0x20  # ideographic space


def normalize_text(text: str) -> str:
    """One recognised string, cleaned: full-width ASCII back to ASCII, runs of spaces to one."""
    return " ".join(text.translate(_FULL_WIDTH).split())


@dataclass(frozen=True)
class TextBox:
    """One recognised run of text: what it says, how sure the engine is, where it is (a polygon or a box)."""

    text: str
    confidence: float | None
    polygon: Sequence[Sequence[float]] | Sequence[float]


@dataclass(frozen=True)
class Member:
    """One part of a printed line as the engine found it: its text, box and score."""

    text: str
    box: Box
    confidence: float | None


@dataclass(frozen=True)
class Row:
    """One printed line: its parts joined left to right."""

    text: str
    box: Box | None
    confidence: float | None
    angle: float | None
    parts: int
    members: tuple[Member, ...] = ()


def join_rows(boxes: Sequence[TextBox]) -> list[Row]:
    """The boxes of each printed line joined, top to bottom (module docstring)."""
    items = []
    for item in boxes:
        text = normalize_text(item.text)
        box, angle = geometry(list(item.polygon))
        if not text or box is None:
            continue
        height = _height(item.polygon, box)
        items.append((text, box, angle, height, item.confidence))
    if not items:
        return []
    wide = [a for _, b, a, h, _ in items if a is not None and (b[2] - b[0]) >= 2 * h]
    tilt = math.radians(statistics.median(wide)) if wide else 0.0
    cos, sin = math.cos(tilt), math.sin(tilt)

    def level(box: Box) -> tuple[float, float]:
        """Centre of ``box`` in the page's own frame (tilt undone): (along the line, down the page)."""
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return cx * cos + cy * sin, -cx * sin + cy * cos

    placed = sorted(((level(b), t, b, a, h, c) for t, b, a, h, c in items), key=lambda x: (x[0][1], x[0][0]))
    rows: list[list[tuple]] = []
    for entry in placed:
        (_, down), _, _, _, height, _ = entry
        if rows:
            row = rows[-1]
            centre = statistics.fmean(e[0][1] for e in row)
            row_height = statistics.fmean(e[4] for e in row)
            if abs(down - centre) <= 0.5 * min(height, row_height):
                row.append(entry)
                continue
        rows.append([entry])
    out = []
    for row in rows:
        row.sort(key=lambda e: e[0][0])
        scores = [e[5] for e in row if e[5] is not None]
        union = (min(e[2][0] for e in row), min(e[2][1] for e in row),
                 max(e[2][2] for e in row), max(e[2][3] for e in row))
        angles = [e[3] for e in row if e[3] is not None]
        out.append(Row(
            text=" ".join(e[1] for e in row),
            box=union,
            confidence=min(scores) if len(scores) == len(row) else None,
            angle=round(statistics.median(angles), 2) if angles else None,
            parts=len(row),
            members=tuple(Member(text=e[1], box=e[2], confidence=e[5]) for e in row),
        ))
    return out


def _height(polygon: Sequence[Sequence[float]] | Sequence[float], box: Box) -> float:
    """The text height of a box: its left edge for a 4-point polygon, else the box's height."""
    points = list(polygon)
    if len(points) == 4 and all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in points):
        (x0, y0), (x3, y3) = points[0][:2], points[3][:2]
        side = math.hypot(x3 - x0, y3 - y0)
        if side > 0:
            return float(side)
    return max(1.0, box[3] - box[1])
