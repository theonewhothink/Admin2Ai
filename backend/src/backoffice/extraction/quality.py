"""Cheap image-quality signals for OCR routing and capture feedback (§11, §15, §17).

Three sources:

* **Precomputed metrics** from whoever looked at the pixels: the phone's
  capture pipeline measures blur, glare and brightness on-device (§11), the
  browser demo measures edge sharpness before it reads a photo. They arrive
  as :class:`ImageMetrics` (``from_mapping`` accepts snake_case or camelCase).
* **Header probing** (:func:`probe_image`): width, height, resolution and
  EXIF orientation read from PNG, JPEG and TIFF headers with the standard
  library only.
* **Pixel measurement** (:func:`measure_pixels`, needs the optional Pillow
  and numpy): edge sharpness, how steep the edges of the printed text are
  compared with their contrast. Scale- and light-independent, and not fooled
  by sensor noise the way the variance of the Laplacian is. A blank or
  textless image is "not measured", never "blurry".

:func:`assess` turns metrics into flags. Flags are data for routing
(complex-layout engine, §15) and for a calm retake request (§11); a missing
metric is recorded as unknown, never assumed good. So is an impossible one
(NaN, infinity, a negative sharpness, a glare share above 1): NaN compares
false with every threshold and would otherwise pass as "sharp".

The default thresholds are engineering starting points, not standards:
variance-of-Laplacian sharpness in particular depends on resolution and
device, so tune them against the golden dataset (§56).
"""

from __future__ import annotations

import math
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from enum import Enum
from typing import Any

from .media import MIME_JPEG, MIME_PNG, MIME_TIFF, sniff_mime

__all__ = [
    "ImageHeader",
    "DEFAULT_THRESHOLDS",
    "ImageMetrics",
    "QualityFlag",
    "QualityReport",
    "QualityThresholds",
    "assess",
    "assess_image",
    "edge_sharpness",
    "measure_pixels",
    "probe_image",
]


class QualityFlag(str, Enum):
    BLURRY = "blurry"
    GLARE = "glare"
    ROTATED = "rotated"
    SKEWED = "skewed"
    LOW_RESOLUTION = "low_resolution"
    TOO_DARK = "too_dark"
    OVEREXPOSED = "overexposed"


# Flags that mean "retaking the photo would help" (§11 glare warning, blur detection).
_RETAKE = frozenset({QualityFlag.BLURRY, QualityFlag.GLARE, QualityFlag.TOO_DARK, QualityFlag.OVEREXPOSED})

_OWNER_HINTS: Mapping[QualityFlag, str] = {
    QualityFlag.BLURRY: "The photo is blurry. Could you take it again, holding the phone still?",
    QualityFlag.GLARE: "There's a reflection on the photo. Could you take it again away from direct light?",
    QualityFlag.TOO_DARK: "The photo is too dark. Could you take it again with more light?",
    QualityFlag.OVEREXPOSED: "The photo is too bright. Could you take it again away from direct light?",
}


@dataclass(frozen=True)
class ImageMetrics:
    """Measurements of one page image. None means "not measured".

    ``sharpness``: variance of the Laplacian of the grayscale image (higher
    is sharper). ``glare_ratio``: share of pixels at or near white
    saturation (0-1). ``brightness``: mean luma (0-255).
    ``rotation_degrees``: how far the stored pixels are turned from upright
    (0, 90, 180, 270). ``skew_degrees``: residual tilt of text lines.
    ``edge_sharpness``: :func:`edge_sharpness` (0-1, higher is sharper).
    """

    width: int | None = None
    height: int | None = None
    dpi: float | None = None
    sharpness: float | None = None
    edge_sharpness: float | None = None
    glare_ratio: float | None = None
    brightness: float | None = None
    rotation_degrees: int | None = None
    skew_degrees: float | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ImageMetrics:
        """Build from a JSON-like mapping; unknown keys and unreadable values are ignored."""
        values: dict[str, Any] = {}
        for f in fields(cls):
            camel = "".join(p.capitalize() if i else p for i, p in enumerate(f.name.split("_")))
            raw = data.get(f.name, data.get(camel))
            if raw is None or isinstance(raw, bool):
                continue
            try:
                number = float(raw)
            except (TypeError, ValueError):
                continue  # "not measured", never a crash on a phone's odd payload
            if not math.isfinite(number):
                continue
            values[f.name] = int(number) if f.name in _INTEGER_METRICS else number
        return cls(**values)

    def sanitized(self) -> tuple[ImageMetrics, frozenset[str]]:
        """These metrics with impossible values removed, and the names removed."""
        bad = frozenset(
            name for name, valid in _VALID.items() if getattr(self, name) is not None and not valid(getattr(self, name))
        )
        return replace(self, **dict.fromkeys(bad)), bad

    def merged(self, other: ImageMetrics) -> ImageMetrics:
        """These metrics, with gaps filled from ``other``."""
        updates = {
            f.name: getattr(other, f.name)
            for f in fields(self)
            if getattr(self, f.name) is None and getattr(other, f.name) is not None
        }
        return replace(self, **updates)


_INTEGER_METRICS = frozenset({"width", "height", "rotation_degrees"})


def _finite(value: float) -> bool:
    return math.isfinite(value)


# Physically possible ranges; anything outside is a broken measurement.
_VALID: Mapping[str, Callable[[float], bool]] = {
    "width": lambda v: v > 0,
    "height": lambda v: v > 0,
    "dpi": lambda v: _finite(v) and v > 0,
    "sharpness": lambda v: _finite(v) and v >= 0,
    "edge_sharpness": lambda v: _finite(v) and 0 <= v <= 1,
    "glare_ratio": lambda v: _finite(v) and 0 <= v <= 1,
    "brightness": lambda v: _finite(v) and 0 <= v <= 255,
    "rotation_degrees": lambda v: True,
    "skew_degrees": _finite,
}


@dataclass(frozen=True)
class QualityThresholds:
    min_sharpness: float = 100.0
    # Edge sharpness under which print is too soft to read reliably. PP-OCRv6 on photographed receipts and
    # invoices blurred step by step: everything read down to about 0.5 (small print) or 0.35 (till receipts),
    # readings garbled around 0.3, nothing read at 0.22-0.3. A starting point to tune on the golden set (§56).
    min_edge_sharpness: float = 0.32
    max_glare_ratio: float = 0.02
    min_short_side_px: int = 900
    min_dpi: float = 150.0
    max_skew_degrees: float = 2.0
    min_brightness: float = 60.0
    max_brightness: float = 235.0


DEFAULT_THRESHOLDS = QualityThresholds()


@dataclass(frozen=True)
class QualityReport:
    flags: frozenset[QualityFlag]
    metrics: ImageMetrics
    unknown: frozenset[str]

    @property
    def ok(self) -> bool:
        return not self.flags

    @property
    def needs_layout_engine(self) -> bool:
        """Poor images go to the complex-document engine (§15)."""
        return bool(self.flags)

    @property
    def should_retake(self) -> bool:
        return bool(self.flags & _RETAKE)

    def owner_hint(self) -> str | None:
        """One calm sentence asking for a retake, or None (§11, §69)."""
        for flag in (QualityFlag.BLURRY, QualityFlag.GLARE, QualityFlag.TOO_DARK, QualityFlag.OVEREXPOSED):
            if flag in self.flags:
                return _OWNER_HINTS[flag]
        return None


def assess(metrics: ImageMetrics, thresholds: QualityThresholds = DEFAULT_THRESHOLDS) -> QualityReport:
    """Flags for every measured metric outside its threshold; impossible values are unknown."""
    t = thresholds
    m, impossible = metrics.sanitized()
    checks: dict[str, tuple[Any, QualityFlag, bool]] = {
        "sharpness": (
            m.sharpness, QualityFlag.BLURRY, m.sharpness is not None and m.sharpness < t.min_sharpness
        ),
        "edge_sharpness": (
            m.edge_sharpness, QualityFlag.BLURRY,
            m.edge_sharpness is not None and m.edge_sharpness < t.min_edge_sharpness,
        ),
        "glare_ratio": (
            m.glare_ratio, QualityFlag.GLARE, m.glare_ratio is not None and m.glare_ratio > t.max_glare_ratio
        ),
        "rotation_degrees": (
            m.rotation_degrees, QualityFlag.ROTATED, m.rotation_degrees is not None and m.rotation_degrees % 360 != 0
        ),
        "skew_degrees": (
            m.skew_degrees, QualityFlag.SKEWED, m.skew_degrees is not None and abs(m.skew_degrees) > t.max_skew_degrees
        ),
        "brightness": (
            m.brightness, QualityFlag.TOO_DARK, m.brightness is not None and m.brightness < t.min_brightness
        ),
    }
    flags = {flag for _, flag, bad in checks.values() if bad}
    unknown = {name for name, (value, _, _) in checks.items() if value is None} | impossible
    if m.sharpness is not None or m.edge_sharpness is not None:  # either measure of blur is enough
        unknown -= {"sharpness", "edge_sharpness"}
    if m.brightness is not None and m.brightness > t.max_brightness:
        flags.add(QualityFlag.OVEREXPOSED)
    if _low_resolution(m, t):
        flags.add(QualityFlag.LOW_RESOLUTION)
    if m.width is None or m.height is None:
        unknown.add("size")
    return QualityReport(frozenset(flags), m, frozenset(unknown))


def _low_resolution(m: ImageMetrics, t: QualityThresholds) -> bool:
    if m.width is not None and m.height is not None and min(m.width, m.height) < t.min_short_side_px:
        return True
    return m.dpi is not None and m.dpi < t.min_dpi


def assess_image(
    data: bytes,
    measured: ImageMetrics | None = None,
    thresholds: QualityThresholds = DEFAULT_THRESHOLDS,
) -> QualityReport:
    """Assess an image from its header, plus any measured pixel metrics."""
    header = probe_image(data)
    from_header = header.metrics() if header else ImageMetrics()
    metrics = (measured or ImageMetrics()).merged(from_header)
    return assess(metrics, thresholds)


# --------------------------------------------------------------------------- pixel measurement

# How edge sharpness is measured; the browser demo measures it the same way (web/lib/ocr.ts).
EDGE_SIDE = 600  # the image is first scaled down to this longer side (area average): noise and scale even out
EDGE_TILE = 16  # square tiles of this many pixels
EDGE_CONTRAST = 80.0  # a tile holds print when its 1st-99th percentile grey range is at least this
EDGE_MIN_TILES = 4  # fewer tiles with print: not measured


def edge_sharpness(gray: Any) -> float | None:
    """How sharp the print in a greyscale image is, 0-1 (higher is sharper), or None when there is no print.

    ``gray`` is a 2-D numpy array (0-255) already scaled to :data:`EDGE_SIDE`. For each tile with print, the
    steepest step between neighbouring pixels divided by the tile's contrast: a crisp edge drops from ink to
    paper in about one pixel (close to 1), a blurred one over many (close to 0). The score is the median of the
    sharpest quarter of tiles, so a page that is partly out of focus is judged by its best text.
    """
    import numpy as np

    a = np.asarray(gray, dtype=np.float32)
    t = EDGE_TILE
    th, tw = a.shape[0] // t, a.shape[1] // t
    if th == 0 or tw == 0:
        return None
    gx = np.zeros_like(a)
    gy = np.zeros_like(a)
    gx[:, :-1] = np.abs(np.diff(a, axis=1))
    gy[:-1, :] = np.abs(np.diff(a, axis=0))
    steep = np.maximum(gx, gy)[: th * t, : tw * t].reshape(th, t, tw, t).max(axis=(1, 3))
    tiles = a[: th * t, : tw * t].reshape(th, t, tw, t).transpose(0, 2, 1, 3).reshape(th, tw, t * t)
    low, high = np.percentile(tiles, [1, 99], axis=2)
    contrast = high - low
    printed = contrast >= EDGE_CONTRAST
    if int(printed.sum()) < EDGE_MIN_TILES:
        return None
    ratios = np.sort(np.minimum(steep[printed] / contrast[printed], 1.0))[::-1]
    best = ratios[: max(1, len(ratios) // 4)]
    return round(float(np.median(best)), 4)


def measure_pixels(data: bytes) -> ImageMetrics | None:
    """Edge sharpness of an image file (:func:`edge_sharpness`); None when Pillow or numpy is missing, the
    file is not an image they can open, or it holds no print."""
    import io

    from ._optional import MissingDependencyError, import_optional

    try:
        pil = import_optional("PIL.Image", feature="Measuring photo sharpness", package="Pillow")
        import_optional("numpy", feature="Measuring photo sharpness")
    except MissingDependencyError:
        return None
    try:
        with pil.open(io.BytesIO(data)) as image:
            image.draft("L", (EDGE_SIDE * 2, EDGE_SIDE * 2))  # JPEG: decode at a reduced size, much faster
            gray = image.convert("L")
            scale = EDGE_SIDE / max(gray.size)
            if scale < 1:
                gray = gray.resize((max(1, round(gray.width * scale)), max(1, round(gray.height * scale))),
                                   pil.Resampling.BOX)
            score = edge_sharpness(gray)
    except Exception:  # unreadable or truncated image: not measured
        return None
    return ImageMetrics(edge_sharpness=score) if score is not None else None


# --------------------------------------------------------------------------- header probing

# EXIF orientation (TIFF tag 274) -> clockwise rotation needed to display upright.
# Mirrored variants (2, 4, 5, 7) report the rotation part only.
_EXIF_ROTATION: Mapping[int, int] = {1: 0, 2: 0, 3: 180, 4: 180, 5: 90, 6: 90, 7: 270, 8: 270}


@dataclass(frozen=True)
class ImageHeader:
    format: str
    width: int | None = None
    height: int | None = None
    dpi: float | None = None
    orientation: int | None = None  # EXIF orientation 1-8

    def metrics(self) -> ImageMetrics:
        rotation = _EXIF_ROTATION.get(self.orientation) if self.orientation else None
        return ImageMetrics(width=self.width, height=self.height, dpi=self.dpi, rotation_degrees=rotation)


def probe_image(data: bytes) -> ImageHeader | None:
    """Size, resolution and orientation from PNG, JPEG or TIFF headers."""
    mime = sniff_mime(data)
    try:
        if mime == MIME_PNG:
            return _probe_png(data)
        if mime == MIME_JPEG:
            return _probe_jpeg(data)
        if mime == MIME_TIFF:
            return _probe_tiff(data)
    except (struct.error, IndexError, ValueError, ZeroDivisionError):
        return ImageHeader(format=mime or "unknown")
    return ImageHeader(format=mime) if mime else None


def _probe_png(data: bytes) -> ImageHeader:
    width, height = struct.unpack(">II", data[16:24])
    dpi = None
    pos = 8
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        if kind == b"pHYs" and length >= 9:
            ppu_x, _, unit = struct.unpack(">IIB", data[pos + 8 : pos + 17])
            if unit == 1:  # pixels per metre
                dpi = round(ppu_x * 0.0254, 1)
        if kind in (b"IDAT", b"IEND"):
            break
        pos += 12 + length
    return ImageHeader(format=MIME_PNG, width=width, height=height, dpi=dpi)


_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def _probe_jpeg(data: bytes) -> ImageHeader:
    width = height = orientation = None
    dpi = None
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker in (0x01, *range(0xD0, 0xD8)):  # standalone markers
            pos += 2
            continue
        if marker == 0xDA:  # start of scan: headers are over
            break
        (length,) = struct.unpack(">H", data[pos + 2 : pos + 4])
        segment = data[pos + 4 : pos + 2 + length]
        if marker in _SOF_MARKERS:
            height, width = struct.unpack(">HH", segment[1:5])
        elif marker == 0xE0 and segment.startswith(b"JFIF\x00"):
            units, x_density = segment[7], struct.unpack(">H", segment[8:10])[0]
            dpi = float(x_density) if units == 1 else round(x_density * 2.54, 1) if units == 2 else dpi
        elif marker == 0xE1 and segment.startswith(b"Exif\x00\x00"):
            orientation = _int_tag(_tiff_tags(segment[6:]).get(274)) or orientation
        pos += 2 + length
    return ImageHeader(format=MIME_JPEG, width=width, height=height, dpi=dpi, orientation=orientation)


def _probe_tiff(data: bytes) -> ImageHeader:
    tags = _tiff_tags(data)
    dpi = None
    x_res = tags.get(282)
    if isinstance(x_res, float):
        unit = tags.get(296, 2)
        dpi = round(x_res, 1) if unit == 2 else round(x_res * 2.54, 1) if unit == 3 else None
    return ImageHeader(
        format=MIME_TIFF,
        width=_int_tag(tags.get(256)),
        height=_int_tag(tags.get(257)),
        dpi=dpi,
        orientation=_int_tag(tags.get(274)),
    )


def _int_tag(value: Any) -> int | None:
    return value if isinstance(value, int) else None


# TIFF field types we read: SHORT (3), LONG (4), RATIONAL (5).
_TYPE_SIZES = {3: 2, 4: 4, 5: 8}


def _tiff_tags(blob: bytes) -> dict[int, int | float]:
    """First-IFD tags of a TIFF structure (also the body of a JPEG EXIF segment)."""
    order = {b"II": "<", b"MM": ">"}.get(blob[:2])
    if order is None:
        return {}
    (ifd,) = struct.unpack(order + "I", blob[4:8])
    (count,) = struct.unpack(order + "H", blob[ifd : ifd + 2])
    tags: dict[int, int | float] = {}
    for i in range(min(count, 512)):
        entry = blob[ifd + 2 + 12 * i : ifd + 14 + 12 * i]
        if len(entry) < 12:
            break
        tag, kind, n = struct.unpack(order + "HHI", entry[:8])
        size = _TYPE_SIZES.get(kind)
        if size is None or n != 1:
            continue
        if kind == 3:
            tags[tag] = struct.unpack(order + "H", entry[8:10])[0]
        elif kind == 4:
            tags[tag] = struct.unpack(order + "I", entry[8:12])[0]
        else:
            (offset,) = struct.unpack(order + "I", entry[8:12])
            num, den = struct.unpack(order + "II", blob[offset : offset + 8])
            if den:
                tags[tag] = num / den
    return tags
