"""Which accountant questions the back office may answer by itself, and what it may say (§25, §28, §39).

A routine answer goes out only for a question the back office truly
understands, about one payment, where **every** claim in the question is
supported by the evidence:

* the amount, the date (day and month, month or year) and the payee match
  the payment and its document;
* a company named is the payment's own company;
* any other word (what the payment is for: "the studio rent") appears in
  the document's own description lines, directly or through a small
  Portuguese/English glossary ("rent" = "renda").

Three kinds of question are understood:

* a yes/no question ("Is the €1,200 transfer to Marta Gonçalves the studio
  rent?"): "Yes." plus the facts, only when every claim is supported;
* which document backs a payment ("Which invoice is behind the €64.10
  payment?", "Do we have the receipt for the €18.75 Uber payment?");
* who was paid ("Who was the €950 payment to?").

Anything else (a decision such as "Which company should it go to?", a tax
or booking judgment, a word the documents do not show, a name that is not
the payee) gets no answer: it stays open for the owner. Nothing is ever
confirmed because an amount happens to match (§3: closure requires evidence).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from backoffice.learning import day_month, fold, format_money

__all__ = ["AMOUNT", "PaymentFacts", "Reply", "amounts_in", "answer_question", "description_lines"]

AMOUNT = re.compile(r"(?:€\s?(\d[\d.,]*\d|\d)|(\d[\d.,]*\d|\d)\s?(?:€|eur(?:os?)?\b))", re.I)
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2}|\d{4}))?\b")
_WORD = re.compile(r"[a-z0-9]+")

_MONTHS = {name: i for i, name in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], start=1)}
_MONTHS.update({name[:3]: i for name, i in list(_MONTHS.items())})
_MONTHS.update({"sept": 9})
_MONTHS.update({name: i for i, name in enumerate(
    ["janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
     "novembro", "dezembro"], start=1)})
_ORDINAL = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?$")

# Words that carry no claim: question words, articles, and the plain ways to name a payment.
_FILLER = frozenset("""
a an the this that these those it its to for on of in at from by with and or as
is was are were be been does did do has have had there any please can could would you your we our us me my i
tell confirm check kindly just
paid pay payment payments transfer transfers transferred debit debits direct charge charged card
amount made sent went received
""".split())
_CONFIRM = frozenset({"is", "was", "are", "were", "does", "did", "do", "has", "have", "can", "could"})
_DOCUMENT_NOUNS = frozenset({"invoice", "invoices", "receipt", "receipts", "document", "documents", "proof", "bill",
                             "evidence", "fatura", "faturas", "recibo", "recibos"})
_DOCUMENT_VERBS = frozenset({"prove", "proves", "support", "supports", "back", "backs", "behind", "show", "shows",
                             "match", "matches", "matched", "cover", "covers", "which", "what", "where"})
# Judgments that are the accountant's or the owner's call: never confirmed from a document.
_NEVER = frozenset({"vat", "iva", "tax", "taxes", "deductible", "deduct", "withholding", "exempt", "category",
                    "account", "book", "booked", "booking", "expense", "cost", "personal", "private", "business",
                    "should", "correct", "right", "ok", "okay", "fine", "allowed", "legal"})
# English words an accountant uses, and the Portuguese a document prints for them.
_GLOSSARY: dict[str, tuple[str, ...]] = {
    "rent": ("renda", "arrendamento", "aluguer"),
    "rental": ("renda", "arrendamento", "aluguer"),
    "lease": ("renda", "arrendamento", "aluguer", "locacao"),
    "studio": ("estudio",),
    "office": ("escritorio",),
    "shop": ("loja",),
    "warehouse": ("armazem",),
    "flat": ("apartamento",),
    "apartment": ("apartamento",),
    "electricity": ("eletricidade", "electricidade", "energia"),
    "energy": ("energia", "eletricidade", "electricidade"),
    "gas": ("gas",),
    "water": ("agua",),
    "phone": ("telefone", "telemovel", "movel", "comunicacoes"),
    "telephone": ("telefone", "telemovel", "comunicacoes"),
    "mobile": ("telemovel", "movel"),
    "internet": ("internet", "fibra"),
    "subscription": ("subscricao", "assinatura"),
    "trip": ("viagem",),
    "ride": ("viagem",),
    "taxi": ("viagem", "taxi"),
    "travel": ("viagem", "viagens"),
    "furniture": ("moveis", "mobiliario", "movel"),
    "insurance": ("seguro", "seguros"),
    "cleaning": ("limpeza",),
    "maintenance": ("manutencao",),
    "repair": ("reparacao",),
}
# Document lines that are fields (tax numbers, totals, dates), not a description of what was bought.
_FIELD_LINE = re.compile(
    r"^\s*(?:nif|nipc|atcud|data|cliente|fatura|factura|recibo|nota de credito|nota de debito|isento|base|iva|"
    r"total|subtotal|codigo|n\.?\s*o|contribuinte|vencimento|referencia|iban|entidade|montante|valor|invoice|"
    r"date|vat|tax|customer|due)\b")


@dataclass(frozen=True)
class PaymentFacts:
    """What the evidence says about the one payment a question is about."""

    amount: Decimal  # absolute
    currency: str
    booked_on: date
    kind: str  # "transfer", "direct debit", "card payment", "payment"
    payee: str  # the payee's display name
    payee_names: tuple[str, ...]  # every name the payee goes by (supplier, aliases, bank descriptor, document)
    company_names: tuple[str, ...]  # the payment's own company (short and legal name)
    other_company_names: tuple[str, ...]  # the owner's other companies
    document: str  # "invoice-receipt FR M2026/9"
    document_word: str  # "invoice-receipt"
    document_date: date | None
    description: tuple[str, ...]  # the document's own description lines (see description_lines)


@dataclass(frozen=True)
class Reply:
    text: str
    claims: tuple[str, ...] = ()  # the question's words the document's description supported


def amounts_in(text: str) -> list[Decimal]:
    """Every money amount written in a question ("€1,200", "418.00 EUR", "€64,10")."""
    from backoffice.countries.pt import parse_pt_amount

    out: list[Decimal] = []
    for m in AMOUNT.finditer(text):
        raw = m.group(1) or m.group(2)
        value: Decimal | None
        if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d{2})?", raw):  # "1,200" / "1,200.00" (English grouping)
            value = Decimal(raw.replace(",", ""))
        elif re.fullmatch(r"\d+", raw):
            value = Decimal(raw)
        else:
            value = parse_pt_amount(raw)
            if value is None:
                try:
                    value = Decimal(raw)
                except InvalidOperation:
                    value = None
        if value is not None:
            out.append(value)
    return out


def description_lines(text: str) -> tuple[str, ...]:
    """A document's own words about what was bought.

    E-invoices (UBL): the item names, descriptions and notes. Text layers: the
    lines after the issuer's header block, minus field lines (tax numbers,
    dates, totals, VAT) and QR payloads. A document with no field lines gives
    nothing: nothing can then be confirmed from it.
    """
    text = text or ""
    if re.search(r"<(?:\w+:)?(?:Invoice|CreditNote)\b", text):
        found = re.findall(r"<(?:cbc:)?(?:Description|Note)>([^<]+)<", text)
        found += re.findall(r"<cac:Item>.*?<cbc:Name>([^<]+)</cbc:Name>", text, re.S)
        return tuple(dict.fromkeys(" ".join(f.split()) for f in found if f.strip()))
    lines: list[str] = []
    header = True
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        if _FIELD_LINE.match(fold(line)) or re.search(r"\b[A-Z]\d?:[\w.]", line):
            header = False  # the header block (who issued it, their address) ends at the first field
            continue
        if not header and re.search(r"[A-Za-zÀ-ÿ]{3}", line):
            lines.append(line)
    return tuple(dict.fromkeys(lines))


def _tokens(names: tuple[str, ...]) -> set[str]:
    return {w for name in names for w in _WORD.findall(fold(name))}


def _phrase(name: str) -> str:
    return " ".join(_WORD.findall(fold(name)))


def _supported(word: str, description: tuple[str, ...]) -> str | None:
    """The description line that shows ``word`` (itself, or a glossary equivalent), if any."""
    stems = (word, *_GLOSSARY.get(word, ()))
    for line in description:
        words = _WORD.findall(fold(line))
        for stem in stems:
            if any(w == stem or (len(stem) >= 4 and w.startswith(stem)) for w in words):
                return line
    return None


def answer_question(question: str, facts: PaymentFacts, today: date) -> Reply | None:
    """The answer to ``question`` from ``facts``, or None when it must stay open for the owner."""
    amounts = amounts_in(question)
    if not amounts or any(a != facts.amount for a in amounts):
        return None
    text = AMOUNT.sub(" ", question)
    dates: list[tuple[int, int, int | None]] = []
    for m in _NUMERIC_DATE.finditer(text):
        year = int(m.group(3)) if m.group(3) else None
        dates.append((int(m.group(1)), int(m.group(2)), 2000 + year if year is not None and year < 100 else year))
    text = _NUMERIC_DATE.sub(" ", text)
    folded = " ".join(_WORD.findall(fold(text)))
    # Companies are named by their whole name ("Company C"), not by one of its words ("studio").
    for name in facts.other_company_names:
        phrase = _phrase(name)
        if phrase and re.search(rf"\b{re.escape(phrase)}\b", folded):
            return None  # another of the owner's companies: not what the evidence says
    for name in sorted(facts.company_names, key=len, reverse=True):
        phrase = _phrase(name)
        if phrase:
            folded = re.sub(rf"\b{re.escape(phrase)}\b", " ", folded)
    words = folded.split()
    if not words:
        return None

    doc_noun = any(w in _DOCUMENT_NOUNS for w in words)
    if words[0] == "who":
        kind = "who"
    elif words[0] in ("which", "what") or (doc_noun and any(w in _DOCUMENT_VERBS for w in words)):
        kind = "document" if doc_noun else ""
    elif words[0] in _CONFIRM:
        kind = "document" if doc_noun else "confirm"
    else:
        kind = ""
    if not kind:
        return None

    payee = _tokens(facts.payee_names)
    months: list[int] = []
    days: list[int] = []
    years: list[int] = []
    claims: list[str] = []
    quoted: list[str] = []
    for w in words:
        if w in _NEVER:
            return None
        if w in _FILLER or w in ("who", "which", "what", "where") or w in _DOCUMENT_NOUNS or w in _DOCUMENT_VERBS:
            continue
        if w in _MONTHS:
            months.append(_MONTHS[w])
            continue
        if re.fullmatch(r"(19|20)\d\d", w):
            years.append(int(w))
            continue
        if (ordinal := _ORDINAL.match(w)) is not None:
            days.append(int(ordinal.group(1)))
            continue
        if w in payee:
            continue
        line = _supported(w, facts.description)
        if line is None:
            return None  # a claim the documents do not show
        claims.append(w)
        quoted.append(line)

    known = [facts.booked_on, *([facts.document_date] if facts.document_date else [])]
    for day, month, year in dates:
        if not any(d.day == day and d.month == month and (year is None or d.year == year) for d in known):
            return None
    if days and not months:
        return None  # "on the 1st" of which month? Not a claim I can check.
    for month in months:
        if not any(d.month == month for d in known):
            return None
    for day in days:
        if not any(d.day == day and d.month in months for d in known):
            return None
    for year in years:
        if not any(d.year == year for d in known):
            return None

    amount = format_money(facts.amount, facts.currency)
    when = day_month(facts.booked_on, today)
    document = facts.document
    if facts.document_date is not None:
        document += f" of {day_month(facts.document_date, today)}"
    if kind == "who":
        body = f"The {amount} {facts.kind} on {when} went to {facts.payee}, as {document} shows."
    else:
        body = f"The {amount} {facts.kind} on {when} went to {facts.payee}, and {document} matches it."
    if quoted:
        body += f" The {facts.document_word} says: “{quoted[0]}”."
    if kind == "confirm" or (kind == "document" and words[0] in _CONFIRM):
        body = f"Yes. {body}"
    return Reply(text=body, claims=tuple(dict.fromkeys(claims)))
