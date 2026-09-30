"""Deposits, staged payments and amounts held back: what the words say (checklist X8, I2, I4, I5).

Event planners, wedding venues and photographers take a deposit when a date is booked; architects,
recruiters and law firms bill by milestone or take a retainer; builders are paid in stages and their
client holds part of each invoice back until the work is accepted. This module only *reads* those
facts from the evidence, in Portuguese and English. It decides nothing: the orchestrator links a
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

# A bank line that says the money is paid ahead of the work. "deposito" alone is a cash deposit at a machine.
_DEPOSIT = re.compile(
    r"(?<![a-z])(?:sinal|adiantamentos?|adiant|deposit|deposits|reserva|retainer|provisao(?:\s+de\s+fundos)?"
    r"|down\s*payment|advance\s+payment|prepayment|pre-payment|pagamento\s+antecipado|pago\s+antecipadamente)"
    r"(?![a-z])")
# Paid to a supplier: only words that mean a deposit or advance (a hotel "reserva" paid in full is a purchase).
_DEPOSIT_OUT = re.compile(
    r"(?<![a-z])(?:sinal|adiantamentos?|adiant|deposit|deposits|down\s*payment|advance\s+payment|prepayment"
    r"|pre-payment|pagamento\s+antecipado|pago\s+antecipadamente)(?![a-z])")
# A refundable security deposit (checklist X9): the customer's money, held until it goes back (car rental,
# equipment hire, a flat let for the holidays). Never a deposit for the work.
_SECURITY = re.compile(
    r"(?<![a-z])(?:caucao|caucoes|caucionamento|security\s+deposits?|damage\s+deposits?|refundable\s+deposits?"
    r"|deposito\s+(?:de\s+)?(?:garantia|caucao)|depositos\s+de\s+garantia|garantia\s+(?:de\s+)?aluguer)(?![a-z])")
# Money out that says it gives such a deposit back.
_GIVING_BACK = re.compile(r"(?<![a-z])(?:devolucao|devol|devolvida|devolvido|restituicao|reembolso|return|returned"
                          r"|refund|refunded)(?![a-z])")
# Money the owner put in the bank themselves: never a customer's deposit.
_CASH_IN = re.compile(r"(?<![a-z])(?:numerario|cash\s+deposit|atm|deposito\s+(?:em\s+)?(?:numerario|dinheiro|cheque))"
                      r"(?![a-z])")
# The quote, proposal or contract a payment names, with its number ("ORC 2026/14", "CONTRATO 2026/7").
_REFERENCE = re.compile(
    r"(?<![a-z])(?P<word>orc(?:amento)?|quote|quotation|proposta|proposal|contrato|contract)"
    r"\.?\s*(?:n\.?\s*[oº°]?\.?\s*|no\.?\s*|nr\.?\s*|#\s*)?:?\s*"
    r"(?P<ref>(?:[a-z]{1,4}[\s-]?)?\d[\w/.-]*)")
_REFERENCE_WORDS = {"orc": "ORC", "orcamento": "ORC", "quote": "QUOTE", "quotation": "QUOTE", "proposta": "PROPOSTA",
                    "proposal": "PROPOSAL", "contrato": "CONTRATO", "contract": "CONTRACT"}
# Words for the part a customer holds back until the work is accepted (retention money).
_HELD = re.compile(r"(?<![a-z])(?:retencao|retencoes|retention|holdback|held\s+back|valor\s+retido|retido|retida)"
                   r"(?![a-z])")
_GUARANTEE = re.compile(r"(?<![a-z])garantia(?![a-z])")
_NOT_HELD = re.compile(r"(?<![a-z])(?:na\s+fonte|fonte|irs|irc|withholding|withheld|imposto|tax)(?![a-z])")
_RELEASE = re.compile(r"(?<![a-z])(?:libert\w*|release\w*|devol\w*|reembols\w*|vence\w*|due|ate|until|paid\s+on|"
                      r"payable\s+on|a\s+pagar\s+em)(?![a-z])")
# A line taking a deposit off: words that name a deposit or advance, and a minus sign or a taking-off word.
# ("Already paid" / "Já pago" alone describe how the invoice itself was paid: never a deposit.)
_DEDUCTION = re.compile(
    r"(?<![a-z])(?:sinal|adiantamentos?|adiant|deposit|deposits|retainer|provisao(?:\s+de\s+fundos)?"
    r"|down\s*payment|advance(?:\s+payment)?|pagamento\s+antecipado)(?![a-z])")
_TAKEN_OFF = re.compile(
    r"(?<![a-z])(?:a\s+deduzir|deduzir|deduzido|deducao|menos|less|minus|recebido|recebida|received|pago|paga|paid"
    r"|regularizacao|regularizado|deducted|abatido|descontado|taken\s+off)(?![a-z])")
_DUE = re.compile(
    r"(?<![a-z])(?:(?:total|valor|montante|saldo)\s+a\s+pagar|a\s+pagar|saldo(?:\s+(?:em\s+divida|final|remanescente))?"
    r"|(?:valor|montante)\s+em\s+divida|balance\s+(?:due|to\s+pay|remaining|outstanding)"
    r"|amount\s+(?:due|payable|outstanding)|total\s+due|remaining\s+balance|left\s+to\s+pay|still\s+to\s+pay"
    r"|due\s+now|now\s+due)(?![a-z])")
_ADVANCE_TITLE = re.compile(
    r"\s*(?:(?:fatura|factura)(?:[\s-]+recibo)?\s+(?:de\s+)?(?:adiantamento|sinal)"
    r"|(?:advance|deposit|down\s*payment|prepayment)\s+invoice"
    r"|invoice\s+for\s+(?:the\s+|an?\s+)?(?:deposit|advance(?:\s+payment)?|down\s*payment))(?![a-z])")
_CUSTOMER_LINE = re.compile(r"^\s*(?:cliente|customer|client|bill(?:ed)?\s+to|adquirente)\s*[:\-]\s*(?P<name>.+?)\s*$",
                            re.IGNORECASE)
_NAME_TAIL = re.compile(r"\s*[,;|]?\s*(?:nif|nipc|vat|tax\s+id|contribuinte)\b.*$", re.IGNORECASE)

# --------------------------------------------------------------------------- numbers and dates

_MONEY = re.compile(
    r"(?<![\d.,])(?P<neg>[-−–]\s*(?:€\s*)?)?(?:(?:€|eur)\s*)?"
    r"(?P<n>\d{1,3}(?:\.\d{3})+,\d{2}|\d{1,3}(?:[  ]\d{3})+,\d{2}|\d+,\d{2}|\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})"
    r"(?![\d]|[.,]\d)(?!\s*%)",
    re.IGNORECASE)
_PERCENT = re.compile(r"(?<![\d.,])(\d{1,2}(?:[.,]\d{1,2})?)\s*%")
_NUMBER = re.compile(r"(?<![\w/])([A-Z]{1,4} [A-Za-z0-9-]{0,20}/\d{1,9})(?![\w/])")
_NUM_DATE = re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?!\d)|(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_MONTHS = {"janeiro": 1, "january": 1, "jan": 1, "fevereiro": 2, "february": 2, "feb": 2, "fev": 2, "marco": 3,
           "march": 3, "mar": 3, "abril": 4, "april": 4, "apr": 4, "abr": 4, "maio": 5, "may": 5, "mai": 5,
           "junho": 6, "june": 6, "jun": 6, "julho": 7, "july": 7, "jul": 7, "agosto": 8, "august": 8, "aug": 8,
           "ago": 8, "setembro": 9, "september": 9, "sept": 9, "sep": 9, "set": 9, "outubro": 10, "october": 10,
           "oct": 10, "out": 10, "novembro": 11, "november": 11, "nov": 11, "dezembro": 12, "december": 12,
           "dec": 12, "dez": 12}
_MONTH_WORDS = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WORD_DATE = re.compile(
    rf"(?<![a-z0-9])(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:de\s+)?({_MONTH_WORDS})\.?,?\s+(?:de\s+)?(\d{{4}})")
_US_DATE = re.compile(rf"(?<![a-z])({_MONTH_WORDS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})")


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
            found.append((m.start(), date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))))
        except (ValueError, KeyError):
            continue
    for m in _US_DATE.finditer(folded):
        try:
            found.append((m.start(), date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))))
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
        word = _REFERENCE_WORDS.get(m.group("word"), m.group("word").upper())
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
        if advance or re.match(r"\s*(?:fatura|factura|invoice|nota)(?![a-z])", folded):
            continue
        if _DEDUCTION.search(folded) and (negative or _TAKEN_OFF.search(folded)):
            number = _NUMBER.search(line)
            on = _date(_NUMBER.sub(" ", line))
            deductions.append(Deduction(amount=amount, number=" ".join(number.group(1).split()) if number else None,
                                        on=on, line=line.strip()))
    return Terms(deductions=tuple(deductions), amount_due=due, held_back=held)
