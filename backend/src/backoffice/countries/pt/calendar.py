"""Portugal's statutory tax calendar for small companies (QA P6, §24 for Portugal): data and rules.

Each :class:`Entry` is one statutory deadline with the rule that sets its date, who does it (the accountant
files, the owner pays), the consequence of missing it, the proof that closes it and, in the comments above
it, the sources that confirm it with the day they were read. An entry whose date no source confirmed stays
out (never an invented date). What every entry shares:

* **A deadline on a Saturday, Sunday or national public holiday moves to the next working day**, unless the
  law says "independentemente de esse dia ser útil ou não útil" (Modelo 22, IES). CPPT art. 20.º n.º 1
  (deadlines ending on a day the offices are closed move to the first working day); the official 2026
  calendar applies it throughout (VAT paid on 26 January, 27 April, 27 July, 26 October and 28 December 2026).
  Municipal holidays are not considered (nor does the official calendar, its note 2).
  - https://www.pgdlisboa.pt/leis/lei_mostra_articulado.php?nid=256&tabela=leis (CPPT art. 20.º), read 2026-10-08
* **August ("férias fiscais e contributivas")**: tax deadlines that end in August move to 31 August
  (LGT art. 57.º-A, declarations and payments alike), and so do Social Security payments (Código
  Contributivo art. 23.º-B: "até ao último dia desse mês, independentemente de ser dia útil").
  - https://www.occ.pt/sites/default/files/public/2026-07/FeriasFiscais2026.pdf, read 2026-10-08
  - https://info.portaldasfinancas.gov.pt/pt/apoio_contribuinte/questoes_frequentes/pages/faqs-00978.aspx
    (invoices due in August: "até ao último dia daquele mês, independentemente de ser dia útil"), read 2026-10-08
* **The official 2026 calendar** (Autoridade Tributária, "Agenda Fiscal 2026", with the dispatches of the
  Secretary of State for Tax Affairs listed in its notes) confirms every 2026 date the rules give, and names
  the three 2026 dates a dispatch moved (:data:`OVERRIDES`). Tests check the rules against it.
  - https://info.portaldasfinancas.gov.pt/pt/apoio_contribuinte/calendario_fiscal/Documents/Obrigacoes_declarativas.pdf
  - https://info.portaldasfinancas.gov.pt/pt/apoio_contribuinte/calendario_fiscal/Documents/Obrigacoes_pagamento.pdf
    both read 2026-10-08

Left out on purpose: the monthly Social Security pay declaration (Declaração de Remunerações): during 2026
employers move one by one to the simplified contribution cycle, after which they file none, and which model
a company is on cannot be told from its evidence (seg-social.pt, "Simplificação do Ciclo Contributivo",
read 2026-10-08). The VAT of the small retailers' scheme, the State surcharge's extra advance payments and
the IRS of sole traders are not small-company deadlines this calendar covers.

Which entries a company gets depends on its :class:`~backoffice.countries.base.TaxProfile`: its VAT rhythm
(monthly or quarterly), whether it pays salaries, makes advance payments or pays other income with tax
withheld. Each fact is set by the owner or the accountant, or learned from the company's own evidence
(:func:`learn_profile`): two VAT payments naming two monthly (or two quarterly) periods, or paid one month
after the other; salaries, payslips or Social Security payments in two different months; an advance payment
of corporate income tax this year. A fact nobody set and the evidence does not show stays unknown, and the
entries that depend on it stay out.
"""

from __future__ import annotations

import calendar as _calendar
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from types import MappingProxyType

from backoffice.countries.base import LearnedProfile, PeriodicObligation, TaxProfile, TaxSignal

from .holidays import national_holidays

__all__ = ["ENTRIES", "OVERRIDES", "RETRIEVED", "Entry", "calendar_proof", "deadline", "learn_profile",
           "obligations", "next_working_day"]

RETRIEVED = date(2026, 10, 8)  # the day every source below was read

# --------------------------------------------------------------------------- sources

AGENDA_FILINGS = ("https://info.portaldasfinancas.gov.pt/pt/apoio_contribuinte/calendario_fiscal/Documents/"
                  "Obrigacoes_declarativas.pdf")
AGENDA_PAYMENTS = ("https://info.portaldasfinancas.gov.pt/pt/apoio_contribuinte/calendario_fiscal/Documents/"
                   "Obrigacoes_pagamento.pdf")
CIVA_41 = "https://info.portaldasfinancas.gov.pt/pt/informacao_fiscal/codigos_tributarios/civa_rep/Pages/iva41.aspx"
CIVA_27 = "https://info.portaldasfinancas.gov.pt/pt/informacao_fiscal/codigos_tributarios/civa_rep/Pages/iva27.aspx"
DL_198_2012 = "https://diariodarepublica.pt/dr/legislacao-consolidada/decreto-lei/2012-106458692"
EFATURA_FAQ = ("https://info.portaldasfinancas.gov.pt/pt/apoio_contribuinte/questoes_frequentes/pages/"
               "faqs-00978.aspx")
EFATURA_APRIL_2026 = "https://occ.pt/pt-pt/noticias/comunicacao-de-faturas-prazo-prorrogado-ate-8-de-abril"
EFATURA_MAY_2026 = "https://invoicexpress.com/blog/calendario-fiscal-maio/"
CIRS_119 = "https://info.portaldasfinancas.gov.pt/pt/informacao_fiscal/codigos_tributarios/cirs_rep/Pages/irs119.aspx"
CIRC_120 = "https://info.portaldasfinancas.gov.pt/pt/informacao_fiscal/codigos_tributarios/circ_rep/Pages/irc120.aspx"
CIRC_121 = "https://info.portaldasfinancas.gov.pt/pt/informacao_fiscal/codigos_tributarios/circ_rep/Pages/irc121.aspx"
CIRC_104 = "http://bdjur.almedina.net/citem.php?field=item_id&value=1425396"
MODELO_22_2026 = "https://www.occ.pt/pt-pt/noticias/declaracao-modelo-22-prazo-de-entrega-alargado-ate-30-de-junho"
SS_DAY_25 = ("https://www.gov.pt/noticias/prazo-de-pagamento-das-contribuicoes-das-entidades-empregadoras-"
             "alargado-ate-ao-dia-25")
SS_GUIDE = "https://www.seg-social.pt/ptss/pssd/documento/cmc1y95ap00dkkl2yk3g4tdla"
FERIAS_FISCAIS = "https://www.occ.pt/sites/default/files/public/2026-07/FeriasFiscais2026.pdf"
CPPT_20 = "https://www.pgdlisboa.pt/leis/lei_mostra_articulado.php?nid=256&tabela=leis"

# --------------------------------------------------------------------------- entries


@dataclass(frozen=True)
class Entry:
    """One statutory deadline and its rule.

    The period is a month, a quarter or a year (``cadence``). The deadline falls in the month ``months_after``
    months after the period's last month (negative: before it ends, as for advance payments), on ``day`` (0:
    that month's last day). ``working_day``: a deadline on a closed day moves to the next working day.
    ``august``: a deadline that falls in August moves to 31 August. ``september``: the June (or second-quarter)
    VAT deadline is moved to this day of September by law. ``opens``: "period_end" (the day after the period
    ends) or "due_month" (the first day of the deadline's month): the obligation is added from then until its
    deadline. ``needs``: the profile fact it depends on. ``first_period``: the first period the rule covers.
    ``words``: folded words (lower case, no accents) that name it on a receipt; ``bank_words`` on a payment's
    bank line. ``title`` and ``proof`` take the period's name ("{period}")."""

    code: str
    title: str
    kind: str  # ObligationKind value: vat_return, filing or tax_deadline
    responsible: str
    issuer: str
    cadence: str  # "month" | "quarter" | "year"
    needs: str
    months_after: int
    day: int
    working_day: bool
    consequence: str
    proof: str
    rule: str  # the rule in plain words, for the owner's "why"
    sources: tuple[str, ...]
    words: tuple[str, ...] = ()
    bank_words: tuple[str, ...] = ()
    august: bool = False
    september: int = 0
    opens: str = "period_end"
    first_period: str = ""


_VAT_BANK = ("iva",)
_LATE_FILING = "Filing late may lead to a fine."
_LATE_PAYMENT = "Paying late adds interest and may lead to a fine."

ENTRIES: tuple[Entry, ...] = (
    # Periodic VAT return, monthly regime (turnover of €650,000 or more, or by option): by the 20th of the
    # second month after the month; June's by 20 September. CIVA art. 41.º n.º 1 a) and n.º 10.
    # Sources (read 2026-10-08): CIVA_41; AGENDA_FILINGS (2026: 20 Jan, 20 Feb, 20 Mar, 20 Apr, 20 May, 22 Jun,
    # 20 Jul, 21 Sep for June and July, 20 Oct, 20 Nov, 21 Dec).
    Entry("pt-vat-return-monthly", "VAT return for {period}", "vat_return", "accountant", "tax_authority", "month",
          "vat_monthly", 2, 20, True, _LATE_FILING, "The tax office's filing receipt for the VAT return for {period}.",
          "Due on the 20th of the second month after; June's on 20 September.", (CIVA_41, AGENDA_FILINGS, CPPT_20),
          words=("declaracao periodica", "iva"), september=20),
    # VAT payment, monthly regime: by the 25th of the second month after; June's by 25 September.
    # CIVA art. 27.º n.º 1 a) and n.º 10. Sources (read 2026-10-08): CIVA_27; AGENDA_PAYMENTS (2026: 26 Jan,
    # 25 Feb, 25 Mar, 27 Apr, 25 May, 25 Jun, 27 Jul, 25 Sep for June and July, 26 Oct, 25 Nov, 28 Dec).
    Entry("pt-vat-payment-monthly", "VAT payment for {period}", "tax_deadline", "owner", "tax_authority", "month",
          "vat_monthly", 2, 25, True, _LATE_PAYMENT,
          "The bank payment of the VAT for {period}, or the tax office's payment document.",
          "Due on the 25th of the second month after; June's on 25 September.", (CIVA_27, AGENDA_PAYMENTS, CPPT_20),
          bank_words=_VAT_BANK, september=25),
    # Periodic VAT return, quarterly regime (turnover under €650,000): by the 20th of the second month after
    # the quarter; the second quarter's by 20 September. CIVA art. 41.º n.º 1 b) and n.º 10.
    # Sources (read 2026-10-08): CIVA_41; AGENDA_FILINGS (2026: 20 Feb, 20 May, 21 Sep, 20 Nov).
    Entry("pt-vat-return-quarterly", "VAT return for {period}", "vat_return", "accountant", "tax_authority",
          "quarter", "vat_quarterly", 2, 20, True, _LATE_FILING,
          "The tax office's filing receipt for the VAT return for {period}.",
          "Due on the 20th of the second month after the quarter; the second quarter's on 20 September.",
          (CIVA_41, AGENDA_FILINGS, CPPT_20), words=("declaracao periodica", "iva"), september=20),
    # VAT payment, quarterly regime: by the 25th of the second month after the quarter; the second quarter's by
    # 25 September. CIVA art. 27.º n.º 1 b) and n.º 10. Sources (read 2026-10-08): CIVA_27; AGENDA_PAYMENTS
    # (2026: 25 Feb, 25 May, 25 Sep, 25 Nov).
    Entry("pt-vat-payment-quarterly", "VAT payment for {period}", "tax_deadline", "owner", "tax_authority",
          "quarter", "vat_quarterly", 2, 25, True, _LATE_PAYMENT,
          "The bank payment of the VAT for {period}, or the tax office's payment document.",
          "Due on the 25th of the second month after the quarter; the second quarter's on 25 September.",
          (CIVA_27, AGENDA_PAYMENTS, CPPT_20), bank_words=_VAT_BANK, september=25),
    # Monthly communication of the invoices issued (e-fatura, usually the SAF-T file), or that none were: by
    # the 5th of the next month. DL 198/2012 art. 3.º n.º 2; due in August: by 31 August.
    # Sources (read 2026-10-08): DL_198_2012; EFATURA_FAQ; AGENDA_FILINGS (2026: 9 Jan*, 5 Feb, 5 Mar, 8 Apr*,
    # 8 May*, 5 Jun, 6 Jul, 31 Aug, 7 Sep, 6 Oct, 5 Nov, 7 Dec; * moved by a dispatch, see OVERRIDES).
    Entry("pt-invoice-report", "Invoice report to the tax office for {period}", "filing", "accountant",
          "tax_authority", "month", "vat_known", 1, 5, True, "Sending it late may lead to a fine.",
          "The tax office's receipt for the invoice report for {period} (SAF-T or e-fatura).",
          "Due on the 5th of the next month, also when no invoice was issued.",
          (DL_198_2012, EFATURA_FAQ, AGENDA_FILINGS, CPPT_20), words=("saf-t", "saft", "e-fatura", "efatura",
                                                                     "comunicacao de faturas",
                                                                     "elementos das faturas"), august=True),
    # Monthly pay declaration to the tax office (Declaração Mensal de Remunerações): by the 10th of the month
    # after the pay. CIRS art. 119.º n.º 1 c) i). Sources (read 2026-10-08): CIRS_119; AGENDA_FILINGS (2026:
    # 12 Jan, 10 Feb, 10 Mar, 10 Apr, 11 May, 11 Jun, 10 Jul, 31 Aug, 10 Sep, 12 Oct, 10 Nov, 10 Dec).
    Entry("pt-pay-declaration", "Salaries report to the tax office for {period}", "filing", "accountant",
          "tax_authority", "month", "employees", 1, 10, True, _LATE_FILING,
          "The tax office's filing receipt for the salaries report (DMR) for {period}.",
          "Due on the 10th of the month after the pay.", (CIRS_119, AGENDA_FILINGS, CPPT_20, FERIAS_FISCAIS),
          words=("declaracao mensal de remuneracoes", "dmr"), august=True),
    # Payment of the income tax withheld (IRS on salaries, rents or fees): by the 20th of the next month.
    # Sources (read 2026-10-08): AGENDA_PAYMENTS ("IRS – IRC até ao dia 20: entrega das importâncias retidas no
    # mês anterior"; 2026: 20 Jan, 20 Feb, 20 Mar, 20 Apr, 20 May, 22 Jun, 20 Jul, 31 Aug, 21 Sep, 20 Oct,
    # 20 Nov, 21 Dec); FERIAS_FISCAIS.
    Entry("pt-withholding-payment", "Payment of the tax withheld in {period}", "tax_deadline", "owner", "tax_authority",
          "month", "withholding", 1, 20, True, _LATE_PAYMENT,
          "The bank payment of the tax withheld in {period}, or the tax office's payment document.",
          "Due on the 20th of the next month.", (AGENDA_PAYMENTS, CPPT_20, FERIAS_FISCAIS),
          bank_words=("irs", "retencao", "retencoes", "ret fonte", "retencao na fonte"), august=True),
    # Social Security contributions: by the 25th of the next month, from January 2026's contributions (before:
    # the 20th); a closed last day moves to the next working day; July's by the last day of August.
    # Sources (read 2026-10-08): SS_DAY_25 (gov.pt, 9 Feb 2026: "as contribuições relativas ao mês de janeiro
    # de 2026 já podem ser pagas até 25 de fevereiro"); SS_GUIDE (Guia Prático, version 5.42 of 3 Feb 2026);
    # FERIAS_FISCAIS.
    Entry("pt-social-security", "Social Security payment for {period}", "tax_deadline", "owner", "social_security",
          "month", "employees", 1, 25, True, _LATE_PAYMENT,
          "The bank payment of the Social Security contributions for {period}, or Social Security's payment "
          "document.", "Due on the 25th of the next month.", (SS_DAY_25, SS_GUIDE, FERIAS_FISCAIS),
          bank_words=("seguranca social", "seg social", "igfss", "tsu", "contribuicoes"), august=True,
          first_period="2026-01"),
    # Corporate income tax return (Modelo 22): by the last day of May, whether or not a working day.
    # CIRC art. 120.º n.º 1. Sources (read 2026-10-08): CIRC_120; MODELO_22_2026 (2025's moved to 30 June 2026,
    # Despacho n.º 81/2026-XXV, see OVERRIDES); AGENDA_FILINGS.
    Entry("pt-modelo-22", "Corporate income tax return for {period} (Modelo 22)", "filing", "accountant",
          "tax_authority", "year", "company", 5, 0, False, _LATE_FILING,
          "The tax office's filing receipt for the Modelo 22 for {period}.",
          "Due on the last day of May of the next year.", (CIRC_120, AGENDA_FILINGS, MODELO_22_2026),
          words=("modelo 22", "declaracao de rendimentos")),
    # Annual accounts and tax information (IES / declaração anual): by 15 July, whether or not a working day.
    # CIRC art. 121.º n.º 2. Sources (read 2026-10-08): CIRC_121; AGENDA_FILINGS (2026: 15 July).
    Entry("pt-ies", "Annual accounts report for {period} (IES)", "filing", "accountant", "tax_authority", "year",
          "company", 7, 15, False, _LATE_FILING, "The filing receipt for the IES for {period}.",
          "Due on 15 July of the next year.", (CIRC_121, AGENDA_FILINGS),
          words=("ies", "informacao empresarial simplificada")),
    # Annual declaration of other income paid with tax withheld (Modelo 10): by the end of February.
    # CIRS art. 119.º n.º 1 c) ii). Sources (read 2026-10-08): CIRS_119; AGENDA_FILINGS (2026: 2 March, the end of
    # February falling on a Saturday).
    Entry("pt-modelo-10", "Annual report of other income paid in {period} (Modelo 10)", "filing", "accountant",
          "tax_authority", "year", "other_income", 2, 0, True, _LATE_FILING,
          "The tax office's filing receipt for the Modelo 10 for {period}.",
          "Due by the end of February of the next year.", (CIRS_119, AGENDA_FILINGS, CPPT_20),
          words=("modelo 10",)),
    # Advance payments of corporate income tax (pagamentos por conta), in July, September and by 15 December
    # of the year they are for. CIRC art. 104.º n.º 1 a). Sources (read 2026-10-08): CIRC_104; AGENDA_PAYMENTS
    # (2026: "até ao fim do mês" of July, and by 15 December).
    Entry("pt-advance-payment-1", "First advance payment of corporate income tax for {period}", "tax_deadline",
          "owner", "tax_authority", "year", "advance_payments", -5, 0, True, "Paying late adds interest.",
          "The bank payment of the first advance payment for {period}.", "Due by the end of July.",
          (CIRC_104, AGENDA_PAYMENTS, CPPT_20), bank_words=("pagamento por conta", "pag por conta", "pag conta",
                                                            "ppc"), opens="due_month"),
    Entry("pt-advance-payment-2", "Second advance payment of corporate income tax for {period}", "tax_deadline",
          "owner", "tax_authority", "year", "advance_payments", -3, 0, True, "Paying late adds interest.",
          "The bank payment of the second advance payment for {period}.", "Due by the end of September.",
          (CIRC_104, AGENDA_PAYMENTS, CPPT_20), bank_words=("pagamento por conta", "pag por conta", "pag conta",
                                                            "ppc"), opens="due_month"),
    Entry("pt-advance-payment-3", "Third advance payment of corporate income tax for {period}", "tax_deadline",
          "owner", "tax_authority", "year", "advance_payments", 0, 15, True, "Paying late adds interest.",
          "The bank payment of the third advance payment for {period}.", "Due by 15 December.",
          (CIRC_104, AGENDA_PAYMENTS, CPPT_20), bank_words=("pagamento por conta", "pag por conta", "pag conta",
                                                            "ppc"), opens="due_month"),
)

_BY_CODE: Mapping[str, Entry] = MappingProxyType({e.code: e for e in ENTRIES})

# Dates a dispatch of the Secretary of State for Tax Affairs moved, as the official 2026 calendar lists them
# (read 2026-10-08): (entry, period) -> deadline.
OVERRIDES: Mapping[tuple[str, str], date] = MappingProxyType({
    # December 2025's invoices: 9 January 2026 (AGENDA_FILINGS; its notes list Despachos 158/2025 and 166/2025).
    ("pt-invoice-report", "2025-12"): date(2026, 1, 9),
    # March 2026's invoices: 8 April 2026, Despacho n.º 40/2026-XXV (EFATURA_APRIL_2026; AGENDA_FILINGS).
    ("pt-invoice-report", "2026-03"): date(2026, 4, 8),
    # April 2026's invoices: 8 May 2026 (AGENDA_FILINGS; EFATURA_MAY_2026).
    ("pt-invoice-report", "2026-04"): date(2026, 5, 8),
    # 2025's Modelo 22: 30 June 2026, Despacho n.º 81/2026-XXV of 17 June (MODELO_22_2026; AGENDA_FILINGS).
    ("pt-modelo-22", "2025"): date(2026, 6, 30),
})  # fmt: skip

# --------------------------------------------------------------------------- periods

_PT_MONTHS = ("janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
              "novembro", "dezembro")
_EN_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December")
_QUARTER_NAMES = {1: "January to March", 2: "April to June", 3: "July to September", 4: "October to December"}


@dataclass(frozen=True)
class _Period:
    cadence: str
    year: int
    index: int  # month 1-12, quarter 1-4; 1 for a year

    @property
    def key(self) -> str:
        if self.cadence == "month":
            return f"{self.year}-{self.index:02d}"
        if self.cadence == "quarter":
            return f"{self.year}-Q{self.index}"
        return str(self.year)

    @property
    def last_month(self) -> int:
        return {"month": self.index, "quarter": 3 * self.index}.get(self.cadence, 12)

    @property
    def end(self) -> date:
        return date(self.year, self.last_month, _calendar.monthrange(self.year, self.last_month)[1])

    @property
    def name(self) -> str:
        if self.cadence == "month":
            return f"{_EN_MONTHS[self.index - 1]} {self.year}"
        if self.cadence == "quarter":
            return f"{_QUARTER_NAMES[self.index]} {self.year}"
        return str(self.year)

    @property
    def is_june_or_q2(self) -> bool:
        return (self.cadence, self.index) in (("month", 6), ("quarter", 2))

    def words(self) -> tuple[str, ...]:
        """How a receipt or a bank line names this period (folded: lower case, no accents)."""
        y = self.year
        if self.cadence == "month":
            m, name = self.index, _PT_MONTHS[self.index - 1]
            return (f"{y}/{m:02d}", f"{m:02d}/{y}", f"{y}{m:02d}", f"{name} de {y}", f"{name} {y}", f"{name}/{y}",
                    f"{_EN_MONTHS[m - 1].lower()} {y}")
        if self.cadence == "quarter":
            q, m = self.index, 3 * self.index
            return (f"{y}/{m:02d}t", f"{y}{m:02d}t", f"{y % 100:02d}{m:02d}t", f"{q}t {y}", f"{q}t/{y}",
                    f"{q}.o trimestre de {y}", f"{q}.o trimestre {y}", f"{q}o trimestre de {y}",
                    f"{q}o trimestre {y}", f"{q} trimestre de {y}", f"{q} trimestre {y}", f"t{q} {y}", f"t{q}/{y}",
                    f"{y} t{q}", f"{y}/t{q}", f"{y}-t{q}", f"q{q} {y}", f"{y} q{q}")
        return (str(y),)


def _parse_period(key: str) -> _Period:
    if re.fullmatch(r"\d{4}-\d{2}", key):
        return _Period("month", int(key[:4]), int(key[5:]))
    if re.fullmatch(r"\d{4}-Q[1-4]", key):
        return _Period("quarter", int(key[:4]), int(key[6:]))
    if re.fullmatch(r"\d{4}", key):
        return _Period("year", int(key), 1)
    raise ValueError(f"not a calendar period: {key!r}")


def _add_months(year: int, month: int, n: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + n
    return total // 12, total % 12 + 1


def _periods(cadence: str, today: date, back: int) -> Iterable[_Period]:
    """The periods that end around ``today``, oldest first (enough to cover every open window)."""
    if cadence == "month":
        for n in range(back, -1, -1):
            y, m = _add_months(today.year, today.month, -n)
            yield _Period("month", y, m)
    elif cadence == "quarter":
        q0 = today.year * 4 + (today.month - 1) // 3
        for n in range(back, -1, -1):
            q = q0 - n
            yield _Period("quarter", q // 4, q % 4 + 1)
    else:
        for y in range(today.year - back, today.year + 1):
            yield _Period("year", y, 1)


# --------------------------------------------------------------------------- dates


def next_working_day(day: date) -> date:
    """``day`` itself when it is a working day; else the next one (no Saturday, Sunday or national holiday)."""
    while day.weekday() >= 5 or day in national_holidays(day.year):
        day += timedelta(days=1)
    return day


def _due(entry: Entry, period: _Period) -> date:
    override = OVERRIDES.get((entry.code, period.key))
    if override is not None:
        return override
    if entry.september and period.is_june_or_q2:
        return next_working_day(date(period.year, 9, entry.september))
    year, month = _add_months(period.year, period.last_month, entry.months_after)
    last = _calendar.monthrange(year, month)[1]
    day = date(year, month, min(entry.day, last) if entry.day else last)
    if entry.august and day.month == 8:
        return date(day.year, 8, 31)  # "independentemente de ser dia útil"
    return next_working_day(day) if entry.working_day else day


def _opens(entry: Entry, period: _Period, due: date) -> date:
    """The first day the obligation is added: the day after its period ends, or (an advance payment) the first
    day of the month the law names, even when a weekend moved the deadline into the next month."""
    if entry.opens == "due_month":
        year, month = _add_months(period.year, period.last_month, entry.months_after)
        return min(date(year, month, 1), due)
    return period.end + timedelta(days=1)


def deadline(code: str, period: str) -> date:
    """The deadline of one calendar entry for one period ("pt-vat-return-monthly", "2026-08" -> 20 October 2026)."""
    entry = _BY_CODE.get(code)
    if entry is None:
        raise KeyError(code)
    found = _parse_period(period)
    if found.cadence != entry.cadence:
        raise ValueError(f"{code} is set by {entry.cadence}, not {found.cadence}")
    return _due(entry, found)


# --------------------------------------------------------------------------- who gets what


def _is_company(tax_id: str) -> bool:
    """A Portuguese NIF starting with 5 belongs to a legal person (a company): it files the Modelo 22 and the IES."""
    digits = re.sub(r"\D", "", tax_id or "")
    return len(digits) == 9 and digits[0] == "5"


def _applies(needs: str, profile: TaxProfile) -> bool:
    return {
        "vat_monthly": profile.vat == "monthly",
        "vat_quarterly": profile.vat == "quarterly",
        "vat_known": profile.vat in ("monthly", "quarterly", "exempt"),
        "employees": profile.employees is True,
        "withholding": profile.employees is True or profile.other_income is True,
        "other_income": profile.other_income is True,
        "advance_payments": profile.advance_payments is True,
        "company": _is_company(profile.tax_id),
    }[needs]


_WHO = {"vat_monthly": "Your company files VAT every month.",
        "vat_quarterly": "Your company files VAT every quarter.",
        "vat_known": "Every company that charges VAT in Portugal reports its invoices each month.",
        "employees": "Your company pays salaries.",
        "withholding": "Your company withholds income tax when it pays.",
        "other_income": "Your company pays rents or fees with tax withheld.",
        "advance_payments": "Your company makes advance payments of corporate income tax this year.",
        "company": "Every company in Portugal files it."}


def obligations(company_id: str, today: date, profile: TaxProfile | None) -> tuple[PeriodicObligation, ...]:
    """The calendar deadlines whose window is open today for one company, by what is known of it.

    Each is added from the day its period ends (an advance payment: from the first day of its month) until its
    deadline; never one whose deadline already passed (a company set up late is not told it missed a return it
    may well have filed). Nothing that depends on an unknown fact."""
    profile = profile or TaxProfile()
    out: list[PeriodicObligation] = []
    for entry in ENTRIES:
        if not _applies(entry.needs, profile):
            continue
        back = {"month": 14, "quarter": 5, "year": 2}[entry.cadence]
        for period in _periods(entry.cadence, today, back):
            if entry.first_period and period.key < entry.first_period:
                continue
            due = _due(entry, period)
            if not _opens(entry, period, due) <= today <= due:
                continue
            what = entry.title.format(period=period.name)
            out.append(PeriodicObligation(
                key=f"{entry.code}-{company_id}-{period.key}", kind=entry.kind, title=what, period=period.key,
                due_on=due, responsible=entry.responsible, consequence=entry.consequence,
                required_evidence=entry.proof.format(period=period.name),
                reasons=(what, _WHO[entry.needs], entry.rule,
                         "A deadline on a weekend or public holiday moves to the next working day."
                         if entry.working_day else "The date holds even on a weekend or public holiday."),
                issuer=entry.issuer, calendar=entry.code,
            ))
    return tuple(sorted(out, key=lambda o: (o.due_on, o.key)))


# --------------------------------------------------------------------------- proof


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).split())


def _has(text: str, phrase: str) -> bool:
    return re.search(r"(?<![0-9a-z/.\-])" + re.escape(phrase) + r"(?![0-9a-z/])", text) is not None


_MONTHS_RE = "|".join(_PT_MONTHS + tuple(m.lower() for m in _EN_MONTHS))
# Any period a receipt or bank line may name (folded text): "2026/08", "08/2026" (not inside a date),
# "202608", "2026/09t", "3.o trimestre de 2026", "t3 2026", "agosto de 2026".
_SOME_PERIOD = re.compile(
    r"(?<![0-9/.\-])20\d\d\s*/\s*(?:0[1-9]|1[0-2])t?(?![0-9/])"
    r"|(?<![0-9/.\-])(?:0[1-9]|1[0-2])\s*/\s*20\d\d(?![0-9/])"
    r"|(?<![0-9])(?:20)?\d\d(?:0[1-9]|1[0-2])t(?![0-9a-z])"
    r"|(?<![0-9])20\d\d(?:0[1-9]|1[0-2])(?![0-9])"
    r"|(?<![0-9a-z])[1-4]\s*\.?\s*o?\s*t(?:rim(?:estre)?)?\s*(?:de\s+)?/?\s*20\d\d"
    r"|(?<![0-9a-z])[tq][1-4]\s*/?\s*20\d\d|20\d\d\s*[/\-]?\s*[tq][1-4](?![0-9a-z])"
    rf"|(?<![a-z])(?:{_MONTHS_RE})\s*(?:de\s+|/)?\s*20\d\d"
)
_A_YEAR = re.compile(r"(?<![0-9])20\d\d(?![0-9])")


def calendar_proof(text: str, code: str, period: str, *, payment: bool, on: date | None = None) -> bool:
    """Whether ``text`` proves one calendar deadline (module docstring).

    A receipt must name what it is a receipt of (the entry's words) and, when it names a period, this one. A
    payment's bank line must name the tax (the entry's bank words) and this period: a tax payment alone never
    says what it paid. The one exception is a yearly payment (an advance payment of corporate income tax) whose
    line names no year at all: paid (``on``) in its own deadline's month, it is that month's payment."""
    entry = _BY_CODE.get(code)
    if entry is None:
        return False
    try:
        found = _parse_period(period)
    except ValueError:
        return False
    if found.cadence != entry.cadence:
        return False
    folded = _fold(text)
    words = entry.bank_words if payment else entry.words
    if not words or not any(_has(folded, w) for w in words):
        return False
    ours = any(_has(folded, w) for w in found.words())
    if payment:
        if ours or found.cadence != "year" or on is None or _A_YEAR.search(folded):
            return ours
        due = _due(entry, found)
        return _opens(entry, found, due) <= on <= due
    if found.cadence == "year":
        return ours or not _A_YEAR.search(folded)
    return ours or not _SOME_PERIOD.search(folded)


# --------------------------------------------------------------------------- learning the profile

_VAT_WORD = re.compile(r"(?<![a-z])iva(?![a-z])")
_SS_WORDS = ("seguranca social", "seg social", "igfss", "tsu")
_ADVANCE_WORDS = ("pagamento por conta", "pag por conta", "pagamentos por conta", "pag conta irc", "ppc irc")
_QUARTERLY = (
    re.compile(r"(?<![0-9/.\-])(20\d\d)\s*/\s*(03|06|09|12)\s*t(?![0-9a-z])"),
    re.compile(r"(?<![0-9])(20\d\d)(03|06|09|12)t(?![0-9a-z])"),
    re.compile(r"(?<![0-9a-z])([1-4])\s*\.?\s*o?\s*t(?:rim(?:estre)?)?\s*(?:de\s+)?/?\s*(20\d\d)"),
    re.compile(r"(?<![0-9a-z])[tq]([1-4])\s*/?\s*(20\d\d)"),
    re.compile(r"(20\d\d)\s*[/\-]?\s*[tq]([1-4])(?![0-9a-z])"),
)
_MONTHLY = (
    re.compile(r"(?<![0-9/.\-])(20\d\d)\s*/\s*(0[1-9]|1[0-2])(?![0-9/t])"),
    re.compile(r"(?<![0-9/.\-])(0[1-9]|1[0-2])\s*/\s*(20\d\d)(?![0-9/])"),
    re.compile(r"(?<![0-9])(20\d\d)(0[1-9]|1[0-2])(?![0-9t])"),
    re.compile(rf"(?<![a-z])({'|'.join(_PT_MONTHS)})\s*(?:de\s+|/)?\s*(20\d\d)"),
)


def _vat_period(folded: str) -> _Period | None:
    """The VAT period a tax payment's words name ("pag estado iva 2026/07" -> July 2026), or None."""
    for i, pattern in enumerate(_QUARTERLY):
        m = pattern.search(folded)
        if m:
            if i < 2:
                return _Period("quarter", int(m[1]), int(m[2]) // 3)
            if i == 4:
                return _Period("quarter", int(m[1]), int(m[2]))
            return _Period("quarter", int(m[2]), int(m[1]))
    for i, pattern in enumerate(_MONTHLY):
        m = pattern.search(folded)
        if m:
            if i == 0 or i == 2:
                return _Period("month", int(m[1]), int(m[2]))
            if i == 1:
                return _Period("month", int(m[2]), int(m[1]))
            return _Period("month", int(m[2]), _PT_MONTHS.index(m[1]) + 1)
    return None


def _month_key(day: date) -> int:
    return day.year * 12 + day.month - 1


def _month_names(keys: Iterable[int]) -> str:
    names = [f"{_EN_MONTHS[k % 12]} {k // 12}" for k in sorted(set(keys))]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def learn_profile(signals: Sequence[TaxSignal], *, today: date | None = None) -> LearnedProfile:
    """What a Portuguese company's own evidence says about its tax profile (module docstring).

    * VAT every month: VAT payments naming two different months ("IVA 2026/07", "IVA 2026/08"), or paid in two
      consecutive months; every quarter: VAT payments naming two different quarters ("IVA 2026/06T"), or two
      VAT payments a quarter apart and none in between. Both kinds named: unknown (it is asked, never guessed).
    * Salaries: salaries paid, payslips or Social Security payments in two different months.
    * Advance payments of corporate income tax: one made this year (the law sets three a year).
    Nothing is learned from a single payment."""
    reasons: dict[str, tuple[str, ...]] = {}
    vat: str | None = None
    months: dict[str, _Period] = {}
    quarters: dict[str, _Period] = {}
    paid_months: set[int] = set()
    for s in signals:
        if s.kind != "tax":
            continue
        folded = _fold(s.text)
        if not _VAT_WORD.search(folded):
            continue
        paid_months.add(_month_key(s.on))
        period = _vat_period(folded)
        if period is None:
            continue
        (months if period.cadence == "month" else quarters)[period.key] = period
    if len(months) >= 2 and not quarters:
        vat = "monthly"
        reasons["vat"] = (f"VAT payments for {', '.join(p.name for p in sorted(months.values(), key=lambda p: p.key))}"
                          " name single months.",)
    elif len(quarters) >= 2 and not months:
        vat = "quarterly"
        reasons["vat"] = (f"VAT payments for {', '.join(p.name for p in sorted(quarters.values(), key=lambda p: p.key))}"
                          " name quarters.",)
    elif not months and not quarters and len(paid_months) >= 2:
        ordered = sorted(paid_months)
        if any(b - a == 1 for a, b in zip(ordered, ordered[1:], strict=False)):
            vat = "monthly"
            reasons["vat"] = (f"VAT was paid in {_month_names(ordered)}: one month after the other.",)
        elif all(b - a == 3 for a, b in zip(ordered, ordered[1:], strict=False)):
            vat = "quarterly"
            reasons["vat"] = (f"VAT was paid in {_month_names(ordered)}: once a quarter.",)
    employees: bool | None = None
    pay_months: dict[str, set[int]] = {"salary": set(), "payslip": set(), "social security": set()}
    for s in signals:
        if s.kind in ("salary", "payslip"):
            pay_months[s.kind].add(_month_key(s.on))
        elif s.kind == "tax" and any(_has(_fold(s.text), w) for w in _SS_WORDS):
            pay_months["social security"].add(_month_key(s.on))
    seen = set().union(*pay_months.values())
    if len(seen) >= 2:
        employees = True
        labels = {"salary": "Salaries paid", "payslip": "Payslips", "social security": "Social Security paid"}
        reasons["employees"] = tuple(f"{labels[what]} in {_month_names(keys)}." for what, keys in pay_months.items()
                                     if keys)
    advance: bool | None = None
    year = (today or max((s.on for s in signals), default=date.min)).year
    for s in signals:
        folded = _fold(s.text)
        if s.kind == "tax" and s.on.year == year and any(_has(folded, w) for w in _ADVANCE_WORDS):
            advance = True
            reasons["advance_payments"] = (f"An advance payment of corporate income tax was made on "
                                           f"{s.on.day} {_EN_MONTHS[s.on.month - 1]} {s.on.year}.",)
            break
    return LearnedProfile(TaxProfile(vat=vat, employees=employees, advance_payments=advance),
                          MappingProxyType(reasons))
