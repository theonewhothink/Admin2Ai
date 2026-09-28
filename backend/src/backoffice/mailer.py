"""Outgoing email for owner-confirmed messages (SMTP, e.g. Amazon SES in the EU region).

Configured with BACKOFFICE_SMTP_HOST / _PORT / _USER / _PASSWORD / _FROM. Only
messages the owner tapped Send on reach this class (§25).
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage

__all__ = ["SmtpMailer", "mailer_from_env"]


class SmtpMailer:
    def __init__(self, host: str, port: int, user: str, password: str, sender: str) -> None:
        self.host, self.port, self.user, self.password, self.sender = host, port, user, password, sender

    def send(self, to: list[str], subject: str, body: str, files: list[tuple[str, str, bytes]]) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, ", ".join(to), subject
        msg.set_content(body)
        for name, content_type, data in files:
            main, _, sub = content_type.partition("/")
            msg.add_attachment(data, maintype=main or "application", subtype=sub or "octet-stream", filename=name)
        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.user, self.password)
            smtp.send_message(msg)


def mailer_from_env() -> SmtpMailer | None:
    host = os.environ.get("BACKOFFICE_SMTP_HOST")
    if not host:
        return None
    return SmtpMailer(host, int(os.environ.get("BACKOFFICE_SMTP_PORT", "587")),
                      os.environ.get("BACKOFFICE_SMTP_USER", ""), os.environ.get("BACKOFFICE_SMTP_PASSWORD", ""),
                      os.environ.get("BACKOFFICE_SMTP_FROM", "backoffice@localhost"))
