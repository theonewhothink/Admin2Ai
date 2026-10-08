"""Payslips: the evidence a salary needs (§21, checklist J3).

A salary transfer to an employee is proven by that month's payslip for that
employee (a payslip; a pack's own words for one: Portugal's "recibo de vencimento"), never by the words on the
bank line alone. :func:`read_payslip` reads a payslip's text (a text file, or the text layer
of a PDF): who it is for (name, tax number, the IBAN the pay goes to), who pays
(the employer's name and tax number), the month it covers, and the figures:
gross pay, Social Security and income tax (IRS) withheld, other deductions and
the net pay that reaches the employee's account.

Only a document that calls itself a payslip is read as one, and only when the
net pay, the employee and the month are all readable. Its figures must add up
(gross - deductions = net, to the cent) when the gross is printed; a payslip
whose figures do not add up is kept but proves nothing (RED).

:func:`salary_proof` decides whether one payslip proves one bank payment:

* the same employee: the payment went to the IBAN printed on the payslip (or an
  IBAN already known for that employee), else the bank line names the employee
  (every word of the shorter name appears in the longer);
* the same month: paid in the payslip's month or in the first ten days of the next;
* the same amount: the net pay, to the cent, in the same currency.

The Social Security contributions and the IRS withheld on salaries are paid to
the state separately; those payments keep their tax rules (the tax letter or
payment proof), never a payslip.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from functools import cache

from backoffice.closure import Month
from backoffice.countries import LazyPattern, pack_alternatives, pack_words
from backoffice.domain.models import Quality, Transaction
from backoffice.extraction.values import parse_amount, parse_date
from backoffice.fraud import find_ibans, normalize_iban
from backoffice.learning import fold, format_money

__all__ = ["PAID_WITHIN_DAYS", "Payslip", "SalaryProof", "looks_like_payslip", "person_name", "read_payslip",
           "salary_proof"]

PAID_WITHIN_DAYS = 10  # a salary for September may be paid up to 10 October

# A country's own payslip words ("recibo de vencimento", "Segurança Social", "IRS") are in its pack:
# "payroll.<concept>" (backoffice.countries.wording), read with the core's English.
_TITLE = LazyPattern(lambda: (rf"(?<![a-z])(?:{pack_alternatives('payroll.title')}|payslip|pay\s+slip|salary\s+slip|"
                              r"pay\s+stub|nomina)(?![a-z])"))
_EMPLOYEE = LazyPattern(lambda: (rf"^\s*(?:{pack_alternatives('payroll.employee')}|employee(?:\s+name)?)\s*:\s*"
                                 r"(?P<v>.+)$"))
_EMPLOYER = LazyPattern(lambda: rf"^\s*(?:{pack_alternatives('payroll.employer')}|employer|company)\s*:\s*(?P<v>.+)$")
_TAX_ID = LazyPattern(lambda: (rf"(?<![a-z])(?:{pack_alternatives('tax_id_label')}|tax\s*id|vat)[^0-9]{{0,12}}"
                               r"(?P<v>(?:[a-z]{2})?\d[\d ]{7,12}\d)"))
_PERIOD = LazyPattern(lambda: (rf"^\s*(?:{pack_alternatives('payroll.period')}|pay\s+period|period|month)"
                               r"\s*:\s*(?P<v>.+)$"))
_DATE = LazyPattern(lambda: (rf"^\s*(?:{pack_alternatives('payroll.date')}|date|payment\s+date|paid\s+on)\s*:\s*"
                             r"(?P<v>.+)$"))
_TAIL = LazyPattern(lambda: rf"(?i)\s*(?:[-,;|]\s*)?(?:{pack_alternatives('tax_id_label')}|tax\s*id|vat)\b")
_AMOUNT = re.compile(r"\d{1,3}(?:[. ]\d{3})*,\d{2}|\d+,\d{2}|\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2}")

# A figure's labels (folded, plain): the core's English and a pack's ("payroll.label:<figure>").
_LABELS = {
    "gross": ("gross pay", "total gross", "gross salary", "gross"),
    "social": ("social security", "national insurance"),
    "income_tax": ("income tax", "paye", "withholding"),
    "other": ("other deductions", "union dues"),
    "deductions": ("total deductions",),
    "net": ("net pay", "net salary", "take home pay", "take-home pay", "amount paid"),
}
_EN_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
              "november", "december")


@cache
def _labels(figure: str) -> tuple[str, ...]:
    return (*pack_words(f"payroll.label:{figure}"), *_LABELS[figure])


@cache
def _month_words() -> dict[str, int]:
    """Full month names (folded) -> month: every pack's ("month:<n>"), then English (the first found wins)."""
    out = {name: n for n in range(1, 13) for name in pack_words(f"month:{n}")}
    out.update({name: n for n, name in enumerate(_EN_MONTHS, 1) if name not in out})
    return out


@dataclass(frozen=True)
class Payslip:
    employee: str
    period: Month
    net: Decimal
    employee_tax_id: str | None = None
    employee_iban: str | None = None
    employer: str | None = None
    employer_tax_id: str | None = None
    issued_on: date | None = None
    gross: Decimal | None = None
    social_security: Decimal | None = None
    income_tax: Decimal | None = None
    other_deductions: Decimal | None = None
    total_deductions: Decimal | None = None
    currency: str = "EUR"

    @property
    def deductions(self) -> Decimal | None:
        parts = [d for d in (self.social_security, self.income_tax, self.other_deductions) if d is not None]
        if self.total_deductions is not None:
            return self.total_deductions
        return sum(parts, Decimal(0)) if parts else None

    @property
    def adds_up(self) -> bool | None:
        """gross - deductions = net to the cent; None when the gross is not printed."""
        if self.gross is None:
            return None
        return self.gross - (self.deductions or Decimal(0)) == self.net

    @property
    def quality(self) -> Quality:
        """GREEN when its figures add up, RED when they don't, AMBER when there is no gross to check."""
        check = self.adds_up
        return Quality.AMBER if check is None else Quality.GREEN if check else Quality.RED

    @property
    def paid_on_or_before(self) -> date:
        return self.period.last_day + timedelta(days=PAID_WITHIN_DAYS)

    def reasons(self) -> tuple[str, ...]:
        """Plain lines on the payslip's own figures."""
        if self.gross is None:
            return ("The payslip shows the net pay but not the gross, so I can't check its figures.",)
        if not self.adds_up:
            return (f"The payslip does not add up: {format_money(self.gross, self.currency)} gross minus "
                    f"{format_money(self.deductions or Decimal(0), self.currency)} of deductions is not "
                    f"{format_money(self.net, self.currency)}.",)
        return (f"Gross {format_money(self.gross, self.currency)} minus "
                f"{format_money(self.deductions or Decimal(0), self.currency)} of deductions is the net pay of "
                f"{format_money(self.net, self.currency)}.",)


def looks_like_payslip(text: str) -> bool:
    return bool(_TITLE.search(fold(text or "")))


def _last_amount(line: str) -> Decimal | None:
    found = _AMOUNT.findall(line)
    return parse_amount(found[-1]) if found else None


def _labelled(lines: Iterable[tuple[str, str]], labels: tuple[str, ...]) -> Decimal | None:
    for folded, raw in lines:
        head = folded.split(":", 1)[0] if ":" in folded else folded
        if any(re.search(rf"(?<![a-z]){re.escape(label)}(?![a-z])", head) for label in labels):
            amount = _last_amount(raw)
            if amount is not None:
                return abs(amount)
    return None


def _period(value: str) -> Month | None:
    text = fold(value)
    if m := re.search(r"(?<!\d)(\d{1,2})\s*[/.-]\s*(\d{4})(?!\d)", text):
        month, year = int(m.group(1)), int(m.group(2))
        return Month(year, month) if 1 <= month <= 12 else None
    if m := re.search(r"(?<!\d)(\d{4})\s*[/.-]\s*(\d{1,2})(?!\d)", text):
        year, month = int(m.group(1)), int(m.group(2))
        return Month(year, month) if 1 <= month <= 12 else None
    for word, number in _month_words().items():
        if re.search(rf"(?<![a-z]){word}(?![a-z])", text) and (y := re.search(r"(?<!\d)(\d{4})(?!\d)", text)):
            return Month(int(y.group(1)), number)
    if m := re.search(r"(\d{1,2}[/.-]\d{1,2}[/.-]\d{4})", text):  # "01/09/2026 - 30/09/2026": the month it ends in
        dates = [parse_date(d, day_first=True) for d in re.findall(r"\d{1,2}[/.-]\d{1,2}[/.-]\d{4}", text)]
        dates = [d for d in dates if d is not None]
        return Month.of(dates[-1]) if dates else None
    return None


def read_payslip(text: str) -> Payslip | None:
    """A payslip's details, or None when the text is not a readable payslip (module docstring)."""
    if not looks_like_payslip(text):
        return None
    lines = [(fold(line), line) for line in (text or "").splitlines() if line.strip()]
    employee = employer = employee_tax = employer_tax = None
    period = None
    issued = None
    section = None
    for folded, raw in lines:
        if (m := _EMPLOYER.match(folded)) is not None:
            section = "employer"
            employer = _name(raw)
        elif (m := _EMPLOYEE.match(folded)) is not None:
            section = "employee"
            employee = _name(raw)
        if (t := _TAX_ID.search(folded)) is not None:
            digits = re.sub(r"\s+", "", t.group("v")).upper()
            if section == "employee" and employee_tax is None:
                employee_tax = digits
            elif section == "employer" and employer_tax is None:
                employer_tax = digits
        if period is None and (m := _PERIOD.match(folded)) is not None:
            period = _period(m.group("v"))
        if issued is None and (m := _DATE.match(folded)) is not None:
            issued = parse_date(m.group("v").strip(), day_first=True)
    ibans = find_ibans(text or "")
    net = _labelled(lines, _labels("net"))
    if not employee or period is None or net is None or net <= 0:
        return None
    return Payslip(
        employee=employee, period=period, net=net, employee_tax_id=employee_tax,
        employee_iban=normalize_iban(ibans[0]) if ibans else None, employer=employer, employer_tax_id=employer_tax,
        issued_on=issued, gross=_labelled(lines, _labels("gross")), social_security=_labelled(lines, _labels("social")),
        income_tax=_labelled(lines, _labels("income_tax")), other_deductions=_labelled(lines, _labels("other")),
        total_deductions=_labelled(lines, _labels("deductions")),
    )


def _name(raw: str) -> str | None:
    """The value after the label's colon, as printed, without a tax number that follows it on the same line."""
    value = raw.split(":", 1)[1] if ":" in raw else ""
    value = _TAIL.split(value, maxsplit=1)[0]
    value = " ".join(value.split()).strip(" ,;-|")
    return value or None


def person_name(text: str) -> str:
    """A person's name as people write it: 'RUI PEDRO SANTOS' -> 'Rui Pedro Santos' (bank lines shout)."""
    words = " ".join((text or "").split()).split(" ")
    if not any(w.isalpha() and w.islower() for w in words):
        words = [w.capitalize() if w.isupper() else w for w in words]
    return " ".join(words)


# --------------------------------------------------------------------------- proof of a salary


@dataclass(frozen=True)
class SalaryProof:
    why: tuple[str, ...]
    headline: str
    by_iban: bool


def _name_words(text: str | None) -> set[str]:
    return {w for w in re.split(r"[^a-z]+", fold(text or "")) if len(w) > 1}


def same_person(bank_name: str, payslip_name: str) -> bool:
    """Every word of the shorter name is in the longer one ("ANA COSTA" / "Ana Maria Costa"), two at least."""
    a, b = _name_words(bank_name), _name_words(payslip_name)
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 2 and short <= long_


def salary_proof(tx: Transaction, payslip: Payslip, *, known_ibans: Mapping[str, str] | None = None
                 ) -> SalaryProof | None:
    """How ``payslip`` proves ``tx`` (module docstring), or None when it does not."""
    if tx.amount >= 0 or tx.currency.strip().upper() != payslip.currency or abs(tx.amount) != payslip.net:
        return None
    if not payslip.period.first_day <= tx.booked_on <= payslip.paid_on_or_before:
        return None
    iban = normalize_iban(tx.counterparty_iban or "")
    employee_ibans = {normalize_iban(payslip.employee_iban)} if payslip.employee_iban else set()
    employee_ibans |= {i for i, who in (known_ibans or {}).items() if same_person(who, payslip.employee)}
    by_iban = bool(iban) and iban in employee_ibans
    if not by_iban and not same_person(tx.counterparty, payslip.employee):
        return None
    if iban and employee_ibans and not by_iban:
        return None  # paid to another account than the employee's: not this payslip's pay
    who = payslip.employee
    amount = format_money(payslip.net, payslip.currency)
    why = [f"Payslip for {who}, {payslip.period.name} {payslip.period.year}",
           f"Net pay {amount} = payment {amount}",
           (f"Paid to {who}'s account ending in {iban[-4:]}, as on the payslip" if by_iban
            else f"The bank line names {who}")]
    return SalaryProof(why=tuple(why), headline=f"{who}'s salary for {payslip.period.name}", by_iban=by_iban)
