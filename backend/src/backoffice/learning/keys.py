"""Stable keys and display names for counterparties and tax numbers (§6, §20, §23, §38).

Bank descriptors, card descriptors and invoice headers name the same supplier in
different ways ("PAYPAL *ADOBE 402-935-9800", "ADOBE *CREATIVE CLOUD",
"Adobe Systems Software Ireland Ltd"). Learning (series, rules, entity
history) needs one deterministic key per counterparty. :func:`counterparty_key`
is a conservative default: it removes payment-processor prefixes, reference
numbers and legal-form suffixes, and never guesses beyond that. Callers that
already resolved a canonical supplier (for example a supplier id) should pass
that key instead.
"""

from __future__ import annotations

import re
import unicodedata
from functools import cache

__all__ = [
    "EU_VAT_PREFIXES",
    "counterparty_key",
    "display_name",
    "fold",
    "is_canonical_key",
    "match_key",
    "normalize_tax_id",
    "qualified_tax_id",
    "same_tax_id",
    "tax_id_country",
]

# Card/wallet processors that put their own name before the merchant ("PAYPAL *ADOBE").
_PROCESSORS = frozenset(
    {"paypal", "pp", "sumup", "sq", "square", "stripe", "zettle", "izettle", "iz", "ztl", "sp"}
)

# Legal-form words that never identify a counterparty on their own. Words that are also
# ordinary business words (Italian "S.p.A." vs "Hotel & Spa") are left out on purpose:
# a missed merge is safer than merging two different counterparties.
_LEGAL_FORMS = frozenset(
    {
        "limitada", "sa", "ltd", "limited", "inc", "llc",
        "gmbh", "ag", "sl", "slu", "bv", "nv", "plc", "srl", "sas", "sarl",
    }
)  # fmt: skip  (and a pack's own: "keys.legal_forms", Portugal's "Lda.", "Unipessoal", "SGPS")


@cache
def _legal_forms() -> frozenset[str]:
    from backoffice.countries import pack_words

    return _LEGAL_FORMS | frozenset(pack_words("keys.legal_forms"))

# EU VAT number prefixes (ISO 3166 alpha-2, except Greece "EL" and Northern Ireland "XI"),
# plus non-EU countries whose VAT numbers commonly carry an ISO prefix on invoices.
EU_VAT_PREFIXES = frozenset(
    {
        "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR", "HR", "HU",
        "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK", "XI",
        "GB", "CH", "NO",
    }
)  # fmt: skip

_NON_WORD = re.compile(r"[^a-z0-9]+")
_HAS_DIGIT = re.compile(r"\d")
_SPACES = re.compile(r"\s+")


def fold(text: str) -> str:
    """Lower-case, accent-free, single-spaced text for matching ("Alteração" -> "alteracao").

    Invisible format characters (zero-width spaces, soft hyphens, BOM: Unicode
    category Cf) are removed, so they cannot split a word to dodge a match (§26).
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(
        ch for ch in decomposed if not unicodedata.combining(ch) and unicodedata.category(ch) != "Cf"
    )
    return _SPACES.sub(" ", stripped.casefold()).strip()


def _merchant_part(folded: str) -> str:
    """'paypal *adobe' -> 'adobe'; 'uber *trip help.uber.com' -> 'uber'."""
    if "*" not in folded:
        return folded
    head, _, tail = folded.partition("*")
    head_word = head.strip().split(" ")[0] if head.strip() else ""
    if head_word in _PROCESSORS and tail.strip():
        return _merchant_part(tail.strip())
    return head.strip() or tail.strip()


def counterparty_key(name: str | None) -> str | None:
    """Deterministic matching key for a counterparty name, or None when nothing is left.

    >>> counterparty_key("VODAFONE PORTUGAL 123456")
    'vodafone portugal'
    >>> counterparty_key("PAYPAL *ADOBE 402-935-9800")
    'adobe'
    >>> counterparty_key("Hazel Tree, Lda.")
    'hazel tree'
    """
    if not name or not name.strip():
        return None
    text = _merchant_part(fold(name))
    text = re.sub(r"(?<=\b\w)\.(?=\w\b)", "", text)  # "s.a." -> "sa."
    words = [w for w in _NON_WORD.sub(" ", text).split() if not _HAS_DIGIT.search(w)]
    while len(words) > 1 and words[-1] in _legal_forms():
        words.pop()
    return " ".join(words) or None


# A resolved identifier ("sup_0a1b2c3d4e5f6a7b", "supplier_42"): one token joined by
# underscores. counterparty_key() never produces an underscore, so the two cannot collide.
_CANONICAL = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")


def is_canonical_key(value: str | None) -> bool:
    """True for resolved identifiers such as supplier ids, which must be kept verbatim."""
    return bool(value) and _CANONICAL.fullmatch(value.strip().casefold()) is not None  # type: ignore[union-attr]


def match_key(value: str | None) -> str | None:
    """The form a counterparty key is compared in (rules, history).

    Canonical ids are kept as they are (case-folded): running them through
    :func:`counterparty_key` would drop every token with a digit and collapse
    all supplier ids to "sup". Anything else is treated as a name.
    """
    if value is None or not value.strip():
        return None
    if is_canonical_key(value):
        return value.strip().casefold()
    return counterparty_key(value)


_CUT = re.compile(r"\s+[-–—|]\s+|,")
@cache
def _legal_tails() -> tuple[re.Pattern[str], re.Pattern[str]]:
    """A dotted legal form at the end of a name ("S.A."; a pack's own: "keys.legal_form_dotted"), and any."""
    from backoffice.countries import pack_words

    dotted = "".join(f"|{w}" for w in pack_words("keys.legal_form_dotted"))
    return (re.compile(rf"\s+(?:S\.\s?A\.?{dotted})\s*$", re.IGNORECASE),
            re.compile(r"(?:\s+(?:" + "|".join(sorted(_legal_forms(), key=len, reverse=True)) + r")\.?)+\s*$",
                       re.IGNORECASE))


def display_name(name: str | None, fallback: str = "This supplier") -> str:
    """Short owner-facing name: 'Vodafone Portugal - Comunicações Pessoais, S.A.' -> 'Vodafone Portugal'.

    All-caps bank descriptors are title-cased, except short acronyms ('IKEA', 'EDP').
    """
    if not name or not name.strip():
        return fallback
    text = name.strip()
    if "*" in text:
        head, _, tail = text.partition("*")
        text = tail if head.strip().casefold() in _PROCESSORS else head
    text = _CUT.split(text, maxsplit=1)[0].strip() or text.strip()
    for pattern in _legal_tails():
        shorter = pattern.sub("", text).strip()
        text = shorter or text
    words = [w for w in text.split() if not _HAS_DIGIT.search(w)] or text.split()
    if all(not w.isalpha() or w.isupper() for w in words):
        words = [w if len(w) <= 4 else w.capitalize() for w in words]
    return " ".join(words) or fallback


def normalize_tax_id(raw: str | None) -> str | None:
    """'PT 509 123 456' -> '509123456'. Only a known country prefix is removed."""
    if not raw:
        return None
    compact = re.sub(r"[^0-9A-Za-z]", "", raw).upper()
    if len(compact) > 2 and compact[:2] in EU_VAT_PREFIXES and compact[2].isalnum():
        body = compact[2:]
        if any(ch.isdigit() for ch in body):
            return body
    return compact or None


def tax_id_country(raw: str | None) -> str | None:
    """ISO country of a prefixed tax number ('PT509123456' -> 'PT', 'EL…' -> 'GR'); else None."""
    if not raw:
        return None
    compact = re.sub(r"[^0-9A-Za-z]", "", raw).upper()
    if len(compact) > 2 and compact[:2] in EU_VAT_PREFIXES and compact[2:] != "":
        prefix = compact[:2]
        return {"EL": "GR", "XI": "GB"}.get(prefix, prefix)
    return None


# Country code -> VAT prefix where they differ (Greece uses EL on VAT numbers).
_VAT_PREFIX_FOR_COUNTRY = {"GR": "EL"}


def qualified_tax_id(raw: str | None, country: str | None) -> str | None:
    """Tax number with its country made explicit: ('509123456', 'PT') -> 'PT509123456'.

    A company's own tax number is often stored without a prefix, and then
    :func:`same_tax_id` would accept the same digits from any country. Numbers
    that already carry a known prefix, and countries without a VAT prefix, are
    returned unchanged.
    """
    if not raw or not raw.strip():
        return None
    if tax_id_country(raw) is not None or not country:
        return raw
    code = country.strip().upper()
    prefix = _VAT_PREFIX_FOR_COUNTRY.get(code, code)
    if prefix not in EU_VAT_PREFIXES:
        return raw
    return f"{prefix}{raw.strip()}"


def same_tax_id(a: str | None, b: str | None) -> bool:
    """True when both tax numbers name the same registration.

    Explicit, different country prefixes never match even if the digits do.
    """
    na, nb = normalize_tax_id(a), normalize_tax_id(b)
    if na is None or nb is None or na != nb:
        return False
    ca, cb = tax_id_country(a), tax_id_country(b)
    return ca is None or cb is None or ca == cb
