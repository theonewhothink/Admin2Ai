"""OCR contracts, layout heuristics and the engine registry (§13-16)."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from backoffice.domain.models import BoundingBox, ExtractionMethod
from backoffice.ocr import (
    ANY_LANGUAGE,
    EngineRegistry,
    FakeOCRProvider,
    LayoutSignals,
    OCRCapabilities,
    OCRInputError,
    OCRLine,
    OCRPage,
    OCRProviderInterface,
    OCRResult,
    OCRUnavailable,
    PageImage,
    UnknownEngineError,
    as_pages,
    count_pages,
    estimate_pdf_pages,
)
from backoffice.ocr.base import ensure_supported
from backoffice.ocr.layout import (
    count_tables,
    estimate_columns,
    geometry,
    infer_layout,
    median_skew,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
PDF_3 = b"%PDF-1.7\n1 0 obj << /Type /Pages /Count 3 >>\n" + b"<< /Type /Page >>\n" * 3


# --------------------------------------------------------------------------- inputs


def test_as_pages_numbers_pages_across_pdfs_and_images():
    pages = as_pages([PNG, PDF_3, PNG])
    assert [(p.mime_type, p.number) for p in pages] == [
        ("image/png", 1),
        ("application/pdf", 2),
        ("image/png", 5),
    ]
    assert count_pages(pages) == 5
    explicit = PageImage(data=PNG, mime_type="image/png", number=7)
    assert as_pages([explicit])[0] is explicit


def test_as_pages_refuses_unknown_content():
    with pytest.raises(OCRInputError):
        as_pages([b"plain text"])
    with pytest.raises(OCRInputError):
        as_pages([b""])
    with pytest.raises(OCRInputError):
        as_pages(["not bytes"])  # type: ignore[list-item]
    assert as_pages([bytearray(PNG)])[0].data == PNG
    assert as_pages([b"plain text"], default_mime="text/plain")[0].mime_type == "text/plain"
    with pytest.raises(ValueError):
        PageImage(data=b"", mime_type="image/png")
    with pytest.raises(ValueError):
        PageImage(data=PNG, mime_type="image/png", number=0)


def test_pdf_page_estimate_is_honest_about_unknowns():
    assert estimate_pdf_pages(PDF_3) == 3
    assert estimate_pdf_pages(b"%PDF-1.7 /ObjStm compressed") is None
    unknown = PageImage(data=b"%PDF-1.7 compressed", mime_type="application/pdf")
    assert unknown.pages is None
    assert count_pages([unknown]) == 1
    assert PageImage(data=b"%PDF", mime_type="application/pdf", page_count=40).pages == 40


def test_ensure_supported():
    pages = as_pages([PNG, PNG])
    ensure_supported(pages, engine="e")
    with pytest.raises(OCRInputError):
        ensure_supported(pages, engine="e", max_pages=1)
    with pytest.raises(OCRInputError):
        ensure_supported((), engine="e")
    with pytest.raises(OCRInputError):
        ensure_supported(as_pages([b"x"], default_mime="text/plain"), engine="e")


# --------------------------------------------------------------------------- results


def box(y: float, x0: float = 0.0, x1: float = 100.0) -> BoundingBox:
    return BoundingBox(page=1, x0=x0, y0=y, x1=x1, y1=y + 10)


def test_result_text_and_confidence():
    page1 = OCRPage(
        number=1,
        lines=(
            OCRLine(text="Total: 483,60", bbox=box(0), confidence=0.9),
            OCRLine(text="x", confidence=0.1),
            OCRLine(text="unscored"),
        ),
    )
    page2 = OCRPage(number=2, markdown="| a | b |")
    result = OCRResult(engine="e", model_version="1", method=ExtractionMethod.OCR, pages=(page1, page2))
    assert result.full_text == "Total: 483,60\nx\nunscored\n\n| a | b |"
    # Character-weighted: the long confident line dominates the one-letter doubt.
    assert result.mean_confidence == pytest.approx((13 * 0.9 + 1 * 0.1) / 14)
    assert result.low_confidence_share(0.5) == pytest.approx(1 / 14)
    assert result.page_count == 2
    blank = OCRResult(engine="e", model_version="1", method=ExtractionMethod.VLM, pages=(page2,))
    assert blank.mean_confidence is None and blank.low_confidence_share(0.5) is None


def test_results_are_immutable_and_validated():
    line = OCRLine(text="a", confidence=0.5)
    with pytest.raises(ValidationError):
        line.text = "b"
    with pytest.raises(ValidationError):
        OCRLine(text="a", confidence=1.5)
    with pytest.raises(ValidationError):
        OCRResult(engine="e", model_version="1", method=ExtractionMethod.OCR, pages=(), cost=Decimal("-1"))


def test_layout_signals_combine():
    combined = LayoutSignals.combine(
        [
            LayoutSignals(tables=1, skew_degrees=1.0),
            LayoutSignals(tables=2, columns=2, skew_degrees=-3.0, rotation=90),
        ]
    )
    assert combined == LayoutSignals(tables=3, columns=2, skew_degrees=-3.0, rotation=90)
    assert LayoutSignals.combine([]) == LayoutSignals()


def test_capabilities_languages():
    latin = OCRCapabilities(languages=frozenset({"pt", "en"}))
    assert latin.supports_any(["PT"]) and latin.supports_any([])
    assert not latin.supports_any(["he", "ar"])
    assert OCRCapabilities(languages=frozenset({ANY_LANGUAGE})).supports_language("he")


def test_errors_carry_codes_not_content():
    error = OCRUnavailable("pp-ocrv6-medium", "HTTP 503")
    assert error.retryable and error.code == "engine_unavailable"
    assert str(error) == "pp-ocrv6-medium: engine_unavailable (HTTP 503)"


# --------------------------------------------------------------------------- layout heuristics


def test_geometry_from_polygons_and_boxes():
    (b, angle) = geometry([[10, 20], [110, 20], [110, 40], [10, 40]])
    assert b == (10, 20, 110, 40) and angle == 0.0
    b, angle = geometry([0, 0, 100, 10, 100, 20, 0, 10])  # flat, tilted polygon
    assert b == (0, 0, 100, 20) and angle == pytest.approx(5.71, abs=0.01)
    assert geometry([5, 6, 50, 60]) == ((5, 6, 50, 60), None)
    assert geometry([5, 6, 1, 60]) == (None, None)  # x1 < x0
    assert geometry("nope") == (None, None)
    assert geometry([[0, 0], [0, 0], [0, 0]])[1] is None
    assert geometry([[0, 0], [0, 100], [10, 100], [10, 0]])[1] is None  # vertical text


def two_column_page() -> list[tuple[float, float, float, float]]:
    left = [(50, 100 + 30 * i, 280, 120 + 30 * i) for i in range(20)]
    right = [(320, 100 + 30 * i, 560, 120 + 30 * i) for i in range(20)]
    title = [(50, 40, 560, 70)]
    return title + left + right


def test_columns_need_real_gutters_running_down_the_page():
    assert estimate_columns(two_column_page()) == 2
    single = [(50, 100 + 30 * i, 500 - (i % 3) * 40, 120 + 30 * i) for i in range(20)]
    assert estimate_columns(single) == 1
    # A letterhead with two short blocks side by side is not a two-column page.
    letterhead = [(50, 40 + 20 * i, 200, 55 + 20 * i) for i in range(3)] + [
        (400, 40 + 20 * i, 560, 55 + 20 * i) for i in range(3)
    ]
    body = [(50, 200 + 30 * i, 560, 220 + 30 * i) for i in range(15)]
    assert estimate_columns(letterhead + body) == 1
    assert estimate_columns([(0, 0, 10, 10)]) == 1


def test_tables_are_runs_of_multi_cell_rows():
    rows = []
    for i in range(4):
        y = 300 + 25 * i
        rows += [(50, y, 250, y + 15), (300, y, 350, y + 15), (400, y, 450, y + 15), (500, y, 560, y + 15)]
    prose = [(50, 100 + 20 * i, 560, 115 + 20 * i) for i in range(5)]
    assert count_tables(prose + rows) == 1
    assert count_tables(prose) == 0
    two_rows = rows[:8]
    assert count_tables(two_rows) == 0


def test_skew_uses_wide_lines_only():
    boxes = [(0, 0, 500, 20), (0, 30, 500, 50), (0, 60, 10, 70)]
    assert median_skew([3.0, 4.0, 40.0], boxes, 600) == 3.5
    assert median_skew([None, None], [None, None], None) == 0.0
    signals = infer_layout(two_column_page(), [0.5] * 41, width=600, rotation=45)
    assert signals.columns == 2 and signals.inferred and signals.rotation == 0
    assert infer_layout([], [], tables=2).tables == 2


# --------------------------------------------------------------------------- registry


def test_registry_register_swap_and_lookup():
    first, second = FakeOCRProvider("pp-ocrv6-medium"), FakeOCRProvider("other")
    registry = EngineRegistry([first])
    assert isinstance(first, OCRProviderInterface)
    assert registry.get("pp-ocrv6-medium") is first
    with pytest.raises(ValueError):
        registry.register(FakeOCRProvider("pp-ocrv6-medium"))
    assert registry.replace("pp-ocrv6-medium", second) is first
    assert registry.get("pp-ocrv6-medium") is second
    registry.register(first, name="alias")
    assert registry.names() == ("pp-ocrv6-medium", "alias") and len(registry) == 2
    registry.unregister("alias")
    assert "alias" not in registry and registry.find("alias") is None
    with pytest.raises(UnknownEngineError):
        registry.get("alias")
    with pytest.raises(TypeError):
        registry.register(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        registry.register(first, name="  ")
