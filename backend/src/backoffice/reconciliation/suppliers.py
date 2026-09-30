"""Merchant descriptor normalization and supplier resolution (§6, §20).

Bank and card descriptors are noisy: ``VODAFONE PT*1234 LISBOA``,
``UBER *TRIP HELP.UBER.COM``, ``PAYPAL *ADOBE``, ``AMZN Mktp ES*2K4``. This
module turns them into a canonical supplier key, using, in order of strength:

1. the counterparty IBAN (a supplier's known bank account),
2. the supplier tax id (documents),
3. learned alias memory (the owner or accountant taught it once, §5 "I will
   remember this."),
4. known supplier names and aliases, and email/web domains,
5. token similarity (stdlib :mod:`difflib`) as a weak, fuzzy last resort.

Strong signals that point at *different* suppliers are a conflict, never a
guess (§19). Resolution is deterministic: suppliers are compared in a fixed
order and ties are reported as ambiguous instead of being broken silently.

Descriptor vocabularies below are card-network / bank-statement conventions,
not regulatory facts. They are data: extend them, do not branch on them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from functools import lru_cache
from typing import Protocol, runtime_checkable

from backoffice.domain.models import Document, Supplier, Transaction

from ._text import fold, normalize_iban, same_tax_id, tokens

__all__ = [
    "AliasMemory",
    "InMemoryAliasMemory",
    "NameKind",
    "NormalizedDescriptor",
    "ResolveMethod",
    "ResolverConfig",
    "SupplierMatch",
    "SupplierResolver",
    "descriptor_key",
    "key_similarity",
    "normalize_descriptor",
]

# --------------------------------------------------------------------------- vocabularies

# Payment facilitators that put their own name before the real merchant.
# Multi-letter names are stripped even without a '*'; short codes only before '*'.
HARD_PROCESSORS: frozenset[str] = frozenset(
    {"PAYPAL", "SUMUP", "ZETTLE", "IZETTLE", "STRIPE", "PADDLE", "FASTSPRING", "DLOCAL"}
)
SHORT_PROCESSORS: frozenset[str] = frozenset(
    {"PP", "SQ", "SP", "IZ", "FS", "TST", "WL", "DLO", "2CO", "PADDLENET"}
)

# Well-known abbreviations used on card statements -> brand name.
BRAND_ABBREVIATIONS: dict[str, str] = {
    "AMZN": "AMAZON",
    "AMZNPRIME": "AMAZON",
    "FACEBK": "META",
    "MSFT": "MICROSOFT",
}

# Leading words banks add before the merchant (PT / ES / EN statement wording).
LEADING_NOISE: frozenset[str] = frozenset(
    {
        "COMPRA", "COMPRAS", "PAG", "PAGAMENTO", "PAGAMENTOS", "PAGTO", "PAGO",
        "TRF", "TRANSF", "TRANSFERENCIA", "SEPA", "DD", "DEB", "DEBITO", "DIRECTO",
        "DIRETO", "DIR", "CARD", "CARTAO", "TARJETA", "POS", "TPA", "VISA",
        "MASTERCARD", "MC", "MAESTRO", "CONTACTLESS", "PURCHASE", "PAYMENT", "TO",
        "FROM", "DE", "PARA", "MB", "MBWAY", "ELECTRONICA", "ELETRONICA", "ONLINE",
        "INTERNET", "SERV", "RECIBO", "ADEUDO",
    }
)  # fmt: skip

# Small words dropped anywhere so 'Correios de Portugal' == 'Correios Portugal'.
STOP_WORDS: frozenset[str] = frozenset(
    {
        "DE",
        "DA",
        "DO",
        "DOS",
        "DAS",
        "DEL",
        "LA",
        "LE",
        "EL",
        "THE",
        "OF",
        "AND",
        "ET",
        "UND",
    }
)

# Legal-form suffixes: never part of a supplier's identity.
LEGAL_FORMS: frozenset[str] = frozenset(
    {
        "SA", "LDA", "LTDA", "SL", "SLU", "SAS", "SARL", "SRL", "SPA", "GMBH", "AG",
        "BV", "NV", "LTD", "LIMITED", "PLC", "INC", "LLC", "CORP", "CO", "UNIPESSOAL",
        "UNIP", "SGPS", "EIRL", "SNC", "KG", "OY", "AB", "AS", "APS", "SE", "ULC",
        "CIE", "SCA",
    }
)  # fmt: skip

# Country codes / names that descriptors append ("VODAFONE PT", "AMZN Mktp ES").
COUNTRY_WORDS: frozenset[str] = frozenset(
    {
        "PT", "ES", "FR", "DE", "IT", "UK", "GB", "IE", "NL", "BE", "LU", "US", "EU",
        "PRT", "ESP", "GBR", "USA", "PORTUGAL", "ESPANA", "SPAIN", "IRELAND",
    }
)  # fmt: skip

# Descriptor filler that is never the merchant, including invoice-series prefixes
# payers type next to a number ('FT 2026/101', 'INV 5531').
FILLER: frozenset[str] = frozenset(
    {"MKTP", "MKTPLACE", "WWW", "COM", "HELP", "BILL", "BILLING", "TRIP", "REF", "NIF",
     "FT", "FR", "FS", "NC", "ND", "INV", "INVOICE", "FATURA", "FACTURA", "FACT"}
)  # fmt: skip

# Cities commonly appended to card descriptors in the first markets (§63).
# Only dropped from the end of a descriptor, never when it is the only word.
TRAILING_CITIES: frozenset[str] = frozenset(
    {
        "LISBOA", "LISBON", "PORTO", "OPORTO", "BRAGA", "COIMBRA", "FARO", "FUNCHAL",
        "AVEIRO", "SETUBAL", "CASCAIS", "OEIRAS", "AMADORA", "SINTRA", "MADRID",
        "BARCELONA", "VALENCIA", "SEVILLA", "LONDON", "DUBLIN", "LUXEMBOURG",
        "AMSTERDAM", "PARIS", "BERLIN",
    }
)  # fmt: skip

_TLDS = frozenset(
    {"COM", "NET", "ORG", "IO", "CO", "PT", "ES", "DE", "FR", "IT", "UK", "EU", "IE",
     "NL", "BE", "APP", "AI", "DEV", "US", "SO", "ME", "TV", "GG", "LY", "CC", "XYZ",
     "BIZ", "INFO", "TECH", "CLOUD", "SHOP", "STORE", "ONLINE"}
)  # fmt: skip
_DOMAIN_RE = re.compile(
    r"(?<![A-Z0-9.-])((?:[A-Z0-9-]+\.)+(?:"
    + "|".join(sorted(_TLDS))
    + r"))(?:/[A-Z0-9/_-]*)?(?![A-Z0-9-])"
)
MAX_KEY_TOKENS = 3

# Words a payment processor's own legal name adds ('PAYPAL EUROPE SARL',
# 'STRIPE PAYMENTS EUROPE LTD'). After a processor they name no other merchant,
# so the processor itself is the counterparty.
PROCESSOR_ENTITY_WORDS: frozenset[str] = frozenset(
    {"EUROPE", "EMEA", "PAYMENTS", "PAYMENT", "INTERNATIONAL", "INTL", "GLOBAL",
     "SERVICES", "HOLDINGS", "GROUP", "PAYOUT", "PAYOUTS"}
)  # fmt: skip


# --------------------------------------------------------------------------- normalization


@dataclass(frozen=True)
class NormalizedDescriptor:
    """A merchant descriptor or supplier name reduced to its identity.

    ``key`` is the canonical text key ('vodafone', 'uber', 'adobe', 'amazon'),
    '' when nothing informative is left (e.g. 'SEPA DD 000123').
    """

    raw: str
    key: str
    tokens: tuple[str, ...]
    processor: str | None = None
    domain: str | None = None

    @property
    def display(self) -> str:
        """Owner-facing name: 'Vodafone'."""
        return " ".join(t.capitalize() for t in self.key.split())


def normalize_descriptor(text: str | None) -> NormalizedDescriptor:
    """Normalize a bank/card descriptor or a supplier's legal name (§20).

    >>> normalize_descriptor("VODAFONE PT*1234 LISBOA").key
    'vodafone'
    >>> normalize_descriptor("PAYPAL *ADOBE").key
    'adobe'
    """
    return _normalize_cached(text or "")


@lru_cache(maxsize=8192)
def _normalize_cached(raw: str) -> NormalizedDescriptor:
    folded = fold(raw)
    domain_match = _DOMAIN_RE.search(folded)
    domain = domain_match.group(1).lower() if domain_match else None
    body = _DOMAIN_RE.sub(" ", folded)
    merchant, processor = _merchant_words(body)
    cleaned = _clean(merchant)
    if not cleaned and domain:
        cleaned = [_domain_brand(domain)]
    if all(w in COUNTRY_WORDS or w in TRAILING_CITIES for w in cleaned):
        cleaned = _brand_with_digits(_words(body)) or cleaned
    key = " ".join(cleaned[:MAX_KEY_TOKENS]).lower()
    return NormalizedDescriptor(
        raw=raw, key=key, tokens=tuple(cleaned), processor=processor, domain=domain
    )


def _words(text: str) -> list[str]:
    return tokens(text)


def _merchant_words(body: str) -> tuple[list[str], str | None]:
    """Split on the first '*': the merchant is before it, unless that is a processor.

    A processor is stripped only when another merchant is actually named after
    it; 'PAYPAL EUROPE SARL' and 'PAYPAL *' are PayPal itself.
    """
    head, star, tail = body.partition("*")
    head_words = _strip_leading_noise(_words(head))
    if star:
        if _is_processor(head_words):
            merchant = _strip_leading_noise(_words(tail))
            if _names_a_merchant(merchant):
                return merchant, " ".join(head_words)
            return head_words, None
        if head_words:
            return head_words, None
        return _strip_leading_noise(_words(tail)), None
    if len(head_words) > 1 and head_words[0] in HARD_PROCESSORS:
        if _names_a_merchant(head_words[1:]):
            return head_words[1:], head_words[0]
        return head_words[:1], None  # the processor's own entity: 'PAYPAL EUROPE'
    return head_words, None


def _names_a_merchant(words: list[str]) -> bool:
    """True when ``words`` still name someone besides a processor's own entity."""
    generic = PROCESSOR_ENTITY_WORDS | COUNTRY_WORDS | TRAILING_CITIES
    return any(w not in generic for w in _clean(words))


def _is_processor(words: list[str]) -> bool:
    if not words:
        return False
    joined = "".join(words)
    return (
        joined in HARD_PROCESSORS
        or joined in SHORT_PROCESSORS
        or (len(words) == 1 and words[0] in SHORT_PROCESSORS)
    )


def _strip_leading_noise(words: list[str]) -> list[str]:
    start = 0
    while start < len(words) and (
        words[start] in LEADING_NOISE or _is_code(words[start])
    ):
        start += 1
    return words[start:]


def _is_code(word: str) -> bool:
    """Transaction-specific codes: numbers and digit/letter mixes ('1234', '2K4')."""
    return any(ch.isdigit() for ch in word)


def _clean(words: list[str]) -> list[str]:
    kept = [
        BRAND_ABBREVIATIONS.get(w, w)
        for w in words
        if len(w) > 1
        and not _is_code(w)
        and w not in LEGAL_FORMS
        and w not in FILLER
        and w not in STOP_WORDS
    ]
    while len(kept) > 1 and (kept[-1] in COUNTRY_WORDS or kept[-1] in TRAILING_CITIES):
        kept.pop()
    return kept


def _brand_with_digits(words: list[str]) -> list[str]:
    """Last resort for brands containing digits ('O2', '7ELEVEN'): keep lettered words."""
    return [
        w
        for w in words
        if len(w) > 1
        and any(ch.isalpha() for ch in w)
        and w not in LEGAL_FORMS
        and w not in FILLER
        and w not in STOP_WORDS
        and w not in COUNTRY_WORDS
        and w not in TRAILING_CITIES
        and w not in LEADING_NOISE
        and w not in HARD_PROCESSORS
        and w not in SHORT_PROCESSORS
    ][:1]


def _domain_brand(domain: str) -> str:
    labels = [label.upper() for label in domain.split(".") if label]
    while len(labels) > 1 and labels[-1] in _TLDS:
        labels.pop()
    return labels[-1] if labels else ""


def _registrable(domain: str) -> str:
    """'help.uber.com' -> 'uber.com' (good enough for matching known domains).

    Also accepts the forms suppliers are often stored with: '@uber.com' or
    'billing@uber.com'.
    """
    host = domain.strip().rsplit("@", 1)[-1]
    labels = host.lower().strip(".").split(".")
    if len(labels) >= 3 and labels[-2] in {"co", "com"} and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# --------------------------------------------------------------------------- similarity


class NameKind(str, Enum):
    """How two keys relate. EXACT and CONTAINS are strong; the rest are fuzzy."""

    EXACT = "exact"
    CONTAINS = "contains"
    TRUNCATED = "truncated"
    RATIO = "ratio"
    NONE = "none"


STRONG_NAME_KINDS = frozenset({NameKind.EXACT, NameKind.CONTAINS})


@lru_cache(maxsize=65536)
def key_similarity(a: str, b: str) -> tuple[float, NameKind]:
    """Similarity of two canonical keys in [0, 1] and how they relate.

    * equal (ignoring spaces) -> 1.0 / 0.98, EXACT
    * whole-word containment ('vodafone' in 'vodafone portugal') -> 0.85..0.99, CONTAINS
    * truncated descriptor ('adobesystem' vs 'adobe systems software') -> TRUNCATED
    * otherwise difflib ratio, RATIO
    """
    if not a or not b:
        return 0.0, NameKind.NONE
    if a == b:
        return 1.0, NameKind.EXACT
    sa, sb = a.replace(" ", ""), b.replace(" ", "")
    if sa == sb:
        return 0.98, NameKind.EXACT
    short, long_ = (a, b) if len(sa) <= len(sb) else (b, a)
    s_short, s_long = short.replace(" ", ""), long_.replace(" ", "")
    coverage = len(s_short) / len(s_long)
    if len(s_short) >= 4 and _contains_words(short.split(), long_.split()):
        return 0.85 + 0.14 * coverage, NameKind.CONTAINS
    first_long = long_.split()[0]
    if len(s_short) >= max(5, len(first_long)) and s_long.startswith(s_short):
        return 0.80 + 0.15 * coverage, NameKind.TRUNCATED
    return SequenceMatcher(None, a, b, autojunk=False).ratio(), NameKind.RATIO


def _contains_words(short: list[str], long_: list[str]) -> bool:
    n = len(short)
    return any(long_[i : i + n] == short for i in range(len(long_) - n + 1))


# --------------------------------------------------------------------------- memory


@runtime_checkable
class AliasMemory(Protocol):
    """Learned descriptor -> supplier id aliases (§5, §6, §40 one-tap learning)."""

    def get(self, descriptor_key: str) -> str | None: ...

    def put(self, descriptor_key: str, supplier_id: str) -> None: ...


class InMemoryAliasMemory:
    """Dictionary-backed :class:`AliasMemory` (tests, single-process use)."""

    def __init__(self, entries: dict[str, str] | None = None) -> None:
        self._entries: dict[str, str] = dict(entries or {})

    def get(self, descriptor_key: str) -> str | None:
        return self._entries.get(descriptor_key)

    def put(self, descriptor_key: str, supplier_id: str) -> None:
        self._entries[descriptor_key] = supplier_id

    def items(self) -> list[tuple[str, str]]:
        return sorted(self._entries.items())


# --------------------------------------------------------------------------- resolution


class ResolveMethod(str, Enum):
    IBAN = "iban"
    TAX_ID = "tax_id"
    LEARNED = "learned"
    ALIAS = "alias"
    DOMAIN = "domain"
    FUZZY = "fuzzy"
    UNRESOLVED = "unresolved"
    CONFLICT = "conflict"


STRONG_METHODS = frozenset(
    {ResolveMethod.IBAN, ResolveMethod.TAX_ID, ResolveMethod.LEARNED,
     ResolveMethod.ALIAS, ResolveMethod.DOMAIN}
)  # fmt: skip


@dataclass(frozen=True)
class SupplierMatch:
    """Outcome of resolving one descriptor / document to a supplier.

    ``key`` is canonical: the known supplier's id when resolved, otherwise the
    normalized descriptor key ('' when nothing informative). ``candidates`` lists
    supplier ids when the result is ambiguous or conflicting.
    """

    key: str
    method: ResolveMethod
    supplier: Supplier | None = None
    similarity: float = 0.0
    descriptor: NormalizedDescriptor | None = None
    candidates: tuple[str, ...] = ()

    @property
    def is_known(self) -> bool:
        return self.supplier is not None

    @property
    def is_strong(self) -> bool:
        return self.method in STRONG_METHODS

    @property
    def name_key(self) -> str:
        return self.descriptor.key if self.descriptor else ""

    @property
    def display_name(self) -> str:
        if self.supplier is not None:
            return self.supplier.name
        return self.descriptor.display if self.descriptor else ""


@dataclass(frozen=True)
class ResolverConfig:
    """Thresholds for fuzzy resolution (difflib similarity, not money)."""

    fuzzy_threshold: float = 0.86
    resolution_margin: float = 0.02


@dataclass(frozen=True)
class _Profile:
    supplier: Supplier
    keys: tuple[str, ...]
    ibans: frozenset[str]
    domains: frozenset[str]


@dataclass
class SupplierResolver:
    """Resolves descriptors, transactions and documents to known suppliers.

    Deterministic: suppliers are compared sorted by id. Learned aliases are read
    from and written to the injected :class:`AliasMemory`.
    """

    suppliers: Iterable[Supplier] = ()
    memory: AliasMemory = field(default_factory=InMemoryAliasMemory)
    config: ResolverConfig = field(default_factory=ResolverConfig)

    def __post_init__(self) -> None:
        ordered = sorted(self.suppliers, key=lambda s: s.id)
        self.suppliers = tuple(ordered)
        self._profiles = tuple(_profile(s) for s in ordered)
        self._by_id = {s.id: s for s in ordered}
        self._rank_cache: dict[str, list[tuple[float, NameKind, str]]] = {}

    # ------------------------------------------------------------------ public

    def supplier(self, supplier_id: str) -> Supplier | None:
        return self._by_id.get(supplier_id)

    def resolve(
        self,
        text: str | None,
        *,
        iban: str | None = None,
        tax_id: str | None = None,
        fallback_text: str | None = None,
    ) -> SupplierMatch:
        """Resolve a descriptor (plus optional IBAN / tax id) to a supplier (§20)."""
        descriptor = normalize_descriptor(text)
        if not descriptor.key and fallback_text:
            descriptor = normalize_descriptor(fallback_text)
        strong = self._strong_signals(descriptor, iban, tax_id)
        distinct = sorted({sid for _, sid in strong})
        if len(distinct) > 1:
            return SupplierMatch(
                key=descriptor.key,
                method=ResolveMethod.CONFLICT,
                descriptor=descriptor,
                candidates=tuple(distinct),
            )
        if strong:
            method, sid = strong[0]
            return self._known(sid, method, 1.0, descriptor)
        return self._fuzzy(descriptor)

    def resolve_transaction(self, tx: Transaction) -> SupplierMatch:
        """Resolve the payee/payer of a bank or card transaction."""
        return self.resolve(
            tx.counterparty, iban=tx.counterparty_iban, fallback_text=tx.description
        )

    def resolve_document(self, doc: Document) -> SupplierMatch:
        """Resolve the issuer of a document (tax id first, then IBAN, then name)."""
        return self.resolve(
            doc.supplier_name, iban=doc.iban, tax_id=doc.supplier_tax_id
        )

    def learn(self, descriptor: str, supplier_id: str) -> str:
        """Remember that ``descriptor`` means ``supplier_id``. Returns the stored key.

        Raises ValueError for an unknown supplier or an uninformative descriptor.
        """
        if supplier_id not in self._by_id:
            raise ValueError("cannot learn an alias for an unknown supplier")
        key = normalize_descriptor(descriptor).key
        if not key:
            raise ValueError("descriptor has nothing to remember")
        self.memory.put(key, supplier_id)
        return key

    # ------------------------------------------------------------------ internals

    def _strong_signals(
        self, descriptor: NormalizedDescriptor, iban: str | None, tax_id: str | None
    ) -> list[tuple[ResolveMethod, str]]:
        signals: list[tuple[ResolveMethod, str]] = []
        n_iban = normalize_iban(iban)
        for p in self._profiles:
            if n_iban and n_iban in p.ibans:
                signals.append((ResolveMethod.IBAN, p.supplier.id))
        for p in self._profiles:
            if tax_id and same_tax_id(tax_id, p.supplier.tax_id):
                signals.append((ResolveMethod.TAX_ID, p.supplier.id))
        learned = self.memory.get(descriptor.key) if descriptor.key else None
        if learned and learned in self._by_id:
            signals.append((ResolveMethod.LEARNED, learned))
        if descriptor.domain:
            registrable = _registrable(descriptor.domain)
            for p in self._profiles:
                if registrable in p.domains:
                    signals.append((ResolveMethod.DOMAIN, p.supplier.id))
        alias = self._alias(descriptor.key)
        if alias:
            signals.append((ResolveMethod.ALIAS, alias))
        return signals

    def _ranked(self, key: str) -> list[tuple[float, NameKind, str]]:
        cached = self._rank_cache.get(key)
        if cached is not None:
            return cached
        ranked: list[tuple[float, NameKind, str]] = []
        for p in self._profiles:
            best = max(
                (key_similarity(key, k) for k in p.keys),
                key=lambda r: r[0],
                default=(0.0, NameKind.NONE),
            )
            ranked.append((best[0], best[1], p.supplier.id))
        ranked.sort(key=lambda r: (-r[0], r[2]))
        self._rank_cache[key] = ranked
        return ranked

    def _alias(self, key: str) -> str | None:
        """A unique whole-word alias hit, or None."""
        if not key:
            return None
        ranked = [r for r in self._ranked(key) if r[1] in STRONG_NAME_KINDS]
        if not ranked:
            return None
        if (
            len(ranked) > 1
            and ranked[0][0] - ranked[1][0] < self.config.resolution_margin
        ):
            return None
        return ranked[0][2]

    def _fuzzy(self, descriptor: NormalizedDescriptor) -> SupplierMatch:
        ranked = self._ranked(descriptor.key) if descriptor.key else []
        close = [r for r in ranked if r[0] >= self.config.fuzzy_threshold]
        if not close:
            return SupplierMatch(
                key=descriptor.key,
                method=ResolveMethod.UNRESOLVED,
                descriptor=descriptor,
            )
        top = close[0]
        rivals = [r for r in close[1:] if top[0] - r[0] < self.config.resolution_margin]
        if rivals:
            return SupplierMatch(
                key=descriptor.key,
                method=ResolveMethod.UNRESOLVED,
                descriptor=descriptor,
                candidates=tuple(sorted([top[2], *(r[2] for r in rivals)])),
            )
        return self._known(top[2], ResolveMethod.FUZZY, top[0], descriptor)

    def _known(
        self,
        supplier_id: str,
        method: ResolveMethod,
        similarity: float,
        descriptor: NormalizedDescriptor,
    ) -> SupplierMatch:
        return SupplierMatch(
            key=supplier_id,
            method=method,
            supplier=self._by_id[supplier_id],
            similarity=similarity,
            descriptor=descriptor,
        )


def _profile(supplier: Supplier) -> _Profile:
    names = [supplier.name, *supplier.aliases]
    keys = tuple(sorted({k for k in (normalize_descriptor(n).key for n in names) if k}))
    return _Profile(
        supplier=supplier,
        keys=keys,
        ibans=frozenset(normalize_iban(i) for i in supplier.known_ibans if i),
        domains=frozenset(_registrable(d) for d in supplier.email_domains if d),
    )


def descriptor_key(text: str | None) -> str:
    """Shortcut: canonical key of a descriptor ('' when uninformative)."""
    return normalize_descriptor(text).key
