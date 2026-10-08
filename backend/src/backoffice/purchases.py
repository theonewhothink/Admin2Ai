"""Large purchases: when one invoice deserves a closer look (§26, §28, §57).

Two rules. Both are product policy, not law, kept as named constants so the
accountant can see exactly what triggers them.

**Possible capital asset** (accountant-facing only, the owner is never asked):
a purchase invoice whose amount before VAT is at least
:data:`CAPITAL_ASSET_NET_MIN` and whose wording names equipment
(:data:`EQUIPMENT_WORDS`), or any single purchase invoice whose total is at
least :data:`LARGE_INVOICE_MIN`. Whether it is depreciated is the accountant's
call; the flag only makes sure it is not booked as a routine cost unseen.

**High-value first purchase**: a purchase invoice whose total is at least
:data:`HIGH_VALUE_MIN`, from a supplier with no earlier documents or payments.
It closes on its own only when the buyer's tax number printed on it is the
paying company's and its totals add up (amount before VAT + VAT = total).
Otherwise it stays AMBER with a plain reason (§57): a large first payment to
someone new is exactly where a wrong or fake invoice costs most (§26). A
routine small receipt is unaffected.

Pure Python (runs in the browser build too).
"""

from __future__ import annotations

from decimal import Decimal

from backoffice.countries import LazyPattern, pack_words
from backoffice.domain.models import Document, DocumentType
from backoffice.learning import fold

__all__ = [
    "CAPITAL_ASSET_FLAG",
    "CAPITAL_ASSET_NET_MIN",
    "EQUIPMENT_WORDS",
    "HIGH_VALUE_MIN",
    "LARGE_INVOICE_MIN",
    "PURCHASE_INVOICE_TYPES",
    "is_high_value",
    "names_equipment",
    "possible_capital_asset",
]

CAPITAL_ASSET_NET_MIN = Decimal("1000.00")  # amount before VAT, with equipment wording
LARGE_INVOICE_MIN = Decimal("5000.00")  # any single purchase invoice total
HIGH_VALUE_MIN = Decimal("5000.00")  # total of a first purchase from a new supplier

CAPITAL_ASSET_FLAG = "Possible equipment purchase (capital asset) — accountant to confirm"

# Folded (lower-case, accent-free) words that name equipment: English, and a pack's own ("purchases.equipment":
# Portugal's "máquina", "portátil", "viatura").
EQUIPMENT_WORDS: tuple[str, ...] = (
    "machine", "machines", "machinery", "equipment", "laptop", "laptops", "computer",
    "computers", "server", "servers", "vehicle", "vehicles",
)  # fmt: skip
_EQUIPMENT = LazyPattern(lambda: r"(?<![a-z0-9])(?:" + "|".join(
    sorted((*pack_words("purchases.equipment"), *EQUIPMENT_WORDS), key=len, reverse=True)) + r")(?![a-z0-9])")

# Documents that record a purchase the business books (not receipts of payment,
# not credit notes, never supporting documents such as a pro-forma).
PURCHASE_INVOICE_TYPES = frozenset(
    {DocumentType.INVOICE, DocumentType.INVOICE_RECEIPT, DocumentType.SIMPLIFIED_INVOICE, DocumentType.DEBIT_NOTE}
)


def names_equipment(text: str) -> bool:
    """True when the wording names equipment ("máquina", "portátil", "vehicle"...)."""
    return bool(text) and _EQUIPMENT.search(fold(text)) is not None


def possible_capital_asset(doc: Document, text: str = "") -> bool:
    """Accountant flag: a purchase invoice that may be equipment to depreciate, not a routine cost."""
    if doc.doc_type not in PURCHASE_INVOICE_TYPES:
        return False
    net = doc.net_amount if doc.net_amount is not None else doc.gross_amount
    if net is not None and abs(net) >= CAPITAL_ASSET_NET_MIN and names_equipment(text):
        return True
    return doc.gross_amount is not None and abs(doc.gross_amount) >= LARGE_INVOICE_MIN


def is_high_value(doc: Document) -> bool:
    """A purchase invoice large enough to need stronger checks before it closes on its own."""
    return (doc.doc_type in PURCHASE_INVOICE_TYPES and doc.gross_amount is not None
            and abs(doc.gross_amount) >= HIGH_VALUE_MIN)
