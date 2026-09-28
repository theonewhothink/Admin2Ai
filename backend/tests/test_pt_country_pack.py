"""Country-pack registry and the Portugal pack through the generic protocol (§49-50)."""

import os
import subprocess
import sys
from collections.abc import Collection, Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from backoffice.countries import (
    CountryPack,
    FiscalQRError,
    FiscalQRResult,
    NamedObservation,
    NativeDocumentType,
    TaxIdCheck,
    Term,
    UnknownCountryError,
    VATRate,
    available_countries,
    get_pack,
    register_pack,
    unregister_pack,
)
from backoffice.countries.pt import PACK, PortugalPack
from backoffice.domain.models import CriticalField, DocumentType, ExtractionMethod

SRC = Path(__file__).resolve().parents[1] / "src"
QR = ("A:509123457*B:516123459*C:PT*D:FT*E:N*F:20260918*G:FT 2026/183*H:CSDF7T5H-183*"
      "I1:PT*I7:393.17*I8:90.43*N:90.43*O:483.60*Q:kLp0*R:2345")


def test_get_pack_is_case_insensitive_and_returns_one_instance():
    assert get_pack("PT") is get_pack("pt") is get_pack(" Pt ") is PACK
    assert isinstance(PACK, CountryPack)
    assert "PT" in available_countries()


@pytest.mark.parametrize("code", ["ES", "PRT", "", "P1", None])
def test_unknown_countries(code):
    with pytest.raises(UnknownCountryError):
        get_pack(code)
    with pytest.raises(KeyError):  # also a KeyError for callers that expect one
        get_pack(code)


def test_importing_the_registry_does_not_import_any_country():
    code = (
        "import sys, backoffice.countries as c\n"
        "assert 'backoffice.countries.pt' not in sys.modules\n"
        "c.get_pack('PT')\n"
        "assert 'backoffice.countries.pt' in sys.modules\n"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


def test_builtin_pack_comes_back_after_unregister():
    unregister_pack("PT")
    try:
        assert get_pack("PT") is PACK
    finally:
        register_pack(PACK)


def test_registration_rules():
    register_pack(PortugalPack())  # same class again: harmless no-op
    assert get_pack("PT") is PACK
    with pytest.raises(TypeError):
        register_pack(object())  # type: ignore[arg-type]


class FakePack:
    """Minimal pack for an invented country: the core must work with it unchanged."""

    country_code = "ZZ"
    country_name = "Testland"
    currency = "EUR"
    document_types: Mapping[str, NativeDocumentType] = {}
    terminology: Sequence[Term] = ()

    def normalize_tax_id(self, raw: str) -> str | None:
        return raw.strip() or None

    def validate_tax_id(self, raw: str) -> TaxIdCheck:
        return TaxIdCheck(raw=raw, normalized=raw.strip(), valid=bool(raw.strip()))

    def map_document_type(self, native_code: str) -> DocumentType | None:
        return DocumentType.INVOICE if native_code == "INV" else None

    def lookup_term(self, label: str) -> Term | None:
        return None

    def vat_rates(self, on: date, region: str | None = None) -> tuple[VATRate, ...]:
        return ()

    def is_plausible_vat(self, net, vat, *, on=None, region=None) -> bool | None:
        return None

    def parse_fiscal_qr(self, payload: str, evidence_id: str) -> FiscalQRResult | None:
        return None

    def extract_text_fields(self, text: str, source: str, *, method=ExtractionMethod.OCR,
                            known_customer_tax_ids: Collection[str] = ()) -> list[NamedObservation]:
        return []


def core_tax_id_is_ok(country: str, raw: str) -> bool:
    """What core code looks like: no branching on the country."""
    return get_pack(country).validate_tax_id(raw).valid


def test_core_code_does_not_fork_by_country():
    register_pack(FakePack())
    try:
        assert core_tax_id_is_ok("ZZ", "anything")
        assert core_tax_id_is_ok("PT", "PT 123 456 789")
        assert not core_tax_id_is_ok("PT", "123456780")
        with pytest.raises(ValueError):
            register_pack(type("OtherFake", (FakePack,), {})())  # different class, same country
    finally:
        unregister_pack("ZZ")
    with pytest.raises(UnknownCountryError):
        get_pack("ZZ")


def test_replace_allows_swapping_a_pack():
    register_pack(FakePack())
    try:
        other = type("OtherFake", (FakePack,), {})()
        register_pack(other, replace=True)
        assert get_pack("ZZ") is other
    finally:
        unregister_pack("ZZ")


# --------------------------------------------------------------------------- #
# Portugal through the protocol
# --------------------------------------------------------------------------- #


def test_portugal_tax_ids_and_documents():
    pack = get_pack("PT")
    assert (pack.country_code, pack.currency) == ("PT", "EUR")
    assert pack.normalize_tax_id("PT 509 123 457") == "509123457"
    assert pack.validate_tax_id("509123457").valid
    assert pack.map_document_type("FR") == DocumentType.INVOICE_RECEIPT
    assert pack.document_types["NC"].doc_type == DocumentType.CREDIT_NOTE
    assert pack.lookup_term("Fatura-Recibo").concept == "invoice_receipt"
    assert any(t.native == "IVA" for t in pack.terminology)


def test_portugal_vat_through_the_protocol():
    pack = get_pack("PT")
    assert len(pack.vat_rates(date(2026, 9, 27), "PT-AC")) == 3
    assert pack.is_plausible_vat(Decimal("393.17"), Decimal("90.43"), on=date(2026, 9, 18)) is True
    assert pack.is_plausible_vat(Decimal("100.00"), Decimal("16.00"), region="PT-AC",
                                 on=date(2026, 9, 18)) is True
    assert pack.is_plausible_vat(Decimal("100.00"), Decimal("16.00"), on=date(2026, 9, 18)) is False
    assert pack.is_plausible_vat(Decimal("100.00"), Decimal("20.00"), on=date(2005, 1, 1)) is None


def test_portugal_fiscal_qr_through_the_protocol():
    pack = get_pack("PT")
    assert pack.parse_fiscal_qr("https://example.com/i/1", "ev") is None
    result = pack.parse_fiscal_qr(QR, "ev_qr")
    assert result.country == "PT" and result.native_doc_type == "FT"
    assert result.doc_type == DocumentType.INVOICE
    assert result.consistent and result.usable and result.notes == ()
    gross = [o for o in result.observations if o.field == CriticalField.GROSS_AMOUNT]
    assert {o.value for o in gross} == {Decimal("483.60")}
    cancelled = pack.parse_fiscal_qr(QR.replace("E:N", "E:A"), "ev")
    assert not cancelled.usable and cancelled.notes
    with pytest.raises(FiscalQRError):
        pack.parse_fiscal_qr(QR.replace("O:483.60", "O:483,60"), "ev")


def test_portugal_text_fields_through_the_protocol():
    obs = get_pack("PT").extract_text_fields("Total a pagar: 1.492,30 €", "ev_ocr")
    assert [(o.field, o.value) for o in obs] == [(CriticalField.GROSS_AMOUNT, Decimal("1492.30"))]
