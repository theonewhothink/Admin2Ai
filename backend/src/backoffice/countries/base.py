"""Country-pack contract and registry (§49-50).

The core is global. Everything that differs by country (tax identifiers, VAT
rates, fiscal document types, terminology, fiscal QR codes, text conventions)
lives behind the :class:`CountryPack` protocol. The core asks the registry for
a pack and calls the protocol; it never branches on a country code.

    pack = get_pack("PT")
    pack.validate_tax_id("PT 123 456 789")

Built-in packs are imported lazily on first use, so importing this module
never pulls in any country.
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from backoffice.domain.models import (
    CriticalField,
    DocumentType,
    ExtractionMethod,
    FieldObservation,
)

# --------------------------------------------------------------------------- #
# Shared value types
# --------------------------------------------------------------------------- #


class NamedObservation(FieldObservation):
    """A FieldObservation that also names the critical field it observes (§18).

    It is a FieldObservation, so it can go anywhere one is expected; the extra
    ``field`` lets callers group observations without a side channel.
    """

    field: CriticalField


def group_by_field(
    observations: Iterable[NamedObservation],
) -> dict[CriticalField, list[FieldObservation]]:
    """Group named observations by field, preserving input order."""
    grouped: dict[CriticalField, list[FieldObservation]] = {}
    for obs in observations:
        grouped.setdefault(obs.field, []).append(obs)
    return grouped


class TaxIdKind(str, Enum):
    """Coarse, country-neutral hint about who holds a tax identifier."""

    PERSON = "person"
    SOLE_TRADER = "sole_trader"
    COMPANY = "company"
    PUBLIC_BODY = "public_body"
    NON_RESIDENT = "non_resident"
    OTHER_ENTITY = "other_entity"  # estates, funds, condominiums, ...
    PLACEHOLDER = "placeholder"  # generic "no tax id" numbers
    UNKNOWN = "unknown"

    @property
    def is_individual(self) -> bool:
        return self in (TaxIdKind.PERSON, TaxIdKind.SOLE_TRADER)


@dataclass(frozen=True)
class TaxIdCheck:
    """Outcome of validating a tax identifier.

    ``problem`` is a stable machine code; ``message`` is owner-facing plain
    language (§36, §70) and empty when the identifier is valid.
    """

    raw: str
    normalized: str | None
    valid: bool
    kind: TaxIdKind = TaxIdKind.UNKNOWN
    category: str | None = None
    problem: str | None = None
    message: str = ""


class DocumentFamily(str, Enum):
    """Groups of native fiscal document types."""

    INVOICE = "invoice"  # invoices, credit and debit notes
    PAYMENT = "payment"  # receipts
    MOVEMENT = "movement"  # delivery / transport documents
    WORKING = "working"  # quotes, pro-formas, orders, ...


@dataclass(frozen=True)
class NativeDocumentType:
    """A country's fiscal document type and how it maps to the core model."""

    code: str
    native_name: str
    english_name: str
    doc_type: DocumentType
    family: DocumentFamily
    fiscal_invoice: bool  # a valid tax invoice (supports input VAT)
    proves_payment: bool  # the document itself evidences payment
    legacy: bool = False


@dataclass(frozen=True)
class Term:
    """One entry of a country's document vocabulary."""

    native: str
    concept: str  # stable key, e.g. "gross_amount"
    english: str  # plain-language English
    aliases: tuple[str, ...] = ()


class VATBucket(str, Enum):
    ZERO = "zero"
    SUPER_REDUCED = "super_reduced"
    REDUCED = "reduced"
    INTERMEDIATE = "intermediate"
    NORMAL = "normal"


@dataclass(frozen=True)
class VATRate:
    """One dated VAT rate. ``valid_to`` is inclusive; None means still in force."""

    region: str
    bucket: VATBucket
    rate: Decimal  # e.g. Decimal("0.23")
    valid_from: date
    valid_to: date | None
    source: str
    verified_as_of: date

    def applies_on(self, day: date) -> bool:
        return self.valid_from <= day and (self.valid_to is None or day <= self.valid_to)


@dataclass(frozen=True)
class FiscalQRResult:
    """Country-neutral view of a parsed fiscal QR code (§13 Stage 0, §19).

    ``consistent`` is False when the code's own arithmetic does not add up; the
    observations are still returned so that the disagreement surfaces as a
    CONFLICT during verification instead of being hidden (§19).
    ``usable`` is False when the document cannot support a purchase or a
    payment: it was cancelled, or it is not a tax invoice or receipt (a
    pro-forma, quote or transport document also carries a fiscal QR code).
    Callers must not close anything on an unusable result (§3).
    ``notes`` are technical remarks for logs and audit, never owner copy;
    use ``CountryPackError.owner_message`` style text for the owner.

    ``native_doc_type`` is empty when the code does not say which kind of document it is on (the
    Spanish Verifactu code names only the issuer, number, date and total): the document's own
    words decide then. ``issuer_tax_id``, ``buyer_is_final_consumer``, ``currency`` and
    ``vat_parts`` (amounts by VAT rate, :class:`~backoffice.domain.models.VatPart`) are what the
    core reads besides the observations.
    """

    country: str
    native_doc_type: str
    doc_type: DocumentType
    observations: tuple[NamedObservation, ...]
    consistent: bool
    usable: bool
    notes: tuple[str, ...]
    payload: Any  # the country-specific typed object
    issuer_tax_id: str | None = None
    buyer_is_final_consumer: bool = False
    currency: str = "EUR"
    vat_parts: tuple[Any, ...] = ()


@dataclass(frozen=True)
class TextReading:
    """What a pack's text reader found in one text (country-neutral shape).

    ``unassigned_tax_ids`` are valid tax numbers whose role (supplier or customer) the text
    does not say; ``buyer_is_final_consumer`` is True when the buyer is the country's generic
    "no tax number" consumer.
    """

    observations: tuple[NamedObservation, ...]
    unassigned_tax_ids: tuple[str, ...] = ()
    buyer_is_final_consumer: bool = False


@dataclass(frozen=True)
class PeriodicObligation:
    """An obligation a country's calendar sets for a company, with no letter needed (e.g. a quarterly
    VAT return). ``key`` is stable per company and period, so the same obligation is created once."""

    key: str
    kind: str  # an ObligationKind value
    title: str
    period: str  # "2026-Q3"
    due_on: date
    responsible: str
    consequence: str
    required_evidence: str
    reasons: tuple[str, ...] = ()


class CountryPackError(Exception):
    """Base error for country packs. ``owner_message`` is safe to show."""

    owner_message = "I couldn't read this document reliably."


class FiscalQRError(CountryPackError, ValueError):
    """A payload claimed to be a fiscal QR code but is malformed."""

    owner_message = "The QR code on this document couldn't be read reliably, so I didn't use it."


class UnknownCountryError(CountryPackError, KeyError):
    """No pack is available for the requested country."""

    owner_message = "That country isn't supported yet."


# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #


@runtime_checkable
class CountryPack(Protocol):
    """Everything country-specific the core may need (§49-50)."""

    country_code: str  # ISO 3166-1 alpha-2, upper case
    country_name: str
    currency: str  # ISO 4217

    @property
    def document_types(self) -> Mapping[str, NativeDocumentType]: ...

    @property
    def terminology(self) -> Sequence[Term]: ...

    def normalize_tax_id(self, raw: str) -> str | None:
        """Canonical form of a tax id, or None when it cannot be one."""
        ...

    def validate_tax_id(self, raw: str) -> TaxIdCheck: ...

    def map_document_type(self, native_code: str) -> DocumentType | None:
        """Core document type for a native code; None when unknown."""
        ...

    def lookup_term(self, label: str) -> Term | None: ...

    def vat_rates(self, on: date, region: str | None = None) -> tuple[VATRate, ...]:
        """Rates in force on ``on`` (all regions when ``region`` is None).

        Empty for a region the pack does not cover.
        """
        ...

    def is_plausible_vat(
        self,
        net: Decimal,
        vat: Decimal,
        *,
        on: date | None = None,
        region: str | None = None,
    ) -> bool | None:
        """True/False when the rate table covers the region and date; None otherwise.

        Without ``on`` the rates the table currently lists are used; the answer
        never depends on the wall clock. Pass the document's date when known.
        """
        ...

    def parse_fiscal_qr(self, payload: str, evidence_id: str) -> FiscalQRResult | None:
        """Parse a fiscal QR payload.

        Returns None when the payload is not this country's fiscal QR (or the
        country has none). Raises FiscalQRError when it is one but malformed.
        """
        ...

    def extract_text_fields(
        self,
        text: str,
        source: str,
        *,
        method: ExtractionMethod = ExtractionMethod.OCR,
        known_customer_tax_ids: Collection[str] = (),
    ) -> list[NamedObservation]:
        """Low-confidence candidate observations from OCR / plain text."""
        ...


@runtime_checkable
class CompanyPack(CountryPack, Protocol):
    """A pack complete enough to run a company's back office (§49): the core reads every document,
    checks every VAT amount and words every obligation of a company through its own country's pack.

    ``language`` is the pack's document language ("pt", "es"): a document in another language with
    no issuer country is read as one from abroad. ``title_words`` start lines that are never the
    supplier's name. ``obligation_vocabulary`` adds the country's letter wording (folded phrases by
    category, see :mod:`backoffice.closure.obligations`) to the core's English and Portuguese.
    ``public_holidays`` are the country's national public holidays (a working day skips them: the
    monthly accountant package goes on one, backoffice.package_delivery). ``document_code`` is the
    unique code the country's rules print on each fiscal document (Portugal's ATCUD), which proves two
    copies are the same document when their numbers could not be read (backoffice.captures).
    """

    language: str
    title_words: tuple[str, ...]

    def read_text(
        self,
        text: str,
        source: str,
        *,
        method: ExtractionMethod = ExtractionMethod.OCR,
        known_customer_tax_ids: Collection[str] = (),
        known_supplier_tax_ids: Collection[str] = (),
    ) -> Any:
        """A :class:`TextReading` (or an object with the same three attributes)."""
        ...

    def find_fiscal_qr(self, line: str) -> str | None:
        """The fiscal QR payload a decoded-QR text line carries, or None."""
        ...

    def document_kind(self, text: str) -> DocumentType | None:
        """The kind the document's own words name ("Factura rectificativa"); None to use the core's."""
        ...

    def obligation_vocabulary(self) -> Mapping[str, tuple[str, ...]]: ...

    def periodic_obligations(self, company_id: str, today: date) -> tuple[PeriodicObligation, ...]: ...

    def is_private_person(self, tax_id: str | None) -> bool:
        """True when the tax number belongs to a private person (for the accountant's rent flag)."""
        ...

    def public_holidays(self, year: int) -> frozenset[date]:
        """The country's national public holidays in ``year`` (regional ones are not included)."""
        ...

    def document_code(self, text: str) -> tuple[str, str] | None:
        """(the code's name, its value) of the one unique document code ``text`` prints ("ATCUD",
        "JJ3K4L5M-183"); None when the country has none or the text shows none, or more than one."""
        ...


def easter_sunday(year: int) -> date:
    """Easter Sunday in the Gregorian calendar (anonymous Gregorian algorithm): Good Friday, Easter
    and Corpus Christi, public holidays in several countries, are counted from it."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = (h + ell - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def company_pack(country: str) -> CompanyPack:
    """The pack a company of ``country`` runs on; UnknownCountryError when there is none, or it is
    not complete enough to run a company (only a validator)."""
    pack = get_pack(country)
    if not isinstance(pack, CompanyPack):
        raise UnknownCountryError(f"the {pack.country_code} pack cannot run a company yet")
    return pack


def company_countries() -> tuple[str, ...]:
    """Countries a company can be set up in (their packs run a company), sorted."""
    out = []
    for code in available_countries():
        try:
            company_pack(code)
        except UnknownCountryError:
            continue
        out.append(code)
    return tuple(out)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

# Built-in packs, imported on first use. Each module exposes its instance as
# ``PACK`` and registers it on import.
_BUILTIN_PACKS: dict[str, str] = {
    "ES": "backoffice.countries.es",
    "PT": "backoffice.countries.pt",
}

_registry: dict[str, CountryPack] = {}
_lock = threading.RLock()


def _country_key(country: str) -> str:
    key = (country or "").strip().upper()
    if len(key) != 2 or not key.isalpha() or not key.isascii():
        raise UnknownCountryError(f"not an ISO 3166-1 alpha-2 code: {country!r}")
    return key


def register_pack(pack: CountryPack, *, replace: bool = False) -> None:
    """Register a pack under its country code.

    Registering another instance of the same class again is a no-op, so
    module re-imports are harmless. A different pack for an already
    registered country needs ``replace=True``.
    """
    if not isinstance(pack, CountryPack):
        raise TypeError(f"{type(pack).__name__} does not implement CountryPack")
    key = _country_key(pack.country_code)
    with _lock:
        existing = _registry.get(key)
        if existing is not None and not replace:
            if type(existing) is type(pack):
                return
            raise ValueError(f"a different pack is already registered for {key}")
        _registry[key] = pack


def unregister_pack(country: str) -> None:
    """Remove a registered pack (mainly for tests and plugin reloads)."""
    with _lock:
        _registry.pop(_country_key(country), None)


def get_pack(country: str) -> CountryPack:
    """Return the pack for an ISO alpha-2 country code (case-insensitive)."""
    key = _country_key(country)
    with _lock:
        pack = _registry.get(key)
        if pack is None and key in _BUILTIN_PACKS:
            module = importlib.import_module(_BUILTIN_PACKS[key])
            pack = _registry.get(key)
            builtin = getattr(module, "PACK", None)
            if pack is None and builtin is not None:
                register_pack(builtin)
                pack = builtin
    if pack is None:
        raise UnknownCountryError(f"no country pack for {key}")
    return pack


def available_countries() -> tuple[str, ...]:
    """Country codes that have a registered or built-in pack, sorted."""
    with _lock:
        return tuple(sorted(set(_registry) | set(_BUILTIN_PACKS)))
