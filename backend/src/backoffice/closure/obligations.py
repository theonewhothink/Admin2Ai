"""Administrative obligations (§24): find them in letters, prove them done, list what's due.

Detection reads government, bank, landlord and insurer text in Portuguese and
English ("prazo", "data limite", "pagamento até", "Autoridade Tributária",
"Segurança Social", "renovação", "KYC", "due by", ...) and produces a domain
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

Everything here is wording, not law. The phrase tables are common letter
conventions (unverified against a corpus, verified_as_of: never); the
consequences are cautious "may" statements unless the letter itself names
one. Relative deadlines ("no prazo de 15 dias") are counted in calendar days
from the day the letter arrived, which is never later than a business-day
count from the same day; the exact legal counting rule is NOT encoded and the
finding says the date needs checking.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right, insort
from collections.abc import Callable, Iterable, Sequence
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
    "Issuer",
    "ObligationFinding",
    "ProofKind",
    "Satisfaction",
    "VerificationCondition",
    "detect_confirmation",
    "detect_obligation",
    "due_soon",
    "normalize_reference",
    "proof_for",
    "satisfy",
]

DEFAULT_DUE_SOON_DAYS = 14  # product default for Home "Due soon" (§35), not a legal figure
# The consequence of a renewal that happens by itself: nothing is needed unless the owner wants a change.
AUTO_RENEWS = "It renews on its own unless you act."


# =========================================================================== vocabulary


class Issuer(str, Enum):
    TAX_AUTHORITY = "tax_authority"
    SOCIAL_SECURITY = "social_security"
    BANK = "bank"
    LANDLORD = "landlord"
    INSURER = "insurer"
    OTHER = "other"


class ProofKind(str, Enum):
    """What proves an obligation done. DECISION only ever closes a renewal."""

    PAYMENT = "payment"
    SUBMISSION = "submission"  # filing receipt
    REPLY = "reply"  # answer or documents sent to whoever asked
    RENEWAL = "renewal"  # the renewed policy, contract or licence
    DECISION = "decision"  # owner decided not to renew


# Folded phrases (lowercase, no accents). Unverified conventions; extend as letters are seen.
_ISSUER_PHRASES: dict[Issuer, tuple[str, ...]] = {
    Issuer.TAX_AUTHORITY: (
        "autoridade tributaria", "autoridade tributaria e aduaneira", "portal das financas",
        "servico de financas", "servicos de financas", "direcao de financas", "hmrc",
        "hm revenue", "agencia tributaria", "tax authority", "tax office",
    ),
    Issuer.SOCIAL_SECURITY: (
        "seguranca social", "instituto da seguranca social", "igfss", "social security",
    ),
    Issuer.BANK: ("o seu banco", "your bank", "o banco", "the bank"),
    Issuer.LANDLORD: ("senhorio", "landlord", "arrendamento", "contrato de arrendamento", "lease"),
    Issuer.INSURER: ("seguradora", "companhia de seguros", "insurer", "insurance company", "apolice"),
}  # fmt: skip
_SOURCE_ISSUER = {SourceKind.BANK: Issuer.BANK, SourceKind.CARD: Issuer.BANK}

_KYC = ("kyc", "know your customer", "conheca o seu cliente", "atualizacao de dados",
        "actualizacao de dados", "atualizar os seus dados", "atualize os seus dados",
        "actualizar os seus dados", "verify your identity", "identity verification",
        "comprovativo de morada", "proof of address", "beneficiario efetivo",
        "beneficiario efectivo", "beneficial owner", "update your details")  # fmt: skip
_PAYMENT = ("pagamento", "pagar", "a pagar", "valor a pagar", "total a pagar", "liquidar",
            "liquidacao", "contribuicoes", "contribuicao", "prestacao", "payment", "pay",
            "amount due", "balance due", "total due")  # fmt: skip
_FILING = ("declaracao", "declaracao periodica", "entrega da declaracao", "declaracao anual",
           "modelo 22", "ies", "tax return", "vat return", "filing", "file your return",
           "self assessment")  # fmt: skip
_REQUEST = ("notificacao", "notificado", "notificada", "pedido de", "esclarecimento",
            "esclarecimentos", "solicitamos", "apresentar", "documentos", "request", "requested",
            "please provide", "please send", "information request", "audiencia previa")  # fmt: skip
_DEBT = ("cobranca de divida", "cobranca de dividas", "recuperacao de credito",
         "recuperacao de creditos", "injuncao", "divida", "divida em atraso", "debt collection",
         "debt recovery", "collection agency", "outstanding debt", "ultimo aviso", "aviso final",
         "final notice", "final demand", "penhora")  # fmt: skip
_RENEWAL = ("renovacao", "renovar", "renova", "renove", "renewal", "renew", "renews", "expira",
            "expiracao", "caducidade", "caduca", "expires", "expiry", "expiration")  # fmt: skip
_INSURANCE = ("seguro", "apolice", "seguradora", "insurance", "insurer", "policy")
_LICENSE = ("licenca", "alvara", "licence", "license", "permit", "certificacao", "certificado")
_RENT = ("renda", "rendas", "rent", "senhorio", "landlord", "arrendamento")
_BANK_REQUEST = ("documentacao", "documentos em falta", "missing documents", "please provide",
                 "pedido de documentos", "pedido de informacao", "request")  # fmt: skip
_PAYMENT_DEADLINE = ("pagamento ate", "data limite de pagamento", "data limite para pagamento",
                     "data de vencimento", "vencimento:", "vence em", "vence a", "due by",
                     "payment due", "amount due", "due date", "pay by", "valor a pagar",
                     "total a pagar")  # fmt: skip
_AUTO_RENEW = ("renova automaticamente", "renovacao automatica", "renovado automaticamente",
               "renews automatically", "automatic renewal", "auto-renew", "auto renew",
               "tacitamente")  # fmt: skip

# "vencimento" alone also means "salary" in Portuguese, so only its unambiguous forms anchor.
_STRONG_DATE_ANCHORS = ("prazo", "prazo limite", "data limite", "data-limite", "pagamento ate",
                        "ate ao dia", "ate dia", "data de vencimento", "vencimento:", "vence em",
                        "vence a", "due by",
                        "due on", "due date", "deadline", "no later than", "pay by",
                        "renewal date", "data de renovacao", "expires on", "expira em", "expira a",
                        "valid until", "valido ate", "valida ate", "termina em")  # fmt: skip
_WEAK_DATE_ANCHORS = ("ate", "by", "before", "until", "antes de")
_STRONG_AMOUNT_ANCHORS = ("total a pagar", "valor a pagar", "montante a pagar",
                          "importancia a pagar", "quantia a pagar", "valor em divida",
                          "amount due", "total due", "balance due", "amount payable",
                          "total amount due", "outstanding balance")  # fmt: skip
_WEAK_AMOUNT_ANCHORS = ("montante", "valor", "importancia", "quantia", "total", "amount",
                        "renda", "rent", "premio", "premium")  # fmt: skip

_CONSEQUENCE_PHRASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("a fine", ("coima", "coimas", "multa", "multas", "fine", "fines", "penalty", "penalties",
                "penalidade", "penalizacao")),
    ("interest", ("juros", "juros de mora", "interest", "late interest")),
    ("a late fee", ("late fee", "late payment fee", "taxa de atraso")),
    ("suspension", ("suspensao", "suspender", "suspension", "suspend", "suspended", "bloqueio",
                    "bloquear", "blocked")),
    ("cancellation", ("cancelamento", "cancelar", "cancel", "cancellation", "termination",
                      "terminate", "resolucao do contrato")),
    ("legal action", ("execucao fiscal", "penhora", "tribunal", "court", "legal action",
                      "acao judicial", "injuncao")),
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
}

_PROOF_FOR_KIND: dict[ObligationKind, ProofKind] = {
    ObligationKind.TAX_DEADLINE: ProofKind.PAYMENT,
    ObligationKind.RENT: ProofKind.PAYMENT,
    ObligationKind.DEBT_COLLECTION: ProofKind.PAYMENT,
    ObligationKind.PAYMENT_DEADLINE: ProofKind.PAYMENT,
    ObligationKind.FILING: ProofKind.SUBMISSION,
    ObligationKind.GOVERNMENT_REQUEST: ProofKind.REPLY,
    ObligationKind.KYC_REQUEST: ProofKind.REPLY,
    ObligationKind.BANK_REQUEST: ProofKind.REPLY,
    ObligationKind.INSURANCE_RENEWAL: ProofKind.RENEWAL,
    ObligationKind.CONTRACT_RENEWAL: ProofKind.RENEWAL,
    ObligationKind.LICENSE_RENEWAL: ProofKind.RENEWAL,
}

RENEWAL_KINDS: frozenset[ObligationKind] = frozenset(
    {ObligationKind.INSURANCE_RENEWAL, ObligationKind.CONTRACT_RENEWAL, ObligationKind.LICENSE_RENEWAL}
)


def proof_for(kind: ObligationKind) -> ProofKind:
    """What proves an obligation of this kind done: a payment, a filing receipt, a reply or a renewal."""
    return _PROOF_FOR_KIND[ObligationKind(kind)]


# Default routing (§25, §28): the accountant usually files returns; the owner does the rest.
_RESPONSIBLE: dict[ObligationKind, str] = {ObligationKind.FILING: "accountant"}

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


_MONTH_TOKENS: dict[str, tuple[int, bool]] = {}
for _i, (_pt, _en) in enumerate(
    zip(
        ("janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto",
         "setembro", "outubro", "novembro", "dezembro"),
        ("january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"),
    ),
    start=1,
):  # fmt: skip
    _MONTH_TOKENS[_pt] = (_i, False)
    _MONTH_TOKENS[_en] = (_i, False)
for _abbr, _i in {
    "jan": 1, "fev": 2, "feb": 2, "mar": 3, "abr": 4, "apr": 4, "mai": 5, "jun": 6, "jul": 7,
    "ago": 8, "aug": 8, "set": 9, "sep": 9, "sept": 9, "out": 10, "oct": 10, "nov": 11,
    "dez": 12, "dec": 12,
}.items():  # fmt: skip
    _MONTH_TOKENS.setdefault(_abbr, (_i, True))

_MONTH_ALT = "|".join(sorted(_MONTH_TOKENS, key=len, reverse=True))
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_NUM_DATE = re.compile(r"(?<![\d/.-])(\d{1,2})([/.-])(\d{1,2})\2(\d{4}|\d{2})(?![\d/.-]?\d)")
_TEXT_DMY = re.compile(
    rf"(?<![0-9a-z])(\d{{1,2}})(?:st|nd|rd|th|o)?\s+(?:de\s+)?({_MONTH_ALT})\.?"
    rf"(?:,?\s+(?:de\s+)?(\d{{4}}))?(?![0-9a-z])"
)
_TEXT_MDY = re.compile(
    rf"(?<![0-9a-z])({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?(?![0-9a-z])"
)
_RELATIVE = re.compile(
    r"(?:prazo\s+de|no\s+prazo\s+de|within|in\s+the\s+next)\s+(\d{1,3})\s+"
    r"(dias\s+uteis|dias|business\s+days|working\s+days|days)(?![a-z])"
)
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
    for m in _TEXT_DMY.finditer(folded):
        month, abbrev = _MONTH_TOKENS[m.group(2)]
        if abbrev and not m.group(3):
            continue  # "3 out of 5" is not a date
        day = int(m.group(1))
        year = m.group(3)
        add(m, _safe_date(int(year), month, day) if year else _infer_year(month, day, received_on))
    for m in _TEXT_MDY.finditer(folded):
        month, abbrev = _MONTH_TOKENS[m.group(1)]
        year = m.group(3)
        if (abbrev or m.group(1) in ("may", "march", "marco")) and not year:
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

_P = {
    name: phrase_pattern(phrases)
    for name, phrases in {
        "kyc": _KYC, "payment": _PAYMENT, "filing": _FILING, "request": _REQUEST,
        "debt": _DEBT, "renewal": _RENEWAL, "insurance": _INSURANCE, "license": _LICENSE,
        "rent": _RENT, "bank_request": _BANK_REQUEST, "payment_deadline": _PAYMENT_DEADLINE,
        "auto_renew": _AUTO_RENEW, "strong_date": _STRONG_DATE_ANCHORS,
        "weak_date": _WEAK_DATE_ANCHORS, "strong_amount": _STRONG_AMOUNT_ANCHORS,
        "weak_amount": _WEAK_AMOUNT_ANCHORS,
    }.items()
}  # fmt: skip
_ISSUER_PATTERNS = {issuer: phrase_pattern(p) for issuer, p in _ISSUER_PHRASES.items()}
_CONSEQUENCE_PATTERNS = [(label, phrase_pattern(p)) for label, p in _CONSEQUENCE_PHRASES]


def _has(name: str, folded: str) -> bool:
    return _P[name].search(folded) is not None


def _issuer(folded: str, source_kind: SourceKind | None) -> Issuer:
    for issuer in (Issuer.TAX_AUTHORITY, Issuer.SOCIAL_SECURITY):
        if _ISSUER_PATTERNS[issuer].search(folded):
            return issuer
    if source_kind in _SOURCE_ISSUER:
        return _SOURCE_ISSUER[source_kind]
    for issuer in (Issuer.BANK, Issuer.INSURER, Issuer.LANDLORD):
        if _ISSUER_PATTERNS[issuer].search(folded):
            return issuer
    return Issuer.OTHER


def _government_kind(folded: str, has_amount: bool) -> ObligationKind:
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
    }[kind]


def _consequence(folded: str, kind: ObligationKind) -> str:
    if _has("auto_renew", folded):
        return AUTO_RENEWS
    named = [label for label, pattern in _CONSEQUENCE_PATTERNS if pattern.search(folded)]
    if named:
        return f"The letter mentions {join_and(named)}."
    return _GENERIC_CONSEQUENCE[kind]


# =========================================================================== detection: reference and company

# A digit group ends where the sentence does: "123 456 789." is one reference, while
# "123 456 1.234,56" stops before the amount.
_REFERENCE = re.compile(
    r"(?<![a-z])(?:referencia(?:\s+(?:de|para)\s+pagamento|\s+multibanco|\s+mb)?|payment\s+reference"
    r"|reference|ref)\.?\s*(?:n\.?\s?o\.?|number|no\.?|#)?\s*[:.]?\s*"
    r"(\d+(?: \d+){0,5}(?!\d|[.,]\d)|[a-z0-9-]*\d[a-z0-9-]*)"
)
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
    for m in _REFERENCE.finditer(folded):
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
    chosen, tier = _tiered(folded, hits, _P["strong_date"], _P["weak_date"], window=40)
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
    strong = _anchored(folded, _P["strong_amount"], _with_bare(marked, _bare_amount_hits(folded)), 30)
    chosen = strong or _tiered(folded, marked, _P["strong_amount"], _P["weak_amount"], window=30)[0]
    values = sorted({h.value for h in chosen})  # type: ignore[type-var]
    if not values:
        return _Pick(None, False, None)
    if len(values) > 1:
        shown = join_and([format_money(v, c) for v, c in values])  # type: ignore[misc]
        return _Pick(values[0], True, f"The letter gives different amounts: {shown}.")
    return _Pick(values[0], False, None)


def _relative_deadline(folded: str, received_on: date) -> tuple[date, str] | None:
    m = _RELATIVE.search(folded)
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
    """
    body = fold(text)
    everything = fold(f"{sender} {text}")
    amount_pick = _pick_amount(body)
    issuer = _issuer(everything, source_kind)
    kind = _classify(everything, issuer, amount_pick.value is not None)
    deadline = _deadline(body, received_on) if kind is not None else None
    if kind is None or deadline is None:
        return None

    amount, currency, amount_reasons = _amount_due(amount_pick)
    reference = _reference(body)
    entity_id, entity_problem = _entity(everything, entities, default_entity_id)
    condition = _condition(
        kind, amount, currency, reference, deadline.due_on,
        since=received_on, disputed=amount_pick.conflict,
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
            *_why_lines(issuer, deadline.due_on, amount, currency, reference, received_on),
            *deadline.reasons,
            *amount_reasons,
        ),
        missing=tuple(missing),
        obligation=None,
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
}


def _why_lines(
    issuer: Issuer,
    due_on: date | None,
    amount: Decimal | None,
    currency: str,
    reference: str | None,
    today: date,
) -> list[str]:
    """§54 "Why?" lines for a detected obligation."""
    lines = [_ISSUER_LINE[issuer]] if issuer in _ISSUER_LINE else []
    if due_on is not None:
        lines.append(f"Due {day_month(due_on, today)}")
    if amount is not None:
        lines.append(f"Amount {format_money(amount, currency)}")
    if reference:
        lines.append(f"Reference {reference}")
    return lines


# =========================================================================== confirmations (proof that it was done)

# Folded phrases, Portuguese and English. Unverified conventions (verified_as_of: never); extend
# as letters are seen. A confirmation is never proof on its own: the caller checks it against the
# obligations on file (same company, kind, sender and reference) and closes one only when exactly
# one fits (§3, §24).
_STILL_ASKING = ("ainda precisamos", "ainda necessitamos", "continua em falta", "continuam em falta",
                 "documentos em falta", "documentacao em falta", "falta enviar", "faltam", "queira enviar",
                 "por favor envie", "solicitamos", "se nao pagar", "se nao efetuar", "se nao for paga",
                 "se nao for pago", "caso nao pague", "caso nao efetue", "still need", "still missing",
                 "still required", "missing documents", "please send", "please provide", "please upload",
                 "unless", "if you do not", "if payment is not")  # fmt: skip
_DECIDED = ("contrato cessado", "cessacao do contrato", "cessacao do seu contrato", "contrato terminado",
            "contrato rescindido", "rescisao do contrato", "confirmamos a denuncia", "confirmamos a cessacao",
            "confirmamos o cancelamento", "cancelamento confirmado", "apolice anulada", "apolice cancelada",
            "nao sera renovado", "nao sera renovada", "has been cancelled", "has been canceled",
            "has been terminated", "was terminated", "cancellation confirmed", "termination confirmed",
            "will not be renewed")  # fmt: skip
_RENEWED = ("foi renovado", "foi renovada", "foram renovados", "renovado ate", "renovada ate",
            "renovacao efetuada", "renovacao efectuada", "renovacao concluida", "renovacao confirmada",
            "confirmamos a renovacao", "renovado com sucesso", "renovada com sucesso", "has been renewed",
            "was renewed", "renewed until", "renewal confirmed", "renewal is confirmed", "we have renewed",
            "successfully renewed")  # fmt: skip
_SUBMITTED = ("comprovativo de entrega", "declaracao submetida", "declaracao entregue", "declaracao foi submetida",
              "declaracao foi entregue", "declaracao recebida", "entregue com sucesso", "submetida com sucesso",
              "has been submitted", "was submitted", "submitted successfully", "successfully submitted",
              "return received", "received your return", "successfully filed", "filing confirmation")  # fmt: skip
_ANSWERED = ("recebemos os seus documentos", "recebemos a sua documentacao", "recebemos a documentacao",
             "recebemos os documentos", "documentos recebidos", "documentacao recebida", "recebemos a sua resposta",
             "resposta recebida", "confirmamos a rececao", "confirmamos a recepcao", "pedido concluido",
             "processo concluido", "processo encerrado", "dados atualizados", "dados actualizados",
             "atualizacao concluida", "actualizacao concluida", "verificacao concluida",
             "we have received your documents", "we received your documents", "received your documents",
             "documents received", "received your reply", "received your response", "confirm receipt",
             "thank you for sending", "thank you for providing", "details have been updated",
             "your details are up to date", "verification complete", "verification completed",
             "identity has been verified", "request completed", "request has been completed",
             "no further action")  # fmt: skip
_CONTRACT = ("contrato", "contract", "subscricao", "subscription", "assinatura", "avenca", "service agreement")
_VALIDITY = ("ate", "until", "valido ate", "valida ate", "valid until", "valid to", "validade", "nova validade",
             "nova data de validade", "new expiry date", "expires on", "expira em", "expira a", "termina em",
             "ends on", "renovado ate", "renovada ate", "renewed until")  # fmt: skip
_C = {name: phrase_pattern(phrases) for name, phrases in {
    "still_asking": _STILL_ASKING, "decided": _DECIDED, "renewed": _RENEWED, "submitted": _SUBMITTED,
    "answered": _ANSWERED, "contract": _CONTRACT, "validity": _VALIDITY,
}.items()}  # fmt: skip
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
    anchored = _anchored(body, _C["validity"], hits, 40)
    pool = anchored or hits
    values = sorted({h.value for h in pool if h.value > received_on})  # type: ignore[operator]
    return values[0] if len(values) == 1 else None  # type: ignore[return-value]


def _confirmed_kinds(proof: ProofKind, body: str, issuer: Issuer) -> frozenset[ObligationKind]:
    if proof in (ProofKind.RENEWAL, ProofKind.DECISION):
        if _has("license", body):
            return frozenset({ObligationKind.LICENSE_RENEWAL})
        if issuer is Issuer.INSURER or _has("insurance", body):
            return frozenset({ObligationKind.INSURANCE_RENEWAL})
        if _C["contract"].search(body):
            return frozenset({ObligationKind.CONTRACT_RENEWAL})
        return RENEWAL_KINDS
    if proof is ProofKind.SUBMISSION:
        return frozenset({ObligationKind.FILING})
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
) -> ConfirmationFinding | None:
    """Read one letter or message that says an obligation was done, or None (§24).

    "Recebemos os seus documentos", "A sua licença foi renovada até 30/11/2027", "Comprovativo de
    entrega", "Your contract has been terminated". A message that is still asking for something
    ("we still need", "please send", "unless") is never a confirmation. Like detection, this reads
    wording only: the caller decides whether it matches exactly one obligation on file.
    """
    body = fold(text)
    everything = fold(f"{sender} {text}")
    if not body or _C["still_asking"].search(body):
        return None
    proof = next((p for name, p in (("decided", ProofKind.DECISION), ("renewed", ProofKind.RENEWAL),
                                    ("submitted", ProofKind.SUBMISSION), ("answered", ProofKind.REPLY))
                  if _C[name].search(body)), None)
    if proof is None:
        return None
    issuer = _issuer(everything, source_kind)
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
