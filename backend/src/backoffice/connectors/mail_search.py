"""Searching a connected mailbox for one missing document (§22 searches 1, 2 and 6).

The missing-evidence autopilot asks a mailbox "is the invoice for this payment
somewhere in here?". One :class:`MailQuery` says it once; each provider renders
it in its own search language:

* Gmail: the ``q`` of ``users.messages.list`` (``from:``, quoted phrases,
  ``subject:``, ``filename:``, ``has:attachment``, ``after:``/``before:``).
  Gmail searches every label, archived mail included; drafts and chats are left
  out, spam and trash too unless the connection opted in.
* Microsoft Graph: ``$search`` (KQL) on ``/messages`` of the mailbox, which
  covers every folder, the archive included (``received>=``, ``from:``,
  ``subject:``, ``attachment:``, ``hasAttachments:true``).
* IMAP: ``UID SEARCH`` criteria (``SINCE``/``BEFORE``, ``FROM``, ``TEXT``,
  ``SUBJECT``, ``OR``) in each configured mailbox.

A query is ``all_of`` terms (every one must hold) and ``any_of`` terms (at
least one must), inside a date window; ``attachment`` asks for messages with a
file. Values are cleaned to one plain line (no quotes, no control characters),
so text read from a document can never change the query's meaning.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

__all__ = ["MailQuery", "MailTerm", "TermKind", "clean_term", "gmail_query", "graph_search", "imap_criteria"]


class TermKind:
    FROM = "from"  # an address or a domain the message comes from
    TEXT = "text"  # a word or phrase anywhere in the message (subject, body, attachments where indexed)
    SUBJECT = "subject"  # a word or phrase in the subject
    FILENAME = "filename"  # a word in an attachment's file name

    ALL = (FROM, TEXT, SUBJECT, FILENAME)


_MAX_TERM = 80


def clean_term(value: str) -> str:
    """One plain search value: printable, single-spaced, without quotes, brackets or parentheses."""
    text = "".join(ch for ch in str(value or "") if unicodedata.category(ch)[0] != "C")
    text = re.sub(r"[\"'(){}\[\]\\]", " ", text)
    return " ".join(text.split())[:_MAX_TERM]


@dataclass(frozen=True)
class MailTerm:
    kind: str
    value: str

    def __post_init__(self) -> None:
        if self.kind not in TermKind.ALL:
            raise ValueError(f"unknown search term kind {self.kind!r}")
        object.__setattr__(self, "value", clean_term(self.value))


@dataclass(frozen=True)
class MailQuery:
    """Messages received from ``since`` to ``until`` (inclusive) matching every ``all_of`` term and, when
    ``any_of`` is given, at least one of those. At most ``limit`` messages are fetched, newest first."""

    since: date
    until: date
    any_of: tuple[MailTerm, ...] = ()
    all_of: tuple[MailTerm, ...] = ()
    attachment: bool = False
    limit: int = 10

    def __post_init__(self) -> None:
        if self.until < self.since:
            raise ValueError("a search window ends after it starts")
        object.__setattr__(self, "any_of", tuple(t for t in self.any_of if t.value))
        object.__setattr__(self, "all_of", tuple(t for t in self.all_of if t.value))
        if not self.any_of and not self.all_of:
            raise ValueError("a mailbox search needs at least one term")
        if not 1 <= self.limit <= 100:
            raise ValueError("a mailbox search fetches between 1 and 100 messages")


# --------------------------------------------------------------------------- Gmail


def _gmail_term(term: MailTerm) -> str:
    value = term.value
    phrase = value if re.fullmatch(r"\w+", value) else f'"{value}"'
    if term.kind == TermKind.FROM:
        return f"from:{value.replace(' ', '')}"
    if term.kind == TermKind.SUBJECT:
        return f"subject:{phrase}"
    if term.kind == TermKind.FILENAME:
        return f"filename:{value.replace(' ', '')}"
    return phrase


def gmail_query(query: MailQuery) -> str:
    """The Gmail ``q``: ``after:2026/08/05 before:2026/10/04 (from:edp.pt OR "FT 2026/183") has:attachment``."""
    parts = [f"after:{query.since:%Y/%m/%d}", f"before:{query.until + timedelta(days=1):%Y/%m/%d}"]
    parts += [_gmail_term(t) for t in query.all_of]
    if query.any_of:
        terms = [_gmail_term(t) for t in query.any_of]
        parts.append(terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")")
    if query.attachment:
        parts.append("has:attachment")
    parts += ["-in:drafts", "-in:chats"]
    return " ".join(parts)


# --------------------------------------------------------------------------- Microsoft Graph (KQL)


def _kql_term(term: MailTerm) -> str:
    value = term.value
    phrase = value if re.fullmatch(r"\w+", value) else f'"{value}"'
    if term.kind == TermKind.FROM:
        return f"from:{value.replace(' ', '')}"
    if term.kind == TermKind.SUBJECT:
        return f"subject:{phrase}"
    if term.kind == TermKind.FILENAME:
        return f"attachment:{value.replace(' ', '')}"
    return phrase


def graph_search(query: MailQuery) -> str:
    """The value of Graph's ``$search`` (KQL, dates as the documentation shows them: MM/DD/YYYY)::

        "received>=08/05/2026 AND received<=10/03/2026 AND (from:edp.pt OR \\"FT 2026/183\\")"
    """
    parts = [f"received>={query.since:%m/%d/%Y}", f"received<={query.until:%m/%d/%Y}"]
    parts += [_kql_term(t) for t in query.all_of]
    if query.any_of:
        terms = [_kql_term(t) for t in query.any_of]
        parts.append(terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")")
    if query.attachment:
        parts.append("hasAttachments:true")
    kql = " AND ".join(parts)
    return '"' + kql.replace('"', '\\"') + '"'


# --------------------------------------------------------------------------- IMAP (RFC 3501 SEARCH)


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _imap_day(day: date) -> str:
    return f"{day.day}-{_MONTHS[day.month - 1]}-{day.year}"


def _ascii(value: str) -> str:
    """IMAP search strings without a CHARSET are US-ASCII: accents are folded ("Comunicações" -> "Comunicacoes")."""
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return " ".join(folded.split())


def _imap_term(term: MailTerm) -> list[str]:
    value = f'"{_ascii(term.value)}"'
    if term.kind == TermKind.FROM:
        return ["FROM", value]
    if term.kind == TermKind.SUBJECT:
        return ["SUBJECT", value]
    return ["TEXT", value]  # file names are part of the message text (Content-Disposition)


def imap_criteria(query: MailQuery) -> list[str]:
    """``SINCE 5-Aug-2026 BEFORE 4-Oct-2026 OR FROM "edp.pt" TEXT "FT 2026/183"`` as a list of arguments."""
    out = ["SINCE", _imap_day(query.since), "BEFORE", _imap_day(query.until + timedelta(days=1))]
    for term in query.all_of:
        out += _imap_term(term)
    alternatives = [_imap_term(t) for t in query.any_of]
    if alternatives:
        # "OR a b" takes two keys; n alternatives nest as OR a (OR b c).
        nested = alternatives[-1]
        for alt in reversed(alternatives[:-1]):
            nested = ["OR", *alt, *nested]
        out += nested
    return out
