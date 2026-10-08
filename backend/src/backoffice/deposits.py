"""Deposits, staged payments and amounts held back: what the words say (checklist X8, I2, I4, I5).

Event planners, wedding venues and photographers take a deposit when a date is booked; architects,
recruiters and law firms bill by milestone or take a retainer; builders are paid in stages and their
client holds part of each invoice back until the work is accepted. This module only *reads* those
facts from the evidence, in English and the packs' words (Portugal's "sinal", "caução", "retenção de garantia":
"deposits.<concept>", backoffice.countries.wording). It decides nothing: the orchestrator links a
deposit, a part payment or a release only when the evidence proves it (an exact amount, the same
customer or supplier, a reference), or when the owner confirms it with one tap (§3, §19).

* :func:`deposit_wording`: a bank line that says it is a deposit ("SINAL", "ADIANTAMENTO",
  "DEPOSIT", "RESERVA", "RETAINER") or names a quote, booking or contract ("ORC 2026/14").
  A cash deposit at a cash machine ("DEPOSITO NUMERARIO") is not one.
* :func:`is_advance_invoice`: a document titled as an advance or deposit invoice ("Fatura de
  adiantamento", "Advance invoice", "Deposit invoice").
* :func:`read_terms`: what a final invoice takes off (the deposits or advance invoices it states,
  with their number and date when printed), the amount due now, and the part held back by the
  customer ("Retenção de garantia (5%): 500,00 €", "Retention 5%: €500.00, released on 30/06/2027").
  Withholding tax ("retenção na fonte", IRS) is never taken for an amount held back.
* :func:`customer_name`: the customer printed on the business's own invoice ("Cliente: ...").
* :func:`security_deposit_wording`: a refundable security deposit ("CAUÇÃO", "SECURITY DEPOSIT", "DEPÓSITO DE
  GARANTIA", "DAMAGE DEPOSIT") a customer pays and gets back (checklist X9): money held for them, never
  income for the work. :func:`mentions_security_deposit` finds it named on an invoice ("a deduzir da caução").

The wording tables are conventions (unverified against live feeds): extend them as data is seen.
Pure Python (runs in the browser build too).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import cache

from backoffice.countries import LazyPattern, pack_alternatives, pack_text, pack_words
from backoffice.learning import fold

__all__ = [
    "Deduction",
    "DepositWords",
    "HeldBack",
    "SecurityWords",
    "Terms",
    "customer_name",
    "deposit_wording",
    "is_advance_invoice",
    "mentions_security_deposit",
    "read_terms",
    "says_held_back",
    "security_deposit_wording",
]

_ZERO = Decimal("0")

# --------------------------------------------------------------------------- wording (folded: lower case, no accents)

# A country's own words for each are in its pack ("deposits.<concept>", backoffice.countries.wording): regular-
# expression alternatives over folded text, tried with the core's English.


def _words(concept: str, english: str, *, flags: int = 0) -> LazyPattern:
    """A whole-word pattern: the packs' alternatives for ``concept``, then ``english``'s."""
    return LazyPattern(lambda: rf"(?<![a-z])(?:{pack_alternatives(f'deposits.{concept}')}|{english})(?![a-z])", flags)


# A bank line that says the money is paid ahead of the work. "deposito" alone is a cash deposit at a machine.
_DEPOSIT = _words("deposit", r"deposit|deposits|retainer|down\s*payment|advance\s+payment|prepayment|pre-payment")
# Paid to a supplier: only words that mean a deposit or advance (a hotel "reserva" paid in full is a purchase).
_DEPOSIT_OUT = _words("deposit_out", r"deposit|deposits|down\s*payment|advance\s+payment|prepayment|pre-payment")
# A refundable security deposit (checklist X9): the customer's money, held until it goes back (car rental,
# equipment hire, a flat let for the holidays). Never a deposit for the work.
_SECURITY = _words("security", r"security\s+deposits?|damage\s+deposits?|refundable\s+deposits?")
# Money out that says it gives such a deposit back.
_GIVING_BACK = _words("giving_back", r"return|returned|refund|refunded")
# Money the owner put in the bank themselves: never a customer's deposit.
_CASH_IN = _words("cash_in", r"cash\s+deposit|atm")
# The quote, proposal or contract a payment names, with its number ("ORC 2026/14", "CONTRATO 2026/7").
_REFERENCE = LazyPattern(lambda: (
    rf"(?<![a-z])(?P<word>{pack_alternatives('deposits.reference')}|quote|quotation|proposal|contract)"
    r"\.?\s*(?:n\.?\s*[oº°]?\.?\s*|no\.?\s*|nr\.?\s*|#\s*)?:?\s*"
    r"(?P<ref>(?:[a-z]{1,4}[\s-]?)?\d[\w/.-]*)"))
_REFERENCE_WORDS = {"quote": "QUOTE", "quotation": "QUOTE", "proposal": "PROPOSAL", "contract": "CONTRACT"}
# Words for the part a customer holds back until the work is accepted (retention money).
_HELD = _words("held", r"retention|holdback|held\s+back")
_GUARANTEE = LazyPattern(lambda: rf"(?<![a-z])(?:{pack_alternatives('deposits.guarantee')})(?![a-z])")
_NOT_HELD = _words("not_held", r"withholding|withheld|tax")
_RELEASE = _words("release", r"release\w*|due|until|paid\s+on|payable\s+on")
# A line taking a deposit off: words that name a deposit or advance, and a minus sign or a taking-off word.
# ("Already paid" alone describes how the invoice itself was paid: never a deposit.)
_DEDUCTION = _words("deduction", r"deposit|deposits|retainer|down\s*payment|advance(?:\s+payment)?")
_TAKEN_OFF = _words("taken_off", r"less|minus|received|paid|deducted|taken\s+off")
_DUE = _words("due", r"balance\s+(?:due|to\s+pay|remaining|outstanding)|amount\s+(?:due|payable|outstanding)"
                     r"|total\s+due|remaining\s+balance|left\s+to\s+pay|still\s+to\s+pay|due\s+now|now\s+due")
_ADVANCE_TITLE = LazyPattern(lambda: (
    rf"\s*(?:{pack_alternatives('deposits.advance_title')}"
    r"|(?:advance|deposit|down\s*payment|prepayment)\s+invoice"
    r"|invoice\s+for\s+(?:the\s+|an?\s+)?(?:deposit|advance(?:\s+payment)?|down\s*payment))(?![a-z])"))
_CUSTOMER_LINE = LazyPattern(lambda: (rf"^\s*(?:{pack_alternatives('deposits.customer')}|customer|client|"
                                      r"bill(?:ed)?\s+to)\s*[:\-]\s*(?P<name>.+?)\s*$"),
                             re.IGNORECASE)
_NAME_TAIL = LazyPattern(lambda: rf"\s*[,;|]?\s*(?:{pack_alternatives('tax_id_label')}|vat|tax\s+id)\b.*$",
                         re.IGNORECASE)
_INVOICE_LINE = LazyPattern(
    lambda: rf"\s*(?:{pack_alternatives('deposits.invoice_word')}|factura|invoice|nota)(?![a-z])")

# --------------------------------------------------------------------------- numbers and dates

_MONEY = re.compile(
    r"(?<![\d.,])(?P<neg>[-−–]\s*(?:€\s*)?)?(?:(?:€|eur)\s*)?"
    r"(?P<n>\d{1,3}(?:\.\d{3})+,\d{2}|\d{1,3}(?:[  ]\d{3})+,\d{2}|\d+,\d{2}|\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})"
    r"(?![\d]|[.,]\d)(?!\s*%)",
    re.IGNORECASE)
_PERCENT = re.compile(r"(?<![\d.,])(\d{1,2}(?:[.,]\d{1,2})?)\s*%")
_NUMBER = re.compile(r"(?<![\w/])([A-Z]{1,4} [A-Za-z0-9-]{0,20}/\d{1,9})(?![\w/])")
_NUM_DATE = re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?!\d)|(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_EN_MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
              "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
              "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12}


@cache
def _months() -> dict[str, int]:
    """Month names and abbreviations (folded): English and every pack's ("month:<n>", "month_abbr:<n>")."""
    out = dict(_EN_MONTHS)
    for n in range(1, 13):
        out.update({w: n for w in (*pack_words(f"month:{n}"), *pack_words(f"month_abbr:{n}"))})
    return out


def _month_words() -> str:
    return "|".join(sorted(_months(), key=len, reverse=True))


_WORD_DATE = LazyPattern(lambda: (
    rf"(?<![a-z0-9])(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:de\s+)?({_month_words()})\.?,?\s+(?:de\s+)?(\d{{4}})"))
_US_DATE = LazyPattern(lambda: rf"(?<![a-z])({_month_words()})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})")


def _decimal(text: str) -> Decimal | None:
    raw = text.replace(" ", " ").strip()
    if "," in raw and (raw.rfind(",") > raw.rfind(".")):
        raw = raw.replace(".", "").replace(" ", "").replace(",", ".")
    else:
        raw = raw.replace(",", "").replace(" ", "")
    try:
        return Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _amounts(line: str) -> list[tuple[Decimal, bool]]:
    """Money on a line, in order: (absolute amount, printed with a minus sign)."""
    out = []
    for m in _MONEY.finditer(line):
        value = _decimal(m.group("n"))
        if value is not None:
            out.append((value, bool(m.group("neg"))))
    return out


def _date(line: str) -> date | None:
    """The first date on a line: 30/06/2027, 2027-06-30, 30 June 2027, 30 de junho de 2027, June 30, 2027."""
    folded = fold(line)
    found: list[tuple[int, date]] = []
    for m in _NUM_DATE.finditer(line):
        try:
            if m.group(1):
                found.append((m.start(), date(int(m.group(3)), int(m.group(2)), int(m.group(1)))))
            else:
                found.append((m.start(), date(int(m.group(4)), int(m.group(5)), int(m.group(6)))))
        except ValueError:
            continue
    for m in _WORD_DATE.finditer(folded):
        try:
            found.append((m.start(), date(int(m.group(3)), _months()[m.group(2)], int(m.group(1)))))
        except (ValueError, KeyError):
            continue
    for m in _US_DATE.finditer(folded):
        try:
            found.append((m.start(), date(int(m.group(3)), _months()[m.group(1)], int(m.group(2)))))
        except (ValueError, KeyError):
            continue
    return min(found)[1] if found else None


# --------------------------------------------------------------------------- bank lines


@dataclass(frozen=True)
class DepositWords:
    """Why a bank line reads as money paid ahead of the work, in plain words."""

    reference: str | None  # the quote, booking or contract it names ("ORC 2026/14"), when it names one
    said: bool  # it calls itself a deposit ("sinal", "adiantamento", "deposit", "retainer" ...)

    @property
    def why(self) -> str:
        if self.reference and self.said:
            return f"The bank line says it is a deposit for {self.reference}."
        if self.reference:
            return f"The bank line names {self.reference}."
        return "The bank line says it is a deposit."


def deposit_wording(*texts: str | None, outgoing: bool = False) -> DepositWords | None:
    """What makes a bank line a deposit: its words or the quote or contract it names; None otherwise.

    ``outgoing``: money paid to a supplier, where only words that mean a deposit or an advance count.
    """
    folded = fold(" ".join(t for t in texts if t))
    if not folded.strip() or _CASH_IN.search(folded):
        return None
    said = bool((_DEPOSIT_OUT if outgoing else _DEPOSIT).search(folded))
    reference = None
    m = _REFERENCE.search(folded)
    if m is not None and any(ch.isdigit() for ch in m.group("ref")):
        word = _REFERENCE_WORDS.get(m.group("word")) or pack_text(f"deposits.reference_label:{m.group('word')}",
                                                                   m.group("word").upper())
        reference = f"{word} {m.group('ref').upper().rstrip('.-/')}"
    if not said and reference is None:
        return None
    return DepositWords(reference=reference, said=said)


@dataclass(frozen=True)
class SecurityWords:
    """A bank line about a refundable security deposit, in plain words."""

    giving_back: bool  # it says it gives such a deposit back ("DEVOLUCAO CAUCAO")

    @property
    def why(self) -> str:
        if self.giving_back:
            return "The bank line says it gives a security deposit back."
        return "The bank line says it is a security deposit."


def security_deposit_wording(*texts: str | None) -> SecurityWords | None:
    """A refundable security deposit ("CAUCAO VIATURA AA-12-BB", "SECURITY DEPOSIT APT 3"), or None."""
    folded = fold(" ".join(t for t in texts if t))
    if not folded.strip() or not _SECURITY.search(folded):
        return None
    return SecurityWords(giving_back=bool(_GIVING_BACK.search(folded)))


def mentions_security_deposit(text: str | None) -> bool:
    """A document that names a security deposit ("Valor retido da caução: 50,00 €", "Deducted from the security
    deposit")."""
    return bool(_SECURITY.search(fold(text or "")))


def says_held_back(*texts: str | None) -> bool:
    """A bank line about money that was held back ("RETENCAO", "GARANTIA", "RETENTION")."""
    folded = fold(" ".join(t for t in texts if t))
    return bool(_HELD.search(folded) or _GUARANTEE.search(folded)) and not _NOT_HELD.search(folded)


# --------------------------------------------------------------------------- documents


def is_advance_invoice(text: str) -> bool:
    """A document whose title calls it an advance or deposit invoice ("Fatura de adiantamento n.º ...")."""
    return any(_ADVANCE_TITLE.match(fold(line)) for line in (text or "").splitlines()[:12])


def customer_name(text: str) -> str | None:
    """The customer an invoice is addressed to, as printed ("Cliente: Atelier Lume, Lda.")."""
    for line in (text or "").splitlines():
        m = _CUSTOMER_LINE.match(line)
        if m is None:
            continue
        name = _NAME_TAIL.sub("", m.group("name")).strip(" ,:;")
        if name and not name[:1].isdigit():
            return name
    return None


@dataclass(frozen=True)
class Deduction:
    """A deposit or advance a final invoice says it takes off."""

    amount: Decimal
    number: str | None = None  # the advance invoice it names ("FT HT2026/40"), if any
    on: date | None = None  # when the deposit was received, if printed
    line: str = ""  # the line as printed


@dataclass(frozen=True)
class HeldBack:
    """The part of an invoice the customer keeps until the work is accepted (retention money)."""

    amount: Decimal
    percent: Decimal | None = None
    until: date | None = None  # when it is to be released, if printed


@dataclass(frozen=True)
class Terms:
    """What an invoice says about deposits taken off, the amount due now and any part held back."""

    deductions: tuple[Deduction, ...] = ()
    amount_due: Decimal | None = None
    held_back: HeldBack | None = None

    @property
    def deducted(self) -> Decimal:
        return sum((d.amount for d in self.deductions), _ZERO)

    @property
    def empty(self) -> bool:
        return not self.deductions and self.held_back is None

    def mode(self, total: Decimal) -> str:
        """How the deposits it takes off relate to its total.

        ``"includes"``: the total includes them and the amount due is what is left (total - deposits - held
        back = due). ``"inside"``: the total already has them taken off (total - held back = due): they are
        named for the record only. ``"unknown"``: no amount due is printed, so it cannot be told.
        ``"does_not_add_up"``: the amounts disagree. ``"none"``: it takes nothing off.
        """
        if not self.deductions:
            return "none"
        if self.amount_due is None:
            return "unknown"
        held = self.held_back.amount if self.held_back is not None else _ZERO
        total = abs(total)
        if total - self.deducted - held == self.amount_due:
            return "includes"
        if total - held == self.amount_due:
            return "inside"
        return "does_not_add_up"

    def held_back_for(self, total: Decimal) -> HeldBack | None:
        """The part held back, when the invoice's own amounts agree with it (never a guess)."""
        held = self.held_back
        if held is None or held.amount <= 0 or held.amount >= abs(total):
            return None
        if self.amount_due is None:
            return held
        total = abs(total)
        fits = {total - held.amount, total - held.amount - self.deducted}
        return held if self.amount_due in fits else None


def read_terms(text: str, *, advance: bool = False) -> Terms:
    """Deposits taken off, the amount due now and the part held back, as the invoice prints them.

    An advance invoice (``advance``) takes nothing off: its own lines describe the advance itself.
    """
    lines = (text or "").splitlines()
    deductions: list[Deduction] = []
    due: Decimal | None = None
    held: HeldBack | None = None
    for i, line in enumerate(lines):
        folded = fold(line)
        amounts = _amounts(line)
        if not amounts:
            continue
        amount, negative = amounts[-1]
        is_held = (bool(_HELD.search(folded)) or (bool(_GUARANTEE.search(folded)) and bool(_PERCENT.search(line))))
        if is_held and not _NOT_HELD.search(folded):
            if held is None:
                percent = _PERCENT.search(line)
                until = _date(line)
                if until is None and i + 1 < len(lines) and _RELEASE.search(fold(lines[i + 1])):
                    until = _date(lines[i + 1])
                pct = _decimal(percent.group(1).replace(",", ".")) if percent else None
                held = HeldBack(amount=amount, percent=pct, until=until)
            continue
        if _DUE.search(folded):
            due = amount
            continue
        if advance or _INVOICE_LINE.match(folded):
            continue
        if _DEDUCTION.search(folded) and (negative or _TAKEN_OFF.search(folded)):
            number = _NUMBER.search(line)
            on = _date(_NUMBER.sub(" ", line))
            deductions.append(Deduction(amount=amount, number=" ".join(number.group(1).split()) if number else None,
                                        on=on, line=line.strip()))
    return Terms(deductions=tuple(deductions), amount_due=due, held_back=held)
