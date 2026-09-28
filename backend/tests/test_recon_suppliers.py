"""Merchant descriptor normalization and supplier resolution (§20)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from backoffice.domain.models import Document, Supplier, Transaction
from backoffice.reconciliation import (
    InMemoryAliasMemory,
    ResolveMethod,
    SupplierResolver,
    key_similarity,
    normalize_descriptor,
)
from backoffice.reconciliation.suppliers import NameKind, descriptor_key

T = "tenant"

VODAFONE = Supplier(
    id="sup_voda",
    tenant_id=T,
    name="Vodafone Portugal, Comunicações Pessoais, S.A.",
    aliases=["Vodafone"],
    tax_id="PT502544180",
    known_ibans=["PT50 0002 0123 1234 5678 9015 4"],
)
UBER = Supplier(
    id="sup_uber",
    tenant_id=T,
    name="Uber B.V.",
    aliases=["UBER"],
    email_domains=["uber.com"],
)
ADOBE = Supplier(id="sup_adobe", tenant_id=T, name="Adobe Systems Software Ireland Ltd")
AMAZON = Supplier(
    id="sup_amzn", tenant_id=T, name="Amazon EU S.a r.l.", aliases=["Amazon"]
)
GOOGLE = Supplier(
    id="sup_google", tenant_id=T, name="Google Ireland Limited", aliases=["Google"]
)
GOOGLE_CLOUD = Supplier(id="sup_gcloud", tenant_id=T, name="Google Cloud EMEA Limited")


def resolver(
    *suppliers: Supplier, memory: InMemoryAliasMemory | None = None
) -> SupplierResolver:
    chosen = suppliers or (VODAFONE, UBER, ADOBE, AMAZON)
    return SupplierResolver(chosen, memory=memory or InMemoryAliasMemory())


# --- normalization


@pytest.mark.parametrize(
    ("descriptor", "key"),
    [
        ("VODAFONE PT*1234 LISBOA", "vodafone"),
        ("UBER *TRIP HELP.UBER.COM", "uber"),
        ("PAYPAL *ADOBE", "adobe"),
        ("AMZN Mktp ES*2K4", "amazon"),
        ("COMPRA 4817 CONTINENTE LISBOA 15/09", "continente"),
        ("DD VODAFONE PORTUGAL 000123", "vodafone"),
        ("SQ *BLUE BOTTLE COFFEE", "blue bottle coffee"),
        ("FACEBK *ABC123XYZ", "meta"),
        ("APPLE.COM/BILL", "apple"),
        ("Uber B.V.", "uber"),
        ("Adobe Systems Software Ireland Ltd", "adobe systems software"),
        ("Correios de Portugal", "correios"),
        ("TRF ACME LDA FT 2026/101", "acme"),
        ("O2 UK", "o2"),
        ("SEPA DD 000123", ""),
        ("", ""),
    ],
)
def test_descriptors_normalize_to_canonical_keys(descriptor: str, key: str) -> None:
    assert normalize_descriptor(descriptor).key == key


def test_processor_prefix_is_recorded_and_stripped() -> None:
    star = normalize_descriptor("PAYPAL *ADOBE")
    bare = normalize_descriptor("PAYPAL ADOBE SYSTEMS")
    assert star.processor == "PAYPAL" and bare.processor == "PAYPAL"
    assert bare.key == "adobe systems"


def test_short_processor_codes_only_count_before_a_star() -> None:
    assert normalize_descriptor("SQ *BLUE BOTTLE").processor == "SQ"
    assert normalize_descriptor("SP OFFICE SUPPLIES").key == "sp office supplies"


def test_domain_is_extracted_and_used_when_nothing_else_is_left() -> None:
    uber = normalize_descriptor("UBER *TRIP HELP.UBER.COM")
    assert uber.domain == "help.uber.com"
    assert (
        normalize_descriptor("WWW.NOTION.SO").key != ""
    )  # unknown TLD: still something


def test_accents_and_case_do_not_matter() -> None:
    assert descriptor_key("Comunicações Óptimas") == descriptor_key(
        "COMUNICACOES OPTIMAS"
    )


def test_display_name_is_owner_friendly() -> None:
    assert normalize_descriptor("VODAFONE PT*1234 LISBOA").display == "Vodafone"


# --- similarity


def test_key_similarity_kinds() -> None:
    assert key_similarity("uber", "uber") == (1.0, NameKind.EXACT)
    assert key_similarity("adobe systems", "adobesystems")[1] is NameKind.EXACT
    score, kind = key_similarity("vodafone", "vodafone portugal comunicacoes")
    assert kind is NameKind.CONTAINS and 0.85 < score < 1.0
    score, kind = key_similarity("adobesystem", "adobe systems software")
    assert kind is NameKind.TRUNCATED and score >= 0.86


def test_key_similarity_does_not_confuse_word_prefixes() -> None:
    _, kind = key_similarity("mercadona", "mercado central")
    assert kind is NameKind.RATIO
    _, kind = key_similarity("mercado", "mercadona")
    assert kind is NameKind.RATIO


def test_key_similarity_is_symmetric_and_empty_safe() -> None:
    for a, b in [
        ("vodafone", "vodafone portugal"),
        ("uber", "lyft"),
        ("adobesystem", "adobe systems"),
    ]:
        assert key_similarity(a, b) == key_similarity(b, a)
    assert key_similarity("", "uber") == (0.0, NameKind.NONE)


# --- resolution


def test_resolves_the_task_examples_to_known_suppliers() -> None:
    r = resolver()
    assert r.resolve("VODAFONE PT*1234 LISBOA").key == "sup_voda"
    assert r.resolve("UBER *TRIP HELP.UBER.COM").key == "sup_uber"
    assert r.resolve("PAYPAL *ADOBE").key == "sup_adobe"
    assert r.resolve("AMZN Mktp ES*2K4").key == "sup_amzn"


def test_alias_and_domain_resolution_are_strong() -> None:
    r = resolver()
    match = r.resolve("VODAFONE PT*1234 LISBOA")
    assert match.method is ResolveMethod.ALIAS and match.is_strong and match.is_known
    domain_only = r.resolve("XYZ*RIDE HELP.UBER.COM")
    assert domain_only.key == "sup_uber"


def test_iban_identifies_the_supplier_regardless_of_formatting() -> None:
    match = resolver().resolve("SEPA DD 000123", iban="pt50000201231234567890154")
    assert match.method is ResolveMethod.IBAN and match.key == "sup_voda"


def test_tax_id_ignores_country_prefix() -> None:
    doc = Document(
        tenant_id=T,
        evidence_ids=["e"],
        supplier_name="Illegible",
        supplier_tax_id="502 544 180",
    )
    match = resolver().resolve_document(doc)
    assert match.method is ResolveMethod.TAX_ID and match.key == "sup_voda"


def test_truncated_descriptor_resolves_fuzzily() -> None:
    match = resolver().resolve("PAYPAL *ADOBESYSTEM 998877")
    assert match.key == "sup_adobe"
    assert match.method is ResolveMethod.FUZZY and not match.is_strong


def test_unknown_descriptor_keeps_its_own_key() -> None:
    match = resolver().resolve("PADARIA CENTRAL LISBOA")
    assert match.method is ResolveMethod.UNRESOLVED
    assert not match.is_known and match.key == "padaria central"
    assert match.display_name == "Padaria Central"


def test_learned_alias_memory_wins_and_persists() -> None:
    memory = InMemoryAliasMemory()
    r = resolver(memory=memory)
    stored = r.learn("PAYPAL *XYZSOFT 12345", "sup_adobe")
    assert stored == "xyzsoft"
    assert memory.items() == [("xyzsoft", "sup_adobe")]
    again = resolver(memory=memory).resolve("PAYPAL *XYZSOFT 99999")
    assert again.method is ResolveMethod.LEARNED and again.key == "sup_adobe"


def test_learning_rejects_unknown_suppliers_and_empty_descriptors() -> None:
    r = resolver()
    with pytest.raises(ValueError):
        r.learn("PAYPAL *X", "sup_missing")
    with pytest.raises(ValueError):
        r.learn("SEPA DD 000123", "sup_adobe")


def test_strong_signals_that_disagree_are_a_conflict_not_a_guess() -> None:
    match = resolver().resolve("UBER *TRIP", iban="PT50000201231234567890154")
    assert match.method is ResolveMethod.CONFLICT
    assert match.candidates == ("sup_uber", "sup_voda")
    assert not match.is_known


def test_equally_close_suppliers_are_ambiguous_not_picked() -> None:
    twin_a = Supplier(id="sup_a", tenant_id=T, name="Nova Dental Norte")
    twin_b = Supplier(id="sup_b", tenant_id=T, name="Nova Dental Sul")
    match = SupplierResolver([twin_a, twin_b]).resolve("NOVA DENTAL")
    assert match.method is ResolveMethod.UNRESOLVED
    assert set(match.candidates) == {"sup_a", "sup_b"}


def test_more_specific_alias_wins_between_related_suppliers() -> None:
    r = SupplierResolver([GOOGLE, GOOGLE_CLOUD])
    assert r.resolve("GOOGLE CLOUD EMEA LTD").key == "sup_gcloud"
    # Before the star is the merchant, after it the product.
    assert r.resolve("GOOGLE *CLOUD 1234").key == "sup_google"
    assert r.resolve("GOOGLE *ADS1234").key == "sup_google"


def test_resolution_is_independent_of_supplier_order() -> None:
    forward = SupplierResolver([VODAFONE, UBER, ADOBE, AMAZON])
    backward = SupplierResolver([AMAZON, ADOBE, UBER, VODAFONE])
    for text in [
        "VODAFONE PT*1234",
        "PAYPAL *ADOBESYSTEM",
        "UBER *TRIP",
        "AMZN Mktp ES*2K4",
        "PADARIA",
    ]:
        assert forward.resolve(text) == backward.resolve(text)


def test_transaction_falls_back_to_description_when_counterparty_is_blank() -> None:
    tx = Transaction(
        tenant_id=T,
        account_id="a",
        booked_on=date(2026, 9, 1),
        amount=Decimal("-1"),
        counterparty="SEPA DD 000123",
        description="VODAFONE PORTUGAL FATURA 9",
    )
    assert resolver().resolve_transaction(tx).key == "sup_voda"
