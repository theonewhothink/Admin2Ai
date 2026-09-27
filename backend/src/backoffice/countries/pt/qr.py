"""Portuguese AT fiscal invoice QR code (Portaria 195/2020; §13 Stage 0, §19).

Payload: fields "KEY:value" separated by "*", in the fixed order
A B C D E F G H I1..I8 J1..J8 K1..K8 L M N O P Q R S. Fields without content
are left out. Amounts use "." as decimal separator with two decimals.

    A issuer NIF            B buyer tax id (999999990 = final consumer)
    C buyer country         D document type (SAF-T code)
    E document status       F document date (YYYYMMDD)
    G document number       H ATCUD ("0" when not applicable)
    I/J/K 1 fiscal space (PT, PT-AC, PT-MA or a foreign country; I1 may be "0"
            when the document carries no tax detail)
          2 exempt base, 3/4 reduced base/VAT, 5/6 intermediate base/VAT,
          7/8 normal base/VAT
    L not subject / non-taxable  M stamp duty  N total taxes  O gross total
    P withholding  Q 4 hash characters  R software certificate number
    S other information (free text)

Arithmetic (checked against the AT's published example):
    N = sum(VAT fields) + M
    O = sum(base fields) + L + N

Malformed payloads raise QRCodeError listing every problem. A well-formed
code whose arithmetic does not add up is returned with ``is_consistent``
False: its values are still reported so the disagreement becomes a CONFLICT
downstream instead of disappearing (§19).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation

from backoffice.countries.base import FiscalQRError, NamedObservation, VATBucket
from backoffice.domain.models import CriticalField, DocumentType, ExtractionMethod

from .atcud import (
    ATCUD,
    ATCUD_MANDATORY_FROM,
    ATCUD_NOT_APPLICABLE,
    ATCUDError,
    DocumentNumber,
    parse_atcud,
    parse_document_number,
)
from .documents import CANCELLED_STATUS, DOCUMENT_STATUSES, get_document_type
from .nif import FINAL_CONSUMER_NIF, validate_nif
from .vat import PTRegion, RateCheck, check_rate

# --------------------------------------------------------------------------- #
# Specification tables
# --------------------------------------------------------------------------- #

BLOCKS = ("I", "J", "K")
FIELD_ORDER: tuple[str, ...] = (
    "A", "B", "C", "D", "E", "F", "G", "H",
    *(f"{b}{i}" for b in BLOCKS for i in range(1, 9)),
    "L", "M", "N", "O", "P", "Q", "R", "S",
)
MANDATORY_FIELDS = frozenset({"A", "B", "C", "D", "E", "F", "G", "H", "I1", "N", "O", "Q", "R"})

# Maximum lengths per the AT technical specification (as implemented by
# certified open-source generators). S is free text and not length-checked
# here: published sources disagree on its limit.
MAX_LENGTH: Mapping[str, int] = {
    "A": 9, "B": 30, "C": 12, "D": 2, "E": 1, "F": 8, "G": 60, "H": 70,
    **{f"{b}1": 5 for b in BLOCKS},
    **{f"{b}{i}": 16 for b in BLOCKS for i in range(2, 9)},
    "L": 16, "M": 16, "N": 16, "O": 16, "P": 16, "Q": 4, "R": 4,
}
AMOUNT_FIELDS = frozenset(
    {f"{b}{i}" for b in BLOCKS for i in range(2, 9)} | {"L", "M", "N", "O", "P"}
)
UNKNOWN_COUNTRY = "Desconhecido"
NO_TAX_SPACE = "0"
ALL_STATUSES = frozenset(s for statuses in DOCUMENT_STATUSES.values() for s in statuses)

# Per-rate pairs inside a block: (base index, VAT index, bucket).
_RATE_PAIRS = ((3, 4, VATBucket.REDUCED), (5, 6, VATBucket.INTERMEDIATE), (7, 8, VATBucket.NORMAL))

_AMOUNT = re.compile(r"^[0-9]{1,13}\.[0-9]{2}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_SPACE = re.compile(r"^(?:[A-Z]{2}|PT-AC|PT-MA)$")
_HASH = re.compile(r"^[A-Za-z0-9+/=]{4}$")
_NIF_SHAPE = re.compile(r"^[0-9]{9}$")
_CERT = re.compile(r"^[0-9]{1,4}$")
_DATE = re.compile(r"^[0-9]{8}$")

# Rounding allowance for the totals checks: per-rate subtotals are rounded
# separately from the document totals, so they can drift by a cent or two.
DEFAULT_TOLERANCE = Decimal("0.02")

# Confidence of QR-read values. Amounts drop sharply when the code's own
# arithmetic fails, but are still reported so the conflict is visible.
QR_CONFIDENCE = 0.98
QR_INCONSISTENT_CONFIDENCE = 0.40
ARITHMETIC_CONFIDENCE = 0.95


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class QRIssue:
    """One problem or remark about a QR code. ``code`` is stable; ``detail`` is technical."""

    code: str
    field: str | None
    detail: str

    def __str__(self) -> str:
        where = f"{self.field}: " if self.field else ""
        return f"{where}{self.detail}"


class QRCodeError(FiscalQRError):
    """The payload is not a well-formed AT fiscal QR code."""

    def __init__(self, issues: list[QRIssue]):
        self.issues: tuple[QRIssue, ...] = tuple(issues)
        super().__init__("; ".join(str(i) for i in self.issues))


@dataclass(frozen=True)
class QRTaxBlock:
    """One fiscal space (I, J or K block). Absent amounts are None."""

    block: str  # "I", "J" or "K"
    region: str  # "PT", "PT-AC", "PT-MA" or a foreign country code
    exempt_base: Decimal | None = None
    reduced_base: Decimal | None = None
    reduced_vat: Decimal | None = None
    intermediate_base: Decimal | None = None
    intermediate_vat: Decimal | None = None
    normal_base: Decimal | None = None
    normal_vat: Decimal | None = None

    def base_total(self) -> Decimal:
        return _sum(self.exempt_base, self.reduced_base, self.intermediate_base, self.normal_base)

    def vat_total(self) -> Decimal:
        return _sum(self.reduced_vat, self.intermediate_vat, self.normal_vat)

    def rate_pairs(self) -> tuple[tuple[VATBucket, Decimal, Decimal], ...]:
        """(bucket, base, VAT) for each rate with a base; missing VAT counts as zero."""
        pairs = (
            (VATBucket.REDUCED, self.reduced_base, self.reduced_vat),
            (VATBucket.INTERMEDIATE, self.intermediate_base, self.intermediate_vat),
            (VATBucket.NORMAL, self.normal_base, self.normal_vat),
        )
        return tuple((b, base, vat or Decimal("0.00")) for b, base, vat in pairs if base is not None)

    def present_fields(self) -> tuple[str, ...]:
        values = (self.exempt_base, self.reduced_base, self.reduced_vat, self.intermediate_base,
                  self.intermediate_vat, self.normal_base, self.normal_vat)
        return tuple(f"{self.block}{i}" for i, v in enumerate(values, start=2) if v is not None)


@dataclass(frozen=True)
class QRChecks:
    """Internal consistency of a well-formed code."""

    tax_total_expected: Decimal
    tax_total_ok: bool
    gross_total_expected: Decimal | None  # None: no breakdown to check against
    gross_total_ok: bool | None
    atcud_matches_number: bool | None  # None: no ATCUD
    warnings: tuple[QRIssue, ...] = ()

    @property
    def consistent(self) -> bool:
        return (
            self.tax_total_ok
            and self.gross_total_ok is not False
            and self.atcud_matches_number is not False
        )


@dataclass(frozen=True)
class PTQRCode:
    """A parsed, structurally valid AT fiscal QR code."""

    issuer_nif: str
    buyer_tax_id: str
    buyer_country: str
    doc_type_code: str
    status: str
    issue_date: date
    document_number: DocumentNumber
    atcud: ATCUD | None
    tax_blocks: tuple[QRTaxBlock, ...]
    non_taxable: Decimal | None  # L
    stamp_duty: Decimal | None  # M
    tax_total: Decimal  # N
    gross_total: Decimal  # O
    withholding: Decimal | None  # P
    hash_chars: str  # Q
    certificate_number: str  # R
    other_info: str | None  # S
    raw: str
    checks: QRChecks = field(compare=False)

    # -- derived values ---------------------------------------------------- #

    @property
    def has_tax_detail(self) -> bool:
        """True when the code breaks the total down (blocks with amounts, L or M)."""
        return any(b.present_fields() for b in self.tax_blocks) or (
            self.non_taxable is not None or self.stamp_duty is not None
        )

    @property
    def net_total(self) -> Decimal:
        """All bases (exempt + taxable, every fiscal space) plus non-taxable L."""
        return _sum(*(b.base_total() for b in self.tax_blocks), self.non_taxable)

    @property
    def vat_total(self) -> Decimal:
        """VAT only (stamp duty excluded)."""
        return _sum(*(b.vat_total() for b in self.tax_blocks))

    @property
    def amount_payable(self) -> Decimal:
        """Gross total minus withholding: what the buyer actually pays."""
        return self.gross_total - (self.withholding or Decimal("0.00"))

    @property
    def is_consistent(self) -> bool:
        return self.checks.consistent

    @property
    def is_cancelled(self) -> bool:
        return self.status == CANCELLED_STATUS

    @property
    def buyer_is_final_consumer(self) -> bool:
        return self.buyer_tax_id == FINAL_CONSUMER_NIF

    @property
    def doc_type(self) -> DocumentType:
        entry = get_document_type(self.doc_type_code)
        return entry.doc_type if entry else DocumentType.OTHER

    @property
    def invoice_number(self) -> str:
        return str(self.document_number)

    @property
    def currency(self) -> str:
        """QR amounts are the document's euro totals (SAF-T PT values are in EUR)."""
        return "EUR"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def looks_like_pt_qr(payload: str) -> bool:
    """Cheap sniff: does this payload claim to be an AT fiscal QR code?"""
    if not isinstance(payload, str):
        return False
    text = payload.strip().lstrip("﻿")
    return text.startswith("A:") and "*B:" in text


def parse_qr(
    payload: str,
    *,
    tolerance: Decimal = DEFAULT_TOLERANCE,
    require_consistent: bool = False,
) -> PTQRCode:
    """Parse and validate an AT fiscal QR payload.

    Raises QRCodeError for any structural, format or mandatory-field problem
    (all problems are reported at once). With ``require_consistent`` an
    arithmetic or ATCUD inconsistency also raises.
    """
    if not isinstance(payload, str):
        raise QRCodeError([QRIssue("not_text", None, "payload must be text")])
    raw = payload.strip().lstrip("﻿").strip()
    fields = _split_fields(raw)
    issues = _validate_fields(fields)
    if issues:
        raise QRCodeError(issues)
    code = _build(fields, raw, tolerance)
    if require_consistent and not code.is_consistent:
        raise QRCodeError(
            [w for w in code.checks.warnings if w.code in _CONSISTENCY_CODES]
            or [QRIssue("inconsistent", None, "the code's totals do not add up")]
        )
    return code


def _split_fields(raw: str) -> dict[str, str]:
    """Split into an ordered KEY -> value dict, raising on structural errors."""
    if not raw:
        raise QRCodeError([QRIssue("empty", None, "payload is empty")])
    issues: list[QRIssue] = []
    fields: dict[str, str] = {}
    last_index = -1
    for position, segment in enumerate(raw.split("*"), start=1):
        key, sep, value = segment.partition(":")
        if not sep:
            issues.append(QRIssue("syntax", None, f"segment {position} has no 'KEY:value' form"))
            continue
        if key not in FIELD_ORDER:
            issues.append(QRIssue("unknown_field", key or None, "unknown field"))
            continue
        if key in fields:
            issues.append(QRIssue("duplicate_field", key, "field appears more than once"))
            continue
        index = FIELD_ORDER.index(key)
        if index < last_index:
            issues.append(QRIssue("order", key, "field is out of the specified order"))
        last_index = max(last_index, index)
        if value == "":
            issues.append(QRIssue("empty_field", key, "fields without content must be omitted"))
        elif len(value) > MAX_LENGTH.get(key, len(value)):
            issues.append(QRIssue("too_long", key, f"longer than {MAX_LENGTH[key]} characters"))
        fields[key] = value
    missing = sorted(MANDATORY_FIELDS - fields.keys(), key=FIELD_ORDER.index)
    issues.extend(QRIssue("missing_field", key, "mandatory field is missing") for key in missing)
    if issues:
        raise QRCodeError(issues)
    return fields


def _validate_fields(fields: Mapping[str, str]) -> list[QRIssue]:
    """Format and cross-field rules; returns every problem found."""
    issues: list[QRIssue] = []
    issues += _check_parties(fields)
    issues += _check_document(fields)
    issues += _check_blocks(fields)
    for key in sorted(AMOUNT_FIELDS & fields.keys(), key=FIELD_ORDER.index):
        if not _AMOUNT.match(fields[key]):
            issues.append(QRIssue("amount_format", key, "amount must be digits with 2 decimals, '.' separator"))
    if not (_HASH.match(fields["Q"]) or fields["Q"] == "0"):
        issues.append(QRIssue("hash_format", "Q", "must be 4 hash characters"))
    if not _CERT.match(fields["R"]):
        issues.append(QRIssue("certificate_format", "R", "certificate number must be 1-4 digits"))
    return issues


def _check_parties(fields: Mapping[str, str]) -> list[QRIssue]:
    issues: list[QRIssue] = []
    issuer = fields["A"]
    if not _NIF_SHAPE.match(issuer) or not validate_nif(issuer).valid:
        issues.append(QRIssue("issuer_nif", "A", "issuer NIF is not a valid Portuguese NIF"))
    country = fields["C"]
    if not (_COUNTRY.match(country) or country == UNKNOWN_COUNTRY):
        issues.append(QRIssue("country_format", "C", "buyer country must be ISO alpha-2 or 'Desconhecido'"))
    buyer = fields["B"]
    if country == "PT":
        check = validate_nif(buyer, allow_placeholder=True)
        if not (_NIF_SHAPE.match(buyer) and check.valid):
            issues.append(QRIssue("buyer_nif", "B", "Portuguese buyer NIF is not valid"))
    elif not buyer.isprintable() or buyer.strip() != buyer:
        issues.append(QRIssue("buyer_format", "B", "buyer tax id has invalid characters"))
    return issues


def _check_document(fields: Mapping[str, str]) -> list[QRIssue]:
    issues: list[QRIssue] = []
    if get_document_type(fields["D"]) is None or fields["D"] != fields["D"].upper():
        issues.append(QRIssue("doc_type", "D", "unknown document type code"))
    if fields["E"] not in ALL_STATUSES:
        issues.append(QRIssue("status", "E", "unknown document status"))
    if _parse_date(fields["F"]) is None:
        issues.append(QRIssue("date_format", "F", "date must be a real date as YYYYMMDD"))
    try:
        parse_document_number(fields["G"])
    except ATCUDError:
        issues.append(QRIssue("document_number", "G", "must look like '<code> <series>/<number>'"))
    if fields["H"] != ATCUD_NOT_APPLICABLE:
        try:
            parse_atcud(fields["H"])
        except ATCUDError:
            issues.append(QRIssue("atcud_format", "H", "ATCUD must be '<validation code>-<number>' or '0'"))
    return issues


def _check_blocks(fields: Mapping[str, str]) -> list[QRIssue]:
    issues: list[QRIssue] = []
    seen_regions: set[str] = set()
    for block in BLOCKS:
        space_key = f"{block}1"
        present = [f"{block}{i}" for i in range(2, 9) if f"{block}{i}" in fields]
        space = fields.get(space_key)
        if space is None:
            if present:
                issues.append(QRIssue("block_without_space", present[0], f"{space_key} is required"))
            continue
        if space == NO_TAX_SPACE:
            if block != "I":
                issues.append(QRIssue("space_format", space_key, "only I1 may be '0'"))
            elif present or any(f"{b}1" in fields for b in BLOCKS[1:]):
                issues.append(QRIssue("no_tax_with_detail", "I1", "'0' means no tax detail may follow"))
            continue
        if not _SPACE.match(space):
            issues.append(QRIssue("space_format", space_key, "fiscal space must be PT, PT-AC, PT-MA or a country code"))
        elif space in seen_regions:
            issues.append(QRIssue("duplicate_space", space_key, f"fiscal space {space} repeated"))
        seen_regions.add(space)
        for base_i, vat_i, _ in _RATE_PAIRS:
            if f"{block}{vat_i}" in fields and f"{block}{base_i}" not in fields:
                issues.append(QRIssue("vat_without_base", f"{block}{vat_i}", "VAT given without its base"))
    return issues


def _build(fields: Mapping[str, str], raw: str, tolerance: Decimal) -> PTQRCode:
    blocks = tuple(
        _build_block(block, fields)
        for block in BLOCKS
        if fields.get(f"{block}1") not in (None, NO_TAX_SPACE)
    )
    atcud = None if fields["H"] == ATCUD_NOT_APPLICABLE else parse_atcud(fields["H"])
    code = PTQRCode(
        issuer_nif=fields["A"],
        buyer_tax_id=fields["B"],
        buyer_country=fields["C"],
        doc_type_code=fields["D"],
        status=fields["E"],
        issue_date=_required_date(fields["F"]),
        document_number=parse_document_number(fields["G"]),
        atcud=atcud,
        tax_blocks=blocks,
        non_taxable=_amount(fields, "L"),
        stamp_duty=_amount(fields, "M"),
        tax_total=_required_amount(fields, "N"),
        gross_total=_required_amount(fields, "O"),
        withholding=_amount(fields, "P"),
        hash_chars=fields["Q"],
        certificate_number=fields["R"],
        other_info=fields.get("S"),
        raw=raw,
        checks=_UNCHECKED,
    )
    return replace(code, checks=_compute_checks(code, tolerance))


def _build_block(block: str, fields: Mapping[str, str]) -> QRTaxBlock:
    values = [_amount(fields, f"{block}{i}") for i in range(2, 9)]
    return QRTaxBlock(block, fields[f"{block}1"], *values)


# --------------------------------------------------------------------------- #
# Consistency checks
# --------------------------------------------------------------------------- #

_CONSISTENCY_CODES = frozenset({"tax_total_mismatch", "gross_total_mismatch", "atcud_sequence_mismatch"})
# Placeholder while a code is being built; replaced before it is returned.
_UNCHECKED = QRChecks(Decimal("0.00"), True, None, None, None)


def _compute_checks(code: PTQRCode, tolerance: Decimal) -> QRChecks:
    warnings: list[QRIssue] = []
    stamp = code.stamp_duty or Decimal("0.00")

    tax_expected = code.vat_total + stamp
    tax_ok = abs(code.tax_total - tax_expected) <= tolerance
    if not tax_ok:
        warnings.append(QRIssue("tax_total_mismatch", "N",
                                f"N {code.tax_total} but VAT fields + M add up to {tax_expected}"))

    gross_expected: Decimal | None = None
    gross_ok: bool | None = None
    if code.has_tax_detail:
        gross_expected = code.net_total + code.vat_total + stamp
        gross_ok = abs(code.gross_total - gross_expected) <= tolerance
        if not gross_ok:
            warnings.append(QRIssue("gross_total_mismatch", "O",
                                    f"O {code.gross_total} but bases + L + taxes add up to {gross_expected}"))

    atcud_ok: bool | None = None
    if code.atcud is not None:
        atcud_ok = code.atcud.sequence == code.document_number.number
        if not atcud_ok:
            warnings.append(QRIssue("atcud_sequence_mismatch", "H",
                                    "ATCUD sequence differs from the document number"))

    warnings.extend(_remarks(code))
    return QRChecks(tax_expected, tax_ok, gross_expected, gross_ok, atcud_ok, tuple(warnings))


def _remarks(code: PTQRCode) -> list[QRIssue]:
    """Non-blocking observations: they never change a value."""
    remarks: list[QRIssue] = []
    if code.atcud is None and code.issue_date >= ATCUD_MANDATORY_FROM:
        remarks.append(QRIssue("atcud_missing", "H",
                               f"no ATCUD on a document dated {code.issue_date.isoformat()}"))
    if code.hash_chars == "0":
        remarks.append(QRIssue("hash_missing", "Q", "no hash characters"))
    entry = get_document_type(code.doc_type_code)
    if entry is not None and code.status not in DOCUMENT_STATUSES[entry.family]:
        remarks.append(QRIssue("status_family", "E",
                               f"status {code.status} is unusual for {code.doc_type_code}"))
    if code.is_cancelled:
        remarks.append(QRIssue("cancelled", "E", "the document is cancelled"))
    remarks.extend(_rate_remarks(code))
    return remarks


def _rate_remarks(code: PTQRCode) -> list[QRIssue]:
    remarks: list[QRIssue] = []
    for block in code.tax_blocks:
        try:
            region = PTRegion.parse(block.region)
        except ValueError:
            continue  # foreign fiscal space: no rate table
        for bucket, base, vat in block.rate_pairs():
            result = check_rate(base, vat, region, code.issue_date, bucket=bucket)
            if result is RateCheck.IMPLAUSIBLE:
                remarks.append(QRIssue("rate_implausible", block.block,
                                       f"{bucket.value} VAT {vat} on {base} does not match the {region.value} rate"))
            elif result is RateCheck.UNKNOWN:
                remarks.append(QRIssue("rate_unknown", block.block,
                                       f"no {region.value} rate data for {code.issue_date.isoformat()}"))
    return remarks


# --------------------------------------------------------------------------- #
# Observations (§18)
# --------------------------------------------------------------------------- #


def qr_to_observations(
    code: PTQRCode,
    evidence_id: str,
    *,
    include_arithmetic: bool = True,
) -> list[NamedObservation]:
    """Field observations read from the QR code (method=QR).

    Net and VAT are only reported when the code carries a tax breakdown. With
    ``include_arithmetic`` a gross total recomputed from the code's own parts
    is added (method=ARITHMETIC, same source): when the code is internally
    inconsistent it disagrees with field O, so verification sees a CONFLICT.
    Note both come from one evidence item; independence must be judged by
    ``source``, not by ``method``.
    """
    ids = QR_CONFIDENCE
    amounts = QR_CONFIDENCE if code.is_consistent else QR_INCONSISTENT_CONFIDENCE

    def qr(field_: CriticalField, value: object, where: str, confidence: float) -> NamedObservation:
        return NamedObservation(field=field_, value=value, source=evidence_id,
                                method=ExtractionMethod.QR, confidence=confidence,
                                location=f"qr:{where}")

    observations = [
        qr(CriticalField.SUPPLIER_TAX_ID, code.issuer_nif, "A", ids),
        qr(CriticalField.CUSTOMER_TAX_ID, code.buyer_tax_id, "B", ids),
        qr(CriticalField.ISSUE_DATE, code.issue_date, "F", ids),
        qr(CriticalField.INVOICE_NUMBER, code.invoice_number, "G", ids),
        qr(CriticalField.GROSS_AMOUNT, code.gross_total, "O", amounts),
    ]
    if code.has_tax_detail:
        observations.append(qr(CriticalField.NET_AMOUNT, code.net_total, _net_location(code), amounts))
        observations.append(qr(CriticalField.VAT_AMOUNT, code.vat_total, _vat_location(code), amounts))
        if include_arithmetic:
            recomputed = code.net_total + code.vat_total + (code.stamp_duty or Decimal("0.00"))
            observations.append(NamedObservation(
                field=CriticalField.GROSS_AMOUNT, value=recomputed, source=evidence_id,
                method=ExtractionMethod.ARITHMETIC, confidence=ARITHMETIC_CONFIDENCE,
                location="qr:arithmetic(net+vat+M)",
            ))
    return observations


def _net_location(code: PTQRCode) -> str:
    keys = [k for b in code.tax_blocks for k in b.present_fields() if int(k[1]) in (2, 3, 5, 7)]
    if code.non_taxable is not None:
        keys.append("L")
    return "+".join(keys) or "none"


def _vat_location(code: PTQRCode) -> str:
    keys = [k for b in code.tax_blocks for k in b.present_fields() if int(k[1]) in (4, 6, 8)]
    return "+".join(keys) or "none"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _parse_date(text: str) -> date | None:
    if not _DATE.match(text):
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def _amount(fields: Mapping[str, str], key: str) -> Decimal | None:
    value = fields.get(key)
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:  # pragma: no cover - format validated first
        raise QRCodeError([QRIssue("amount_format", key, "not a number")]) from exc


def _required_amount(fields: Mapping[str, str], key: str) -> Decimal:
    value = _amount(fields, key)
    if value is None:  # pragma: no cover - mandatory fields are checked first
        raise QRCodeError([QRIssue("missing_field", key, "mandatory field is missing")])
    return value


def _required_date(text: str) -> date:
    value = _parse_date(text)
    if value is None:  # pragma: no cover - validated first
        raise QRCodeError([QRIssue("date_format", "F", "date must be a real date as YYYYMMDD")])
    return value


def _sum(*values: Decimal | None) -> Decimal:
    return sum((v for v in values if v is not None), Decimal("0.00"))
