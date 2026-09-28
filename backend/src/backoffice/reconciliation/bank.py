"""Bank-declared facts about a transaction that the core model does not carry (§20).

:class:`~backoffice.domain.models.Transaction` holds the booked amount only.
Banks often declare more: the original foreign amount and rate of a card
purchase, a fee included in a transfer, or that a debit pays off a credit card.
Connectors supply these as :class:`BankMetadata`, keyed by transaction id.

Only facts the bank declares count as "declared" for reconciliation: a match can
be exact after a *declared* fee or FX conversion, never after a guessed one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from backoffice.domain.models import Transaction, TransactionKind

from ._text import currency_code, fold

__all__ = [
    "CARD_SETTLEMENT_PHRASES",
    "BankMetadata",
    "FxDetails",
    "fx_from_text",
    "is_card_purchase",
    "is_card_settlement",
    "phrase_in",
]

_CODE = re.compile(r"^[A-Z]{3}$")
_ZERO = Decimal(0)


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (Decimal, int)):
        raise TypeError(f"{name} must be Decimal (or int), never float")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _currency(value: str, name: str) -> str:
    code = value.strip().upper()
    if not _CODE.match(code):
        raise ValueError(f"{name} is not a currency code")
    return code


@dataclass(frozen=True)
class FxDetails:
    """A foreign-currency conversion as declared by the bank.

    ``original_amount`` is what the merchant charged, positive, in
    ``original_currency``. ``rate`` (optional) is account-currency units per one
    original unit as applied by the bank. ``fee`` is a declared conversion fee in
    the account currency, positive, already included in the booked amount.

    ``from_text`` marks details parsed from statement wording by
    :func:`fx_from_text`. Such text may have been typed by a payer and its rate
    may be quoted either way round, so scoring only treats it as exact when the
    rate arithmetic proves the booked amount (§57).
    """

    original_amount: Decimal
    original_currency: str
    rate: Decimal | None = None
    fee: Decimal = _ZERO
    from_text: bool = False  # parsed from statement wording, not a structured field

    def __post_init__(self) -> None:
        amount = _decimal(self.original_amount, "original_amount")
        if amount <= 0:
            raise ValueError("original_amount must be positive")
        object.__setattr__(self, "original_amount", amount)
        object.__setattr__(
            self,
            "original_currency",
            _currency(self.original_currency, "original_currency"),
        )
        if self.rate is not None:
            rate = _decimal(self.rate, "rate")
            if rate <= 0:
                raise ValueError("rate must be positive")
            object.__setattr__(self, "rate", rate)
        fee = _decimal(self.fee, "fee")
        if fee < 0:
            raise ValueError("fee cannot be negative")
        object.__setattr__(self, "fee", fee)


@dataclass(frozen=True)
class BankMetadata:
    """Extra facts a bank or card feed declares for one transaction.

    * ``fee``: bank fee included in the booked amount (positive). For a payment
      out it made the debit larger; for money in it was deducted.
    * ``fx``: declared currency conversion.
    * ``card_present``: True for an in-shop card purchase, False for online.
    * ``settles_card_last4`` / ``settlement_period``: this debit pays off the
      credit card ending in these digits, for purchases booked in that period.
    """

    fee: Decimal = _ZERO
    fx: FxDetails | None = None
    card_present: bool | None = None
    settles_card_last4: str | None = None
    settlement_period: tuple[date, date] | None = None

    def __post_init__(self) -> None:
        fee = _decimal(self.fee, "fee")
        if fee < 0:
            raise ValueError("fee cannot be negative")
        object.__setattr__(self, "fee", fee)
        if self.settlement_period is not None:
            start, end = self.settlement_period
            if start > end:
                raise ValueError("settlement_period starts after it ends")

    @property
    def total_fee(self) -> Decimal:
        """Declared bank fee plus declared conversion fee (account currency)."""
        return self.fee + (self.fx.fee if self.fx else _ZERO)


NO_METADATA = BankMetadata()

# --------------------------------------------------------------------------- FX in bank text

_AMOUNT = r"\d{1,3}(?:[ .,]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_CUR_THEN_AMOUNT = re.compile(rf"(?<![A-Z])([A-Z]{{3}})\s?({_AMOUNT})(?![\d])")
_AMOUNT_THEN_CUR = re.compile(rf"(?<![\d.,])({_AMOUNT})\s?([A-Z]{{3}})(?![A-Z])")
_RATE = re.compile(
    r"(?:RATE|TAXA|CAMBIO|CAMBIO APLICADO|TIPO CAMBIO|FX|@)\s*:?\s*(\d+[.,]\d+)"
)
_KNOWN_CURRENCIES = frozenset(
    {"EUR", "USD", "GBP", "CHF", "JPY", "CAD", "AUD", "SEK", "NOK", "DKK", "PLN",
     "CZK", "HUF", "RON", "BGN", "ILS", "BRL", "MXN", "CNY", "HKD", "SGD", "ZAR",
     "TRY", "INR", "NZD", "AED"}
)  # fmt: skip


def _parse_amount(text: str) -> Decimal | None:
    """'1.234,56' / '1,234.56' / '100,00' / '100' -> Decimal (never float)."""
    cleaned = text.replace(" ", "")
    last_sep = max(cleaned.rfind(","), cleaned.rfind("."))
    if last_sep == -1:
        whole, frac = cleaned, ""
    else:
        whole, frac = cleaned[:last_sep], cleaned[last_sep + 1 :]
        if len(frac) == 3:  # '1.234' is a thousands separator, not decimals
            whole, frac = cleaned, ""
    digits = re.sub(r"[.,]", "", whole)
    try:
        return Decimal(f"{digits}.{frac}" if frac else digits)
    except InvalidOperation:
        return None


def fx_from_text(text: str | None, account_currency: str) -> FxDetails | None:
    """Read a declared foreign amount (and rate) from bank text, if unambiguous.

    'COMPRA AMAZON.COM USD 100,00 TAXA 0,9215' -> FxDetails(100.00, 'USD', 0.9215).
    Returns None unless exactly one foreign currency amount is present.
    """
    folded = fold(text)
    account = currency_code(account_currency)
    found: set[tuple[str, Decimal]] = set()
    for match in _CUR_THEN_AMOUNT.finditer(folded):
        _collect(found, match.group(1), match.group(2), account)
    for match in _AMOUNT_THEN_CUR.finditer(folded):
        _collect(found, match.group(2), match.group(1), account)
    if len(found) != 1:
        return None
    currency, amount = next(iter(found))
    rate_match = _RATE.search(folded)
    rate = _parse_rate(rate_match.group(1)) if rate_match else None
    return FxDetails(
        original_amount=amount, original_currency=currency, rate=rate, from_text=True
    )


def _collect(
    found: set[tuple[str, Decimal]], code: str, raw: str, account: str
) -> None:
    if code not in _KNOWN_CURRENCIES or code == account:
        return
    amount = _parse_amount(raw)
    if amount is not None and amount > 0:
        found.add((code, amount))


def _parse_rate(raw: str) -> Decimal | None:
    try:
        rate = Decimal(raw.replace(",", "."))
    except InvalidOperation:
        return None
    return rate if rate > 0 else None


# --------------------------------------------------------------------------- card settlements

# Statement wording for a debit that pays off a credit card (PT / ES / EN).
# Bank-statement conventions, not regulation; extend as feeds are observed.
CARD_SETTLEMENT_PHRASES: tuple[str, ...] = (
    "LIQUIDACAO CARTAO",
    "LIQ CARTAO",
    "LIQUIDACAO DE CARTAO",
    "PAGAMENTO CARTAO CREDITO",
    "PAGAMENTO CARTAO DE CREDITO",
    "PAG CARTAO CREDITO",
    "LIQUIDACION TARJETA",
    "PAGO TARJETA CREDITO",
    "CREDIT CARD PAYMENT",
    "CREDIT CARD REPAYMENT",
    "CARD SETTLEMENT",
    "CARD REPAYMENT",
)


def phrase_in(text: str, phrases: tuple[str, ...]) -> str | None:
    """First phrase whose words appear consecutively in ``text`` (folded words)."""
    words = f" {' '.join(re.findall(r'[A-Z0-9]+', fold(text)))} "
    for phrase in phrases:
        if f" {phrase} " in words:
            return phrase
    return None


def is_card_purchase(tx: Transaction) -> bool:
    """A payment made *with* a card: card kind and a card number.

    ``Transaction.kind`` defaults to CARD, so the kind alone is not trusted.
    """
    return tx.kind == TransactionKind.CARD and bool(tx.card_last4)


def is_card_settlement(tx: Transaction, meta: BankMetadata | None = None) -> bool:
    """True when this debit pays off a credit card (declared, or by wording)."""
    if tx.amount >= 0:
        return False
    if meta is not None and meta.settles_card_last4:
        return True
    if is_card_purchase(tx):
        return False  # a purchase made with a card, not a repayment of one
    text = f"{tx.counterparty} {tx.description}"
    return phrase_in(text, CARD_SETTLEMENT_PHRASES) is not None
