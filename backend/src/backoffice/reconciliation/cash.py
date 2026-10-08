"""Cash moving between the bank and the business's cash box (checklist X4, X5).

* Cash taken out of the bank for the cash box: an ATM withdrawal ("LEVANTAMENTO", "LEV MB", "CASH
  WITHDRAWAL", "ATM WDL") or a transfer the bank line says is for the cash box ("FUNDO DE MANEIO", "PETTY
  CASH", "REFORÇO DE CAIXA"). It is not an expense and needs no invoice: the cash receipts it pays for are
  the evidence of what it bought, and the cash box (backoffice.cashbook) shows anything they do not explain.
* Cash paid into the bank ("DEPÓSITO NUMERÁRIO", "DEP NUM", "CASH DEPOSIT"): the till's cash sales, proven
  by the till reports (Z reports) it comes from, never counted as sales a second time.

The words are the bank's own names for these operations. They are conventions compiled from Portuguese,
English and Spanish statements, not verified against live bank feeds (verified_as_of: never). Nothing here
closes anything on wording alone: a withdrawal is booked into the cash box, whose balance at the end of every
period is shown with anything unexplained, and a cash deposit closes only on the till reports that explain it.
"""

from __future__ import annotations


from backoffice.countries import LazyPattern, pack_alternatives
from backoffice.domain.models import Transaction

from ._text import fold

__all__ = ["CASH_BOX", "CASH_DEPOSIT", "WITHDRAWAL", "cash_movement"]

WITHDRAWAL = "withdrawal"  # cash taken out at a cash machine
CASH_BOX = "cash_box"  # money moved to the cash box (petty cash float)
CASH_DEPOSIT = "cash_deposit"  # cash paid into the bank

# Bank wording (folded, upper case): English and Spanish here, a pack's own in "cash.<kind>" (Portugal's
# "LEVANTAMENTO", "FUNDO DE MANEIO", "DEPOSITO NUMERARIO").
_WITHDRAWAL = LazyPattern(lambda: (
    rf"(?<![A-Z])(?:{pack_alternatives('cash.withdrawal')}|CASH\s+WITHDRAWAL|"
    r"ATM\s+(?:WITHDRAWAL|WDL|CASH)|CASH\s+(?:MACHINE|WDL)|WITHDRAWAL\s+ATM|RETIRADA\s+(?:DE\s+)?EFECTIVO|"
    r"REINTEGRO(?:\s+CAJERO)?)(?![A-Z])"))
_CASH_BOX = LazyPattern(lambda: (
    rf"(?<![A-Z])(?:{pack_alternatives('cash.cash_box')}|PETTY\s+CASH|CASH\s+BOX|CASH\s+FLOAT|CAJA\s+CHICA)"
    r"(?![A-Z])"))
_CASH_DEPOSIT = LazyPattern(lambda: (
    rf"(?<![A-Z])(?:{pack_alternatives('cash.deposit')}|CASH\s+DEPOSIT|"
    r"DEPOSITO\s+(?:DE\s+)?EFECTIVO|INGRESO\s+(?:EN\s+)?EFECTIVO|PAID\s+IN\s+CASH\s+AT)(?![A-Z])"))


def cash_movement(tx: Transaction) -> str | None:
    """:data:`WITHDRAWAL`, :data:`CASH_BOX` (money out) or :data:`CASH_DEPOSIT` (money in), else None."""
    text = fold(f"{tx.counterparty} {tx.description} {tx.reference or ''}")
    if tx.amount < 0:
        # A cash machine pays nobody: a transfer to someone's account is never a withdrawal
        # ("LEVANTAMENTO TOPOGRÁFICO" is a surveyor's bill).
        if not tx.counterparty_iban and _WITHDRAWAL.search(text):
            return WITHDRAWAL
        if _CASH_BOX.search(text):
            return CASH_BOX
        return None
    if tx.amount > 0 and _CASH_DEPOSIT.search(text):
        return CASH_DEPOSIT
    return None
