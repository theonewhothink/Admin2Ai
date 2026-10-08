"""An accountant invites a client business (§29 distribution loop, §62).

"Your accountant has enabled Back Office for you." The accountant enters the
client's email; the client receives a link, signs up (or signs in) and
accepts. On acceptance the accountant gets an accountant membership for that
business, limited to the companies the invitation names (by NIF) when it names
any.

The link carries a single-use token: 32 random bytes, shown once in the email.
Only its SHA-256 is stored, the invitation expires after
:data:`INVITATION_DAYS` days, and it can be accepted once, by the account whose
email it was sent to. These checks live here so the demo (the engine's
simulated outbox) and production (the server's store and mailer) agree.

Pure Python: no network, no framework (the browser build imports it).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from backoffice.countries import pack_text, pack_words

__all__ = [
    "HEADLINE",
    "INVITATION_DAYS",
    "CleanInvitation",
    "InvitationRefused",
    "check_acceptance",
    "clean_invitation",
    "companies_for",
    "expiry",
    "hash_token",
    "invitation_email",
    "new_token",
    "token_ok",
]

INVITATION_DAYS = 14
HEADLINE = "Your accountant has enabled Back Office for you."
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")

NOT_VALID = "This invitation link is not valid. Ask your accountant to send a new one."
USED = "This invitation was already used."
EXPIRED = "This invitation has expired. Ask your accountant to send a new one."
WRONG_ACCOUNT = "This invitation was sent to another email address. Sign in with that address to accept it."
OWN = "You can't accept your own invitation."


class InvitationRefused(Exception):
    """An invitation that cannot be accepted; ``status`` is the HTTP status, ``message`` plain words."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def new_token() -> tuple[str, str]:
    """A fresh single-use token and its SHA-256 (the only thing stored)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(str(token).encode()).hexdigest()


def token_ok(token: object) -> bool:
    return isinstance(token, str) and bool(_TOKEN.match(token))


@dataclass(frozen=True)
class CleanInvitation:
    email: str
    client_name: str
    tax_ids: tuple[str, ...]
    firm: str


def clean_invitation(body: object) -> CleanInvitation:
    """The client's email, the business name, optional NIFs (company limit) and the firm, checked.

    Raises ValueError with a plain message.
    """
    b = body if isinstance(body, dict) else {}
    email = str(b.get("email") or "").strip().lower()
    if not _EMAIL.match(email) or len(email) > 254:
        raise ValueError("That doesn't look like an email address.")
    name = " ".join(str(b.get("clientName") or b.get("name") or "").split())
    if len(name) > 160:
        raise ValueError("That name is too long.")
    firm = " ".join(str(b.get("firm") or "").split())[:120]
    raw = b.get("taxIds") if isinstance(b.get("taxIds"), list) else ([b["taxId"]] if b.get("taxId") else [])
    tax_ids: list[str] = []
    for value in raw[:20]:
        digits = re.sub(r"\D", "", str(value or ""))
        if not any(re.fullmatch(shape, digits) for shape in pack_words("invitations.tax_id")):
            raise ValueError(pack_text("invitations.tax_id_problem", "Check the company tax numbers."))
        if digits not in tax_ids:
            tax_ids.append(digits)
    return CleanInvitation(email=email, client_name=name, tax_ids=tuple(tax_ids), firm=firm)


def invitation_email(*, inviter: str, firm: str, client_name: str, link: str | None,
                     expires_at: datetime) -> tuple[str, str]:
    """Subject and plain-text body of the invitation (§29 wording, §36 plain language)."""
    who = inviter if not firm or firm == inviter else f"{inviter} ({firm})"
    greeting = f"Hello {client_name}," if client_name else "Hello,"
    lines = [
        greeting,
        "",
        HEADLINE,
        "",
        f"{who} uses Back Office to receive your monthly documents. Connect your email and your bank once, "
        "and Back Office collects your invoices and receipts, matches them to your payments and sends the "
        "month to your accountant.",
        "",
    ]
    if link:
        lines += ["Create your account or sign in with this email address, then accept:", link, ""]
    lines += [f"This link works once, until {expires_at.day} {expires_at:%B %Y}.",
              "If you were not expecting it, you can ignore this email."]
    return HEADLINE, "\n".join(lines)


def expiry(now: datetime, days: int = INVITATION_DAYS) -> datetime:
    return now + timedelta(days=days)


def check_acceptance(*, token_hash: str, stored_hash: str, email: str, invited_email: str, now: datetime,
                     expires_at: datetime, accepted_at: datetime | None, inviter_id: str,
                     acceptor_id: str) -> None:
    """Refuse an invitation that is not this one, used, expired, for another account or the inviter's own."""
    if not hmac.compare_digest(token_hash, stored_hash):
        raise InvitationRefused(404, NOT_VALID)
    if accepted_at is not None:
        raise InvitationRefused(409, USED)
    if expires_at <= now:
        raise InvitationRefused(410, EXPIRED)
    if email.strip().lower() != invited_email.strip().lower():
        raise InvitationRefused(403, WRONG_ACCOUNT)
    if inviter_id == acceptor_id:
        raise InvitationRefused(409, OWN)


def companies_for(tax_ids: Sequence[str], companies: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """Company ids (from ``(id, NIF)`` pairs) named by the invitation's NIFs, in the business's order."""
    wanted = set(tax_ids)
    return tuple(cid for cid, nif in companies if nif and nif in wanted)
