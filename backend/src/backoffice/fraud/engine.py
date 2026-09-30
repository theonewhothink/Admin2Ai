"""Fraud engine (§26): hard stops before money moves.

Checks one case — a supplier document and/or a payment instruction, the email
that delivered it, the supplier's profile and history, and the tenant's own
companies — and returns a :class:`FraudAssessment`:

* ``hard_stop``: any HIGH or CRITICAL signal. Payment is blocked until a
  human verifies out of band; money movement needs hard approval anyway (§25).
* ``signals``: every finding with its severity and a plain owner line.
* ``owner_message``: e.g. "Vodafone changed the IBAN shown on its invoice.
  Payment blocked."

Hard-stop checks (§26): changed supplier IBAN, invalid bank details, new
payment recipient, changed or lookalike sender domain, unusual amount,
duplicate invoice with different IBAN, unusual country, invoice recipient
mismatch, suspicious payment instructions, altered document.

Unusual currency (checklist Q6) is a check, not a hard stop: an invoice in a
currency the supplier has never used (with enough history), or bank details
in a country whose currency does not fit the invoice's, is a WARNING. Next
to a bank-detail change it becomes a hard stop (HIGH): the two together are
how a diverted payment usually looks.

The engine is read-only. It never adds an IBAN to a supplier profile and has
no switch to accept changed beneficiary information: only a recorded human
hard approval, verified out of band, can do that
(:func:`backoffice.fraud.beneficiary.trust_iban`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backoffice.domain.models import Document, DocumentType, LegalEntity, Supplier
from backoffice.learning.keys import display_name, qualified_tax_id, same_tax_id, tax_id_country
from backoffice.learning.plain import count_phrase, format_money
from backoffice.learning.stats import OUTLIER_Z, mad, median, modified_z

from .domains import DomainVerdict, check_sender_domain
from .iban import find_ibans, iban_country, is_valid_iban, mask_iban, normalize_iban
from .phrases import PhraseCategory, find_suspicious_phrases

__all__ = [
    "COUNTRY_NAMES",
    "CURRENCY_NAMES",
    "IBAN_CURRENCY",
    "DEFAULT_CONFIG",
    "HARD_STOP_SEVERITIES",
    "AlteredDocumentHint",
    "AlteredSignal",
    "FraudAssessment",
    "FraudCase",
    "FraudConfig",
    "FraudSignal",
    "Severity",
    "SignalKind",
    "assess",
]


class Severity(str, Enum):
    INFO = "info"  # context only (e.g. not enough history)
    WARNING = "warning"  # worth a look, does not block
    HIGH = "high"  # hard stop
    CRITICAL = "critical"  # hard stop, strongest evidence of fraud


HARD_STOP_SEVERITIES = frozenset({Severity.HIGH, Severity.CRITICAL})
_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.WARNING: 2, Severity.INFO: 3}


class SignalKind(str, Enum):
    CHANGED_IBAN = "changed_iban"
    INVALID_BANK_DETAILS = "invalid_bank_details"
    DUPLICATE_DIFFERENT_IBAN = "duplicate_different_iban"
    LOOKALIKE_DOMAIN = "lookalike_domain"
    ALTERED_DOCUMENT = "altered_document"
    SUSPICIOUS_INSTRUCTIONS = "suspicious_instructions"
    NEW_PAYMENT_RECIPIENT = "new_payment_recipient"
    CHANGED_EMAIL_DOMAIN = "changed_email_domain"
    UNUSUAL_COUNTRY = "unusual_country"
    RECIPIENT_MISMATCH = "recipient_mismatch"
    UNUSUAL_AMOUNT = "unusual_amount"
    DUPLICATE_INVOICE = "duplicate_invoice"
    NOT_ENOUGH_HISTORY = "not_enough_history"
    UNUSUAL_CURRENCY = "unusual_currency"


_KIND_ORDER = {kind: i for i, kind in enumerate(SignalKind)}
_BENEFICIARY_KINDS = frozenset(
    {SignalKind.CHANGED_IBAN, SignalKind.NEW_PAYMENT_RECIPIENT, SignalKind.DUPLICATE_DIFFERENT_IBAN}
)

# Owner-facing country names ("in the Netherlands"). Unlisted codes are shown as-is.
COUNTRY_NAMES: dict[str, str] = {
    "AT": "Austria", "BE": "Belgium", "BG": "Bulgaria", "BR": "Brazil", "CH": "Switzerland",
    "CY": "Cyprus", "CZ": "Czechia", "DE": "Germany", "DK": "Denmark", "EE": "Estonia",
    "ES": "Spain", "FI": "Finland", "FR": "France", "GB": "the United Kingdom", "GR": "Greece",
    "HR": "Croatia", "HU": "Hungary", "IE": "Ireland", "IL": "Israel", "IS": "Iceland",
    "IT": "Italy", "LI": "Liechtenstein", "LT": "Lithuania", "LU": "Luxembourg", "LV": "Latvia",
    "MC": "Monaco", "MT": "Malta", "NL": "the Netherlands", "NO": "Norway", "PL": "Poland",
    "PT": "Portugal", "RO": "Romania", "SE": "Sweden", "SI": "Slovenia", "SK": "Slovakia",
    "SM": "San Marino", "US": "the United States",
}  # fmt: skip


# Owner-facing currency names ("in US dollars"). Unlisted codes are shown as-is.
CURRENCY_NAMES: dict[str, str] = {
    "EUR": "euros", "USD": "US dollars", "GBP": "pounds", "CHF": "Swiss francs", "SEK": "Swedish kronor",
    "DKK": "Danish kroner", "NOK": "Norwegian kroner", "PLN": "Polish zloty", "CZK": "Czech koruna",
    "HUF": "Hungarian forint", "RON": "Romanian lei", "BGN": "Bulgarian lev", "ISK": "Icelandic kronur",
    "CAD": "Canadian dollars", "AUD": "Australian dollars", "NZD": "New Zealand dollars", "JPY": "Japanese yen",
    "ILS": "shekels", "BRL": "Brazilian reais", "TRY": "Turkish lira", "AED": "UAE dirhams",
}  # fmt: skip

# The currency a bank account in each country normally holds (IBAN country -> ISO 4217):
# the euro area (Bulgaria since 2026-01-01) and the micro-states using the euro, then the
# others. verified_as_of: 2026-09 (author knowledge). Unlisted countries are not judged.
_EURO_IBAN_COUNTRIES = frozenset(
    "AT BE BG CY DE EE ES FI FR GR HR IE IT LT LU LV MT NL PT SI SK AD MC SM VA ME XK".split()
)
IBAN_CURRENCY: dict[str, str] = {
    **{c: "EUR" for c in sorted(_EURO_IBAN_COUNTRIES)},
    "GB": "GBP", "GI": "GBP", "CH": "CHF", "LI": "CHF", "SE": "SEK", "DK": "DKK", "FO": "DKK", "GL": "DKK",
    "NO": "NOK", "PL": "PLN", "CZ": "CZK", "HU": "HUF", "RO": "RON", "IS": "ISK", "IL": "ILS", "TR": "TRY",
    "AE": "AED", "BR": "BRL",
}  # fmt: skip


def _country(code: str) -> str:
    return COUNTRY_NAMES.get(code.upper(), code.upper())


def _currency_name(code: str) -> str:
    return CURRENCY_NAMES.get(code.upper(), code.upper())


def _currencies(codes: Sequence[str]) -> str:
    names = [_currency_name(c) for c in sorted(codes)]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"


def _countries(codes: Sequence[str]) -> str:
    names = [_country(c) for c in sorted(codes)]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"


# --------------------------------------------------------------------------- inputs and outputs


@runtime_checkable
class AlteredSignal(Protocol):
    """Shape of an altered-document signal (matches the verification layer's tamper signals)."""

    kind: Any  # enum or str
    strength: Any  # "strong" | "weak" (enum or str)
    detail: str


@dataclass(frozen=True)
class AlteredDocumentHint:
    kind: str
    strength: str  # "strong" | "weak"
    detail: str = ""


@dataclass(frozen=True)
class FraudConfig:
    min_history: int = 5  # past invoices needed before judging an amount
    outlier_z: Decimal = OUTLIER_Z  # modified z-score cut-off (Iglewicz & Hoaglin)
    min_relative_change: Decimal = Decimal("0.5")  # and at least 50% away from the median


DEFAULT_CONFIG = FraudConfig()


@dataclass(frozen=True)
class FraudCase:
    """Everything the engine looks at. Nothing here is modified."""

    entities: Sequence[LegalEntity]  # the tenant's own companies
    supplier: Supplier | None = None  # the supplier profile, if the supplier is known
    document: Document | None = None
    history: Sequence[Document] = ()  # this supplier's earlier documents
    sender: str | None = None  # From: of the email that delivered the document/instruction
    message_text: str = ""  # subject + body of that email
    payment_iban: str | None = None  # where a payment would go, when not on the document
    payment_amount: Decimal | None = None  # when there is no document
    payment_currency: str = "EUR"
    altered: Sequence[AlteredSignal] = ()
    # The document's bank details arrived on a later copy of an invoice already on file (checklist Q4): they are
    # judged exactly like bank details on a first copy, and the owner is told where they came from.
    iban_on_later_copy: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.payment_amount, float):
            raise TypeError("money must be Decimal, never float")


class FraudSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: SignalKind
    severity: Severity
    owner_line: str  # plain, calm, safe to show
    facts: dict[str, str] = Field(default_factory=dict)  # internal detail for audit (§55)

    @property
    def hard_stop(self) -> bool:
        return self.severity in HARD_STOP_SEVERITIES


class FraudAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)

    hard_stop: bool
    signals: tuple[FraudSignal, ...]
    owner_message: str | None
    beneficiary_ibans: tuple[str, ...] = ()  # normalized, valid IBANs a payment would go to
    needs_beneficiary_verification: bool = False  # call the supplier on a number you already trust
    passed: tuple[str, ...] = ()  # plain "Why?" lines for checks that passed (§54)

    @model_validator(mode="after")
    def _consistent(self) -> FraudAssessment:
        if self.hard_stop != any(s.hard_stop for s in self.signals):
            raise ValueError("hard_stop must reflect the signals")
        if self.hard_stop and not self.owner_message:
            raise ValueError("a hard stop always tells the owner why")
        return self

    def of_kind(self, kind: SignalKind) -> tuple[FraudSignal, ...]:
        return tuple(s for s in self.signals if s.kind is kind)


# --------------------------------------------------------------------------- engine


@dataclass
class _Findings:
    name: str  # supplier display name
    signals: list[FraudSignal] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)

    def add(self, kind: SignalKind, severity: Severity, line: str, **facts: object) -> None:
        self.signals.append(
            FraudSignal(kind=kind, severity=severity, owner_line=line, facts={k: str(v) for k, v in facts.items()})
        )


@dataclass(frozen=True)
class _Beneficiary:
    iban: str
    origin: str  # "invoice" | "payment" | "email"


def assess(case: FraudCase, config: FraudConfig = DEFAULT_CONFIG) -> FraudAssessment:
    """Run every §26 check and return the assessment. Pure: inputs are never changed."""
    supplier_name = case.supplier.name if case.supplier else (case.document.supplier_name if case.document else None)
    found = _Findings(name=display_name(supplier_name))
    beneficiaries = _beneficiaries(case, found)
    known = _known_ibans(case.supplier)
    _check_ibans(beneficiaries, known, found)
    _check_sender(case, found)
    _check_amount(case, config, found)
    _check_duplicates(case, found)
    _check_country(beneficiaries, known, case, found)
    _check_recipient(case, found)
    _check_language(case, found)
    _check_altered(case, found)
    _check_currency(beneficiaries, known, case, config, found)
    signals = tuple(sorted(found.signals, key=lambda s: (_SEVERITY_ORDER[s.severity], _KIND_ORDER[s.kind])))
    return FraudAssessment(
        hard_stop=any(s.hard_stop for s in signals),
        signals=signals,
        owner_message=_owner_message(signals),
        beneficiary_ibans=tuple(b.iban for b in beneficiaries),
        needs_beneficiary_verification=any(s.kind in _BENEFICIARY_KINDS for s in signals),
        passed=tuple(found.passed),
    )


def _owner_message(signals: Sequence[FraudSignal]) -> str | None:
    hard = [s for s in signals if s.hard_stop]
    if not hard:
        return None
    parts = [hard[0].owner_line]
    if len(hard) > 1:
        parts.append(f"I also found {count_phrase(len(hard) - 1, 'other warning sign')}.")
    parts.append("Payment blocked.")
    return " ".join(parts)


# --------------------------------------------------------------------------- bank details


def _known_ibans(supplier: Supplier | None) -> frozenset[str]:
    if supplier is None:
        return frozenset()
    return frozenset(normalize_iban(i) for i in supplier.known_ibans if i and i.strip())


def _own_ibans(entities: Sequence[LegalEntity]) -> frozenset[str]:
    return frozenset(normalize_iban(i) for e in entities for i in e.own_ibans if i and i.strip())


def _beneficiaries(case: FraudCase, found: _Findings) -> list[_Beneficiary]:
    raw: list[tuple[str, str]] = []
    if case.document is not None and case.document.iban:
        raw.append((case.document.iban, "copy" if case.iban_on_later_copy else "invoice"))
    if case.payment_iban:
        raw.append((case.payment_iban, "payment"))
    raw.extend((iban, "email") for iban in find_ibans(case.message_text))
    own = _own_ibans(case.entities)
    result: list[_Beneficiary] = []
    for value, origin in raw:
        if not is_valid_iban(value):
            where = "on this invoice" if origin in ("invoice", "copy") else "for this payment"
            found.add(SignalKind.INVALID_BANK_DETAILS, Severity.CRITICAL,
                      f"The bank details {where} are not valid.", origin=origin)  # fmt: skip
            continue
        iban = normalize_iban(value)
        if iban in own or any(b.iban == iban for b in result):
            continue
        result.append(_Beneficiary(iban, origin))
    return result


def _changed_line(name: str, origin: str) -> str:
    if origin == "invoice":
        return f"{name} changed the IBAN shown on its invoice."
    if origin == "copy":
        return f"A new copy of {name}'s invoice shows a bank account you have not paid before."
    if origin == "email":
        return f"The email gives a new IBAN for {name}."
    return f"This payment goes to a new IBAN for {name}."


def _check_ibans(beneficiaries: Sequence[_Beneficiary], known: frozenset[str], found: _Findings) -> None:
    if not beneficiaries:
        return
    for b in beneficiaries:
        if known and b.iban not in known:
            found.add(SignalKind.CHANGED_IBAN, Severity.CRITICAL, _changed_line(found.name, b.origin),
                      origin=b.origin, iban=mask_iban(b.iban),
                      known=", ".join(sorted(mask_iban(k) for k in known)))  # fmt: skip
        elif not known:
            line = (f"A new copy of {found.name}'s invoice asks to be paid into an account you have not paid before."
                    if b.origin == "copy" else
                    f"{found.name} asks to be paid into an account you have not paid before.")
            found.add(SignalKind.NEW_PAYMENT_RECIPIENT, Severity.HIGH, line,
                      origin=b.origin, iban=mask_iban(b.iban))  # fmt: skip
    if known and all(b.iban in known for b in beneficiaries):
        found.passed.append(f"Bank details match what {found.name} used before.")


# --------------------------------------------------------------------------- sender


def _check_sender(case: FraudCase, found: _Findings) -> None:
    if not case.sender:
        return
    domains = list(case.supplier.email_domains) if case.supplier else []
    addresses = [case.supplier.contact_email] if case.supplier and case.supplier.contact_email else []
    check = check_sender_domain(case.sender, domains, addresses)
    host = check.sender_domain or "an unknown address"
    if check.verdict is DomainVerdict.LOOKALIKE:
        found.add(SignalKind.LOOKALIKE_DOMAIN, Severity.CRITICAL,
                  f"The email came from an address made to look like {check.imitated}.",
                  sender_domain=host, imitated=check.imitated)  # fmt: skip
    elif check.verdict is DomainVerdict.NEW_ADDRESS:
        # Same free-mail provider (gmail.com…) proves nothing: only the exact address does.
        found.add(SignalKind.CHANGED_EMAIL_DOMAIN, Severity.HIGH,
                  f"The email came from a personal address {found.name} has not used before.",
                  sender_domain=host, known=", ".join(addresses or domains))  # fmt: skip
    elif check.verdict is DomainVerdict.FREE_MAIL:
        found.add(SignalKind.CHANGED_EMAIL_DOMAIN, Severity.HIGH,
                  f"The email came from a personal address, not {found.name}'s usual one.",
                  sender_domain=host, known=", ".join(domains))  # fmt: skip
    elif check.verdict is DomainVerdict.CHANGED:
        found.add(SignalKind.CHANGED_EMAIL_DOMAIN, Severity.HIGH,
                  f"The email came from {host}, not {found.name}'s usual address.",
                  sender_domain=host, known=", ".join(domains))  # fmt: skip
    elif check.verdict is DomainVerdict.KNOWN:
        found.passed.append(f"Sent from {found.name}'s usual email address.")


# --------------------------------------------------------------------------- amount


@dataclass(frozen=True)
class _Amount:
    value: Decimal  # absolute
    currency: str  # upper-case ISO code
    what: str  # "This invoice" | "This payment"


def _amounts_to_judge(case: FraudCase) -> list[_Amount]:
    """The invoice total and, when it differs, the amount actually being paid.

    A credit note is money coming back, never an unusual charge. A payment
    instruction is judged even when a document is attached: the payment is
    what moves money, and it may not be the invoice total.
    """
    amounts: list[_Amount] = []
    doc = case.document
    if doc is not None and doc.gross_amount is not None and doc.doc_type is not DocumentType.CREDIT_NOTE:
        amounts.append(_Amount(abs(doc.gross_amount), doc.currency.strip().upper(), "This invoice"))
    if case.payment_amount is not None:
        payment = _Amount(abs(case.payment_amount), case.payment_currency.strip().upper(), "This payment")
        if not any(a.value == payment.value and a.currency == payment.currency for a in amounts):
            amounts.append(payment)
    return amounts


def _check_amount(case: FraudCase, config: FraudConfig, found: _Findings) -> None:
    for current in _amounts_to_judge(case):
        _judge_amount(case, current, config, found)


def _past_amounts(case: FraudCase, currency: str) -> list[Decimal]:
    current_id = case.document.id if case.document else None
    return [
        abs(d.gross_amount)
        for d in case.history
        if d.gross_amount is not None
        and d.currency.strip().upper() == currency
        and d.doc_type is not DocumentType.CREDIT_NOTE
        and d.id != current_id
    ]


def _judge_amount(case: FraudCase, current: _Amount, config: FraudConfig, found: _Findings) -> None:
    amount, currency = current.value, current.currency
    past = _past_amounts(case, currency)
    if len(past) < config.min_history:
        if not any(s.kind is SignalKind.NOT_ENOUGH_HISTORY for s in found.signals):
            found.add(SignalKind.NOT_ENOUGH_HISTORY, Severity.INFO,
                      f"Not enough history with {found.name} to judge the amount yet.",
                      history=len(past), needed=config.min_history)  # fmt: skip
        return
    center, spread = median(past), mad(past)
    if center <= 0:
        return
    relative = abs(amount - center) / center
    z = modified_z(amount, center, spread)
    far = relative >= config.min_relative_change and (z is None or abs(z) >= config.outlier_z)
    usual = format_money(center, currency)
    facts = {"median": str(center), "mad": str(spread), "relative": f"{relative:.4f}", "z": str(z),
             "judged": current.what}  # fmt: skip
    if far and amount > center:
        found.add(SignalKind.UNUSUAL_AMOUNT, Severity.HIGH,
                  f"{found.name} usually charges around {usual}. {current.what} is {format_money(amount, currency)}.",
                  **facts)  # fmt: skip
    elif far:
        found.add(SignalKind.UNUSUAL_AMOUNT, Severity.WARNING,
                  f"This is much lower than {found.name} usually charges (around {usual}).", **facts)  # fmt: skip
    else:
        line = f"The amount is in line with {found.name}'s usual invoices."
        if line not in found.passed:
            found.passed.append(line)


# --------------------------------------------------------------------------- duplicates


def _invoice_key(number: str | None) -> str | None:
    if not number:
        return None
    key = "".join(number.split()).upper()
    return key or None


def _same_invoice(doc: Document, prev: Document) -> bool:
    mine, theirs = _invoice_key(doc.invoice_number), _invoice_key(prev.invoice_number)
    if mine and theirs:
        return mine == theirs
    return (
        doc.gross_amount is not None
        and doc.issue_date is not None
        and prev.gross_amount == doc.gross_amount
        and prev.issue_date == doc.issue_date
    )


def _check_duplicates(case: FraudCase, found: _Findings) -> None:
    doc = case.document
    if doc is None:
        return
    earlier = sorted(
        (d for d in case.history if d.id != doc.id and d.doc_type is not DocumentType.CREDIT_NOTE),
        key=lambda d: (d.issue_date is None, d.issue_date, d.id),
        reverse=True,
    )
    for prev in earlier:
        if not _same_invoice(doc, prev):
            continue
        number = doc.invoice_number or prev.invoice_number
        label = f"invoice {number.strip()}" if number else "this invoice"
        mine = normalize_iban(doc.iban) if doc.iban and is_valid_iban(doc.iban) else None
        theirs = normalize_iban(prev.iban) if prev.iban and is_valid_iban(prev.iban) else None
        if mine and theirs and mine != theirs:
            found.add(SignalKind.DUPLICATE_DIFFERENT_IBAN, Severity.CRITICAL,
                      f"{found.name} already sent {label} with different bank details.",
                      iban=mask_iban(mine), earlier_iban=mask_iban(theirs))  # fmt: skip
        else:
            found.add(SignalKind.DUPLICATE_INVOICE, Severity.WARNING,
                      f"This looks like a copy of {label}, which we already have.")  # fmt: skip
        return


# --------------------------------------------------------------------------- country


def _check_country(
    beneficiaries: Sequence[_Beneficiary], known: frozenset[str], case: FraudCase, found: _Findings
) -> None:
    if case.supplier is None:
        return
    profile = {c.strip().upper() for c in case.supplier.countries if c and c.strip()}
    profile |= {iban_country(k) for k in known}
    if not profile:
        return
    reported: set[str] = set()
    for b in beneficiaries:
        country = iban_country(b.iban)
        if country not in profile and country not in reported:
            reported.add(country)
            found.add(SignalKind.UNUSUAL_COUNTRY, Severity.HIGH,
                      f"This bank account is in {_country(country)}. "
                      f"{found.name} is normally paid in {_countries(sorted(profile))}.",
                      country=country, usual=",".join(sorted(profile)))  # fmt: skip
    doc = case.document
    tax_country = tax_id_country(doc.supplier_tax_id) if doc is not None else None
    if tax_country and tax_country not in profile and tax_country not in reported:
        reported.add(tax_country)
        found.add(SignalKind.UNUSUAL_COUNTRY, Severity.HIGH,
                  f"The invoice shows a tax number from {_country(tax_country)}, "
                  f"not {_countries(sorted(profile))}.",
                  country=tax_country, usual=",".join(sorted(profile)))  # fmt: skip
    if beneficiaries and not reported:
        where = _countries(sorted({iban_country(b.iban) for b in beneficiaries}))
        found.passed.append(f"The bank account is in {where}, as usual.")


# --------------------------------------------------------------------------- currency


def _check_currency(
    beneficiaries: Sequence[_Beneficiary], known: frozenset[str], case: FraudCase, config: FraudConfig,
    found: _Findings,
) -> None:
    """An unusual currency is a check; next to a bank-detail change, a hard stop (module docstring)."""
    doc = case.document
    if doc is not None and doc.gross_amount is not None:
        currency, what = doc.currency.strip().upper(), "invoice"
    elif case.payment_amount is not None:
        currency, what = case.payment_currency.strip().upper(), "payment"
    else:
        return
    changed = any(s.kind in _BENEFICIARY_KINDS for s in found.signals)
    severity = Severity.HIGH if changed else Severity.WARNING
    if doc is not None and doc.gross_amount is not None and doc.doc_type is not DocumentType.CREDIT_NOTE:
        past = [d.currency.strip().upper() for d in case.history
                if d.id != doc.id and d.gross_amount is not None and d.doc_type is not DocumentType.CREDIT_NOTE]
        if len(past) >= config.min_history and currency not in past:
            usual = sorted(set(past))
            found.add(SignalKind.UNUSUAL_CURRENCY, severity,
                      f"{found.name} usually bills you in {_currencies(usual)}. "
                      f"This invoice is in {_currency_name(currency)}.",
                      currency=currency, usual=",".join(usual), history=len(past), bank_change=changed)  # fmt: skip
    # Nothing new: the account already took this currency, or the supplier is paid in countries that
    # use it and this account is in one of them (a supplier paid in Portugal and the UK, billing in euros).
    profile = {c.strip().upper() for c in case.supplier.countries if c and c.strip()} if case.supplier else set()
    profile |= {iban_country(k) for k in known}
    profile_currencies = {IBAN_CURRENCY[c] for c in profile if c in IBAN_CURRENCY}
    before = {(normalize_iban(d.iban), d.currency.strip().upper()) for d in case.history
              if d.iban and is_valid_iban(d.iban) and (doc is None or d.id != doc.id)}
    for b in beneficiaries:
        country = iban_country(b.iban)
        expected = IBAN_CURRENCY.get(country)
        if expected is None or expected == currency or (b.iban, currency) in before:
            continue
        if country in profile and currency in profile_currencies:
            continue
        found.add(SignalKind.UNUSUAL_CURRENCY, severity,
                  f"The bank account is in {_country(country)}, which uses {_currency_name(expected)}, "
                  f"but the {what} is in {_currency_name(currency)}.",
                  country=country, account_currency=expected, currency=currency, bank_change=changed)  # fmt: skip
        return


# --------------------------------------------------------------------------- recipient


def _check_recipient(case: FraudCase, found: _Findings) -> None:
    doc = case.document
    if doc is None or not doc.customer_tax_id:
        return
    for entity in case.entities:
        # Our own number is often stored without a country prefix: make it explicit,
        # so the same digits from another country are not taken for us.
        if same_tax_id(qualified_tax_id(entity.tax_id, entity.country), doc.customer_tax_id):
            found.passed.append(f"Addressed to {display_name(entity.name, fallback=entity.name)}.")
            return
    found.add(SignalKind.RECIPIENT_MISMATCH, Severity.HIGH,
              f"This invoice is addressed to another company (tax number {' '.join(doc.customer_tax_id.split())}).",
              customer_tax_id=doc.customer_tax_id)  # fmt: skip


# --------------------------------------------------------------------------- language


def _check_language(case: FraudCase, found: _Findings) -> None:
    hits = find_suspicious_phrases(case.message_text)
    if not hits:
        return
    categories = {h.category for h in hits}
    phrases = "; ".join(h.phrase for h in hits)
    if PhraseCategory.BANK_CHANGE in categories:
        severity, line = Severity.CRITICAL, "The email asks you to pay into new bank details."
    elif {PhraseCategory.URGENCY, PhraseCategory.SECRECY} <= categories:
        severity, line = Severity.HIGH, "The email pushes for an urgent, confidential payment."
    elif PhraseCategory.URGENCY in categories:
        severity, line = Severity.WARNING, "The email pushes for urgent payment."
    else:
        severity, line = Severity.WARNING, "The email asks to keep the payment confidential."
    found.add(SignalKind.SUSPICIOUS_INSTRUCTIONS, severity, line, phrases=phrases)


# --------------------------------------------------------------------------- altered document


def _strength(signal: AlteredSignal) -> str:
    value = getattr(signal.strength, "value", signal.strength)
    return str(value).strip().lower()


def _check_altered(case: FraudCase, found: _Findings) -> None:
    if not case.altered:
        return
    strong = [s for s in case.altered if _strength(s) == "strong"]
    kinds = ", ".join(str(getattr(s.kind, "value", s.kind)) for s in case.altered)
    if strong or len(case.altered) >= 2:
        severity = Severity.CRITICAL if strong else Severity.HIGH
        found.add(SignalKind.ALTERED_DOCUMENT, severity, "This document shows signs of being edited.", kinds=kinds)
    else:
        found.add(SignalKind.ALTERED_DOCUMENT, Severity.WARNING, "This document may have been edited.", kinds=kinds)
