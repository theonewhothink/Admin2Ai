"""Layout signals from text-box geometry (§15, §17).

Line recognisers such as PP-OCR return text boxes, not a layout model. These
deterministic heuristics derive what the router needs to decide whether a
page deserves the complex-document engine:

* **skew**: median tilt of the top edge of reasonably wide text polygons;
* **columns**: vertical gutters that split the boxes into sides that each
  hold a real share of the text and run down a real share of the page, so a
  two-block letter head is not mistaken for columns;
* **tables**: runs of at least three consecutive rows that each hold three or
  more horizontally separate cells.

They err toward "complex": a false alarm costs one self-hosted VLM pass, a
miss can cost a wrong field (§17).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from itertools import pairwise
from typing import Any

from backoffice.domain.models import BoundingBox

from .base import LayoutSignals

__all__ = [
    "Box",
    "count_tables",
    "estimate_columns",
    "geometry",
    "infer_layout",
    "median_skew",
    "to_bbox",
]

Box = tuple[float, float, float, float]  # x0, y0, x1, y1

_MIN_BOXES_FOR_COLUMNS = 6
_MIN_TABLE_ROWS = 3
_MIN_TABLE_CELLS = 3
_MAX_COLUMNS = 4


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def geometry(raw: Any) -> tuple[Box | None, float | None]:
    """Box and top-edge angle (degrees) from a polygon or a flat box.

    Accepts ``[x0, y0, x1, y1]``, a flat 8-number polygon, or a list of
    ``[x, y]`` points ordered clockwise from the top-left corner (PaddleOCR).
    Anything malformed yields ``(None, None)``.
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        return None, None
    if all(isinstance(v, (list, tuple)) for v in raw):
        coords = [(_number(p[0]), _number(p[1])) if len(p) >= 2 else (None, None) for p in raw]
    else:
        flat = [_number(v) for v in raw]
        if len(flat) == 4:
            return _flat_box(flat), None
        coords = list(zip(flat[0::2], flat[1::2], strict=True)) if len(flat) % 2 == 0 else []
    points = [(x, y) for x, y in coords if x is not None and y is not None]
    if len(points) < 3 or len(points) != len(coords):
        return None, None
    xs, ys = [x for x, _ in points], [y for _, y in points]
    return (min(xs), min(ys), max(xs), max(ys)), _top_edge_angle(points[0], points[1])


def _flat_box(values: list[float | None]) -> Box | None:
    x0, y0, x1, y1 = values
    if x0 is None or y0 is None or x1 is None or y1 is None or x1 < x0 or y1 < y0:
        return None
    return x0, y0, x1, y1


def _top_edge_angle(a: tuple[float, float], b: tuple[float, float]) -> float | None:
    """Tilt of the edge a->b; None when degenerate or not roughly horizontal."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dy == 0:
        return None
    angle = math.degrees(math.atan2(dy, dx))
    return angle if -45.0 < angle <= 45.0 else None


def to_bbox(box: Box | None, page: int) -> BoundingBox | None:
    if box is None:
        return None
    x0, y0, x1, y1 = box
    return BoundingBox(page=page, x0=x0, y0=y0, x1=x1, y1=y1)


def median_skew(angles: Sequence[float | None], boxes: Sequence[Box | None], width: float | None) -> float:
    """Median tilt of wide-enough lines; 0.0 when nothing measurable."""
    extent = width or max((b[2] for b in boxes if b), default=0.0)
    min_width = max(20.0, 0.05 * extent)
    tilts = [
        a for a, b in zip(angles, boxes, strict=True) if a is not None and b is not None and (b[2] - b[0]) >= min_width
    ]
    return round(statistics.median(tilts), 2) if tilts else 0.0


def estimate_columns(boxes: Sequence[Box]) -> int:
    """Number of text columns separated by clean vertical gutters."""
    boxes = [b for b in boxes if b[2] > b[0] and b[3] > b[1]]
    n = len(boxes)
    if n < _MIN_BOXES_FOR_COLUMNS:
        return 1
    left, right = min(b[0] for b in boxes), max(b[2] for b in boxes)
    top, bottom = min(b[1] for b in boxes), max(b[3] for b in boxes)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return 1
    allowed_crossings = max(1, n // 20)  # a full-width title may cross a gutter
    free = [
        k
        for k in range(15, 86)
        if sum(1 for b in boxes if b[0] < left + k * width / 100 < b[2]) <= allowed_crossings
    ]
    partitions: set[int] = set()
    for run in _runs(free):
        gutter = left + (run[0] + run[-1]) / 2 * width / 100
        sides = ([b for b in boxes if b[2] <= gutter], [b for b in boxes if b[0] >= gutter])
        if all(_is_column(side, n, height) for side in sides) and _overlap(*sides) >= 0.5:
            partitions.add(len(sides[0]))
    return min(1 + len(partitions), _MAX_COLUMNS)


def _runs(positions: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for k in positions:
        if runs and k == runs[-1][-1] + 1:
            runs[-1].append(k)
        else:
            runs.append([k])
    return runs


def _span(boxes: Sequence[Box]) -> tuple[float, float]:
    return min(b[1] for b in boxes), max(b[3] for b in boxes)


def _is_column(side: Sequence[Box], total: int, height: float) -> bool:
    if len(side) < 0.25 * total:
        return False
    top, bottom = _span(side)
    return bottom - top >= 0.4 * height


def _overlap(a: Sequence[Box], b: Sequence[Box]) -> float:
    """Vertical overlap of two sides, relative to the shorter one."""
    (a0, a1), (b0, b1) = _span(a), _span(b)
    shorter = min(a1 - a0, b1 - b0)
    return max(0.0, min(a1, b1) - max(a0, b0)) / shorter if shorter > 0 else 0.0


def count_tables(boxes: Sequence[Box]) -> int:
    """Runs of three or more consecutive rows with three or more separate cells."""
    rows = _rows(boxes)
    tables = run = 0
    for row in rows:
        if _is_tabular(row):
            run += 1
            if run == _MIN_TABLE_ROWS:
                tables += 1
        else:
            run = 0
    return tables


def _rows(boxes: Sequence[Box]) -> list[list[Box]]:
    """Group boxes whose vertical centre falls inside the first box's band."""
    rows: list[list[Box]] = []
    band: tuple[float, float] | None = None
    for box in sorted(boxes, key=lambda b: ((b[1] + b[3]) / 2, b[0])):
        centre = (box[1] + box[3]) / 2
        if band is not None and band[0] <= centre <= band[1]:
            rows[-1].append(box)
        else:
            rows.append([box])
            band = (box[1], box[3])
    return rows


def _is_tabular(row: list[Box]) -> bool:
    if len(row) < _MIN_TABLE_CELLS:
        return False
    cells = sorted(row, key=lambda b: b[0])
    return all(nxt[0] >= cur[2] - 1.0 for cur, nxt in pairwise(cells))


def infer_layout(
    boxes: Sequence[Box | None],
    angles: Sequence[float | None],
    *,
    width: float | None = None,
    rotation: int = 0,
    tables: int | None = None,
) -> LayoutSignals:
    """Layout signals for one page from its line geometry.

    ``tables`` overrides the heuristic when the engine reported tables itself.
    """
    known = [b for b in boxes if b is not None]
    return LayoutSignals(
        tables=count_tables(known) if tables is None else tables,
        columns=estimate_columns(known),
        skew_degrees=median_skew(angles, boxes, width),
        rotation=rotation if rotation in (0, 90, 180, 270) else 0,
        inferred=True,
    )
