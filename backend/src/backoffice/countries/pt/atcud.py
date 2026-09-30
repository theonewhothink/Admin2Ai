"""ATCUD (Código Único de Documento) and fiscal document numbers (§50).

ATCUD = "<series validation code>-<sequential number>". The validation code is
assigned by the AT when the series is registered and has at least 8
characters; the sequential number is the document's number within the series.
Example: document "FT AB2019/0035" with ATCUD "CSDF7T5H-0035".

Fiscal document numbers follow the SAF-T (PT) pattern
"<internal code> <series>/<number>", e.g. "FT 2026/183" or "FR A/123".
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

from backoffice.countries.base import CountryPackError

# ATCUD is mandatory on invoices and other fiscal documents from this date
# (Decreto-Lei 28/2019 art. 35, after the transitional period that ended on
# 2022-12-31). Before it, "0" was printed/encoded instead.
# Source: AT FAQ "Séries/ATCUD" and software-vendor notices. verified_as_of: 2026-09-27.
ATCUD_MANDATORY_FROM = date(2023, 1, 1)

# Placeholder used (e.g. in QR field H) when no ATCUD applies.
ATCUD_NOT_APPLICABLE = "0"

# The AT spec says "at least 8 characters". Vendor notes describe the code as
# upper-case consonants and digits; we accept any upper-case alphanumerics so
# a narrower reading never rejects a real code.
_ATCUD = re.compile(r"^([A-Z0-9]{8,})-([0-9]+)$")
_ATCUD_MAX_LENGTH = 70  # QR field H limit

# An "ATCUD" label is only a label when a colon or a space follows it; a
# validation code may itself start with those letters.
_LABEL = re.compile(r"^ATCUD(?:\s*:\s*|\s+)", re.IGNORECASE)

# SAF-T (PT) InvoiceNo / DocumentNumber / PaymentRefNo pattern.
_DOC_NUMBER = re.compile(r"^([^ ]+) ([^/ ]+)/([0-9]+)$")


class ATCUDError(CountryPackError, ValueError):
    """Malformed ATCUD or document number."""

    owner_message = "The document's unique code doesn't look right, so I didn't use it."


@dataclass(frozen=True)
class ATCUD:
    validation_code: str
    sequence: int
    sequence_text: str  # as printed, leading zeros kept

    def __str__(self) -> str:
        return f"{self.validation_code}-{self.sequence_text}"


@dataclass(frozen=True)
class DocumentNumber:
    prefix: str  # internal document code chosen by the software (often FT, FR...)
    series: str
    number: int
    number_text: str

    def __str__(self) -> str:
        return f"{self.prefix} {self.series}/{self.number_text}"


def parse_atcud(raw: str) -> ATCUD:
    """Parse "CSDF7T5H-0035". Surrounding spaces and an "ATCUD:" label are ignored."""
    if not isinstance(raw, str):
        raise ATCUDError("ATCUD must be text")
    text = _LABEL.sub("", raw.strip(), count=1).strip()
    if len(text) > _ATCUD_MAX_LENGTH:
        raise ATCUDError(f"ATCUD longer than {_ATCUD_MAX_LENGTH} characters")
    if text == ATCUD_NOT_APPLICABLE:
        raise ATCUDError("ATCUD is the not-applicable placeholder '0'")
    match = _ATCUD.match(text)
    if match is None:
        raise ATCUDError(
            "ATCUD must be a validation code of at least 8 upper-case letters/digits, "
            "a hyphen and a sequential number"
        )
    code, seq = match.groups()
    return ATCUD(validation_code=code, sequence=int(seq), sequence_text=seq)


def is_valid_atcud(raw: str) -> bool:
    try:
        parse_atcud(raw)
    except ATCUDError:
        return False
    return True


def parse_document_number(raw: str) -> DocumentNumber:
    """Parse "FT 2026/183" (SAF-T pattern ``[^ ]+ [^/ ]+/[0-9]+``)."""
    if not isinstance(raw, str):
        raise ATCUDError("document number must be text")
    match = _DOC_NUMBER.match(raw.strip())
    if match is None:
        raise ATCUDError("document number must look like '<code> <series>/<number>'")
    prefix, series, number = match.groups()
    return DocumentNumber(prefix=prefix, series=series, number=int(number), number_text=number)


@dataclass(frozen=True)
class SeriesRegistration:
    """A document series as registered with the AT (known for own sales)."""

    validation_code: str
    series: str
    doc_type: str  # SAF-T code, e.g. "FT"
    first_number: int = 1


@dataclass(frozen=True)
class ATCUDLink:
    """How an ATCUD relates to its document number and registered series."""

    atcud: ATCUD
    document: DocumentNumber
    series: SeriesRegistration | None
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


def link_atcud(
    atcud: ATCUD,
    document: DocumentNumber,
    registry: Mapping[str, SeriesRegistration] | Iterable[SeriesRegistration] = (),
    *,
    doc_type: str | None = None,
) -> ATCUDLink:
    """Check that an ATCUD belongs to a document and, if known, to its series.

    ``registry`` maps validation codes to registrations (or is an iterable of
    registrations). An unknown code is only a problem when a registry is given.
    """
    problems: list[str] = []
    if atcud.sequence != document.number:
        problems.append(
            f"ATCUD sequence {atcud.sequence_text} does not match document number "
            f"{document.number_text}"
        )
    by_code = _index(registry)
    series = by_code.get(atcud.validation_code)
    if by_code and series is None:
        problems.append("ATCUD validation code is not a registered series")
    if series is not None:
        if series.series != document.series:
            problems.append(
                f"series '{document.series}' does not match registered series '{series.series}'"
            )
        if doc_type is not None and series.doc_type != doc_type:
            problems.append(
                f"document type {doc_type} does not match registered type {series.doc_type}"
            )
        if document.number < series.first_number:
            problems.append("document number is below the series' first number")
    return ATCUDLink(atcud=atcud, document=document, series=series, problems=tuple(problems))


def _index(
    registry: Mapping[str, SeriesRegistration] | Iterable[SeriesRegistration],
) -> dict[str, SeriesRegistration]:
    if isinstance(registry, Mapping):
        return dict(registry)
    return {reg.validation_code: reg for reg in registry}
