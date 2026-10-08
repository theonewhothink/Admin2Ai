"""Leasing and renting contracts read into a payment plan (checklist X24; cases 7, 9, 33, 35).

A contract is recognised by its title near the top ("Lease agreement", "Hire agreement"; a pack's own: Portugal's
"Contrato de locação financeira", "Contrato de aluguer de longa duração" (ALD), "leases.title"),
never by the word "leasing" alone: the leasing company's monthly invoice says "leasing" too, and it is an
invoice. From the text (a PDF's text layer, a photo read, an uploaded text) it reads:

* the leasing company and its tax number, the business (lessee / hirer) and its tax number;
* the contract number, the asset and a vehicle's plate;
* the first payment date, the term in months, the monthly payment before VAT, its VAT and the monthly
  payment with VAT (what the bank takes), and the residual value (the final payment to keep it), when stated.

The monthly amounts must add up (before VAT + VAT = with VAT); a contract whose amounts do not is never
used on a guess. :meth:`LeaseContract.schedule` is the payment plan: one line per month from the first
payment, on the same day of the month.

Which evidence closes a monthly payment depends on the country (:data:`CONTRACT_WITH_STATEMENT`; the
company's own country unless the contract is plainly British, §49): in Portugal (and in Spain, "factura"
per "cuota") the leasing company invoices every rent ("fatura" per "renda"), so each payment needs that invoice;
in the United Kingdom a hire agreement that sets out every payment and its VAT can stand as the tax document
for them, so the agreement plus the leasing company's statement showing the payment is enough. These are
conventions of each country's practice as commonly described, not verified with every tax office
(verified_as_of: never).

Pure Python, no I/O. Money is Decimal.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from backoffice._reading import amounts, first_date, tax_ids
from backoffice.countries import LazyPattern, pack_alternatives
from backoffice.learning.keys import fold
from backoffice.learning.plain import day_month, format_money

__all__ = ["CONTRACT_WITH_STATEMENT", "LeaseContract", "LeaseLine", "read_lease"]

_CENT = Decimal("0.01")
# Countries whose practice lets the contract (with every payment and its VAT) stand as the tax document for
# each monthly payment, so the leasing company's statement showing the payment is enough (module docstring).
CONTRACT_WITH_STATEMENT = frozenset({"GB"})
PAYMENT_WINDOW_DAYS = 10  # a monthly payment taken this close to its due date is that month's payment

# A country's own contract words ("Contrato de locação financeira", "renda", "com IVA") are in its pack:
# "leases.<concept>" (backoffice.countries.wording), regular-expression alternatives over folded text.


def _pt(concept: str) -> str:
    return pack_alternatives(f"leases.{concept}")


_TITLE = LazyPattern(lambda: (
    rf"(?<![a-z])(?:{_pt('title')}|(?:finance\s+|operating\s+|vehicle\s+|equipment\s+|car\s+)?lease\s+agreement|"
    r"(?:vehicle\s+|contract\s+|equipment\s+)?hire\s+agreement|leasing\s+agreement)(?![a-z])"))
_NOT_A_CONTRACT = LazyPattern(lambda: (rf"(?<![a-z])(?:{_pt('document')}|factura|invoice|recibo|receipt)"
                                       r"\s*(?:n\.?\s*[ºo°]|no\.?|number|#|:)"))
_RENTING = LazyPattern(lambda: rf"(?<![a-z])(?:renting|{_pt('renting')}|operating\s+lease|hire|rental\s+agreement)(?![a-z])")

_LESSOR = LazyPattern(lambda: (rf"^\s*(?:{_pt('article')}|the\s+)?(?:{_pt('lessor')}|lessor|owner|leasing\s+company|"
                               r"finance\s+company)\b\s*(?:\([^)]*\))?\s*[:\-]?\s*(.*)$"))
_CUSTOMER = LazyPattern(lambda: (rf"^\s*(?:{_pt('article')}|the\s+)?(?:{_pt('customer')}|lessee|hirer|customer)\b"
                                 r"\s*(?:\([^)]*\))?\s*[:\-]?\s*(.*)$"))
_NUMBER = LazyPattern(lambda: (rf"(?:{_pt('contract')}|contract|agreement)\s*(?:de\s+\w+(?:\s+\w+)?\s+)?"
                               r"(?:n\.?\s*[ºo°]\.?|no\.?|number|#|numero|nr\.?)\s*[:.]?\s*"
                               r"([A-Z0-9][A-Z0-9/\-.]{2,24}[A-Z0-9])"), re.IGNORECASE)
_ASSET = LazyPattern(lambda: (rf"^\s*(?:{_pt('asset')}|vehicle|asset|goods|equipment)\b[^:]{{0,30}}:\s*(.+)$"))
_PLATE_LABEL = LazyPattern(lambda: rf"\s*[,;]?\s*(?:{_pt('plate_label')}|registration|reg\.)", re.IGNORECASE)
_PLATE = LazyPattern(lambda: (rf"(?:{_pt('plate')}|registration(?:\s+(?:no|number|mark))?|reg\.?\s*(?:no|mark)?)"
                              r"\s*[:.]?\s*([a-z0-9]{2}[- ]?[a-z0-9]{2}[- ]?[a-z0-9]{2,3})(?![a-z0-9])"))
_START = LazyPattern(lambda: (rf"^\s*(?:{_pt('start')}|start\s+date|commencement(?:\s+date)?|"
                              r"first\s+(?:monthly\s+)?(?:payment|rental|instalment)(?:\s+date|\s+due)?|"
                              r"date\s+of\s+first\s+payment)\b"))
_TERM = LazyPattern(lambda: (rf"(?:{_pt('term')}|term|period|number\s+of\s+(?:rentals|payments|instalments|"
                             r"installments))\b[^0-9\n]{0,20}(\d{1,3})\s*"
                             rf"(months|{_pt('term_unit')}|rentals|payments|instalments|installments)?"))
_TERM_ALONE = LazyPattern(lambda: (rf"(?<!\d)(\d{{1,3}})\s+(?:{_pt('term_alone')}|monthly\s+(?:rentals|payments|"
                                   r"instalments))(?![a-z])"))
_INSTALMENT = LazyPattern(lambda: (rf"(?<![a-z])(?:{_pt('instalment')}|rental|rentals|instalment|installment|"
                                   r"monthly\s+payment|monthly\s+rent)(?![a-z])"))
_NOT_INSTALMENT = LazyPattern(lambda: (rf"(?<![a-z])(?:{_pt('not_instalment')}|number\s+of|first|initial|due|date|"
                                       r"day\s+of|residual|final|deposit|advance)(?![a-z])"))
_GROSS_WORDS = LazyPattern(lambda: (rf"(?<![a-z])(?:{_pt('gross')}|incl(?:uding|\.)?\s+vat|inc\.?\s+vat|total)"
                                    r"(?![a-z])"))
_NET_WORDS = LazyPattern(lambda: (rf"(?<![a-z])(?:{_pt('net')}|excl(?:uding|\.)?\s+vat|ex\.?\s+vat|\+\s*vat|"
                                  r"plus\s+vat|before\s+vat|base)(?![a-z])"))
_VAT_LINE = LazyPattern(lambda: rf"(?<![a-z])(?:{_pt('vat')}|vat)(?![a-z])")
# A line about the monthly payment, and one that says VAT is added on top of the amount it shows.
_MONTHLY = LazyPattern(lambda: rf"{_pt('monthly')}|rental|instalment|installment|monthly")
_MONTHLY_VAT = LazyPattern(lambda: rf"{_pt('monthly')}|rental|instalment|monthly")
_PLUS_VAT = LazyPattern(lambda: rf"{_pt('plus_vat')}|\+\s*vat|plus\s+vat")
_PLUS_VAT_FINAL = LazyPattern(lambda: rf"{_pt('plus_vat_final')}|plus\s+vat|\+\s*vat")
_TAX_ID_TAIL = LazyPattern(lambda: rf"\s*(?:,\s*)?(?:{pack_alternatives('tax_id_label')}|VAT(?:\s+No\.?)?)\b",
                           re.IGNORECASE)
_RATE = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s*%")
_RESIDUAL = LazyPattern(lambda: (rf"(?<![a-z])(?:{_pt('residual')}|residual\s+value|purchase\s+option|"
                                 r"option\s+to\s+purchase(?:\s+fee)?|balloon(?:\s+payment)?|final\s+payment)(?![a-z])"))
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass(frozen=True)
class LeaseLine:
    """One payment of the plan."""

    number: int  # 1-based
    due: date
    amount: Decimal  # with VAT: what the bank takes
    residual: bool = False  # the final payment to keep the asset


@dataclass(frozen=True)
class LeaseContract:
    kind: str  # "leasing" (a finance lease) | "renting" (an operating lease or long-term hire)
    lessor: str
    lessor_tax_id: str | None
    lessor_email: str | None
    customer: str | None
    customer_tax_id: str | None
    number: str | None
    asset: str | None
    plate: str | None
    start: date | None
    term: int | None
    net: Decimal | None
    vat: Decimal | None
    vat_rate: Decimal | None
    gross: Decimal | None
    residual: Decimal | None
    currency: str
    country: str
    problems: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing and not self.problems

    @property
    def contract_suffices(self) -> bool:
        """The contract plus the leasing company's statement can prove a monthly payment (module docstring)."""
        return self.country in CONTRACT_WITH_STATEMENT

    def m(self, value: Decimal) -> str:
        return format_money(value, self.currency)

    @property
    def what(self) -> str:
        """The asset as the owner calls it: 'the Renault Kangoo (12-AB-34)'."""
        if self.asset and self.plate and fold(self.plate).replace("-", "") not in fold(self.asset).replace("-", ""):
            return f"the {self.asset} ({self.plate})"
        if self.asset:
            return f"the {self.asset}"
        return f"the vehicle {self.plate}" if self.plate else "the leased equipment"

    @property
    def word(self) -> str:
        return "renting" if self.kind == "renting" else "leasing"

    def due(self, number: int) -> date:
        assert self.start is not None
        months = self.start.month - 1 + number - 1
        year, month = self.start.year + months // 12, months % 12 + 1
        return date(year, month, min(self.start.day, calendar.monthrange(year, month)[1]))

    def schedule(self) -> list[LeaseLine]:
        if not self.complete or self.start is None or self.term is None or self.gross is None:
            return []
        lines = [LeaseLine(number=n, due=self.due(n), amount=self.gross) for n in range(1, self.term + 1)]
        if self.residual:
            lines.append(LeaseLine(number=self.term + 1, due=self.due(self.term + 1), amount=self.residual,
                                   residual=True))
        return lines

    def line_near(self, day: date, *, skip: set[int] = frozenset(), window: int = PAYMENT_WINDOW_DAYS  # type: ignore[assignment]
                  ) -> LeaseLine | None:
        """The payment of the plan due closest to ``day`` (within ``window`` days), not already paid."""
        near = [line for line in self.schedule() if line.number not in skip and abs((line.due - day).days) <= window]
        return min(near, key=lambda line: (abs((line.due - day).days), line.number)) if near else None

    def describe(self, today: date | None = None) -> str:
        """'48 monthly payments of €350.00, the first on 5 March 2026'"""
        if self.term is None or self.gross is None or self.start is None:
            return "its monthly payments"
        return f"{self.term} monthly payments of {self.m(self.gross)}, the first on {day_month(self.start, today)}"


# --------------------------------------------------------------------------- reading


def _q(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _after_label(lines: list[str], folded: list[str], i: int, captured: str) -> str:
    """The value after a label, or on the next line when the label stands alone."""
    raw = lines[i]
    colon = raw.find(":")
    value = raw[colon + 1:].strip() if colon >= 0 else captured.strip()
    if not value and i + 1 < len(lines):
        value = lines[i + 1].strip()
    value = _TAX_ID_TAIL.split(value, maxsplit=1)[0]
    return value.strip(" ,;-")


def _party(lines: list[str], folded: list[str], pattern: re.Pattern[str]) -> tuple[str | None, str | None, int | None]:
    for i, line in enumerate(folded):
        m = pattern.match(line)
        if not m:
            continue
        name = _after_label(lines, folded, i, m.group(1))
        ids = tax_ids(" ".join(lines[i:i + 3]))
        return (name or None), (ids[0] if ids else None), i
    return None, None, None


def read_lease(text: str, home: str = "PT") -> LeaseContract | None:
    """The contract in ``text``, or None when it is not a leasing or renting contract. ``home`` is the country of
    the company it is for (§49): its practice applies unless the contract is plainly British (a GB VAT number or
    pounds)."""
    if not text or not text.strip():
        return None
    lines = [line for line in text.splitlines()]
    folded = [fold(line) for line in lines]
    head = "\n".join(folded[:15])
    title = _TITLE.search(head)
    if title is None or _NOT_A_CONTRACT.search(head):
        return None
    whole = "\n".join(folded)
    lessor, lessor_tax, lessor_at = _party(lines, folded, _LESSOR)
    customer, customer_tax, _ = _party(lines, folded, _CUSTOMER)
    number = _NUMBER.search(text)
    asset = next((_ASSET.match(f) for f in folded if _ASSET.match(f)), None)
    asset_text = None
    if asset is not None:
        i = next(i for i, f in enumerate(folded) if _ASSET.match(f))
        asset_text = lines[i][lines[i].find(":") + 1:].strip()
        asset_text = _PLATE_LABEL.split(asset_text, maxsplit=1)[0].strip(" ,;-") or None
    plate = _PLATE.search(whole)
    start = next((first_date(lines[i]) for i, f in enumerate(folded) if _START.match(f) and first_date(lines[i])),
                 None)
    term_m = _TERM.search(whole) or _TERM_ALONE.search(whole)
    term = int(term_m.group(1)) if term_m else None
    currency = "GBP" if "£" in text or re.search(r"\bGBP\b", text) else "EUR"
    net, vat, gross, rate, problems = _instalment(lines, folded)
    residual = None
    for raw, f in zip(lines, folded, strict=True):
        if _RESIDUAL.search(f):
            found = amounts(raw)
            if found:
                residual = found[-1]
                break
    email = None
    if lessor_at is not None:
        near = " ".join(lines[max(0, lessor_at - 1):lessor_at + 5])
        found_email = _EMAIL.search(near)
        email = found_email.group(0).lower() if found_email else None
    country = "GB" if (lessor_tax or "").startswith("GB") or currency == "GBP" else home
    missing = tuple(name for name, value in (("the leasing company", lessor), ("the first payment date", start),
                                             ("the term", term), ("the monthly payment", gross)) if not value)
    return LeaseContract(
        kind="renting" if _RENTING.search(title.group(0)) else "leasing", lessor=lessor or "the leasing company",
        lessor_tax_id=lessor_tax, lessor_email=email, customer=customer, customer_tax_id=customer_tax,
        number=number.group(1) if number else None, asset=asset_text, plate=plate.group(1).upper() if plate else None,
        start=start, term=term, net=net, vat=vat, vat_rate=rate, gross=gross, residual=residual, currency=currency,
        country=country, problems=tuple(problems), missing=missing)


def _instalment(lines: list[str], folded: list[str]
                ) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None, list[str]]:
    """The monthly payment before VAT, its VAT, with VAT, the VAT rate, and what does not add up."""
    net = vat = gross = rate = None
    plain: list[Decimal] = []
    plus_vat = False
    for raw, f in zip(lines, folded, strict=True):
        found = amounts(raw)
        rates = [Decimal(r.replace(",", ".")) for r in _RATE.findall(f)]
        # "IVA sobre a renda (23%): 65,45 €" is the VAT of the monthly payment, not a monthly payment.
        vat_line = _VAT_LINE.search(f) and not _GROSS_WORDS.search(f) and not _NET_WORDS.search(f)
        if vat_line and found and len(found) < 3 and _MONTHLY.search(f):
            vat = vat or found[-1]
            rate = rate or (rates[0] if rates else None)
            continue
        if _INSTALMENT.search(f) and not _NOT_INSTALMENT.search(f.split(":")[0]) and found:
            rate = rate or (rates[0] if rates else None)
            if len(found) >= 3 and found[-3] + found[-2] == found[-1]:
                net, vat, gross = net or found[-3], vat or found[-2], gross or found[-1]
                continue
            if _GROSS_WORDS.search(f):
                gross = gross or found[-1]
            elif _NET_WORDS.search(f):
                net = net or found[0]
                plus_vat = plus_vat or bool(_PLUS_VAT.search(f))
            else:
                plain.append(found[-1])
            continue
        if _VAT_LINE.search(f) and found and _MONTHLY_VAT.search(f):
            vat = vat or found[-1]
            rate = rate or (rates[0] if rates else None)
        elif _VAT_LINE.search(f) and rates and not found:
            rate = rate or rates[0]
    problems: list[str] = []
    if plain and net is None and gross is None:
        if plus_vat or any(_PLUS_VAT_FINAL.search(f) for f in folded):
            net = plain[0]
        else:
            gross = plain[0]
    if gross is not None and net is not None and vat is not None and net + vat != gross:
        problems.append(f"The monthly payment before VAT ({format_money(net)}) and its VAT ({format_money(vat)}) do "
                        f"not add up to the monthly payment with VAT ({format_money(gross)}).")
    if gross is None and net is not None and vat is not None:
        gross = net + vat
    if gross is None and net is not None and rate is not None:
        vat = _q(net * rate / 100)
        gross = net + vat
    if net is None and gross is not None and vat is not None:
        net = gross - vat
    if net is None and gross is not None and rate is not None:
        net = _q(gross * 100 / (100 + rate))
        vat = gross - net
    if vat is None and gross is not None and net is not None:
        vat = gross - net
    return net, vat, gross, rate, problems
