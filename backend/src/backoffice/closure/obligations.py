"""Administrative obligations (§24): find them in letters, prove them done, list what's due.

Detection reads government, bank, landlord and insurer text in English ("due by",
"amount due", "KYC", "tax office", ...) plus the wording of the companies' countries,
which lives in their country packs (§49, ``CompanyPack.obligation_vocabulary``): Portugal's
"prazo", "data limite de pagamento", "Autoridade Tributária", "Segurança Social",
"renovação"; Spain's "plazo", "importe a ingresar", "Agencia Tributaria"... A letter is read
with its company's country's wording (the business's companies' when it names none; every
company pack's when there are no companies yet). It produces a domain
:class:`~backoffice.domain.models.Obligation` with the responsible person,
amount, date, company, consequence, required evidence and a machine-checkable
verification condition.

Rules of evidence:

* Words in a letter are never verified evidence: every detection is AMBER at
  best. When the letter gives two different deadlines or amounts the finding
  is RED (CONFLICT) and the earliest date is kept only so reminders are never
  late; a person confirms it (§19, §57).
* :func:`satisfy` closes an obligation only with GREEN facts that meet the
  condition exactly (amount to the cent, reference, kind of proof). A
  same-reference payment with a different amount is a CONFLICT, never a guess.
* When the letter gives two different amounts, no amount is kept: the
  condition is marked *disputed* and nothing closes it until a person
  confirms what is due (a new condition replaces it).
* Proof must be newer than the letter (``since``) unless it identifies this
  very obligation: a payment or reply carrying its reference, or a renewal
  that runs past the deadline. Last month's rent is not this month's rent.
* A zero or negative total ("-405,00 €") is never read as an amount to pay.

Local fees and grants (checklist X26, X30):

* The municipal tourist tax ("tourist tax", "city tax"; Portugal's "taxa municipal turística"):
  with an amount it is a payment to the municipality (proven by that payment), without one it is the
  monthly declaration (proven by the submission receipt or the owner's confirmation).
* Grant and subsidy communications ("grant", "subsidy"; the agencies and words a country's pack names,
  such as Portugal's IFAP, Portugal 2030, "subsídio", "candidatura"): documents to send by a deadline
  (proven by the agency's acknowledgement or the owner's confirmation), and a grant payment announced,
  approved or made (proven only by the money arriving in the bank). Payroll words (Portugal's "subsídio
  de férias") and support lines ("customer support", "apoio ao cliente") are never a grant.

Everything here is wording, not law. The phrase tables are common letter
conventions (unverified against a corpus, verified_as_of: never); the
consequences are cautious "may" statements unless the letter itself names
one. Relative deadlines ("within 15 days", "no prazo de 15 dias") are counted in calendar days
from the day the letter arrived, which is never later than a business-day
count from the same day; the exact legal counting rule is NOT encoded and the
finding says the date needs checking.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right, insort
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum

from backoffice.domain.models import (
    LegalEntity,
    Obligation,
    ObligationKind,
    Quality,
    SourceKind,
)

from ._text import (
    MIDDLE_DOT,
    cents,
    count_phrase,
    day_month,
    fold,
    format_money,
    join_and,
    phrase_pattern,
    require_money,
)

__all__ = [
    "AUTO_RENEWS",
    "DEFAULT_DUE_SOON_DAYS",
    "RENEWAL_KINDS",
    "ConfirmationFinding",
    "DueItem",
    "EvidenceFact",
    "GRANT_KINDS",
    "Issuer",
    "ObligationFinding",
    "ProofKind",
    "Satisfaction",
    "VerificationCondition",
    "detect_confirmation",
    "detect_obligation",
    "due_soon",
    "grant_agency",
    "is_grant_text",
    "mentions_tourist_tax",
    "normalize_reference",
    "pack_vocabulary",
    "proof_for",
    "satisfy",
]

DEFAULT_DUE_SOON_DAYS = 14  # product default for Home "Due soon" (§35), not a legal figure
GRANT_NOTICE_LEAD_DAYS = 7  # a grant payment may be booked up to a week before the letter's (or its own) date
# The consequence of a renewal that happens by itself: nothing is needed unless the owner wants a change.
AUTO_RENEWS = "It renews on its own unless you act."


# =========================================================================== vocabulary


class Issuer(str, Enum):
    TAX_AUTHORITY = "tax_authority"
    SOCIAL_SECURITY = "social_security"
    BANK = "bank"
    LANDLORD = "landlord"
    INSURER = "insurer"
    MUNICIPALITY = "municipality"
    GRANT_AGENCY = "grant_agency"
    OTHER = "other"


class ProofKind(str, Enum):
    """What proves an obligation done. DECISION only ever closes a renewal."""

    PAYMENT = "payment"
    SUBMISSION = "submission"  # filing receipt
    REPLY = "reply"  # answer or documents sent to whoever asked
    RENEWAL = "renewal"  # the renewed policy, contract or licence
    DECISION = "decision"  # owner decided not to renew


# The core's own wording: English, as folded phrases (lowercase, no accents). A country's words live in its
# pack (``CompanyPack.obligation_vocabulary``, e.g. backoffice.countries.pt.obligations) and are read with these.
# Unverified conventions; extend as letters are seen.
_ISSUER_PHRASES: dict[Issuer, tuple[str, ...]] = {
    Issuer.TAX_AUTHORITY: ("hmrc", "hm revenue", "tax authority", "tax office"),
    Issuer.SOCIAL_SECURITY: ("social security",),
    Issuer.BANK: ("your bank", "the bank"),
    Issuer.LANDLORD: ("landlord", "lease"),
    Issuer.INSURER: ("insurer", "insurance company"),
    Issuer.MUNICIPALITY: ("city council", "municipality", "town hall"),
}  # fmt: skip
_SOURCE_ISSUER = {SourceKind.BANK: Issuer.BANK, SourceKind.CARD: Issuer.BANK}

_KYC = ("kyc", "know your customer", "verify your identity", "identity verification", "proof of address",
        "beneficial owner", "update your details")  # fmt: skip
_PAYMENT = ("payment", "pay", "amount due", "balance due", "total due")
_FILING = ("tax return", "vat return", "filing", "file your return", "self assessment")
_REQUEST = ("request", "requested", "please provide", "please send", "information request")
_DEBT = ("debt collection", "debt recovery", "collection agency", "outstanding debt", "final notice",
         "final demand")  # fmt: skip
_RENEWAL = ("renewal", "renew", "renews", "expires", "expiry", "expiration")
_INSURANCE = ("insurance", "insurer", "policy")
_LICENSE = ("licence", "license", "permit")
_RENT = ("rent", "landlord")
_BANK_REQUEST = ("missing documents", "please provide", "request")
_PAYMENT_DEADLINE = ("due by", "payment due", "amount due", "due date", "pay by")
# The municipal tourist tax (the operator declares and pays it monthly).
_TOURIST_TAX = ("tourist tax", "city tax", "overnight tax")
# Grant words. Some are enough on their own; a core word ("grant", Portugal's "candidatura") needs a second grant
# word next to it (a loan letter's "funding" alone is never a grant).
_GRANT_ALONE = ("subsidy", "subsidies", "grant application", "grant agreement", "grant payment", "grant award",
                "grant funding")
_GRANT_CORE = ("grant", "grants")
_GRANT_PAIRED = (*_GRANT_CORE, "funding", "awarded", "beneficiary")
# Never a grant: customer support lines, access being granted (a pack adds its payroll allowances).
_NOT_GRANT = ("customer support", "grant access", "granted access", "grants access")
# In a grant letter: documents to send (an application, a payment claim, an acceptance) ...
_GRANT_DOCUMENTS = ("submit", "send", "provide", "upload", "documents", "supporting documents", "acceptance form")
# ... or money the agency pays: approved, to be paid, or paid.
_GRANT_PAID = ("payment", "paid", "approved", "transfer", "transferred")
_AUTO_RENEW = ("renews automatically", "automatic renewal", "auto-renew", "auto renew")

_STRONG_DATE_ANCHORS = ("due by", "due on", "due date", "deadline", "no later than", "pay by", "renewal date",
                        "expires on", "valid until")  # fmt: skip
_WEAK_DATE_ANCHORS = ("by", "before", "until")
_STRONG_AMOUNT_ANCHORS = ("amount due", "total due", "balance due", "amount payable", "total amount due",
                          "outstanding balance")  # fmt: skip
_WEAK_AMOUNT_ANCHORS = ("total", "amount", "rent", "premium")

# What a letter says may happen, by the label the owner reads (a pack adds "consequence:<label>" phrases).
_CONSEQUENCE_PHRASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("a fine", ("fine", "fines", "penalty", "penalties")),
    ("interest", ("interest", "late interest")),
    ("a late fee", ("late fee", "late payment fee")),
    ("suspension", ("suspension", "suspend", "suspended", "blocked")),
    ("cancellation", ("cancel", "cancellation", "termination", "terminate")),
    ("legal action", ("court", "legal action")),
)  # fmt: skip

_GENERIC_CONSEQUENCE: dict[ObligationKind, str] = {
    ObligationKind.TAX_DEADLINE: "Paying late may lead to a fine and interest.",
    ObligationKind.FILING: "Filing late may lead to a fine.",
    ObligationKind.GOVERNMENT_REQUEST: "Not replying in time may lead to a fine or further action.",
    ObligationKind.KYC_REQUEST: "The bank may limit the account until this is done.",
    ObligationKind.BANK_REQUEST: "The bank may limit the account until this is done.",
    ObligationKind.INSURANCE_RENEWAL: "Cover may stop if it is not renewed.",
    ObligationKind.CONTRACT_RENEWAL: "The contract may renew or end on its own.",
    ObligationKind.LICENSE_RENEWAL: "The licence may lapse if it is not renewed.",
    ObligationKind.RENT: "Paying late may cost a late fee.",
    ObligationKind.DEBT_COLLECTION: "Not paying may lead to legal action.",
    ObligationKind.PAYMENT_DEADLINE: "Paying late may cost a fee.",
    ObligationKind.TOURIST_TAX: "Paying late may lead to a fine and interest.",
    ObligationKind.TOURIST_TAX_DECLARATION: "Declaring late may lead to a fine.",
    ObligationKind.GRANT_DOCUMENTS: "The grant may be delayed or cancelled if the documents are late.",
    ObligationKind.GRANT_PAYMENT: "Nothing to pay. I check that the money arrives.",
    ObligationKind.VAT_RETURN: "Filing late may lead to a fine and a surcharge.",
}

_PROOF_FOR_KIND: dict[ObligationKind, ProofKind] = {
    ObligationKind.TAX_DEADLINE: ProofKind.PAYMENT,
    ObligationKind.RENT: ProofKind.PAYMENT,
    ObligationKind.DEBT_COLLECTION: ProofKind.PAYMENT,
    ObligationKind.PAYMENT_DEADLINE: ProofKind.PAYMENT,
    ObligationKind.FILING: ProofKind.SUBMISSION,
    ObligationKind.VAT_RETURN: ProofKind.SUBMISSION,
    ObligationKind.GOVERNMENT_REQUEST: ProofKind.REPLY,
    ObligationKind.KYC_REQUEST: ProofKind.REPLY,
    ObligationKind.BANK_REQUEST: ProofKind.REPLY,
    ObligationKind.INSURANCE_RENEWAL: ProofKind.RENEWAL,
    ObligationKind.CONTRACT_RENEWAL: ProofKind.RENEWAL,
    ObligationKind.LICENSE_RENEWAL: ProofKind.RENEWAL,
    ObligationKind.TOURIST_TAX: ProofKind.PAYMENT,
    ObligationKind.TOURIST_TAX_DECLARATION: ProofKind.SUBMISSION,
    ObligationKind.GRANT_DOCUMENTS: ProofKind.REPLY,
    ObligationKind.GRANT_PAYMENT: ProofKind.PAYMENT,  # money in: the grant arriving in the bank
}

# Grant and subsidy obligations (checklist X30).
GRANT_KINDS: frozenset[ObligationKind] = frozenset({ObligationKind.GRANT_DOCUMENTS, ObligationKind.GRANT_PAYMENT})

RENEWAL_KINDS: frozenset[ObligationKind] = frozenset(
    {ObligationKind.INSURANCE_RENEWAL, ObligationKind.CONTRACT_RENEWAL, ObligationKind.LICENSE_RENEWAL}
)


def proof_for(kind: ObligationKind) -> ProofKind:
    """What proves an obligation of this kind done: a payment, a filing receipt, a reply or a renewal."""
    return _PROOF_FOR_KIND[ObligationKind(kind)]


# Default routing (§25, §28): the accountant usually files returns; the owner does the rest.
_RESPONSIBLE: dict[ObligationKind, str] = {ObligationKind.FILING: "accountant",
                                           ObligationKind.VAT_RETURN: "accountant"}

_ACCEPTED_FACTS: dict[ProofKind, frozenset[ProofKind]] = {
    ProofKind.PAYMENT: frozenset({ProofKind.PAYMENT}),
    ProofKind.SUBMISSION: frozenset({ProofKind.SUBMISSION}),
    ProofKind.REPLY: frozenset({ProofKind.REPLY}),
    ProofKind.RENEWAL: frozenset({ProofKind.RENEWAL, ProofKind.DECISION}),
}

_PAST_VERB = {
    ProofKind.PAYMENT: "Paid",
    ProofKind.SUBMISSION: "Submitted",
    ProofKind.REPLY: "Sent",
    ProofKind.RENEWAL: "Renewed",
    ProofKind.DECISION: "Decided",
}


# =========================================================================== verification condition


def normalize_reference(value: str) -> str:
    """'123 456 789' -> '123456789'; letters upper-cased; separators dropped."""
    return re.sub(r"[^0-9A-Z]", "", value.upper())


@dataclass(frozen=True, slots=True)
class VerificationCondition:
    """Machine-checkable proof requirement, stored in ``Obligation.verification_condition``.

    Encoded as ``v1;proof=payment;amount=405.00;currency=EUR;reference=123456789;by=2026-10-20``
    plus, when set, ``since=2026-09-20`` (the earliest date proof may carry unless it names
    this obligation, e.g. the day the letter arrived) and ``disputed=1`` (the letter
    contradicts itself; only a person can settle it, §19).
    """

    proof: ProofKind
    amount: Decimal | None = None
    currency: str = "EUR"
    reference: str | None = None
    by: date | None = None
    since: date | None = None
    disputed: bool = False

    def __post_init__(self) -> None:
        if self.proof is ProofKind.DECISION:
            raise ValueError("a decision is a kind of fact, not a condition")
        if self.amount is not None:
            amount = require_money(self.amount, "amount")
            if amount <= 0:
                raise ValueError("amount must be positive")
            object.__setattr__(self, "amount", cents(amount))
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError(f"not a currency code: {self.currency!r}")
        if self.reference is not None:
            ref = normalize_reference(self.reference)
            object.__setattr__(self, "reference", ref or None)

    def encode(self) -> str:
        parts = ["v1", f"proof={self.proof.value}"]
        if self.amount is not None:
            parts += [f"amount={self.amount}", f"currency={self.currency}"]
        if self.reference:
            parts.append(f"reference={self.reference}")
        if self.by is not None:
            parts.append(f"by={self.by.isoformat()}")
        if self.since is not None:
            parts.append(f"since={self.since.isoformat()}")
        if self.disputed:
            parts.append("disputed=1")
        return ";".join(parts)

    @classmethod
    def parse(cls, text: str) -> VerificationCondition:
        """Inverse of :meth:`encode`; raises ValueError on anything else."""
        parts = text.strip().split(";")
        if not parts or parts[0] != "v1":
            raise ValueError("not a v1 verification condition")
        fields: dict[str, str] = {}
        for part in parts[1:]:
            key, sep, value = part.partition("=")
            if not sep or key in fields:
                raise ValueError(f"bad condition part: {part!r}")
            fields[key] = value
        unknown = set(fields) - {"proof", "amount", "currency", "reference", "by", "since", "disputed"}
        if unknown or "proof" not in fields:
            raise ValueError("bad condition fields")
        if fields.get("disputed", "1") != "1":
            raise ValueError("bad disputed flag")
        return cls(
            proof=ProofKind(fields["proof"]),
            amount=Decimal(fields["amount"]) if "amount" in fields else None,
            currency=fields.get("currency", "EUR"),
            reference=fields.get("reference"),
            by=date.fromisoformat(fields["by"]) if "by" in fields else None,
            since=date.fromisoformat(fields["since"]) if "since" in fields else None,
            disputed="disputed" in fields,
        )

    def describe(self, today: date | None = None) -> str:
        """Plain wording, e.g. 'Proof of a payment of €405.00 with reference 123456789 by 20 October.'"""
        what = {
            ProofKind.PAYMENT: "Proof of a payment",
            ProofKind.SUBMISSION: "Proof that it was submitted",
            ProofKind.REPLY: "A copy of the reply that was sent",
            ProofKind.RENEWAL: "The renewed document, or a decision not to renew",
        }[self.proof]
        if self.amount is not None:
            what += f" of {format_money(self.amount, self.currency)}"
        if self.reference:
            what += f" with reference {self.reference}"
        if self.by is not None:
            what += f" by {day_month(self.by, today)}"
        return what + "."


# =========================================================================== detection: dates


# Month names (a pack adds its own as "month:<n>" / "month_abbr:<n>"). A month name that is also an ordinary word
# ("may", "march") needs a year after it to be a date ("month_ambiguous").
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december")
_MONTH_ABBREVIATIONS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
                        "oct": 10, "nov": 11, "dec": 12}  # fmt: skip
_AMBIGUOUS_MONTHS = ("may", "march")
# Relative deadlines ("within 15 days"; a pack adds "relative_lead" and "relative_days") and payment references
# ("payment reference: 123 456 789"; a pack adds "reference_label").
_RELATIVE_LEADS = ("within", "in the next")
_RELATIVE_DAYS = ("business days", "working days", "days")
_REFERENCE_LABELS = ("payment reference", "reference", "ref")
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_NUM_DATE = re.compile(r"(?<![\d/.-])(\d{1,2})([/.-])(\d{1,2})\2(\d{4}|\d{2})(?![\d/.-]?\d)")
_SENTENCE_BREAK = re.compile(r"[.;!?]\s")


@dataclass(frozen=True, slots=True)
class _Hit:
    start: int
    end: int
    value: object


def _overlaps(spans: Sequence[tuple[int, int]], start: int, end: int) -> bool:
    """True if [start, end) overlaps one of ``spans`` (sorted, non-overlapping). O(log n)."""
    i = bisect_right(spans, (start, end))
    before = spans[i - 1] if i else None
    after = spans[i] if i < len(spans) else None
    return (before is not None and before[1] > start) or (after is not None and after[0] < end)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _infer_year(month: int, day: int, received_on: date) -> date | None:
    """Next occurrence on or after the day the letter arrived (deadlines lie ahead)."""
    for year in (received_on.year, received_on.year + 1):
        candidate = _safe_date(year, month, day)
        if candidate is not None and candidate >= received_on:
            return candidate
    return None


def _full_year(text: str) -> int:
    year = int(text)
    return 2000 + year if len(text) == 2 else year


def _date_hits(folded: str, received_on: date) -> list[_Hit]:
    """All dates in the text. Numeric dates are day-first (EU convention, §63)."""
    hits: list[_Hit] = []
    taken: list[tuple[int, int]] = []  # sorted, non-overlapping spans already used

    def add(m: re.Match[str], value: date | None) -> None:
        if value is None or _overlaps(taken, m.start(), m.end()):
            return
        insort(taken, (m.start(), m.end()))
        hits.append(_Hit(m.start(), m.end(), value))

    for m in _ISO_DATE.finditer(folded):
        add(m, _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in _NUM_DATE.finditer(folded):
        a, b, year = int(m.group(1)), int(m.group(3)), _full_year(m.group(4))
        value = _safe_date(year, b, a) or (_safe_date(year, a, b) if a <= 12 < b else None)
        add(m, value)
    words = _words()
    for m in words.text_dmy.finditer(folded):
        month, abbrev = words.months[m.group(2)]
        if abbrev and not m.group(3):
            continue  # "3 out of 5" is not a date
        day = int(m.group(1))
        year = m.group(3)
        add(m, _safe_date(int(year), month, day) if year else _infer_year(month, day, received_on))
    for m in words.text_mdy.finditer(folded):
        month, abbrev = words.months[m.group(1)]
        year = m.group(3)
        if (abbrev or m.group(1) in words.ambiguous_months) and not year:
            continue  # "you may 2 ..." / "march 3 miles" are not dates
        day = int(m.group(2))
        add(m, _safe_date(int(year), month, day) if year else _infer_year(month, day, received_on))
    return sorted(hits, key=lambda h: h.start)


# =========================================================================== detection: amounts

_CURRENCY_TOKENS = {"€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR", "£": "GBP",
                    "gbp": "GBP", "$": "USD", "usd": "USD"}  # fmt: skip
_CUR = r"(?:€|euros|euro|eur|£|gbp|\$|usd)"
_NUM = r"\d{1,3}(?:[ .,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
# A minus sign written against the number or its symbol ("-405,00", "€-405", "-€405", "−405").
# A dash with spaces around it ("Total - 405,00 €") is punctuation, not a sign.
_SIGN = r"[-−]"
_AMOUNT = re.compile(
    rf"(?:(?<![a-z])(?P<s0>{_SIGN})?(?P<c1>{_CUR})\s?(?P<s1>{_SIGN})?(?P<n1>{_NUM})(?![\d])"
    rf"|(?<![\d.,])(?P<s2>{_SIGN})?(?P<n2>{_NUM})\s?(?P<c2>{_CUR}))"
    rf"(?![0-9a-z])"
)
_BARE_AMOUNT = re.compile(rf"(?<![\d.,])({_SIGN})?(\d{{1,3}}(?:[ .,]\d{{3}})*[.,]\d{{2}})(?![\d.,]?\d)")


def _parse_number(raw: str) -> Decimal | None:
    """'1.234,56' / '1,234.56' / '1 234,56' / '405' -> Decimal; ambiguous shapes -> None."""
    text = raw.strip()
    decimal_part = re.search(r"([.,])(\d{1,2})$", text)
    int_part, frac, dec_sep = text, "", None
    if decimal_part:
        int_part, frac, dec_sep = text[: decimal_part.start()], decimal_part.group(2), decimal_part.group(1)
    separators = set(re.findall(r"[ .,]", int_part))
    if len(separators) > 1 or (dec_sep is not None and dec_sep in separators):
        return None
    groups = re.split(r"[ .,]", int_part)
    if len(groups) > 1 and (
        not 1 <= len(groups[0]) <= 3 or groups[0].startswith("0") or any(len(g) != 3 for g in groups[1:])
    ):
        return None  # "08 405,00" is two numbers, not 8,405.00
    digits = "".join(groups)
    if not digits.isdigit():
        return None
    return cents(Decimal(f"{digits}.{frac or '0'}"))


def _scan(pattern: re.Pattern[str], text: str, read: Callable[[re.Match[str]], object | None]) -> list[_Hit]:
    """Like finditer, but a match that does not parse is retried one character later,
    so '08 405,00 €' still yields '405,00 €' instead of swallowing it."""
    hits: list[_Hit] = []
    pos = 0
    while (m := pattern.search(text, pos)) is not None:
        value = read(m)
        if value is None:
            pos = m.start() + 1
            continue
        hits.append(_Hit(m.start(), m.end(), value))
        pos = m.end()
    return hits


def _signed(value: Decimal | None, negative: bool) -> Decimal | None:
    return None if value is None else (-value if negative and value else value)


def _read_marked(m: re.Match[str]) -> tuple[Decimal, str] | None:
    """(amount, currency). Zero and negative amounts are kept: "total: -5,00 €" means
    nothing to pay, and must not let another amount in the letter take its place."""
    negative = any(m.group(g) for g in ("s0", "s1", "s2"))
    value = _signed(_parse_number(m.group("n1") or m.group("n2")), negative)
    if value is None:
        return None
    return value, _CURRENCY_TOKENS[m.group("c1") or m.group("c2")]


def _read_bare(m: re.Match[str]) -> tuple[Decimal, str] | None:
    value = _signed(_parse_number(m.group(2)), bool(m.group(1)))
    return None if value is None else (value, "EUR")


def _amount_hits(folded: str) -> list[_Hit]:
    return _scan(_AMOUNT, folded, _read_marked)


def _bare_amount_hits(folded: str) -> list[_Hit]:
    """Unmarked '405,00' — only trusted right after an amount word (assumed EUR)."""
    return _scan(_BARE_AMOUNT, folded, _read_bare)


# =========================================================================== anchoring


def _anchored(
    folded: str, anchors: re.Pattern[str], hits: Sequence[_Hit], window: int
) -> list[_Hit]:
    """Hits that start within ``window`` characters after an anchor, same sentence.

    Only the first hit after each anchor can qualify (any later one is farther
    away and crosses the same text), so a binary search keeps this O(n log n)
    on long emails instead of anchors x hits.
    """
    ordered = sorted(hits, key=lambda h: h.start)
    starts = [h.start for h in ordered]
    found: dict[int, _Hit] = {}
    for a in anchors.finditer(folded):
        i = bisect_left(starts, a.end())
        if i == len(ordered):
            break
        h = ordered[i]
        if h.start - a.end() <= window and not _SENTENCE_BREAK.search(folded, a.end(), h.start):
            found.setdefault(h.start, h)
    return [found[k] for k in sorted(found)]


def _tiered(
    folded: str,
    hits: Sequence[_Hit],
    strong: re.Pattern[str],
    weak: re.Pattern[str],
    window: int,
) -> tuple[list[_Hit], str]:
    """Strong-anchored hits, else weak-anchored, else a lone hit; with the tier name."""
    for tier, pattern in (("strong", strong), ("weak", weak)):
        anchored = _anchored(folded, pattern, hits, window)
        if anchored:
            return anchored, tier
    return (list(hits), "lone") if len({h.value for h in hits}) == 1 else ([], "none")


# =========================================================================== classification

# The core's own wording (English), by category. A country pack adds its own
# (``CompanyPack.obligation_vocabulary``, folded phrases by category; issuer phrases under "issuer:<issuer>"):
# a letter is read with its company's country's, see :func:`_letter_vocabulary`.
_BASE_PHRASES: dict[str, tuple[str, ...]] = {
    "kyc": _KYC, "payment": _PAYMENT, "filing": _FILING, "request": _REQUEST,
    "debt": _DEBT, "renewal": _RENEWAL, "insurance": _INSURANCE, "license": _LICENSE,
    "rent": _RENT, "bank_request": _BANK_REQUEST, "payment_deadline": _PAYMENT_DEADLINE,
    "auto_renew": _AUTO_RENEW, "strong_date": _STRONG_DATE_ANCHORS,
    "weak_date": _WEAK_DATE_ANCHORS, "strong_amount": _STRONG_AMOUNT_ANCHORS,
    "weak_amount": _WEAK_AMOUNT_ANCHORS, "tourist_tax": _TOURIST_TAX, "grant_alone": _GRANT_ALONE,
    "grant_paired": _GRANT_PAIRED, "grant_core": _GRANT_CORE, "not_grant": _NOT_GRANT,
    "grant_documents": _GRANT_DOCUMENTS,
    "grant_paid": _GRANT_PAID,
    # Grant agencies a country names ("ifap"), with their names for the owner under "agency:<folded>".
    "grant_agency": (),
    # A periodic VAT return a country names as such ("modelo 303"): none in the core's own wording.
    "vat_return": (),
    **{f"issuer:{issuer.value}": phrases for issuer, phrases in _ISSUER_PHRASES.items()},
}  # fmt: skip
_NEVER = re.compile(r"(?!x)x")


def _compile(phrases: Mapping[str, Sequence[str]]) -> dict[str, re.Pattern[str]]:
    return {name: phrase_pattern(p) if p else _NEVER for name, p in phrases.items()}


def _alternation(phrases: Iterable[str]) -> str:
    """Folded phrases as a regex alternation, longest first, any spacing between words."""
    ordered = sorted({p for p in phrases if p}, key=len, reverse=True)
    return "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in ordered) or "(?!x)x"


@dataclass(frozen=True, slots=True)
class _Words:
    """The wording one call reads letters with: the core's English and the countries' words (their packs')."""

    patterns: dict[str, re.Pattern[str]]
    confirmations: dict[str, re.Pattern[str]]
    titles: dict[str, str]
    consequences: tuple[tuple[str, re.Pattern[str]], ...]
    agencies: dict[str, str]
    months: dict[str, tuple[int, bool]]
    ambiguous_months: frozenset[str]
    text_dmy: re.Pattern[str]
    text_mdy: re.Pattern[str]
    relative: re.Pattern[str]
    reference: re.Pattern[str]


def _build(vocabulary: Mapping[str, Sequence[str]]) -> _Words:
    """The core's wording with ``vocabulary`` (a pack's categories, plus ``title:<kind>``, ``consequence:<label>``,
    ``agency:<folded>``, ``month:<n>``, ``month_abbr:<n>``, ``month_ambiguous``, ``relative_lead``,
    ``relative_days`` and ``reference_label``)."""
    merged = {name: tuple(phrases) for name, phrases in _BASE_PHRASES.items()}
    confirmations = {name: tuple(phrases) for name, phrases in _C_PHRASES.items()}
    consequences = {label: tuple(phrases) for label, phrases in _CONSEQUENCE_PHRASES}
    titles: dict[str, str] = {}
    agencies: dict[str, str] = {}
    months = {name: (i, False) for i, name in enumerate(_MONTHS, start=1)}
    abbreviations = list(_MONTH_ABBREVIATIONS.items())
    ambiguous = set(_AMBIGUOUS_MONTHS)
    leads, days, labels = list(_RELATIVE_LEADS), list(_RELATIVE_DAYS), list(_REFERENCE_LABELS)
    for name, values in vocabulary.items():
        phrases = tuple(values)
        kind, _, rest = name.partition(":")
        if kind == "title":
            if phrases:
                titles[rest] = phrases[0]
        elif kind == "consequence":
            consequences[rest] = (*consequences.get(rest, ()), *phrases)
        elif kind == "agency":
            if phrases:
                agencies[rest] = phrases[0]
        elif kind == "month":
            months.update({p: (int(rest), False) for p in phrases})
        elif kind == "month_abbr":
            abbreviations += [(p, int(rest)) for p in phrases]
        elif name == "month_ambiguous":
            ambiguous.update(phrases)
        elif name == "relative_lead":
            leads += phrases
        elif name == "relative_days":
            days += phrases
        elif name == "reference_label":
            labels += phrases
        elif name in confirmations:
            confirmations[name] = (*confirmations[name], *phrases)
        else:
            merged[name] = (*merged.get(name, ()), *phrases)
    merged["grant_agency"] = (*merged["grant_agency"], *agencies)
    for abbreviation, month in abbreviations:
        months.setdefault(abbreviation, (month, True))
    month_alt = "|".join(sorted(months, key=len, reverse=True))
    return _Words(
        patterns=_compile(merged),
        confirmations=_compile(confirmations),
        titles=titles,
        consequences=tuple((label, phrase_pattern(p)) for label, p in consequences.items() if p),
        agencies=agencies,
        months=months,
        ambiguous_months=frozenset(ambiguous),
        text_dmy=re.compile(
            rf"(?<![0-9a-z])(\d{{1,2}})(?:st|nd|rd|th|o)?\s+(?:de\s+)?({month_alt})\.?"
            rf"(?:,?\s+(?:de\s+)?(\d{{4}}))?(?![0-9a-z])"
        ),
        text_mdy=re.compile(
            rf"(?<![0-9a-z])({month_alt})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?(?![0-9a-z])"
        ),
        relative=re.compile(rf"(?:{_alternation(leads)})\s+(\d{{1,3}})\s+(?:{_alternation(days)})(?![a-z])"),
        # A digit group ends where the sentence does: "123 456 789." is one reference, while
        # "123 456 1.234,56" stops before the amount.
        reference=re.compile(
            rf"(?<![a-z])(?:{_alternation(labels)})\.?\s*(?:n\.?\s?o\.?|number|no\.?|#)?\s*[:.]?\s*"
            r"(\d+(?: \d+){0,5}(?!\d|[.,]\d)|[a-z0-9-]*\d[a-z0-9-]*)"
        ),
    )


# The wording in use for the current call; built once per distinct vocabulary.
_ACTIVE: ContextVar[_Words | None] = ContextVar("obligation_words", default=None)
_BUILT: dict[tuple[tuple[str, tuple[str, ...]], ...], _Words] = {}


def _built(vocabulary: Mapping[str, Sequence[str]]) -> _Words:
    key = tuple(sorted((k, tuple(v)) for k, v in vocabulary.items()))
    words = _BUILT.get(key)
    if words is None:
        words = _BUILT[key] = _build(vocabulary)
    return words


def pack_vocabulary(countries: Iterable[str] = ()) -> dict[str, tuple[str, ...]]:
    """The letter wording of ``countries``' packs, merged (every company pack's when none is given).

    A country no complete pack covers adds nothing: its letters are read with the core's English."""
    from backoffice.countries import CountryPackError, company_countries, company_pack

    merged: dict[str, tuple[str, ...]] = {}
    for country in dict.fromkeys(c.upper() for c in (tuple(countries) or company_countries()) if c):
        try:
            vocabulary = company_pack(country).obligation_vocabulary()
        except CountryPackError:
            continue
        for name, phrases in vocabulary.items():
            merged[name] = tuple(dict.fromkeys((*merged.get(name, ()), *phrases)))
    return merged


def _words() -> _Words:
    """The wording of the call in progress; outside one, the core's with every company pack's."""
    return _ACTIVE.get() or _built(pack_vocabulary())


def _pats() -> dict[str, re.Pattern[str]]:
    return _words().patterns


def _letter_vocabulary(
    text: str,
    sender: str,
    entities: Sequence[LegalEntity],
    default_entity_id: str | None,
    vocabulary: Mapping[str, Sequence[str]] | None,
) -> Mapping[str, Sequence[str]]:
    """The wording a letter is read with (§49): ``vocabulary`` when the caller gives it, else its company's
    country's pack's (the company its tax number or name shows), else the business's companies' countries',
    else every company pack's."""
    if vocabulary is not None:
        return vocabulary
    entity_id, _ = _entity(fold(f"{sender} {text}"), entities, default_entity_id)
    company = next((e for e in entities if e.id == entity_id), None)
    countries = (company.country,) if company is not None else tuple(e.country for e in entities)
    return pack_vocabulary(countries)


@contextmanager
def _wording(vocabulary: Mapping[str, Sequence[str]]) -> Iterator[None]:
    """Use the core's wording plus ``vocabulary`` (folded phrases by category) for one call."""
    token = _ACTIVE.set(_built(vocabulary))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def _has(name: str, folded: str) -> bool:
    return _pats()[name].search(folded) is not None


def _issuer(folded: str, source_kind: SourceKind | None) -> Issuer:
    pats = _pats()
    if pats["grant_agency"].search(folded):  # a grant letter may name Social Security among the proofs it wants
        return Issuer.GRANT_AGENCY
    for issuer in (Issuer.TAX_AUTHORITY, Issuer.SOCIAL_SECURITY):
        if pats[f"issuer:{issuer.value}"].search(folded):
            return issuer
    if source_kind in _SOURCE_ISSUER:
        return _SOURCE_ISSUER[source_kind]
    for issuer in (Issuer.BANK, Issuer.INSURER, Issuer.LANDLORD, Issuer.MUNICIPALITY):
        if pats[f"issuer:{issuer.value}"].search(folded):
            return issuer
    return Issuer.OTHER


def grant_agency(text: str) -> str | None:
    """The grant agency a text names, as the owner knows it ('IFAP', 'Portugal 2030'), or None."""
    words = _words()
    m = words.patterns["grant_agency"].search(fold(text))
    return words.agencies.get(" ".join(m.group(0).split())) if m else None


def mentions_tourist_tax(text: str) -> bool:
    """'tourist tax', 'city tax', and a pack's words for it ('Taxa Municipal Turística')."""
    return _has("tourist_tax", fold(text))


def is_grant_text(text: str) -> bool:
    """A grant or subsidy communication: an agency named, a word that means a grant on its own ('subsidy',
    Portugal's 'subsídio'), or two grant words together ('grant' and 'funding', 'candidatura' and 'apoio').
    Payroll allowances ('subsídio de férias') and support lines ('customer support') never count. While a letter
    is read, its wording (see :func:`_letter_vocabulary`) is used; outside one, every company pack's."""
    pats = _pats()
    folded = pats["not_grant"].sub(" ", fold(text))
    if pats["grant_agency"].search(folded) or pats["grant_alone"].search(folded):
        return True
    if not pats["grant_core"].search(folded):
        return False
    return len({" ".join(m.group(0).split()) for m in pats["grant_paired"].finditer(folded)}) >= 2


def _grant_kind(folded: str, has_amount: bool) -> ObligationKind | None:
    """Documents to send come first (a payment claim is documents too); then money the agency pays."""
    if _has("grant_documents", folded):
        return ObligationKind.GRANT_DOCUMENTS
    if has_amount and _has("grant_paid", folded):
        return ObligationKind.GRANT_PAYMENT
    return None


def _government_kind(folded: str, has_amount: bool) -> ObligationKind:
    if _has("vat_return", folded):  # a periodic VAT return its country names as such ("modelo 303")
        return ObligationKind.VAT_RETURN
    paying = _has("payment", folded)
    if paying and has_amount:
        return ObligationKind.TAX_DEADLINE
    if _has("filing", folded):
        return ObligationKind.FILING
    if paying:
        return ObligationKind.TAX_DEADLINE
    return ObligationKind.GOVERNMENT_REQUEST


def _renewal_kind(folded: str, issuer: Issuer) -> ObligationKind:
    if issuer is Issuer.INSURER or _has("insurance", folded):
        return ObligationKind.INSURANCE_RENEWAL
    if _has("license", folded):
        return ObligationKind.LICENSE_RENEWAL
    return ObligationKind.CONTRACT_RENEWAL


def _classify(folded: str, issuer: Issuer, has_amount: bool) -> ObligationKind | None:
    """First matching rule wins; the order is the policy."""
    if _has("kyc", folded):
        return ObligationKind.KYC_REQUEST
    if _has("tourist_tax", folded):  # the municipality's tourist tax: the payment, or the declaration
        return ObligationKind.TOURIST_TAX if has_amount else ObligationKind.TOURIST_TAX_DECLARATION
    if is_grant_text(folded):
        return _grant_kind(_pats()["not_grant"].sub(" ", folded), has_amount)
    if issuer in (Issuer.TAX_AUTHORITY, Issuer.SOCIAL_SECURITY):
        return _government_kind(folded, has_amount)
    if _has("debt", folded):
        return ObligationKind.DEBT_COLLECTION
    if _has("renewal", folded):
        return _renewal_kind(folded, issuer)
    if issuer is Issuer.LANDLORD or _has("rent", folded):
        return ObligationKind.RENT
    if issuer is Issuer.BANK and _has("bank_request", folded):
        return ObligationKind.BANK_REQUEST
    if _has("payment_deadline", folded):
        return ObligationKind.PAYMENT_DEADLINE
    return None


def _title(kind: ObligationKind, issuer: Issuer) -> str:
    if kind is ObligationKind.VAT_RETURN:
        return _words().titles.get(kind.value) or "VAT return"
    social = issuer is Issuer.SOCIAL_SECURITY
    if kind is ObligationKind.TAX_DEADLINE:
        return "Social Security payment" if social else "Tax payment"
    if kind is ObligationKind.FILING:
        return "Social Security declaration" if social else "Tax return"
    if kind is ObligationKind.GOVERNMENT_REQUEST:
        if social:
            return "Request from Social Security"
        return "Request from the tax office" if issuer is Issuer.TAX_AUTHORITY else "Official request"
    return {
        ObligationKind.KYC_REQUEST: "Your bank needs updated details",
        ObligationKind.BANK_REQUEST: "Request from your bank",
        ObligationKind.INSURANCE_RENEWAL: "Insurance renewal",
        ObligationKind.CONTRACT_RENEWAL: "Contract renewal",
        ObligationKind.LICENSE_RENEWAL: "Licence renewal",
        ObligationKind.RENT: "Rent payment",
        ObligationKind.DEBT_COLLECTION: "Debt collection letter",
        ObligationKind.PAYMENT_DEADLINE: "Payment due",
        ObligationKind.TOURIST_TAX: "Tourist tax payment",
        ObligationKind.TOURIST_TAX_DECLARATION: "Tourist tax declaration",
        ObligationKind.GRANT_DOCUMENTS: "Documents for your grant",
        ObligationKind.GRANT_PAYMENT: "Grant payment to receive",
    }[kind]


def _consequence(folded: str, kind: ObligationKind) -> str:
    if _has("auto_renew", folded):
        return AUTO_RENEWS
    named = [label for label, pattern in _words().consequences if pattern.search(folded)]
    if named:
        return f"The letter mentions {join_and(named)}."
    return _GENERIC_CONSEQUENCE[kind]


# =========================================================================== detection: reference and company

# ISO 11649 creditor references are printed in groups of four: "RF18 5390 0754 7034".
_RF_HEAD = re.compile(r"rf\d{2}(?![0-9a-z])")
_RF_GROUP = re.compile(r" ([0-9a-z]{1,4})(?![0-9a-z])")
_RF_SHAPE = re.compile(r"RF\d{2}[0-9A-Z]{1,21}")


def _rf_valid(ref: str) -> bool:
    """ISO 11649 check digits: move 'RFnn' to the end, letters to 10..35, mod 97 == 1."""
    if not _RF_SHAPE.fullmatch(ref):
        return False
    rearranged = ref[4:] + ref[:4]
    return int("".join(str(int(ch, 36)) for ch in rearranged)) % 97 == 1


def _rf_reference(folded: str, start: int) -> str | None:
    """A grouped RF reference starting at ``start``: the longest reading whose check digits
    hold, so a following word or date is never glued on. None when no reading is valid."""
    head = _RF_HEAD.match(folded, start)
    if head is None:
        return None
    groups: list[str] = []
    pos = head.end()
    while len(groups) < 6 and (g := _RF_GROUP.match(folded, pos)) is not None:
        groups.append(g.group(1))
        pos = g.end()
    for n in range(len(groups), 0, -1):
        candidate = normalize_reference(head.group(0) + "".join(groups[:n]))
        if _rf_valid(candidate):
            return candidate
    return None


def _reference(folded: str) -> str | None:
    refs: set[str] = set()
    for m in _words().reference.finditer(folded):
        token = m.group(1)
        ref = _rf_reference(folded, m.start(1)) if _RF_HEAD.fullmatch(token) else normalize_reference(token)
        if ref is not None and len(ref) >= 4:
            refs.add(ref)
    return refs.pop() if len(refs) == 1 else None


def _alnum_words(text: str) -> str:
    return " ".join(re.findall(r"[0-9a-z]+", fold(text)))


def _mentions_tax_id(folded: str, tax_id: str) -> bool:
    """'PT 123 456 789' / '123.456.789' / '123456789' all mention tax id PT123456789."""
    digits = re.sub(r"\D", "", tax_id)
    if len(digits) < 5:
        return False
    pattern = r"[ .]?".join(digits)
    return re.search(rf"(?<![\d.]){pattern}(?![\d]|[.]\d)", folded) is not None


def _entity(
    folded: str, entities: Sequence[LegalEntity], default_entity_id: str | None
) -> tuple[str | None, str | None]:
    """(entity id, owner-facing problem). NIF matches outrank name matches; ties are not guessed."""
    words = f" {_alnum_words(folded)} "
    by_tax = [e.id for e in entities if _mentions_tax_id(folded, e.tax_id)]
    by_name = [e.id for e in entities if (n := _alnum_words(e.name)) and f" {n} " in words]
    for found in (by_tax, by_name):
        unique = sorted(set(found))
        if len(unique) == 1:
            return unique[0], None
        if len(unique) > 1:
            return None, "which company this is for"
    if default_entity_id:
        return default_entity_id, None
    return None, "which company this is for"


# =========================================================================== finding


@dataclass(frozen=True, slots=True)
class ObligationFinding:
    """What a letter asks for, with how sure we are and what is still unknown.

    ``obligation`` is built when the date and company are known. ``quality`` is
    AMBER (from wording) or RED (the letter contradicts itself), never GREEN.
    """

    kind: ObligationKind
    issuer: Issuer
    title: str
    due_on: date | None
    amount: Decimal | None
    currency: str
    reference: str | None
    entity_id: str | None
    responsible: str
    consequence: str
    required_evidence: str
    condition: VerificationCondition
    quality: Quality
    reasons: tuple[str, ...]
    missing: tuple[str, ...]
    obligation: Obligation | None
    agency: str | None = None  # the grant agency the letter names ("IFAP"), for a grant letter

    @property
    def complete(self) -> bool:
        return self.obligation is not None

    @property
    def renews_on_its_own(self) -> bool:
        """A renewal the letter says happens by itself: shown for information, nothing to do (§24, §42)."""
        return self.kind in RENEWAL_KINDS and self.consequence == AUTO_RENEWS

    @property
    def question(self) -> str | None:
        """Owner-facing ask when something is unknown, e.g. 'I still need the due date.'"""
        if not self.missing:
            return None
        return f"I still need {join_and(list(self.missing))}."


@dataclass(frozen=True, slots=True)
class _Pick:
    value: object | None
    conflict: bool
    reason: str | None


def _pick_date(folded: str, received_on: date) -> _Pick:
    """Deadline date. A lone unanchored date counts only if it lies ahead (not the letter's date)."""
    hits = _date_hits(folded, received_on)
    chosen, tier = _tiered(folded, hits, _pats()["strong_date"], _pats()["weak_date"], window=40)
    values = sorted({h.value for h in chosen})  # type: ignore[type-var]
    if tier == "lone":
        values = [v for v in values if v >= received_on]  # type: ignore[operator]
    if not values:
        return _Pick(None, False, None)
    if len(values) > 1:
        shown = join_and([day_month(v, received_on) for v in values])  # type: ignore[arg-type]
        return _Pick(values[0], True, f"The letter gives different dates: {shown}.")
    reason = None if tier != "lone" else "This is the only date in the letter."
    return _Pick(values[0], False, reason)


def _with_bare(marked: Sequence[_Hit], bare: Sequence[_Hit]) -> list[_Hit]:
    """Marked amounts plus bare ones that don't overlap them, in text order."""
    spans = sorted((m.start, m.end) for m in marked)
    extra = [b for b in bare if not _overlaps(spans, b.start, b.end)]
    return sorted([*marked, *extra], key=lambda h: h.start)


def _pick_amount(folded: str) -> _Pick:
    marked = _amount_hits(folded)
    pats = _pats()
    strong = _anchored(folded, pats["strong_amount"], _with_bare(marked, _bare_amount_hits(folded)), 30)
    chosen = strong or _tiered(folded, marked, pats["strong_amount"], pats["weak_amount"], window=30)[0]
    values = sorted({h.value for h in chosen})  # type: ignore[type-var]
    if not values:
        return _Pick(None, False, None)
    if len(values) > 1:
        shown = join_and([format_money(v, c) for v, c in values])  # type: ignore[misc]
        return _Pick(values[0], True, f"The letter gives different amounts: {shown}.")
    return _Pick(values[0], False, None)


def _relative_deadline(folded: str, received_on: date) -> tuple[date, str] | None:
    m = _words().relative.search(folded)
    if not m:
        return None
    days = int(m.group(1))
    if days <= 0:
        return None
    reason = (
        f"The letter gives {count_phrase(days, 'day')}. I counted from the day it arrived; "
        "please check the exact date."
    )
    return received_on + timedelta(days=days), reason


def _required_evidence(kind: ObligationKind, condition: VerificationCondition) -> str:
    if kind is ObligationKind.GRANT_DOCUMENTS:
        by = f" by {day_month(condition.by)}" if condition.by is not None else ""
        return f"Proof that the documents were sent{by}."
    if kind is ObligationKind.GRANT_PAYMENT:
        amount = f" of {format_money(condition.amount, condition.currency)}" if condition.amount is not None else ""
        return f"The grant payment{amount} arriving in your bank."
    if condition.proof is ProofKind.RENEWAL:
        thing = {
            ObligationKind.INSURANCE_RENEWAL: "policy",
            ObligationKind.LICENSE_RENEWAL: "licence",
        }.get(kind, "contract")
        return f"The renewed {thing}, or your decision not to renew."
    return condition.describe()


def detect_obligation(
    text: str,
    *,
    tenant_id: str,
    received_on: date,
    sender: str = "",
    source_kind: SourceKind | None = None,
    entities: Sequence[LegalEntity] = (),
    default_entity_id: str | None = None,
    vocabulary: Mapping[str, Sequence[str]] | None = None,
) -> ObligationFinding | None:
    """Read one letter or message and return the obligation it creates, if any (§24).

    Returns None when the text is not an obligation: it must name a kind of
    obligation *and* carry a deadline signal (a date near a deadline word, a
    relative deadline, or a deadline word). ``sender`` (e.g. the From name)
    helps recognise who wrote; dates and amounts are read from ``text`` only.

    Meant for letters and notices from government, banks, landlords and
    insurers; supplier invoices belong to the document pipeline. Detection
    does not judge authenticity: "update your details or your account will be
    suspended" is also how phishing reads, so only pass messages that already
    passed the sender checks (§26).

    ``vocabulary`` is the wording of the countries to read it in (their packs' ``obligation_vocabulary``),
    read together with the core's English. Without it, the letter is read in its company's country's
    wording (the company its tax number or name shows), else the ``entities``' countries', else every
    company pack's (§49).
    """
    with _wording(_letter_vocabulary(text, sender, entities, default_entity_id, vocabulary)):
        return _detect_obligation(text, tenant_id=tenant_id, received_on=received_on, sender=sender,
                                  source_kind=source_kind, entities=entities, default_entity_id=default_entity_id)


def _detect_obligation(
    text: str,
    *,
    tenant_id: str,
    received_on: date,
    sender: str,
    source_kind: SourceKind | None,
    entities: Sequence[LegalEntity],
    default_entity_id: str | None,
) -> ObligationFinding | None:
    body = fold(text)
    everything = fold(f"{sender} {text}")
    amount_pick = _pick_amount(body)
    issuer = _issuer(everything, source_kind)
    kind = _classify(everything, issuer, amount_pick.value is not None)
    deadline = _deadline(body, received_on) if kind is not None else None
    if kind is ObligationKind.GRANT_PAYMENT and (deadline is None or deadline.due_on is None):
        deadline = _payment_date(body, received_on) or deadline  # "was paid on 15/10": the date it was made
    if kind is None or deadline is None:
        return None

    amount, currency, amount_reasons = _amount_due(amount_pick)
    reference = _reference(body)
    entity_id, entity_problem = _entity(everything, entities, default_entity_id)
    since = received_on
    if kind is ObligationKind.GRANT_PAYMENT:
        # A notice that a grant was paid often arrives after the money: a week before the payment date counts.
        since = min(received_on, deadline.due_on or received_on) - timedelta(days=GRANT_NOTICE_LEAD_DAYS)
    condition = _condition(
        kind, amount, currency, reference, deadline.due_on,
        since=since, disputed=amount_pick.conflict,
    )  # fmt: skip
    missing = [m for m in ("the due date" if deadline.due_on is None else None, entity_problem) if m]
    if condition.proof is ProofKind.PAYMENT and amount is None and (reference is None or amount_pick.conflict):
        missing.append("the amount")
    title = _title(kind, issuer)
    finding = ObligationFinding(
        kind=kind,
        issuer=issuer,
        title=title,
        due_on=deadline.due_on,
        amount=amount,
        currency=currency,
        reference=reference,
        entity_id=entity_id,
        responsible=_RESPONSIBLE.get(kind, "owner"),
        consequence=_consequence(everything, kind),
        required_evidence=_required_evidence(kind, condition),
        condition=condition,
        quality=Quality.RED if (deadline.conflict or amount_pick.conflict) else Quality.AMBER,
        reasons=(
            *_why_lines(issuer, deadline.due_on, amount, currency, reference, received_on,
                        agency=grant_agency(everything)),
            *deadline.reasons,
            *amount_reasons,
        ),
        missing=tuple(missing),
        obligation=None,
        agency=grant_agency(everything) if kind in GRANT_KINDS else None,
    )
    return replace(finding, obligation=_obligation(finding, tenant_id))


@dataclass(frozen=True, slots=True)
class _Deadline:
    due_on: date | None
    reasons: tuple[str, ...]
    conflict: bool


def _deadline(body: str, received_on: date) -> _Deadline | None:
    """An explicit date, else a relative one, else just a deadline word; None if no signal."""
    pick = _pick_date(body, received_on)
    if pick.value is not None:
        return _Deadline(pick.value, (pick.reason,) if pick.reason else (), pick.conflict)  # type: ignore[arg-type]
    relative = _relative_deadline(body, received_on)
    if relative is not None:
        return _Deadline(relative[0], (relative[1],), False)
    if _has("strong_date", body):
        return _Deadline(None, (), False)
    return None


def _payment_date(body: str, received_on: date) -> _Deadline | None:
    """A grant payment the letter says was made (or is made) on a date: the latest date it names."""
    values = sorted({h.value for h in _date_hits(body, received_on)})  # type: ignore[type-var]
    if not values:
        return None
    return _Deadline(values[-1], (), False)  # type: ignore[arg-type]


def _amount_due(pick: _Pick) -> tuple[Decimal | None, str, tuple[str, ...]]:
    """(amount to pay, currency, reasons). A conflict keeps no amount (§19); a zero or
    negative total is shown, never turned into an amount to pay."""
    if pick.value is None:
        return None, "EUR", ()
    value, currency = pick.value  # type: ignore[misc]
    if pick.conflict:
        return None, currency, (pick.reason,) if pick.reason else ()
    if value <= 0:
        shown = format_money(value, currency)
        return None, currency, (f"The amount in the letter is {shown}, so there may be nothing to pay.",)
    return value, currency, ()


def _condition(
    kind: ObligationKind,
    amount: Decimal | None,
    currency: str,
    reference: str | None,
    due_on: date | None,
    *,
    since: date | None = None,
    disputed: bool = False,
) -> VerificationCondition:
    proof = _PROOF_FOR_KIND[kind]
    return VerificationCondition(
        proof=proof,
        amount=amount if proof is ProofKind.PAYMENT else None,
        currency=currency,
        reference=reference if proof in (ProofKind.PAYMENT, ProofKind.REPLY) else None,
        by=due_on,
        since=since,
        disputed=disputed and proof is ProofKind.PAYMENT,
    )


def _obligation(finding: ObligationFinding, tenant_id: str) -> Obligation | None:
    """The domain obligation, once the date and the company are known."""
    if finding.due_on is None or finding.entity_id is None:
        return None
    return Obligation(
        tenant_id=tenant_id,
        entity_id=finding.entity_id,
        kind=finding.kind,
        title=finding.title,
        due_on=finding.due_on,
        amount=finding.amount,
        responsible=finding.responsible,
        consequence=finding.consequence,
        required_evidence=finding.required_evidence,
        verification_condition=finding.condition.encode(),
    )


_ISSUER_LINE = {
    Issuer.TAX_AUTHORITY: "From the tax office",
    Issuer.SOCIAL_SECURITY: "From Social Security",
    Issuer.BANK: "From your bank",
    Issuer.LANDLORD: "From your landlord",
    Issuer.INSURER: "From your insurer",
    Issuer.MUNICIPALITY: "From the municipality",
    Issuer.GRANT_AGENCY: "From the grant agency",
}


def _why_lines(
    issuer: Issuer,
    due_on: date | None,
    amount: Decimal | None,
    currency: str,
    reference: str | None,
    today: date,
    *,
    agency: str | None = None,
) -> list[str]:
    """§54 "Why?" lines for a detected obligation."""
    lines = [_ISSUER_LINE[issuer]] if issuer in _ISSUER_LINE else []
    if issuer is Issuer.GRANT_AGENCY and agency:
        lines = [f"From {agency}"]
    if due_on is not None:
        lines.append(f"Due {day_month(due_on, today)}")
    if amount is not None:
        lines.append(f"Amount {format_money(amount, currency)}")
    if reference:
        lines.append(f"Reference {reference}")
    return lines


# =========================================================================== confirmations (proof that it was done)

# Folded phrases in English; a country's pack adds its own under the same names (Portugal's "recebemos os seus
# documentos", "comprovativo de entrega"). Unverified conventions (verified_as_of: never); extend
# as letters are seen. A confirmation is never proof on its own: the caller checks it against the
# obligations on file (same company, kind, sender and reference) and closes one only when exactly
# one fits (§3, §24).
_STILL_ASKING = ("still need", "still missing", "still required", "missing documents", "please send",
                 "please provide", "please upload", "unless", "if you do not", "if payment is not")  # fmt: skip
_DECIDED = ("has been cancelled", "has been canceled", "has been terminated", "was terminated",
            "cancellation confirmed", "termination confirmed", "will not be renewed")  # fmt: skip
_RENEWED = ("has been renewed", "was renewed", "renewed until", "renewal confirmed", "renewal is confirmed",
            "we have renewed", "successfully renewed")  # fmt: skip
_SUBMITTED = ("has been submitted", "was submitted", "submitted successfully", "successfully submitted",
              "return received", "received your return", "successfully filed", "filing confirmation")  # fmt: skip
_ANSWERED = ("we have received your documents", "we received your documents", "received your documents",
             "documents received", "received your reply", "received your response", "confirm receipt",
             "thank you for sending", "thank you for providing", "details have been updated",
             "your details are up to date", "verification complete", "verification completed",
             "identity has been verified", "request completed", "request has been completed",
             "no further action")  # fmt: skip
_CONTRACT = ("contract", "subscription", "service agreement")
_VALIDITY = ("until", "valid until", "valid to", "new expiry date", "expires on", "ends on",
             "renewed until")  # fmt: skip
_C_PHRASES: dict[str, tuple[str, ...]] = {
    "still_asking": _STILL_ASKING, "decided": _DECIDED, "renewed": _RENEWED, "submitted": _SUBMITTED,
    "answered": _ANSWERED, "contract": _CONTRACT, "validity": _VALIDITY,
}  # fmt: skip


def _cpats() -> dict[str, re.Pattern[str]]:
    """The confirmation patterns in use: the core's, with the letter's countries' wording."""
    return _words().confirmations


_CONFIRMED_LINE = {
    ProofKind.REPLY: "They confirm they received what they asked for",
    ProofKind.SUBMISSION: "Filing receipt",
    ProofKind.RENEWAL: "Renewed",
    ProofKind.DECISION: "Confirmed it will not be renewed",
}
_REPLY_KINDS = frozenset({ObligationKind.KYC_REQUEST, ObligationKind.BANK_REQUEST, ObligationKind.GOVERNMENT_REQUEST})


@dataclass(frozen=True, slots=True)
class ConfirmationFinding:
    """A letter or message saying that something asked for was done (§24).

    ``proof`` is what it shows: a reply received, a filing receipt, a renewal (and how long it
    now runs, ``valid_until``) or an ending (a DECISION not to renew). ``kinds`` are the kinds of
    obligation it can confirm. It proves nothing by itself: the caller closes an obligation with
    it only when exactly one open obligation fits (§3).
    """

    proof: ProofKind
    kinds: frozenset[ObligationKind]
    issuer: Issuer
    entity_id: str | None
    reference: str | None
    valid_until: date | None
    on: date
    reasons: tuple[str, ...]

    def fact(self, evidence_id: str, quality: Quality = Quality.GREEN) -> EvidenceFact:
        """The confirmation as proof for :func:`satisfy`."""
        return EvidenceFact(evidence_id=evidence_id, kind=self.proof, on=self.on, quality=quality,
                            reference=self.reference, valid_until=self.valid_until)


def _validity(body: str, received_on: date) -> date | None:
    """The date a renewal now runs until: the one date after a validity word, else the one future date."""
    hits = _date_hits(body, received_on)
    anchored = _anchored(body, _cpats()["validity"], hits, 40)
    pool = anchored or hits
    values = sorted({h.value for h in pool if h.value > received_on})  # type: ignore[operator]
    return values[0] if len(values) == 1 else None  # type: ignore[return-value]


def _confirmed_kinds(proof: ProofKind, body: str, issuer: Issuer) -> frozenset[ObligationKind]:
    if issuer is Issuer.GRANT_AGENCY or (proof is ProofKind.REPLY and is_grant_text(body)):
        return frozenset({ObligationKind.GRANT_DOCUMENTS})
    if proof is ProofKind.SUBMISSION and _has("tourist_tax", body):
        return frozenset({ObligationKind.TOURIST_TAX_DECLARATION})
    if proof in (ProofKind.RENEWAL, ProofKind.DECISION):
        if _has("license", body):
            return frozenset({ObligationKind.LICENSE_RENEWAL})
        if issuer is Issuer.INSURER or _has("insurance", body):
            return frozenset({ObligationKind.INSURANCE_RENEWAL})
        if _cpats()["contract"].search(body):
            return frozenset({ObligationKind.CONTRACT_RENEWAL})
        return RENEWAL_KINDS
    if proof is ProofKind.SUBMISSION:
        return frozenset({ObligationKind.FILING, ObligationKind.VAT_RETURN})
    if _has("kyc", body):
        return frozenset({ObligationKind.KYC_REQUEST})
    if issuer is Issuer.BANK:
        return frozenset({ObligationKind.KYC_REQUEST, ObligationKind.BANK_REQUEST})
    if issuer in (Issuer.TAX_AUTHORITY, Issuer.SOCIAL_SECURITY):
        return frozenset({ObligationKind.GOVERNMENT_REQUEST})
    return _REPLY_KINDS


def detect_confirmation(
    text: str,
    *,
    received_on: date,
    sender: str = "",
    source_kind: SourceKind | None = None,
    entities: Sequence[LegalEntity] = (),
    default_entity_id: str | None = None,
    vocabulary: Mapping[str, Sequence[str]] | None = None,
) -> ConfirmationFinding | None:
    """Read one letter or message that says an obligation was done, or None (§24).

    "Recebemos os seus documentos", "A sua licença foi renovada até 30/11/2027", "Comprovativo de
    entrega", "Your contract has been terminated". A message that is still asking for something
    ("we still need", "please send", "unless") is never a confirmation. Like detection, this reads
    wording only: the caller decides whether it matches exactly one obligation on file.
    ``vocabulary``: the wording of the countries to read it in, as for detection.
    """
    with _wording(_letter_vocabulary(text, sender, entities, default_entity_id, vocabulary)):
        return _detect_confirmation(text, received_on=received_on, sender=sender, source_kind=source_kind,
                                    entities=entities, default_entity_id=default_entity_id)


def _detect_confirmation(
    text: str,
    *,
    received_on: date,
    sender: str,
    source_kind: SourceKind | None,
    entities: Sequence[LegalEntity],
    default_entity_id: str | None,
) -> ConfirmationFinding | None:
    body = fold(text)
    everything = fold(f"{sender} {text}")
    pats = _cpats()
    if not body or pats["still_asking"].search(body):
        return None
    proof = next((p for name, p in (("decided", ProofKind.DECISION), ("renewed", ProofKind.RENEWAL),
                                    ("submitted", ProofKind.SUBMISSION), ("answered", ProofKind.REPLY))
                  if pats[name].search(body)), None)
    if proof is None:
        return None
    issuer = _issuer(everything, source_kind)
    if proof is ProofKind.SUBMISSION and (issuer is Issuer.GRANT_AGENCY or is_grant_text(body)):
        proof = ProofKind.REPLY  # an application submitted to the agency: what it asked for was sent
    valid_until = _validity(body, received_on) if proof is ProofKind.RENEWAL else None
    reference = _reference(body)
    entity_id, _ = _entity(everything, entities, default_entity_id)
    lines = [_ISSUER_LINE[issuer]] if issuer in _ISSUER_LINE else []
    lines.append(_CONFIRMED_LINE[proof])
    if valid_until is not None:
        lines.append(f"Valid until {day_month(valid_until, received_on)}")
    if reference:
        lines.append(f"Reference {reference}")
    return ConfirmationFinding(
        proof=proof, kinds=_confirmed_kinds(proof, body, issuer), issuer=issuer, entity_id=entity_id,
        reference=reference, valid_until=valid_until, on=received_on, reasons=tuple(lines),
    )


# =========================================================================== satisfaction


@dataclass(frozen=True, slots=True)
class EvidenceFact:
    """A verified-or-not fact that might prove an obligation done.

    ``amount`` is the magnitude paid (sign ignored). ``on`` is the payment,
    submission, sending or decision date. ``valid_until`` is the end of a
    renewed policy, contract or licence.
    """

    evidence_id: str
    kind: ProofKind
    on: date
    quality: Quality
    amount: Decimal | None = None
    currency: str = "EUR"
    reference: str | None = None
    valid_until: date | None = None

    def __post_init__(self) -> None:
        if not self.evidence_id or not self.evidence_id.strip():
            raise ValueError("evidence_id is required")
        # Stored values arrive as strings ("payment", "verified"); compare as enums.
        object.__setattr__(self, "kind", ProofKind(self.kind))
        object.__setattr__(self, "quality", Quality(self.quality))
        if self.amount is not None:
            object.__setattr__(self, "amount", cents(abs(require_money(self.amount, "amount"))))

    @property
    def normalized_reference(self) -> str | None:
        if not self.reference:
            return None
        return normalize_reference(self.reference) or None


class _Verdict(str, Enum):
    MATCH = "match"
    CONFLICT = "conflict"
    UNSURE = "unsure"
    NO_MATCH = "no_match"


@dataclass(frozen=True, slots=True)
class Satisfaction:
    """Result of :func:`satisfy`. ``obligation`` carries the proof ids when satisfied."""

    satisfied: bool
    quality: Quality
    evidence_ids: tuple[str, ...]
    late: bool
    reasons: tuple[str, ...]
    obligation: Obligation


_OLDER = "The proof is older than the letter."


def _older_than_letter(cond: VerificationCondition, fact: EvidenceFact) -> bool:
    return cond.since is not None and fact.on < cond.since


def _check_payment(cond: VerificationCondition, fact: EvidenceFact) -> tuple[_Verdict, str | None]:
    """A matching reference identifies the debt, whatever the date; an amount alone does not."""
    if cond.amount is None and cond.reference is None:
        return _Verdict.UNSURE, "I need the amount or a reference to confirm this payment."
    ref = fact.normalized_reference
    ref_ok = None if not cond.reference or ref is None else ref == cond.reference
    amount_ok = None
    if cond.amount is not None and fact.amount is not None:
        amount_ok = fact.currency == cond.currency and fact.amount == cond.amount
    if cond.reference and ref_ok is False:
        return _Verdict.NO_MATCH, None
    if cond.reference and ref_ok:
        if amount_ok is False:
            paid = format_money(fact.amount or Decimal(0), fact.currency)
            due = format_money(cond.amount or Decimal(0), cond.currency)
            return _Verdict.CONFLICT, f"The payment was {paid} but {due} was due."
        if amount_ok is None and cond.amount is not None:
            return _Verdict.UNSURE, "The payment doesn't show the amount."
        return _Verdict.MATCH, None
    if amount_ok and _older_than_letter(cond, fact):
        return _Verdict.NO_MATCH, _OLDER
    if cond.reference and amount_ok:
        return _Verdict.UNSURE, "The amount matches but the payment doesn't show the reference."
    if not cond.reference and amount_ok:
        return _Verdict.MATCH, None
    return _Verdict.NO_MATCH, None


def _check_fact(cond: VerificationCondition, fact: EvidenceFact) -> tuple[_Verdict, str | None]:
    if fact.kind not in _ACCEPTED_FACTS[cond.proof]:
        return _Verdict.NO_MATCH, None
    if cond.proof is ProofKind.PAYMENT:
        return _check_payment(cond, fact)
    if cond.proof is ProofKind.RENEWAL and fact.kind is ProofKind.RENEWAL and cond.by is not None:
        # A renewal that runs past the deadline proves itself, even if signed early.
        if fact.valid_until is None:
            return _Verdict.UNSURE, "The renewal doesn't show how long it lasts."
        if fact.valid_until > cond.by:
            return _Verdict.MATCH, None
        return _Verdict.NO_MATCH, "The renewal ends before the current one."
    ref = fact.normalized_reference
    if cond.reference and ref is not None:
        return (_Verdict.MATCH, None) if ref == cond.reference else (_Verdict.NO_MATCH, None)
    if _older_than_letter(cond, fact):
        return _Verdict.NO_MATCH, _OLDER
    return _Verdict.MATCH, None


def _installments(cond: VerificationCondition, facts: Sequence[EvidenceFact]) -> list[EvidenceFact]:
    """GREEN same-reference payments that add up exactly to the amount due."""
    if cond.proof is not ProofKind.PAYMENT or not cond.reference or cond.amount is None:
        return []
    parts = [
        f
        for f in facts
        if f.kind is ProofKind.PAYMENT
        and f.quality is Quality.GREEN
        and f.normalized_reference == cond.reference
        and f.amount is not None
        and f.currency == cond.currency
    ]
    if len(parts) >= 2 and sum((f.amount for f in parts), Decimal(0)) == cond.amount:  # type: ignore[misc]
        return sorted(parts, key=lambda f: (f.on, f.evidence_id))
    return []


def _satisfied(
    obligation: Obligation, cond: VerificationCondition, proof: Sequence[EvidenceFact]
) -> Satisfaction:
    last = max(f.on for f in proof)
    late = cond.by is not None and last > cond.by
    verb = _PAST_VERB[proof[-1].kind]
    when = day_month(last, cond.by or last)
    reasons = [f"{verb} on {when}."]
    if len(proof) > 1:
        reasons[0] = f"{verb} in {len(proof)} parts, the last on {when}."
    if late and cond.by is not None:
        days = (last - cond.by).days
        reasons.append(f"That was {count_phrase(days, 'day')} after the deadline.")
    ids = tuple(f.evidence_id for f in proof)
    merged = sorted({*obligation.satisfied_by_evidence_ids, *ids})
    return Satisfaction(
        satisfied=True,
        quality=Quality.GREEN,
        evidence_ids=ids,
        late=late,
        reasons=tuple(reasons),
        obligation=obligation.model_copy(update={"satisfied_by_evidence_ids": merged}),
    )


def _unsatisfied(obligation: Obligation, quality: Quality, reasons: Iterable[str]) -> Satisfaction:
    unique = tuple(dict.fromkeys(reasons)) or ("I haven't seen proof yet.",)
    return Satisfaction(False, quality, (), False, unique, obligation)


def _unique_facts(facts: Iterable[EvidenceFact]) -> tuple[list[EvidenceFact], bool]:
    """One fact per evidence id (a payment listed twice is still one payment), and whether
    any evidence id was read two different ways (a conflict, §19)."""
    by_id: dict[str, EvidenceFact] = {}
    clash = False
    for fact in facts:
        seen = by_id.setdefault(fact.evidence_id, fact)
        clash = clash or seen != fact
    return list(by_id.values()), clash


def satisfy(obligation: Obligation, facts: Sequence[EvidenceFact]) -> Satisfaction:
    """Check ``obligation``'s verification condition against ``facts`` deterministically.

    Only GREEN facts can satisfy. A GREEN same-reference payment with another
    amount is a CONFLICT (RED), and so is a disputed condition whatever the
    proof, or one evidence id given with two different readings. The same
    evidence listed twice counts once. Proof older than ``since`` counts only when it names this
    obligation. Weaker signals leave it AMBER and open. The input obligation
    is never mutated; earlier proof is never removed (§3).
    """
    try:
        cond = VerificationCondition.parse(obligation.verification_condition)
    except (ValueError, ArithmeticError):
        return _unsatisfied(obligation, Quality.AMBER, ["I can't check this one automatically."])
    if cond.disputed:
        return _unsatisfied(
            obligation, Quality.RED, ["The letter gives different amounts. Please confirm what is due."]
        )
    unique, clash = _unique_facts(facts)
    if clash:
        return _unsatisfied(obligation, Quality.RED, ["The same proof was read two different ways."])

    ordered = sorted(unique, key=lambda f: (f.on, f.evidence_id))
    matches: list[EvidenceFact] = []
    conflicts: list[str] = []
    doubts: list[str] = []
    for fact in ordered:
        verdict, why = _check_fact(cond, fact)
        green = fact.quality is Quality.GREEN
        if verdict is _Verdict.MATCH and green:
            matches.append(fact)
        elif verdict is _Verdict.MATCH:
            doubts.append("The proof isn't verified yet.")
        elif verdict is _Verdict.CONFLICT and green:
            conflicts.append(why or "The proof doesn't agree with what was due.")
        elif verdict in (_Verdict.CONFLICT, _Verdict.UNSURE) or (why and verdict is _Verdict.NO_MATCH):
            doubts.append(why or "The proof isn't verified yet.")

    if matches:
        return _satisfied(obligation, cond, matches[:1])
    parts = _installments(cond, ordered)
    if parts:
        return _satisfied(obligation, cond, parts)
    if conflicts:
        return _unsatisfied(obligation, Quality.RED, conflicts)
    return _unsatisfied(obligation, Quality.AMBER, doubts)


# =========================================================================== due soon (§35)


@dataclass(frozen=True, slots=True)
class DueItem:
    obligation_id: str
    entity_id: str
    title: str
    due_on: date
    days_left: int  # negative when late
    amount: Decimal | None
    responsible: str
    currency: str = "EUR"  # obligations carry no currency; the company's own is assumed

    @property
    def overdue(self) -> bool:
        return self.days_left < 0

    @property
    def when(self) -> str:
        """'due today' / 'due tomorrow' / 'due in 3 days' / '2 days late'."""
        if self.days_left == 0:
            return "due today"
        if self.days_left == 1:
            return "due tomorrow"
        if self.days_left > 1:
            return f"due in {self.days_left} days"
        return f"{count_phrase(-self.days_left, 'day')} late"

    @property
    def line(self) -> str:
        """Owner-facing, e.g. 'Tax payment €405.00 · due in 3 days'."""
        if self.amount is None:
            head = self.title
        else:
            head = f"{self.title} {format_money(self.amount, self.currency)}"
        return f"{head}{MIDDLE_DOT}{self.when}"


def due_soon(
    obligations: Iterable[Obligation],
    today: date,
    *,
    within_days: int = DEFAULT_DUE_SOON_DAYS,
    entity_id: str | None = None,
    currency: str = "EUR",
) -> list[DueItem]:
    """Unsatisfied obligations due within ``within_days`` (late ones included), soonest first."""
    if within_days < 0:
        raise ValueError("within_days cannot be negative")
    horizon = today + timedelta(days=within_days)
    items = [
        DueItem(
            obligation_id=o.id,
            entity_id=o.entity_id,
            title=o.title,
            due_on=o.due_on,
            days_left=(o.due_on - today).days,
            amount=o.amount,
            responsible=o.responsible,
            currency=currency,
        )
        for o in obligations
        if not o.satisfied_by_evidence_ids
        and o.due_on <= horizon
        and (entity_id is None or o.entity_id == entity_id)
    ]
    return sorted(items, key=lambda d: (d.due_on, d.title, d.obligation_id))
