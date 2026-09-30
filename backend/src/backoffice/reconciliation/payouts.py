"""Payouts from card terminals and payment or sales platforms (§20, §21).

A card terminal (SIBS / Multibanco, REDUNIQ, Comercia), a payment provider
(Stripe, PayPal, Mollie, SumUp, Square, Adyen, Shopify Payments) or a sales
platform (Amazon, Glovo, Uber Eats, Bolt Food, Booking.com, Airbnb) collects
the customers' money, keeps its fees and commissions, gives refunds and
disputed payments back, and pays the rest into the bank in one transfer: the
*payout*. That transfer is a net settlement, not a customer paying an invoice.
Its evidence is the provider's payout report (settlement statement), which
says how many sales it covers, what was kept and why.

Do not confuse this with ``bank.is_card_repayment`` (historically
``is_card_settlement``): that is the business paying *off* its own credit
card, money out. A payout is money *in* from a provider.

This module only recognises providers: on a bank line (:func:`payout_provider`)
and by name on a report or invoice (:func:`provider_named`). The wording
tables are statement and legal-name conventions compiled from public naming,
NOT verified against live bank feeds (verified_as_of: never). Wording is AMBER
evidence (§57): it decides what to look for, never what is closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from backoffice.domain.models import Transaction, TransactionKind

from .bank import is_card_purchase, phrase_in

__all__ = [
    "CARD_TERMINAL",
    "PAYOUT_PROVIDERS",
    "PayoutProvider",
    "ProviderKind",
    "compatible_providers",
    "payout_provider",
    "provider_by_key",
    "provider_named",
]


class ProviderKind(str, Enum):
    CARD_TERMINAL = "card_terminal"  # card machine acquirers (TPA)
    PAYMENTS = "payments"  # online and in-person payment providers
    MARKETPLACE = "marketplace"
    DELIVERY = "delivery"  # food delivery platforms
    ACCOMMODATION = "accommodation"  # booking platforms


@dataclass(frozen=True)
class PayoutProvider:
    """One provider that pays the business out.

    ``label`` is how the owner reads it in a sentence after "from" ("Payout
    from Stripe.", "Payout from your card terminal."). ``bank_phrases`` are
    folded words seen on bank lines; ``names`` are folded words that name it on
    reports and invoices (legal names included). ``fee_word`` is what the
    provider calls what it keeps ("fees" or "commission").
    """

    key: str
    label: str
    kind: ProviderKind
    bank_phrases: tuple[str, ...]
    names: tuple[str, ...]
    fee_word: str = "fees"

    @property
    def is_card_terminal(self) -> bool:
        return self.kind is ProviderKind.CARD_TERMINAL

    @property
    def title(self) -> str:
        """As a name on its own ('Stripe', 'SIBS'; the terminal no acquirer is named for: 'Card terminal')."""
        return self.label[5:].capitalize() if self.label.startswith("your ") else self.label


def _p(key: str, label: str, kind: ProviderKind, bank: tuple[str, ...], names: tuple[str, ...] = (),
       fee_word: str = "fees") -> PayoutProvider:
    return PayoutProvider(key, label, kind, bank, tuple(dict.fromkeys((*bank, *names))), fee_word)


_K = ProviderKind

# Specific providers first; the generic card terminal last (a 'TPA' line with no acquirer named).
PAYOUT_PROVIDERS: tuple[PayoutProvider, ...] = (
    # Portuguese card terminals (acquirers)
    _p("sibs", "SIBS", _K.CARD_TERMINAL, ("SIBS", "SIBS PAGAMENTOS", "SIBS FPS"),
       ("SIBS FORWARD PAYMENT SOLUTIONS", "SIBS PAYMENTS")),
    _p("reduniq", "REDUNIQ", _K.CARD_TERMINAL, ("REDUNIQ", "UNICRE")),
    _p("comercia", "Comercia", _K.CARD_TERMINAL, ("COMERCIA", "COMERCIA GLOBAL PAYMENTS")),
    # Payment providers
    _p("stripe", "Stripe", _K.PAYMENTS, ("STRIPE",), ("STRIPE PAYMENTS EUROPE", "STRIPE TECHNOLOGY EUROPE")),
    _p("paypal", "PayPal", _K.PAYMENTS, ("PAYPAL",), ("PAYPAL EUROPE",)),
    _p("shopify", "Shopify Payments", _K.PAYMENTS, ("SHOPIFY", "SHOPIFY PAYMENTS")),
    _p("mollie", "Mollie", _K.PAYMENTS, ("MOLLIE",)),
    _p("sumup", "SumUp", _K.PAYMENTS, ("SUMUP", "SUM UP PAYMENTS")),
    _p("square", "Square", _K.PAYMENTS, ("SQUARE PAYMENTS", "SQUARE EUROPE", "SQUAREUP", "SQUARE PAYOUT")),
    _p("adyen", "Adyen", _K.PAYMENTS, ("ADYEN",)),
    # Marketplaces and platforms
    _p("amazon", "Amazon", _K.MARKETPLACE,
       ("AMAZON PAYMENTS", "AMAZON SERVICES EUROPE", "AMAZON MARKETPLACE", "AMAZON SELLER"),
       fee_word="commission"),
    _p("glovo", "Glovo", _K.DELIVERY, ("GLOVO", "GLOVOAPP", "GLOVOAPP23"), ("GLOVOAPP PORTUGAL",),
       fee_word="commission"),
    _p("uber_eats", "Uber Eats", _K.DELIVERY, ("UBER EATS", "UBEREATS", "UBER PORTIER"), fee_word="commission"),
    _p("bolt_food", "Bolt Food", _K.DELIVERY, ("BOLT FOOD", "BOLTFOOD"), fee_word="commission"),
    _p("booking", "Booking.com", _K.ACCOMMODATION, ("BOOKING COM", "BOOKINGCOM"), fee_word="commission"),
    _p("airbnb", "Airbnb", _K.ACCOMMODATION, ("AIRBNB",), ("AIRBNB PAYMENTS",), fee_word="commission"),
)

CARD_TERMINAL = _p(
    "card_terminal", "your card terminal", _K.CARD_TERMINAL,
    ("TPA", "VENDAS TPA", "LIQ TPA", "LIQUIDACAO TPA", "TERMINAL PAGAMENTO", "TERMINAL DE PAGAMENTO",
     "VENDAS MULTIBANCO", "LIQ MULTIBANCO", "LIQUIDACAO MULTIBANCO", "MULTIBANCO TPA", "CARD TERMINAL",
     "POS SETTLEMENT", "MERCHANT SETTLEMENT"),
)

_BY_KEY = {p.key: p for p in (*PAYOUT_PROVIDERS, CARD_TERMINAL)}


def provider_by_key(key: str | None) -> PayoutProvider | None:
    return _BY_KEY.get((key or "").strip().lower())


def payout_provider(tx: Transaction) -> PayoutProvider | None:
    """The provider paying out ``tx``, or None.

    Only money in that is not a card refund and not a bank-declared fee or
    internal move can be a payout: a refund of a card purchase is money back
    on the card, and money between the owner's own accounts is decided earlier.
    """
    if tx.amount <= 0 or is_card_purchase(tx) or tx.kind in (TransactionKind.FEE, TransactionKind.INTERNAL):
        return None
    text = f"{tx.counterparty} {tx.description} {tx.reference or ''}"
    for provider in (*PAYOUT_PROVIDERS, CARD_TERMINAL):
        if phrase_in(text, provider.bank_phrases):
            return provider
    return None


def provider_named(*texts: str | None) -> PayoutProvider | None:
    """The provider a report, a file name or an invoice's supplier names ('Booking.com B.V.' -> Booking.com)."""
    joined = " ".join(t for t in texts if t)
    if not joined.strip():
        return None
    for provider in (*PAYOUT_PROVIDERS, CARD_TERMINAL):
        if phrase_in(joined, provider.names):
            return provider
    return None


def compatible_providers(a: PayoutProvider, b: PayoutProvider) -> bool:
    """Same provider, or a card-terminal line with no acquirer named and any acquirer's report."""
    if a.key == b.key:
        return True
    return a.is_card_terminal and b.is_card_terminal and CARD_TERMINAL.key in (a.key, b.key)
