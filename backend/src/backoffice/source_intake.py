"""Something missing? on Sources: what the owner typed, understood as a place to read (plug and play).

The owner types one line ("laura@hazeltree.pt", "PT50 0033 …", "my Revolut card ending 4821", "the EDP website",
a Google Drive link, "Moloni") and :func:`understand` says what it is and which add form to open, with what it
could read already filled in: ``{"kind", "fields", "message"}``. It never adds anything by itself: the owner
checks the form and taps Add (``POST /api/sources``).

* an email address: a mailbox; the provider from its domain (gmail.com: Google; outlook, hotmail, live, msn:
  Microsoft; a domain one of the owner's connected mailboxes is on: the same provider), else the owner is asked;
* an IBAN: a bank account, checked by its check digits; the bank from its national code
  (backoffice.known_banks), else the owner is asked; the company whose own account it is, when it is one;
* "card" and its last 4 digits: a card (a whole card number keeps only its last 4 digits);
* a bank's name: a bank account (or that bank's card);
* a Google Drive, OneDrive or SharePoint link or name: cloud storage;
* TOConline, Moloni, InvoiceXpress: accounting software;
* a website address or "the EDP website": that supplier's website;
* anything else: ``kind`` "ask" and the chat helps ("I'll ask Claude to help with that.").

Pure Python (the live demo runs it in the browser); it reads nothing but the text and :class:`IntakeContext`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from backoffice.fraud.iban import IBAN_LENGTHS, find_ibans, mask_iban, normalize_iban
from backoffice.invoice_sites import host_of, on_host, site_for_host, site_for_name
from backoffice.known_banks import bank_for_iban, banks_named_in, fold

__all__ = ["ASK_CLAUDE", "IntakeContext", "understand", "MAX_TEXT"]

MAX_TEXT = 500
ASK_CLAUDE = "I'll ask Claude to help with that."

_EMAIL = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})(?![\w-])")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
_DOMAIN = re.compile(r"(?<![\w@.-])((?:[a-z0-9-]+\.)+[a-z]{2,})(?:/[^\s]*)?(?![\w-])", re.IGNORECASE)
# Something shaped like an IBAN (country, check digits, the rest), to say so when its digits don't add up.
_IBAN_SHAPED = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2}\d{2}(?:[ .-]?[A-Za-z0-9]){11,30})(?![A-Za-z0-9])")
_CARD_WORD = re.compile(r"\b(?:card|cards|visa|mastercard|amex|debit|credit)\b", re.IGNORECASE)
_LAST4 = re.compile(r"(?<![\d])(\d{4})(?![\d])")
_LONG_NUMBER = re.compile(r"(?<![\dA-Za-z])(?<![\dA-Za-z][ -])(\d(?:[ -]?\d){12,18})(?![ -]?\d)")
_WEBSITE_WORD = re.compile(r"\b(?:website|web site|site|portal|online account|customer area|online)\b", re.IGNORECASE)
_BANK_WORD = re.compile(r"\b(?:bank|banks|iban)\b", re.IGNORECASE)

_GOOGLE_MAIL = frozenset({"gmail.com", "googlemail.com"})
# Microsoft's own mail domains (Outlook.com): anything else on Microsoft 365 is known from the owner's mailboxes.
_MICROSOFT_MAIL = frozenset({
    "outlook.com", "outlook.pt", "outlook.es", "outlook.fr", "outlook.de", "outlook.it", "hotmail.com",
    "hotmail.co.uk", "hotmail.es", "hotmail.fr", "hotmail.it", "hotmail.de", "live.com", "live.co.uk", "live.fr",
    "live.it", "live.com.pt", "msn.com",
})
# Mail providers read with an app password (IMAP), and their documented mail servers.
_IMAP_HOSTS = {"yahoo.com": "imap.mail.yahoo.com", "ymail.com": "imap.mail.yahoo.com",
               "icloud.com": "imap.mail.me.com", "me.com": "imap.mail.me.com", "mac.com": "imap.mail.me.com"}

_DRIVE_HOSTS = ("drive.google.com", "docs.google.com")
_ONEDRIVE_HOSTS = ("onedrive.live.com", "1drv.ms", "sharepoint.com")
_ACCOUNTING = (
    ("toconline", "TOConline", ("toconline",), ("toconline.pt", "toconline.com")),
    ("moloni", "Moloni", ("moloni",), ("moloni.pt", "moloni.com")),
    ("invoicexpress", "InvoiceXpress", ("invoicexpress", "invoice express"), ("invoicexpress.com", "app.invoicexpress.com")),
)
# Words around a supplier's name that are not its name ("the EDP website" -> "EDP").
_FILLER = frozenset("""a an the my our your their its please add read also and from of on in at for to with is it
    i me we you this that there here invoices invoice bills bill account accounts login log sign signin website web
    site portal online area customer client page fetch get want need missing reading not yet""".split())


@dataclass(frozen=True)
class IntakeContext:
    """What the business already has, so the answer can say "already connected" and fill in the obvious."""

    mailboxes: Mapping[str, str] = field(default_factory=dict)  # address -> provider ("google" | "microsoft" | "imap")
    mail_hosts: Mapping[str, str] = field(default_factory=dict)  # an IMAP mailbox's address -> its mail server
    ibans: Mapping[str, str] = field(default_factory=dict)  # a connected account's IBAN -> how the owner sees it
    company_ibans: Mapping[str, str] = field(default_factory=dict)  # a company's own IBAN -> the company id
    cards: Sequence[str] = ()  # last 4 digits of the connected cards
    banks: Sequence[str] = ()  # banks already read
    files: Sequence[tuple[str, str]] = ()  # (provider, account) of the connected cloud storage
    portals: Sequence[str] = ()  # supplier websites already signed in to (their names)
    suppliers: Sequence[tuple[str, Sequence[str], Sequence[str]]] = ()  # (name, other names, email domains)
    owner_email: str = ""


def _reply(kind: str, message: str, fields: Mapping[str, Any] | None = None, *, already: bool = False
           ) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": kind, "fields": dict(fields or {}), "message": message}
    if already:
        out["already"] = True
    return out


def understand(text: str, context: IntakeContext | None = None) -> dict[str, Any]:
    """What ``text`` names as a place to read: ``{"kind", "fields", "message"}`` (and ``"already": True`` when it
    is connected). ``kind``: "email", "bank", "card", "files", "accounting", "portal", or "ask" (anything else:
    the chat helps). ``fields`` are the add form's own fields, filled with what the text gave."""
    ctx = context or IntakeContext()
    line = " ".join(str(text or "").split())[:MAX_TEXT]
    if not line:
        raise ValueError("empty")
    for step in (_iban, _card_number, _url, _email, _cloud_words, _accounting_words, _card, _bank, _website):
        found = step(line, ctx)
        if found is not None:
            return found
    return _reply("ask", ASK_CLAUDE, {"text": line})


# --------------------------------------------------------------------------- each kind


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _card_number(line: str, ctx: IntakeContext) -> dict[str, Any] | None:
    """A whole card number typed or pasted: only its last 4 digits are kept, never the number."""
    for m in _LONG_NUMBER.finditer(line):
        digits = re.sub(r"\D", "", m.group(1))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            rest = line[:m.start()] + " " + line[m.end():]
            return _card_reply(digits[-4:], rest, ctx, whole_number=True)
    return None


def _iban(line: str, ctx: IntakeContext) -> dict[str, Any] | None:
    valid = find_ibans(line)
    if valid:
        iban = valid[0]
        shown = mask_iban(iban)
        if iban in ctx.ibans:
            return _reply("bank", f"{ctx.ibans[iban]} is already connected.", {"iban": iban}, already=True)
        fields: dict[str, Any] = {"iban": iban}
        company = ctx.company_ibans.get(iban)
        if company:
            fields["companyId"] = company
        named = banks_named_in(line.replace(iban, " "))
        bank = bank_for_iban(iban) or (named[0] if named else None)
        if bank is None:
            return _reply("bank", f"Which bank is {shown} with? Fill it in and tap Add.", fields)
        fields["bank"] = bank.name
        return _reply("bank", f"{shown} is a {bank.name} account. Check the company and tap Add.", fields)
    for m in _IBAN_SHAPED.finditer(line):
        raw = normalize_iban(m.group(1))
        if raw[:2].upper() in IBAN_LENGTHS and sum(c.isdigit() for c in raw) >= 10:
            return _reply("bank", "That IBAN doesn't add up. Check the digits.", {"iban": raw})
    return None


def _email(line: str, ctx: IntakeContext) -> dict[str, Any] | None:
    m = _EMAIL.search(line)
    if m is None:
        return None
    address = m.group(1).lower().rstrip(".")
    if address in {a.lower() for a in ctx.mailboxes}:
        return _reply("email", f"{address} is already connected.", {"address": address}, already=True)
    domain = address.rsplit("@", 1)[1]
    fields: dict[str, Any] = {"address": address}
    known = {a.lower().rsplit("@", 1)[1]: (p, a) for a, p in ctx.mailboxes.items() if "@" in a}
    if domain in _GOOGLE_MAIL:
        fields["provider"] = "google"
        return _reply("email", f"{address} is a Gmail address. Sign in with Google once and I read it.", fields)
    if domain in _MICROSOFT_MAIL:
        fields["provider"] = "microsoft"
        return _reply("email", f"{address} is an Outlook address. Sign in with Microsoft once and I read it.",
                      fields)
    if domain in known:
        provider, other = known[domain]
        fields["provider"] = provider
        if provider == "imap":
            host = ctx.mail_hosts.get(other)
            if host:
                fields["host"] = host
            return _reply("email", f"{domain} uses the same mail server as {other}. Enter its app password "
                                   "and tap Add.", fields)
        label = "Google" if provider == "google" else "Microsoft"
        return _reply("email", f"{domain} is on {label}, like {other}. Sign in once and I read it.", fields)
    if domain in _IMAP_HOSTS:
        fields.update(provider="imap", host=_IMAP_HOSTS[domain])
        return _reply("email", f"For {address} I need an app password from your mail provider. Enter it and "
                               "tap Add.", fields)
    return _reply("email", f"Is {address} with Google, Microsoft or another provider? Choose it and tap Add.",
                  fields)


def _url(line: str, ctx: IntakeContext) -> dict[str, Any] | None:
    """A link: a Drive or OneDrive folder, the accounting software, or a supplier's website."""
    m = _URL.search(line)
    if m is None:
        return None
    url = m.group(0).rstrip(".,;)")
    host = host_of(url if "://" in url else f"https://{url}") or ""
    if on_host(host, _DRIVE_HOSTS):
        return _files("google", ctx, folder=url if "/folders/" in url or "id=" in url else None)
    if on_host(host, _ONEDRIVE_HOSTS):
        return _files("microsoft", ctx)
    for key, name, _, hosts in _ACCOUNTING:
        if on_host(host, hosts):
            account = host.split(".")[0] if key == "invoicexpress" and host.endswith(".app.invoicexpress.com") else None
            return _accounting(key, name, account)
    return _portal(_supplier_for_host(host, ctx), ctx, host=host)


def _cloud_words(line: str, ctx: IntakeContext) -> dict[str, Any] | None:
    words = f" {fold(line)} "
    if any(f" {w} " in words for w in ("google drive", "gdrive", "drive")):
        return _files("google", ctx)
    if any(f" {w} " in words for w in ("onedrive", "one drive", "sharepoint", "share point")):
        return _files("microsoft", ctx)
    return None


def _accounting_words(line: str, ctx: IntakeContext) -> dict[str, Any] | None:
    words = f" {fold(line)} "
    for key, name, names, _ in _ACCOUNTING:
        if any(f" {n} " in words for n in names):
            return _accounting(key, name, None)
    return None


def _card(line: str, ctx: IntakeContext) -> dict[str, Any] | None:
    if not _CARD_WORD.search(line):
        return None
    digits = _LAST4.findall(line)
    if digits:
        return _card_reply(digits[-1], line, ctx)
    banks = banks_named_in(line)
    fields: dict[str, Any] = {"bank": banks[0].name} if banks else {}
    return _reply("card", "What are the card's last 4 digits? Fill them in and tap Add.", fields)


def _card_reply(last4: str, line: str, ctx: IntakeContext, *, whole_number: bool = False) -> dict[str, Any]:
    kept = " I only keep the last 4 digits." if whole_number else ""
    if last4 in ctx.cards:
        return _reply("card", f"Card •••• {last4} is already connected.{kept}", {"last4": last4}, already=True)
    fields: dict[str, Any] = {"last4": last4}
    banks = banks_named_in(line)
    if banks:
        fields["bank"] = banks[0].name
        return _reply("card", f"Card •••• {last4} from {banks[0].name}. Check the company and tap Add.{kept}",
                      fields)
    return _reply("card", f"Card •••• {last4}: which bank issued it? Fill it in and tap Add.{kept}", fields)


def _bank(line: str, ctx: IntakeContext) -> dict[str, Any] | None:
    banks = banks_named_in(line)
    if not banks:
        if _BANK_WORD.search(line) and not _WEBSITE_WORD.search(line) and _supplier_named(line, ctx) is None:
            return _reply("bank", "Which bank, and which account? Fill them in and tap Add.", {})
        return None
    bank = banks[0]
    if any(fold(b) == fold(bank.name) for b in ctx.banks):
        return _reply("bank", f"I already read {bank.name}. To add another account there, check the details and "
                              "tap Add.", {"bank": bank.name})
    return _reply("bank", f"Add your {bank.name} account: check the details and tap Add.", {"bank": bank.name})


def _website(line: str, ctx: IntakeContext) -> dict[str, Any] | None:
    """ "the EDP website", "edp.pt", or a supplier the business has by its name."""
    m = _DOMAIN.search(line)
    if m is not None:
        host = m.group(1).lower()
        return _portal(_supplier_for_host(host, ctx), ctx, host=host)
    named = _supplier_named(line, ctx)
    if _WEBSITE_WORD.search(line):
        return _portal(named or _name_without_filler(line), ctx)
    if named:
        return _portal(named, ctx)
    return None


# --------------------------------------------------------------------------- helpers


def _files(provider: str, ctx: IntakeContext, *, folder: str | None = None) -> dict[str, Any]:
    name = "Google Drive" if provider == "google" else "OneDrive"
    account = next((a for a, p in ctx.mailboxes.items() if p == provider), "")
    if not account and ctx.owner_email:
        account = ctx.owner_email
    fields: dict[str, Any] = {"provider": provider}
    if account:
        fields["address"] = account
    if folder:
        fields["folder"] = folder
    if not folder and any(p == provider and a.lower() == account.lower() for p, a in ctx.files):
        return _reply("files", f"Your {name} is already connected.", fields, already=True)
    return _reply("files", f"{name}: sign in once and I search it for missing invoices.", fields)


def _accounting(key: str, name: str, account: str | None) -> dict[str, Any]:
    fields: dict[str, Any] = {"provider": key}
    if account:
        fields["account"] = account
    return _reply("accounting", f"{name}: add its access for the company it keeps and I read your documents there.",
                  fields)


def _portal(supplier: str, ctx: IntakeContext, *, host: str | None = None) -> dict[str, Any]:
    supplier = supplier.strip()
    if not supplier:
        return _reply("portal", "Which supplier's website? Fill it in with your sign-in there and tap Add.", {})
    if any(fold(p) == fold(supplier) for p in ctx.portals):
        return _reply("portal", f"I already sign in to {supplier}'s website.", {"supplier": supplier}, already=True)
    where = f" ({host})" if host else ""
    return _reply("portal", f"{supplier}'s website{where}: add your sign-in there and I fetch your invoices.",
                  {"supplier": supplier})


def _supplier_for_host(host: str, ctx: IntakeContext) -> str:
    """The supplier a website belongs to: one the business has (by its email domain), a known invoice website,
    else the site's own name ("vodafone.pt" -> "Vodafone")."""
    for name, _, domains in ctx.suppliers:
        if on_host(host, tuple(domains)):
            return name
    site = site_for_host(host)
    if site is not None:
        return site.name
    labels = [p for p in host.split(".") if p and p not in ("www", "my", "app", "online", "login", "clientes")]
    if not labels:
        return ""
    core = labels[-2] if len(labels) >= 2 else labels[0]
    return core.upper() if len(core) <= 3 else core.capitalize()


def _supplier_named(line: str, ctx: IntakeContext) -> str | None:
    words = f" {fold(line)} "
    for name, aliases, _ in ctx.suppliers:
        for n in (name, *aliases):
            if fold(n) and f" {fold(n)} " in words:
                return name
    site = site_for_name(_name_without_filler(line))
    return site.name if site is not None else None


def _name_without_filler(line: str) -> str:
    """ "the EDP website" -> "EDP"; "my Vodafone account online" -> "Vodafone"."""
    words = [w for w in re.split(r"[\s,;:!?]+", line.strip().strip(".")) if w]
    kept = [w for w in words if fold(w) not in _FILLER and fold(w)]
    return " ".join(kept)[:80].strip(" '\"")
