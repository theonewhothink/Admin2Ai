"""Each company's tax profile (QA P6): what its country's calendar needs to know about it.

A company's statutory calendar (Portugal's VAT return and payment, invoice report, salaries report, Social
Security, Modelo 22, IES, Modelo 10, advance payments: ``backoffice.countries.pt.calendar``) depends on a few
facts: does it file VAT every month or every quarter, does it pay salaries, does it make advance payments of
corporate income tax, does it pay rents or fees with tax withheld. Each fact is

* set by the owner (``POST /api/companies/{id}/profile`` ``{vat, employees, advancePayments, otherIncome}``) or
  by the accountant, in one sentence like any rule ("Hazel Tree files VAT every quarter", "No employees"), or
* learned from the company's own evidence by its country's pack: its tax and Social Security payments, the
  salaries it paid, its payslips (:func:`signals`).

What the owner or accountant set always wins over what was learned; a fact nobody set and the evidence does
not show stays unknown, and the calendar entries that depend on it stay out (never a guessed deadline).
Nothing here reaches the network or the clock: the same evidence gives the same profile on every replay.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from datetime import date
from typing import Any

from backoffice.countries import (
    CountryPackError,
    LazyPattern,
    LearnedProfile,
    TaxProfile,
    TaxSignal,
    company_pack,
    pack_alternatives,
    pack_words,
)

__all__ = ["FIELDS", "describe", "learned", "parse_setting", "profile", "settings_profile", "signals"]

# The facts, by their API name and their profile attribute.
FIELDS: Mapping[str, str] = {"vat": "vat", "employees": "employees", "advancePayments": "advance_payments",
                             "otherIncome": "other_income"}
_VAT_VALUES = ("monthly", "quarterly", "exempt")


def signals(repo: Any, company_id: str) -> list[TaxSignal]:
    """The company's own evidence about its taxes: payments to the tax office or Social Security (their bank
    lines), salaries paid, and payslips that add up (each on its month)."""
    out: list[TaxSignal] = []
    for rec in repo.transactions.values():
        if rec.company_id != company_id or rec.tx.amount >= 0 or rec.decision is None or rec.private:
            continue
        expectation = rec.decision.expectation.value
        if expectation == "tax_notice_or_proof" and rec.decision.rule not in ("tourist_tax", "grant"):
            out.append(TaxSignal(on=rec.tx.booked_on, kind="tax", text=f"{rec.tx.counterparty} {rec.tx.description}",
                                 evidence_id=rec.evidence_id))
        elif expectation == "payroll":
            out.append(TaxSignal(on=rec.tx.booked_on, kind="salary", evidence_id=rec.evidence_id))
    for doc in repo.documents.values():
        payslip = getattr(doc, "payslip", None)
        if payslip is None or doc.document.entity_id != company_id or doc.document.quality.value == "red":
            continue
        out.append(TaxSignal(on=payslip.period.last_day, kind="payslip", evidence_id=doc.evidence_ids[0]))
    return sorted(out, key=lambda s: (s.on, s.kind, s.evidence_id))


def learned(repo: Any, company_id: str, today: date) -> LearnedProfile:
    """What the company's evidence says, read by its own country's pack (nothing for a country without one)."""
    entity = repo.companies.get(company_id)
    if entity is None:
        return LearnedProfile()
    try:
        return company_pack(entity.country).learn_tax_profile(signals(repo, company_id), today)
    except CountryPackError:
        return LearnedProfile()


def settings_profile(repo: Any, company_id: str) -> TaxProfile:
    """What the owner or the accountant set (unknown facts left None)."""
    saved = repo.tax_settings.get(company_id) or {}
    return TaxProfile(vat=saved.get("vat"), employees=saved.get("employees"),
                      advance_payments=saved.get("advance_payments"), other_income=saved.get("other_income"))


def profile(repo: Any, company_id: str, today: date) -> TaxProfile:
    """The profile the calendar uses: what was set over what was learned, with the company's tax number."""
    entity = repo.companies.get(company_id)
    base = learned(repo, company_id, today).profile
    merged = settings_profile(repo, company_id).over(base)
    return TaxProfile(vat=merged.vat, employees=merged.employees, advance_payments=merged.advance_payments,
                      other_income=merged.other_income, tax_id=entity.tax_id if entity is not None else "")


def describe(repo: Any, company_id: str, today: date) -> dict[str, Any]:
    """The profile for the API: each fact, who said it ("owner", "accountant", "learned" or None) and why."""
    found = learned(repo, company_id, today)
    saved = repo.tax_settings.get(company_id) or {}
    by = saved.get("by") or {}
    out: dict[str, Any] = {}
    sources: dict[str, str | None] = {}
    why: dict[str, list[str]] = {}
    for api, attr in FIELDS.items():
        value = saved.get(attr)
        if value is not None:
            out[api], sources[api] = value, by.get(attr, "owner")
            continue
        value = getattr(found.profile, attr)
        out[api], sources[api] = value, ("learned" if value is not None else None)
        if value is not None:
            why[api] = list(found.reasons.get(attr, ()))
    return {**out, "setBy": sources, "why": why}


def clean_values(body: Mapping[str, Any]) -> dict[str, Any]:
    """The profile facts in a request body ({vat, employees, advancePayments, otherIncome}); ValueError with a
    plain message for a value that is not one. null clears a fact (it is learned again)."""
    values: dict[str, Any] = {}
    if "vat" in body:
        raw = body.get("vat")
        vat = str(raw).strip().lower() if raw not in (None, "") else None
        if vat is not None and vat not in _VAT_VALUES:
            raise ValueError("Say how the company files VAT: monthly, quarterly or exempt.")
        values["vat"] = vat
    for api in ("employees", "advancePayments", "otherIncome"):
        if api in body:
            raw = body.get(api)
            if raw is not None and not isinstance(raw, bool):
                raise ValueError("Answer yes or no (true or false).")
            values[FIELDS[api]] = raw
    return values


def apply(repo: Any, company_id: str, values: Mapping[str, Any], by: str) -> dict[str, Any]:
    """Save what the owner or accountant said (``values`` by profile attribute); None clears a fact."""
    saved = dict(repo.tax_settings.get(company_id) or {})
    who = dict(saved.get("by") or {})
    for attr, value in values.items():
        if value is None:
            saved.pop(attr, None)
            who.pop(attr, None)
        else:
            saved[attr], who[attr] = value, by
    saved["by"] = who
    if any(k != "by" for k in saved):
        repo.tax_settings[company_id] = saved
    else:
        repo.tax_settings.pop(company_id, None)
    return saved


# --------------------------------------------------------------------------- the accountant's sentence


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).split())


# The core's English; a pack's own words are in "tax_profiles.<concept>" (Portugal's "IVA mensal", "não tem
# trabalhadores", "pagamentos por conta", "Modelo 10"), regular-expression alternatives over folded text.


def _or(concept: str) -> str:
    return "".join(f"|{w}" for w in pack_words(f"tax_profiles.{concept}"))


def _no() -> str:
    return rf"(?:no|without|has\s+no|have\s+no|does\s+not\s+have|doesn't\s+have|does\s+not\s+make{_or('no')})"


def _staff() -> str:
    return rf"(?:employees?|staff|salaries|workers{_or('staff')})"


def _advance() -> str:
    return rf"(?:advance\s+payments?{_or('advance')})"


_VAT = LazyPattern(lambda: rf"(?<![a-z])(?:vat{_or('vat')})(?![a-z])")
_MONTHLY = LazyPattern(lambda: rf"(?<![a-z])(?:monthly|every\s+month|each\s+month{_or('monthly')})(?![a-z])")
_QUARTERLY = LazyPattern(lambda: rf"(?<![a-z])(?:quarterly|every\s+quarter|each\s+quarter{_or('quarterly')})(?![a-z])")
_EXEMPT = LazyPattern(lambda: rf"(?<![a-z])(?:exempt{_or('exempt')})(?![a-z])")
_EMPLOYEES = LazyPattern(lambda: (rf"(?<![a-z])(?:has|have|with|employs|pays{_or('has')})\s+(?:\w+\s+){{0,2}}"
                                  rf"{_staff()}(?![a-z])"))
_NO_EMPLOYEES = LazyPattern(lambda: rf"(?<![a-z]){_no()}\s+(?:\w+\s+){{0,2}}{_staff()}(?![a-z])")
# An ordinary accountant rule ("Treat all Uber as Staff training", "EDP always needs an invoice"): never a tax fact.
_ORDINARY_RULE = LazyPattern(lambda: (r"^\s*(?:treat|classify|book|require|ask)\b|(?<![a-z])(?:invoices?"
                                      rf"{_or('invoice')})(?![a-z])"))
_ADVANCE = LazyPattern(lambda: rf"(?<![a-z]){_advance()}(?![a-z])")
_NO_ADVANCE = LazyPattern(lambda: rf"(?<![a-z]){_no()}\s+(?:\w+\s+){{0,2}}{_advance()}(?![a-z])")
# Rents or fees paid with tax withheld, reported once a year (a pack names its form: "tax_profiles.other_form").
_OTHER = LazyPattern(lambda: (r"(?<![a-z])(?:rents?\s+or\s+fees\s+with\s+tax\s+withheld|other\s+income\s+with\s+tax"
                              rf"\s+withheld|withholds\s+tax\s+on\s+(?:rents?|fees){_or('other_form')})(?![a-z])"))
_NO_OTHER = LazyPattern(lambda: (rf"(?<![a-z]){_no()}\s+(?:\w+\s+){{0,2}}"
                                 rf"(?:{pack_alternatives('tax_profiles.other_form')})(?![a-z])"))


def parse_setting(text: str) -> dict[str, Any] | None:
    """The profile facts an accountant's sentence states, by profile attribute; None when it states none (it is
    then an ordinary rule). "Hazel Tree files VAT every quarter", "IVA mensal", "Has employees", "No employees",
    "Makes advance payments", "Files Modelo 10"."""
    folded = _fold(text)
    if _ORDINARY_RULE.search(folded):
        return None
    values: dict[str, Any] = {}
    if _VAT.search(folded):
        kinds = [v for v, p in (("monthly", _MONTHLY), ("quarterly", _QUARTERLY), ("exempt", _EXEMPT)) if p.search(folded)]
        if len(kinds) == 1:
            values["vat"] = kinds[0]
    if _NO_EMPLOYEES.search(folded):
        values["employees"] = False
    elif _EMPLOYEES.search(folded):
        values["employees"] = True
    if _NO_ADVANCE.search(folded):
        values["advance_payments"] = False
    elif _ADVANCE.search(folded):
        values["advance_payments"] = True
    if _NO_OTHER.search(folded):
        values["other_income"] = False
    elif _OTHER.search(folded):
        values["other_income"] = True
    return values or None


def label(values: Mapping[str, Any]) -> str:
    """The facts in plain words: "Files VAT every quarter, pays salaries"."""
    parts = []
    vat = values.get("vat")
    if vat == "exempt":
        parts.append("files no periodic VAT return")
    elif vat:
        parts.append(f"files VAT every {'month' if vat == 'monthly' else 'quarter'}")
    for attr, yes, no in (("employees", "pays salaries", "pays no salaries"),
                          ("advance_payments", "makes advance payments of corporate income tax",
                           "makes no advance payments of corporate income tax"),
                          ("other_income", "pays rents or fees with tax withheld",
                           "pays no rents or fees with tax withheld")):
        if attr in values and values[attr] is not None:
            parts.append(yes if values[attr] else no)
    text = ", ".join(parts)
    return text[:1].upper() + text[1:]
