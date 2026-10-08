"""Cheap image-quality signals (§11, §15, §17)."""

import struct
import zlib

import pytest

from backoffice.extraction import (
    ImageMetrics,
    QualityFlag,
    QualityThresholds,
    assess,
    assess_image,
    probe_image,
)
from backoffice.extraction.media import sniff_mime, sniff_text_format


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png(width=1240, height=1754, ppm: int | None = 11811) -> bytes:
    data = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    if ppm is not None:
        data += _chunk(b"pHYs", struct.pack(">IIB", ppm, ppm, 1))
    return data + _chunk(b"IDAT", b"\x00") + _chunk(b"IEND", b"")


def jpeg(width=1500, height=2000, orientation: int | None = 6, dpi=300) -> bytes:
    app0 = b"JFIF\x00\x01\x01" + struct.pack(">BHHBB", 1, dpi, dpi, 0, 0)
    out = b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", len(app0) + 2) + app0
    if orientation is not None:
        tiff = b"MM\x00*" + struct.pack(">I", 8) + struct.pack(">H", 1)
        tiff += struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0) + struct.pack(">I", 0)
        app1 = b"Exif\x00\x00" + tiff
        out += b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
    sof = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    out += b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
    return out + b"\xff\xda\x00\x02" + b"\x00" * 8 + b"\xff\xd9"


def tiff(width=2480, height=3508, orientation=3, dpi=300) -> bytes:
    entries = [(256, 3, width), (257, 3, height), (274, 3, orientation), (296, 3, 2)]
    ifd_offset = 8
    rational_offset = ifd_offset + 2 + 12 * (len(entries) + 1) + 4
    body = struct.pack("<H", len(entries) + 1)
    for tag, kind, value in entries:
        body += struct.pack("<HHIHH", tag, kind, 1, value, 0)
    body += struct.pack("<HHII", 282, 5, 1, rational_offset) + struct.pack("<I", 0)
    return b"II*\x00" + struct.pack("<I", ifd_offset) + body + struct.pack("<II", dpi, 1)


def test_sniffing_by_content():
    assert sniff_mime(png()) == "image/png"
    assert sniff_mime(jpeg()) == "image/jpeg"
    assert sniff_mime(tiff()) == "image/tiff"
    assert sniff_mime(b"junk\n%PDF-1.7") == "application/pdf"
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_mime(b"hello") is None
    assert sniff_text_format(b"\xef\xbb\xbf<?xml version='1.0'?><Invoice/>") == "application/xml"
    assert sniff_text_format(b"  <!DOCTYPE html><html>") == "text/html"
    assert sniff_text_format(b'{"a": 1}') == "application/json"
    assert sniff_text_format(b"plain words") is None


def test_probe_png_size_and_resolution():
    header = probe_image(png())
    assert (header.format, header.width, header.height) == ("image/png", 1240, 1754)
    assert header.dpi == pytest.approx(300, abs=0.1)
    assert probe_image(png(ppm=None)).dpi is None


def test_probe_jpeg_reads_exif_orientation():
    header = probe_image(jpeg())
    assert (header.width, header.height, header.dpi, header.orientation) == (1500, 2000, 300.0, 6)
    assert header.metrics().rotation_degrees == 90
    assert probe_image(jpeg(orientation=None)).orientation is None


def test_probe_tiff():
    header = probe_image(tiff())
    assert (header.width, header.height, header.orientation, header.dpi) == (2480, 3508, 3, 300.0)
    assert header.metrics().rotation_degrees == 180


def test_probe_truncated_or_unknown_images():
    truncated = probe_image(b"\x89PNG\r\n\x1a\n\x00")
    assert truncated.format == "image/png" and truncated.width is None
    assert probe_image(b"GIF89a....").format == "image/gif"
    assert probe_image(b"not an image") is None


def test_assess_flags_each_problem():
    report = assess(
        ImageMetrics(
            width=640,
            height=480,
            sharpness=40.0,
            glare_ratio=0.08,
            brightness=30.0,
            rotation_degrees=90,
            skew_degrees=-4.5,
        )
    )
    assert report.flags == {
        QualityFlag.BLURRY,
        QualityFlag.GLARE,
        QualityFlag.TOO_DARK,
        QualityFlag.ROTATED,
        QualityFlag.SKEWED,
        QualityFlag.LOW_RESOLUTION,
    }
    assert report.should_retake and report.needs_layout_engine and not report.ok
    assert report.owner_hint() == "The photo is blurry. Could you take it again, holding the phone still?"


def test_assess_good_image_and_unknown_metrics():
    report = assess(ImageMetrics(width=2000, height=3000, sharpness=450.0, glare_ratio=0.0, brightness=180.0))
    assert report.ok and report.owner_hint() is None
    assert report.unknown == {"rotation_degrees", "skew_degrees"}
    empty = assess(ImageMetrics())
    assert empty.ok  # nothing measured is not the same as good, and is recorded as unknown
    assert {"sharpness", "glare_ratio", "brightness", "size"} <= empty.unknown


def test_assess_overexposure_and_dpi_threshold():
    report = assess(ImageMetrics(brightness=250.0, dpi=96.0), QualityThresholds(min_dpi=150))
    assert report.flags == {QualityFlag.OVEREXPOSED, QualityFlag.LOW_RESOLUTION}
    assert "too bright" in report.owner_hint()
    assert assess(ImageMetrics(rotation_degrees=360)).flags == set()


def test_metrics_from_phone_json_and_header_merge():
    measured = ImageMetrics.from_mapping(
        {"sharpness": 55, "glareRatio": 0.01, "rotationDegrees": 0, "extra": 1}
    )
    assert measured == ImageMetrics(sharpness=55.0, glare_ratio=0.01, rotation_degrees=0)
    report = assess_image(jpeg(orientation=6), measured)
    # The phone said upright; the measured value wins over the EXIF header.
    assert report.metrics.rotation_degrees == 0
    assert report.metrics.width == 1500
    assert report.flags == {QualityFlag.BLURRY}
    assert QualityFlag.ROTATED in assess_image(jpeg(orientation=8)).flags


# --------------------------------------------------------------------------- pixels (edge sharpness)

PHOTOS = __import__("pathlib").Path(__file__).parent / "fixtures" / "photos"


def test_edge_sharpness_flags_only_the_badly_blurred_photo():
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from backoffice.extraction.quality import measure_pixels

    scores = {}
    for name in ("ft-fba2026-1207.jpg", "fs-pb2026-0441.jpg", "fs-mr2026-0088.jpg", "fs-mr2026-0088-blurred.jpg"):
        data = (PHOTOS / name).read_bytes()
        metrics = measure_pixels(data)
        assert metrics is not None and metrics.sharpness is None  # its own measure, not a Laplacian variance
        scores[name] = metrics.edge_sharpness
        flags = assess_image(data, metrics).flags
        assert (QualityFlag.BLURRY in flags) is (name == "fs-mr2026-0088-blurred.jpg"), (name, scores[name])
    assert scores["fs-pb2026-0441.jpg"] > scores["fs-mr2026-0088.jpg"] > scores["fs-mr2026-0088-blurred.jpg"]


def test_edge_sharpness_is_not_measured_on_a_blank_or_broken_image():
    pytest.importorskip("numpy")
    pil = pytest.importorskip("PIL.Image")
    import io

    from backoffice.extraction.quality import measure_pixels

    blank = io.BytesIO()
    pil.new("RGB", (800, 1000), (250, 250, 250)).save(blank, format="PNG")
    assert measure_pixels(blank.getvalue()) is None  # no print: never "blurry"
    assert measure_pixels(png()) is None  # a header with no pixels behind it
    assert measure_pixels(b"not an image") is None


def test_edge_sharpness_threshold_and_phone_metrics():
    assert QualityFlag.BLURRY in assess(ImageMetrics(edge_sharpness=0.2)).flags
    report = assess(ImageMetrics(edge_sharpness=0.9))
    assert QualityFlag.BLURRY not in report.flags and "sharpness" not in report.unknown
    assert "edge_sharpness" in assess(ImageMetrics(edge_sharpness=1.5)).unknown  # impossible: not measured
    assert ImageMetrics.from_mapping({"edgeSharpness": 0.4}).edge_sharpness == 0.4
    assert QualityFlag.BLURRY not in assess(ImageMetrics(edge_sharpness=0.2),
                                            QualityThresholds(min_edge_sharpness=0.1)).flags
