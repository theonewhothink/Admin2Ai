"""Supplier chasing (§22, §45): polite requests, reminders and reply matching.

Messages are composed from facts only — invoice number when known, amount,
payment date, and our company name and tax number — in Portuguese or English:

    "Hello, could you please resend invoice FT 2026/183 relating to the
    €117.20 payment dated 18 September? Thank you."

Internal identifiers never appear in what a supplier reads; a guard refuses
to compose a message containing one. Header fields are single clean lines:
the supplier address must be one plain address and an invoice number read
from a document is cleaned (:func:`clean_invoice_number`), so text from a
document or a profile can never add headers such as ``Bcc:``. Each chase thread carries a short
reference ("Ref. 7KQ2MX", derived one-way from the case, so it reveals
nothing) in the subject, and our own Message-IDs are remembered, so a reply
is matched by In-Reply-To, then References, then the subject reference.

Reminders follow :class:`ReminderPolicy` (default: first after 6 days, then
after 4 more, at most 2, never on a weekend), then the case goes back to the
owner.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backoffice.domain.models import LegalEntity, Supplier, Transaction
from backoffice.fraud.domains import email_domain, registrable_domain
from backoffice.learning.keys import display_name, normalize_tax_id
from backoffice.learning.plain import day_month, format_money, quantize_money

__all__ = [
    "PT_MONTHS",
    "ChaseFacts",
    "ChaseMessage",
    "ChaseThread",
    "InboundEmail",
    "Language",
    "MatchMethod",
    "RecurringChaseFacts",
    "ReminderDecision",
    "ReminderPolicy",
    "ReminderStep",
    "ReplyMatch",
    "SentMessage",
    "activity_line",
    "choose_language",
    "clean_invoice_number",
    "compose_correction_request",
    "compose_recurring_request",
    "compose_reminder",
    "compose_request",
    "day_month_pt",
    "format_money_pt",
    "match_reply",
    "next_reminder",
    "recurring_activity_line",
    "thread_token",
    "valid_mail_domain",
]


class Language(str, Enum):
    EN = "en"
    PT = "pt"


PT_MONTHS = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)  # fmt: skip
_PT_SYMBOLS = {"EUR": "€", "GBP": "£", "USD": "$"}
_TOKEN_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U
_TOKEN_LENGTH = 6
_TOKEN_IN_SUBJECT = re.compile(r"\bref\b\.?\s*[:#]?\s*([0-9A-Z]{6})\b", re.IGNORECASE)
# One plain ASCII address: no display name, no list, no whitespace (header safety).
_PLAIN_ADDRESS = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
_HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+")
MAX_INVOICE_NUMBER_LENGTH = 60
_INTERNAL_ID = re.compile(
    r"\b[a-z]{2,8}_[0-9a-f]{16}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- header safety


def clean_invoice_number(raw: str | None) -> str | None:
    """One printable line: whitespace runs (line breaks too) become one space,
    control and invisible characters are dropped. None when nothing plausible is left."""
    if raw is None:
        return None
    visible = "".join(ch for ch in raw if unicodedata.category(ch) not in ("Cc", "Cf") or ch.isspace())
    text = " ".join(visible.split())
    if not text or len(text) > MAX_INVOICE_NUMBER_LENGTH:
        return None
    return text


def valid_mail_domain(domain: str) -> str:
    """The domain our Message-IDs use; refuses anything that is not a plain host name."""
    if not _HOSTNAME.fullmatch(domain or ""):
        raise ValueError("message-id domain must be a plain host name")
    return domain


def _single_line(value: str) -> bool:
    """No control characters and no Unicode line/paragraph separators."""
    return not any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in value)


# --------------------------------------------------------------------------- formatting


def format_money_pt(amount: Decimal | int, currency: str = "EUR") -> str:
    """'1.234,56 €' (Portuguese separators, symbol after the amount)."""
    rounded = quantize_money(amount)
    whole, cents = f"{abs(rounded):,.2f}".split(".")
    digits = f"{whole.replace(',', '.')},{cents}"
    sign = "-" if rounded < 0 else ""
    code = currency.strip().upper()
    return f"{sign}{digits} {_PT_SYMBOLS.get(code, code)}"


def day_month_pt(value: date, today: date | None = None) -> str:
    """'18 de setembro', or '18 de setembro de 2025' outside ``today``'s year."""
    text = f"{value.day} de {PT_MONTHS[value.month - 1]}"
    if today is not None and value.year != today.year:
        text = f"{text} de {value.year}"
    return text


def thread_token(tenant_id: str, subject_id: str) -> str:
    """Short, stable, one-way reference for a chase thread ('7KQ2MX')."""
    digest = hashlib.sha256(f"{tenant_id}\x1f{subject_id}".encode()).digest()
    number = int.from_bytes(digest[:4], "big") >> (32 - 5 * _TOKEN_LENGTH)
    chars = []
    for _ in range(_TOKEN_LENGTH):
        chars.append(_TOKEN_ALPHABET[number & 31])
        number >>= 5
    return "".join(reversed(chars))


def choose_language(supplier: Supplier | None, email: str | None = None) -> Language:
    """Portuguese for suppliers in Portugal (profile country or a .pt mailbox), else English."""
    if supplier is not None and any(c.strip().upper() == "PT" for c in supplier.countries):
        return Language.PT
    domain = email_domain(email) if email else None
    return Language.PT if domain and domain.endswith(".pt") else Language.EN


# --------------------------------------------------------------------------- facts and messages


@dataclass(frozen=True)
class ChaseFacts:
    """Everything a chase message may say. Nothing else reaches the supplier."""

    supplier_name: str
    supplier_email: str
    amount: Decimal  # absolute
    currency: str
    paid_on: date
    company_name: str
    company_tax_id: str
    company_country: str = "PT"
    invoice_number: str | None = None
    language: Language = Language.EN
    # Money back from the supplier: what is asked for is its credit note, for the refund received.
    refund: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.amount, float) or not isinstance(self.amount, (Decimal, int)):
            raise TypeError("money must be Decimal, never float")
        if self.amount <= 0:
            raise ValueError("amount is absolute and positive: there is no invoice for nothing")
        if not _PLAIN_ADDRESS.fullmatch(self.supplier_email):
            raise ValueError("supplier email must be one plain address")
        if self.invoice_number is not None and clean_invoice_number(self.invoice_number) != self.invoice_number:
            raise ValueError("invoice number must be one clean line (see clean_invoice_number)")

    @classmethod
    def build(
        cls,
        transaction: Transaction,
        supplier: Supplier,
        company: LegalEntity,
        *,
        invoice_number: str | None = None,
        language: Language | None = None,
    ) -> ChaseFacts:
        if not supplier.contact_email:
            raise ValueError("the supplier has no contact email")
        return cls(
            supplier_name=display_name(supplier.name),
            supplier_email=supplier.contact_email,
            amount=abs(transaction.amount),
            currency=transaction.currency,
            paid_on=transaction.booked_on,
            company_name=company.name.strip(),
            company_tax_id=company.tax_id,
            company_country=company.country,
            invoice_number=clean_invoice_number(invoice_number),
            language=language or choose_language(supplier, supplier.contact_email),
            refund=transaction.amount > 0,
        )


class ChaseMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    to: str
    subject: str
    body: str
    language: Language
    token: str
    message_id: str  # set as the Message-ID header by the sending connector
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    reminder_number: int = 0  # 0 = the first request

    @model_validator(mode="after")
    def _headers_are_single_lines(self) -> ChaseMessage:
        headers = [self.to, self.subject, self.message_id, self.in_reply_to or "", *self.references]
        if not all(_single_line(h) for h in headers):
            raise ValueError("email headers must be single lines")
        return self


def _guard(*texts: str) -> None:
    for text in texts:
        if _INTERNAL_ID.search(text):
            raise ValueError("a supplier message may not contain internal identifiers")


def _our_details(facts: ChaseFacts | RecurringChaseFacts) -> str:
    is_pt = facts.company_country.strip().upper() == "PT"
    tax_id = (normalize_tax_id(facts.company_tax_id) if is_pt else None) or facts.company_tax_id.strip()
    label = "NIF" if is_pt else ("NIF/VAT" if facts.language is Language.PT else "VAT number")
    lead = "Os nossos dados" if facts.language is Language.PT else "Our details"
    return f"{lead}: {facts.company_name}, {label} {tax_id}."


def _message_id(token: str, number: int, today: date, domain: str) -> str:
    return f"<chase-{token.lower()}-{number}-{today:%Y%m%d}@{valid_mail_domain(domain)}>"


def _money_and_date(facts: ChaseFacts, today: date) -> tuple[str, str]:
    if facts.language is Language.PT:
        return format_money_pt(facts.amount, facts.currency), day_month_pt(facts.paid_on, today)
    return format_money(facts.amount, facts.currency), day_month(facts.paid_on, today)


def _refund_request_text(facts: ChaseFacts, today: date) -> tuple[str, str]:
    """Money came back without its credit note: ask for the credit note (§20 refunds, §22)."""
    money, when = _money_and_date(facts, today)
    if facts.language is Language.PT:
        subject = f"Nota de crédito do reembolso de {money} de {when}"
        body = (
            f"Olá,\n\nRecebemos um reembolso de {money} a {when}. Poderiam, por favor, enviar-nos a nota de "
            f"crédito correspondente? Agradecemos desde já.\n\n{_our_details(facts)}\n\n"
            f"Com os melhores cumprimentos,\n{facts.company_name}"
        )
        return subject, body
    subject = f"Credit note for the {money} refund of {when}"
    body = (
        f"Hello,\n\nWe received a refund of {money} on {when}. Could you please send us the credit note for it? "
        f"Thank you.\n\n{_our_details(facts)}\n\nKind regards,\n{facts.company_name}"
    )
    return subject, body


def _request_text(facts: ChaseFacts, today: date) -> tuple[str, str]:
    if facts.refund:
        return _refund_request_text(facts, today)
    money, when = _money_and_date(facts, today)
    number = facts.invoice_number
    if facts.language is Language.PT:
        subject = f"Fatura {number}" if number else f"Fatura do pagamento de {money} de {when}"
        ask = (
            f"Poderiam, por favor, reenviar a fatura {number} referente ao pagamento de {money} de {when}?"
            if number
            else f"Poderiam, por favor, enviar-nos a fatura referente ao pagamento de {money} de {when}?"
        )
        body = (
            f"Olá,\n\n{ask} Agradecemos desde já.\n\n{_our_details(facts)}\n\n"
            f"Com os melhores cumprimentos,\n{facts.company_name}"
        )
        return subject, body
    subject = f"Invoice {number}" if number else f"Invoice for the {money} payment of {when}"
    ask = (
        f"Could you please resend invoice {number} relating to the {money} payment dated {when}?"
        if number
        else f"Could you please send the invoice for the {money} payment dated {when}?"
    )
    body = f"Hello,\n\n{ask} Thank you.\n\n{_our_details(facts)}\n\nKind regards,\n{facts.company_name}"
    return subject, body


def _reminder_text(facts: ChaseFacts, today: date) -> str:
    money, when = _money_and_date(facts, today)
    number = facts.invoice_number
    if facts.refund:
        if facts.language is Language.PT:
            return (
                f"Olá,\n\nRelembramos o nosso pedido: a nota de crédito do reembolso de {money} de {when}. "
                f"Poderiam enviá-la assim que possível? Agradecemos desde já.\n\n{_our_details(facts)}\n\n"
                f"Com os melhores cumprimentos,\n{facts.company_name}"
            )
        return (
            f"Hello,\n\nA quick reminder about the credit note for the {money} refund of {when}. "
            f"Could you please send it when you can? Thank you.\n\n{_our_details(facts)}\n\n"
            f"Kind regards,\n{facts.company_name}"
        )
    if facts.language is Language.PT:
        what = f"a fatura {number}" if number else "a fatura"
        return (
            f"Olá,\n\nRelembramos o nosso pedido: {what} referente ao pagamento de {money} de {when}. "
            f"Poderiam enviá-la assim que possível? Agradecemos desde já.\n\n{_our_details(facts)}\n\n"
            f"Com os melhores cumprimentos,\n{facts.company_name}"
        )
    what = f"invoice {number} for" if number else "the invoice for"
    return (
        f"Hello,\n\nA quick reminder about {what} the {money} payment dated {when}. "
        f"Could you please send it when you can? Thank you.\n\n{_our_details(facts)}\n\n"
        f"Kind regards,\n{facts.company_name}"
    )


def _correction_text(facts: ChaseFacts, today: date) -> tuple[str, str]:
    """``facts.paid_on`` is the invoice's date here: the invoice is on hold and was not paid."""
    money, when = _money_and_date(facts, today)
    number = facts.invoice_number
    if facts.language is Language.PT:
        what = f"a fatura {number}" if number else "uma fatura"
        subject = f"Fatura {number}: pedido de fatura corrigida" if number else "Pedido de fatura corrigida"
        body = (
            f"Olá,\n\nRecebemos {what} de {money}, com data de {when}, e alguns dos seus dados não correspondem "
            "aos que temos registados. Não a vamos pagar tal como está.\n\n"
            "Poderiam, por favor, enviar-nos uma fatura corrigida, ou confirmar-nos que está correta? "
            f"Agradecemos desde já.\n\n{_our_details(facts)}\n\nCom os melhores cumprimentos,\n{facts.company_name}"
        )
        return subject, body
    what = f"invoice {number}" if number else "an invoice"
    subject = f"Invoice {number}: corrected invoice, please" if number else "Corrected invoice, please"
    body = (
        f"Hello,\n\nWe received {what} for {money}, dated {when}, and some of its details do not match the ones "
        "we have on file. We will not pay it as it stands.\n\n"
        "Could you please send us a corrected invoice, or confirm that it is correct? Thank you.\n\n"
        f"{_our_details(facts)}\n\nKind regards,\n{facts.company_name}"
    )
    return subject, body


def compose_correction_request(facts: ChaseFacts, *, token: str, today: date,
                               message_id_domain: str) -> ChaseMessage:
    """Ask for a corrected invoice when one is on hold (§26). It goes to the address on file, never to the
    sender of the held invoice, and never repeats the bank details that changed."""
    subject, body = _correction_text(facts, today)
    subject = f"{subject} (Ref. {token})"
    _guard(subject, body)
    return ChaseMessage(
        to=facts.supplier_email,
        subject=subject,
        body=body,
        language=facts.language,
        token=token,
        message_id=_message_id(token, 0, today, message_id_domain),
    )


def compose_request(facts: ChaseFacts, *, token: str, today: date, message_id_domain: str) -> ChaseMessage:
    """First, polite request for the invoice (§22)."""
    subject, body = _request_text(facts, today)
    subject = f"{subject} (Ref. {token})"
    _guard(subject, body)
    return ChaseMessage(
        to=facts.supplier_email,
        subject=subject,
        body=body,
        language=facts.language,
        token=token,
        message_id=_message_id(token, 0, today, message_id_domain),
    )


# --------------------------------------------------------------------------- threads and reminders


class SentMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    sent_on: date
    reminder_number: int = 0


class ChaseThread(BaseModel):
    """What we sent to one supplier about one payment, and whether they replied."""

    model_config = ConfigDict(frozen=True)

    token: str
    supplier_email: str
    subject: str
    sent: tuple[SentMessage, ...] = ()
    replied_on: date | None = None

    @classmethod
    def start(cls, message: ChaseMessage, sent_on: date) -> ChaseThread:
        return cls(
            token=message.token,
            supplier_email=message.to,
            subject=message.subject,
            sent=(SentMessage(message_id=message.message_id, sent_on=sent_on),),
        )

    def with_sent(self, message: ChaseMessage, sent_on: date) -> ChaseThread:
        entry = SentMessage(message_id=message.message_id, sent_on=sent_on, reminder_number=message.reminder_number)
        return self.model_copy(update={"sent": (*self.sent, entry)})

    def with_reply(self, on: date) -> ChaseThread:
        return self if self.replied_on is not None else self.model_copy(update={"replied_on": on})

    @property
    def message_ids(self) -> tuple[str, ...]:
        return tuple(m.message_id for m in self.sent)

    @property
    def reminders_sent(self) -> int:
        return sum(1 for m in self.sent if m.reminder_number > 0)


def compose_reminder(
    facts: ChaseFacts, thread: ChaseThread, *, today: date, message_id_domain: str
) -> ChaseMessage:
    """Reminder sent in the same thread (Re: subject, In-Reply-To and References set)."""
    if not thread.sent:
        raise ValueError("nothing was sent in this thread yet")
    number = thread.reminders_sent + 1
    subject = thread.subject if thread.subject.lower().startswith("re:") else f"Re: {thread.subject}"
    body = _reminder_text(facts, today)
    _guard(subject, body)
    return ChaseMessage(
        to=thread.supplier_email,
        subject=subject,
        body=body,
        language=facts.language,
        token=thread.token,
        message_id=_message_id(thread.token, number, today, message_id_domain),
        in_reply_to=thread.sent[-1].message_id,
        references=thread.message_ids,
        reminder_number=number,
    )


class ReminderPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    first_after_days: int = Field(default=6, ge=1, le=90)
    then_every_days: int = Field(default=4, ge=1, le=90)
    max_reminders: int = Field(default=2, ge=0, le=10)
    skip_weekends: bool = True


class ReminderStep(str, Enum):
    WAIT = "wait"
    SEND_REMINDER = "send_reminder"
    ESCALATE = "escalate"  # reminders exhausted: back to the owner
    DONE = "done"  # the supplier replied


class ReminderDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    step: ReminderStep
    on: date | None = None  # when the next step is due
    reminder_number: int = 0


def _business_day(value: date) -> date:
    while value.weekday() >= 5:
        value += timedelta(days=1)
    return value


def next_reminder(thread: ChaseThread, policy: ReminderPolicy, today: date) -> ReminderDecision:
    """What to do next in a chase thread, deterministically from its history."""
    if thread.replied_on is not None:
        return ReminderDecision(step=ReminderStep.DONE)
    if not thread.sent:
        raise ValueError("nothing was sent in this thread yet")
    sent = thread.reminders_sent
    wait = policy.first_after_days if sent == 0 else policy.then_every_days
    due = thread.sent[-1].sent_on + timedelta(days=wait)
    if policy.skip_weekends:
        due = _business_day(due)
    if today < due:
        return ReminderDecision(step=ReminderStep.WAIT, on=due, reminder_number=sent)
    if sent >= policy.max_reminders:
        return ReminderDecision(step=ReminderStep.ESCALATE, on=due, reminder_number=sent)
    return ReminderDecision(step=ReminderStep.SEND_REMINDER, on=due, reminder_number=sent + 1)


# --------------------------------------------------------------------------- reply matching


@dataclass(frozen=True)
class InboundEmail:
    from_address: str
    subject: str = ""
    message_id: str | None = None
    in_reply_to: str | None = None
    references: Sequence[str] | str = ()


class MatchMethod(str, Enum):
    IN_REPLY_TO = "in_reply_to"
    REFERENCES = "references"
    SUBJECT_REFERENCE = "subject_reference"


@dataclass(frozen=True)
class ReplyMatch:
    thread: ChaseThread
    method: MatchMethod
    sender_matches: bool  # the reply came from the supplier's own domain


def _msg_ids(value: Sequence[str] | str | None) -> list[str]:
    if not value:
        return []
    text = value if isinstance(value, str) else " ".join(value)
    bracketed = re.findall(r"<([^<>\s]+)>", text)
    ids = bracketed or text.split()
    return [i.strip().strip("<>").casefold() for i in ids if i.strip().strip("<>")]


def _same_sender(thread: ChaseThread, inbound: InboundEmail) -> bool:
    theirs, ours = email_domain(inbound.from_address), email_domain(thread.supplier_email)
    return bool(theirs and ours and registrable_domain(theirs) == registrable_domain(ours))


def match_reply(threads: Iterable[ChaseThread], inbound: InboundEmail) -> ReplyMatch | None:
    """The chase thread an inbound email answers, or None. Ambiguity is never guessed."""
    pool = list(threads)
    index = {mid: t for t in pool for mid in _msg_ids(" ".join(t.message_ids))}
    for mid in _msg_ids(inbound.in_reply_to):
        if mid in index:
            return ReplyMatch(index[mid], MatchMethod.IN_REPLY_TO, _same_sender(index[mid], inbound))
    for mid in reversed(_msg_ids(inbound.references)):
        if mid in index:
            return ReplyMatch(index[mid], MatchMethod.REFERENCES, _same_sender(index[mid], inbound))
    tokens = {m.group(1).upper() for m in _TOKEN_IN_SUBJECT.finditer(inbound.subject or "")}
    candidates = [t for t in pool if t.token.upper() in tokens]
    if len(candidates) > 1:
        candidates = [t for t in candidates if _same_sender(t, inbound)]
    if len(candidates) != 1:
        return None
    return ReplyMatch(candidates[0], MatchMethod.SUBJECT_REFERENCE, _same_sender(candidates[0], inbound))


def activity_line(facts: ChaseFacts) -> str:
    """Quiet Activity entry (§42): 'Asked Vodafone for the invoice for the €117.20 payment.'"""
    if facts.refund:
        return (f"Asked {facts.supplier_name} for the credit note for the "
                f"{format_money(facts.amount, facts.currency)} refund.")
    return f"Asked {facts.supplier_name} for the invoice for the {format_money(facts.amount, facts.currency)} payment."


# --------------------------------------------------------------------------- a usual invoice that has not arrived (§23)

_EN_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December")


@dataclass(frozen=True)
class RecurringChaseFacts:
    """A supplier's usual invoice that has not arrived yet (§23). There is no payment to quote:
    only the period and the day it normally arrives by. Nothing else reaches the supplier."""

    supplier_name: str
    supplier_email: str
    period_year: int
    period_month: int  # 1..12: the period the missing invoice is for
    usually_by: date  # it normally arrives by this day
    company_name: str
    company_tax_id: str
    company_country: str = "PT"
    language: Language = Language.EN

    def __post_init__(self) -> None:
        if not _PLAIN_ADDRESS.fullmatch(self.supplier_email):
            raise ValueError("supplier email must be one plain address")
        if not 1 <= self.period_month <= 12:
            raise ValueError("period month must be 1..12")

    @classmethod
    def build(cls, supplier: Supplier, company: LegalEntity, *, period: date, usually_by: date,
              language: Language | None = None) -> RecurringChaseFacts:
        if not supplier.contact_email:
            raise ValueError("the supplier has no contact email")
        return cls(
            supplier_name=display_name(supplier.name), supplier_email=supplier.contact_email,
            period_year=period.year, period_month=period.month, usually_by=usually_by,
            company_name=company.name.strip(), company_tax_id=company.tax_id, company_country=company.country,
            language=language or choose_language(supplier, supplier.contact_email),
        )


def compose_recurring_request(facts: RecurringChaseFacts, *, token: str, today: date,
                              message_id_domain: str) -> ChaseMessage:
    """Ask for a usual invoice that is overdue (§23): 'Your invoice for October 2026 usually reaches us by
    26 October, and we have not received it yet. Could you please send it?'"""
    ours = _our_details(facts)
    if facts.language is Language.PT:
        period = f"{PT_MONTHS[facts.period_month - 1]} de {facts.period_year}"
        subject = f"Fatura de {period}"
        body = (
            f"Olá,\n\nA vossa fatura de {period} costuma chegar-nos até {day_month_pt(facts.usually_by, today)} e "
            f"ainda não a recebemos. Poderiam, por favor, enviá-la? Agradecemos desde já.\n\n{ours}\n\n"
            f"Com os melhores cumprimentos,\n{facts.company_name}"
        )
    else:
        period = f"{_EN_MONTHS[facts.period_month - 1]} {facts.period_year}"
        subject = f"Invoice for {period}"
        body = (
            f"Hello,\n\nYour invoice for {period} usually reaches us by {day_month(facts.usually_by, today)}, and we "
            f"have not received it yet. Could you please send it? Thank you.\n\n{ours}\n\n"
            f"Kind regards,\n{facts.company_name}"
        )
    subject = f"{subject} (Ref. {token})"
    _guard(subject, body)
    return ChaseMessage(to=facts.supplier_email, subject=subject, body=body, language=facts.language, token=token,
                        message_id=_message_id(token, 0, today, message_id_domain))


def recurring_activity_line(facts: RecurringChaseFacts) -> str:
    """'Asked Vodafone for its usual invoice for October.'"""
    return f"Asked {facts.supplier_name} for its usual invoice for {_EN_MONTHS[facts.period_month - 1]}."
