"""Stage 0 extraction and cheap quality signals (§13, §17, §18).

Public API
----------
Structured evidence (Stage 0, outranks OCR):
    extract_structured(data, source=..., ...) -> tuple[Stage0Result, ...]
    parse_einvoice(xml, source=...)            UBL 2.1 / CII
    extract_html_structured(html, source=...)  schema.org JSON-LD + microdata
    JsonFieldMapper(fields).extract(payload, source=...)
    extract_from_pdf(pdf, source=..., text_extractor=...)   (optional pypdf)
    QRHook([...handlers]).extract(payloads, source=...), parse_epc_qr,
    fiscal_qr_handler(country_pack.parse_fiscal_qr), ZXingDecoder (optional)
    combine(results) -> FieldMap for the OCR router

Field plumbing:
    FieldMap, FieldExtractor (protocol), from_named_observations(fn),
    LabelledFieldExtractor (generic "Label: value" text extractor),
    ranked / best (METHOD_RANK ordering)

Values (§19, never guess):
    parse_amount -> Decimal | None, parse_date, comparison_key, typed_value,
    is_usable (parses for its field; IBAN passes mod-97),
    normalize_tax_id, normalize_iban, iban_is_valid, normalize_currency

Image quality (§11, §15):
    ImageMetrics, assess(metrics) -> QualityReport, assess_image(bytes),
    probe_image(bytes)
"""

from ._optional import MissingDependencyError, import_optional
from .fields import (
    FieldExtractor,
    FieldMap,
    Stage0Result,
    StructuredDataError,
    best,
    combine,
    from_named_observations,
    group_named,
    merge_field_maps,
    rank_key,
    ranked,
)
from .labelled import DEFAULT_LABELS, LabelledFieldExtractor
from .media import IMAGE_MIME_TYPES, OCR_INPUT_MIME_TYPES, sniff_mime, sniff_text_format
from .quality import (
    ImageHeader,
    ImageMetrics,
    QualityFlag,
    QualityReport,
    QualityThresholds,
    assess,
    assess_image,
    probe_image,
)
from .structured import (
    DecodedCode,
    FieldPath,
    JsonFieldMapper,
    PdfContent,
    QRHook,
    StructuredFormatError,
    UnsafeXMLError,
    XMLSyntaxError,
    ZXingDecoder,
    extract_from_pdf,
    extract_html_structured,
    extract_structured,
    fiscal_qr_handler,
    parse_einvoice,
    parse_epc_qr,
    parse_xml,
    read_pdf,
)
from .values import (
    comparison_key,
    iban_is_valid,
    is_usable,
    normalize_currency,
    normalize_iban,
    normalize_reference,
    normalize_tax_id,
    parse_amount,
    parse_date,
    typed_value,
)

__all__ = [
    "DEFAULT_LABELS",
    "IMAGE_MIME_TYPES",
    "OCR_INPUT_MIME_TYPES",
    "DecodedCode",
    "FieldExtractor",
    "FieldMap",
    "FieldPath",
    "ImageHeader",
    "ImageMetrics",
    "JsonFieldMapper",
    "LabelledFieldExtractor",
    "MissingDependencyError",
    "PdfContent",
    "QRHook",
    "QualityFlag",
    "QualityReport",
    "QualityThresholds",
    "Stage0Result",
    "StructuredDataError",
    "StructuredFormatError",
    "UnsafeXMLError",
    "XMLSyntaxError",
    "ZXingDecoder",
    "assess",
    "assess_image",
    "best",
    "combine",
    "comparison_key",
    "extract_from_pdf",
    "extract_html_structured",
    "extract_structured",
    "fiscal_qr_handler",
    "from_named_observations",
    "group_named",
    "iban_is_valid",
    "import_optional",
    "is_usable",
    "merge_field_maps",
    "normalize_currency",
    "normalize_iban",
    "normalize_reference",
    "normalize_tax_id",
    "parse_amount",
    "parse_date",
    "parse_einvoice",
    "parse_epc_qr",
    "parse_xml",
    "probe_image",
    "rank_key",
    "ranked",
    "read_pdf",
    "sniff_mime",
    "sniff_text_format",
    "typed_value",
]
