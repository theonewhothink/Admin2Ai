"""The send path for every email the back office sends (§22, §25, §28).

One rule everywhere: **a message counts as sent only when a transport
accepted it.** Until then it is written and waiting to be sent, and every
screen says so.

Transports (anything with ``send(to, subject, body, files, headers=None)``
that raises when it could not deliver):

* :class:`SmtpMailer`: real email over SMTP (e.g. Amazon SES in the EU region),
  configured with BACKOFFICE_SMTP_HOST / _PORT / _USER / _PASSWORD / _FROM
  (:func:`mailer_from_env`).
* :class:`SimulatedOutbox`: the demo's transport. It accepts every message and
  keeps it in memory; nothing ever leaves the demo. It is explicit (``simulated
  = True``) so the demo can say so where it matters.

Owner-confirmed drafts (the chat's Send button) and the back office's own
messages (supplier invoice requests, answers to the accountant) all go through
one of these; without one, they wait.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

__all__ = ["SIMULATED_NOTE", "AcceptedMessage", "SimulatedOutbox", "SmtpMailer", "is_simulated", "mailer_from_env"]

# Said once, where a demo send is confirmed to the owner.
SIMULATED_NOTE = "This is the demo: no real email leaves it."


class SmtpMailer:
    def __init__(self, host: str, port: int, user: str, password: str, sender: str) -> None:
        self.host, self.port, self.user, self.password, self.sender = host, port, user, password, sender

    def send(self, to: list[str], subject: str, body: str, files: list[tuple[str, str, bytes]],
             headers: Mapping[str, str] | None = None) -> None:
        # Imported here: the browser build (Pyodide) has no ssl module, and it never sends real email.
        import smtplib
        import ssl

        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, ", ".join(to), subject
        for name, value in (headers or {}).items():
            if value and name.lower() in ("message-id", "in-reply-to", "references"):
                msg[name] = value
        msg.set_content(body)
        for name, content_type, data in files:
            main, _, sub = content_type.partition("/")
            msg.add_attachment(data, maintype=main or "application", subtype=sub or "octet-stream", filename=name)
        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.user, self.password)
            smtp.send_message(msg)


@dataclass(frozen=True)
class AcceptedMessage:
    """One message the simulated outbox accepted."""

    to: tuple[str, ...]
    subject: str
    body: str
    files: tuple[str, ...] = ()
    headers: tuple[tuple[str, str], ...] = ()


@dataclass
class SimulatedOutbox:
    """The demo's transport: accepts every message and keeps it here. Nothing leaves the demo."""

    simulated = True
    accepted: list[AcceptedMessage] = field(default_factory=list)

    def send(self, to: Sequence[str], subject: str, body: str, files: Sequence[tuple[str, str, bytes]] = (),
             headers: Mapping[str, str] | None = None) -> None:
        self.accepted.append(AcceptedMessage(
            to=tuple(to), subject=subject, body=body, files=tuple(name for name, _, _ in files),
            headers=tuple(sorted((headers or {}).items()))))


def is_simulated(transport: Any) -> bool:
    """True for the demo's transport (its sends never leave the demo)."""
    return bool(getattr(transport, "simulated", False))


def mailer_from_env() -> SmtpMailer | None:
    host = os.environ.get("BACKOFFICE_SMTP_HOST")
    if not host:
        return None
    return SmtpMailer(host, int(os.environ.get("BACKOFFICE_SMTP_PORT", "587")),
                      os.environ.get("BACKOFFICE_SMTP_USER", ""), os.environ.get("BACKOFFICE_SMTP_PASSWORD", ""),
                      os.environ.get("BACKOFFICE_SMTP_FROM", "backoffice@localhost"))
