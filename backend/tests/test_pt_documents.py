"""SAF-T document types and Portuguese vocabulary (§50)."""

import pytest

from backoffice.countries.base import DocumentFamily
from backoffice.countries.pt import (
    PT_DOCUMENT_TYPES,
    PT_TERMS,
    get_document_type,
    lookup_term,
    map_document_type,
    preferred_code,
    status_label,
)
from backoffice.domain.models import DocumentType


@pytest.mark.parametrize(
    ("code", "doc_type"),
    [
        ("FT", DocumentType.INVOICE),
        ("FR", DocumentType.INVOICE_RECEIPT),
        ("FS", DocumentType.SIMPLIFIED_INVOICE),
        ("NC", DocumentType.CREDIT_NOTE),
        ("ND", DocumentType.DEBIT_NOTE),
        ("RG", DocumentType.RECEIPT),
        ("RC", DocumentType.RECEIPT),
        ("VD", DocumentType.INVOICE_RECEIPT),
        ("GT", DocumentType.OTHER),
        ("GR", DocumentType.OTHER),
        ("PF", DocumentType.OTHER),
        ("OR", DocumentType.OTHER),
        ("ft", DocumentType.INVOICE),
        (" FR ", DocumentType.INVOICE_RECEIPT),
    ],
)
def test_map_document_type(code, doc_type):
    assert map_document_type(code) == doc_type


@pytest.mark.parametrize("code", ["XX", "", "FTX", None])
def test_unknown_codes_map_to_none(code):
    assert map_document_type(code) is None


def test_pro_forma_and_transport_documents_are_not_invoices():
    for code in ("PF", "OR", "NE", "GT", "GR", "FC"):
        assert not PT_DOCUMENT_TYPES[code].fiscal_invoice, code
    for code in ("FT", "FR", "FS", "NC", "ND"):
        assert PT_DOCUMENT_TYPES[code].fiscal_invoice, code


def test_which_documents_prove_payment():
    assert get_document_type("FR").proves_payment
    assert get_document_type("RG").proves_payment
    assert not get_document_type("FT").proves_payment
    assert not get_document_type("FS").proves_payment


def test_families_and_statuses():
    assert get_document_type("RG").family == DocumentFamily.PAYMENT
    assert get_document_type("GT").family == DocumentFamily.MOVEMENT
    assert get_document_type("PF").family == DocumentFamily.WORKING
    assert status_label("FT", "A") == "cancelled"
    assert status_label("FT", "S") == "self-billed"
    assert status_label("RG", "S") is None
    assert status_label("GT", "T") == "third-party"
    assert status_label("XX", "N") is None


def test_preferred_code_round_trip():
    for doc_type in (DocumentType.INVOICE, DocumentType.INVOICE_RECEIPT,
                     DocumentType.SIMPLIFIED_INVOICE, DocumentType.CREDIT_NOTE,
                     DocumentType.DEBIT_NOTE):
        assert map_document_type(preferred_code(doc_type)) == doc_type
    assert preferred_code(DocumentType.RECEIPT) is None  # RC vs RG is not ours to guess


def test_codes_are_two_upper_case_letters():
    for code, entry in PT_DOCUMENT_TYPES.items():
        assert code == entry.code and len(code) == 2 and code.isupper()


@pytest.mark.parametrize(
    ("label", "concept"),
    [
        ("Fatura", "invoice"),
        ("Fatura-Recibo", "invoice_receipt"),
        ("fatura recibo", "invoice_receipt"),
        ("Fatura Simplificada", "simplified_invoice"),
        ("Recibo", "receipt"),
        ("NOTA DE CRÉDITO", "credit_note"),
        ("Nota de credito", "credit_note"),
        ("Base tributável:", "net_amount"),
        ("Base de incidência", "net_amount"),
        ("IVA", "vat_amount"),
        ("Total", "gross_amount"),
        ("Total a pagar", "amount_payable"),
        ("Retenção na fonte", "withholding"),
        ("Contribuinte", "tax_id"),
        ("NIF", "tax_id"),
        ("Data de emissão", "issue_date"),
        ("Data de vencimento", "due_date"),
        ("Referência Multibanco", "payment_reference"),
        ("Entidade", "payment_entity"),
        ("  imposto   do selo ", "stamp_duty"),
    ],
)
def test_lookup_term(label, concept):
    term = lookup_term(label)
    assert term is not None and term.concept == concept


def test_unknown_term():
    assert lookup_term("Bananas") is None
    assert lookup_term(None) is None  # type: ignore[arg-type]


def test_every_label_resolves_to_the_term_that_declares_it():
    for term in PT_TERMS:
        for label in (term.native, *term.aliases):
            assert lookup_term(label) is term, label


def test_owner_labels_avoid_jargon():
    for term in PT_TERMS:
        assert term.english and "debit" not in term.english.lower()
