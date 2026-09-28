"""Sender-domain checks: changed and lookalike domains (§26).

Compares the domain an email really came from with the domains a supplier is
known to use. Subdomains of a known domain are fine (mail.vodafone.pt is
vodafone.pt). Anything else is a *changed* domain, and a *lookalike* when it
is built to be mistaken for a known one:

* homoglyphs and look-alike characters (vodafοne with a Greek omicron,
  vodaf0ne, rn for m, vv for w), including punycode (xn--) domains;
* one or two typos (vodafome.pt);
* the brand embedded in someone else's domain (vodafone-pt.com,
  vodafone.pt.billing-secure.com).

The multi-label public suffix list below is a small built-in subset for the
first markets, not the full Public Suffix List; unknown suffixes fall back to
"last two labels", which only makes the check stricter.

Free-mail domains (gmail.com, sapo.pt, …) are shared by millions of people:
a supplier that invoices from a personal mailbox is recognised by its exact
address, never by the domain. Any other address on that domain is
``NEW_ADDRESS``.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from email.utils import parseaddr
from enum import Enum

__all__ = [
    "CROSS_SCRIPT_LETTERS",
    "FREE_MAIL_DOMAINS",
    "DomainCheck",
    "DomainVerdict",
    "check_sender_domain",
    "email_domain",
    "registrable_domain",
    "skeleton",
]

_MULTI_LABEL_SUFFIXES = frozenset(
    {
        "com.pt", "org.pt", "gov.pt", "edu.pt", "net.pt", "co.uk", "org.uk", "gov.uk",
        "ac.uk", "com.es", "org.es", "nom.es", "co.il", "org.il", "gov.il", "com.br",
        "com.au", "co.nz", "co.jp", "com.mx", "com.tr", "co.za",
    }
)  # fmt: skip

# Personal mailboxes: a supplier invoice from one of these is never the supplier's usual domain.
FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
        "yahoo.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
        "gmx.com", "gmx.net", "sapo.pt", "mail.com", "zoho.com",
    }
)  # fmt: skip

# Characters from other scripts that render like Latin letters, plus digit/symbol swaps.
_CONFUSABLES = str.maketrans(
    {
        "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i",
        "ј": "j", "ѕ": "s", "ԁ": "d", "һ": "h", "ӏ": "l", "ԛ": "q", "ԝ": "w", "ɡ": "g",
        "ο": "o", "α": "a", "ν": "v", "ι": "i", "κ": "k", "τ": "t", "ρ": "p", "ε": "e",
        "0": "o", "1": "l", "3": "e", "5": "s", "$": "s", "@": "a", "i": "l", "!": "l",
    }
)  # fmt: skip
_MULTI_CHAR = (("rn", "m"), ("vv", "w"), ("cl", "d"))
# Only the letters from other scripts that render as Latin ones (no digits or symbols):
# safe to fold into ordinary text before matching words (§26 wording check).
CROSS_SCRIPT_LETTERS = str.maketrans(
    {k: v for k, v in ((chr(c), _CONFUSABLES[c]) for c in _CONFUSABLES) if not k.isascii()}
)


class DomainVerdict(str, Enum):
    KNOWN = "known"  # a known domain or one of its subdomains
    CHANGED = "changed"  # not a known domain
    FREE_MAIL = "free_mail"  # a personal mailbox provider
    LOOKALIKE = "lookalike"  # built to be mistaken for a known domain
    NEW_ADDRESS = "new_address"  # the supplier's free-mail provider, but not its known address
    UNKNOWN = "unknown"  # nothing to compare with (no known domains, or no sender)


@dataclass(frozen=True)
class DomainCheck:
    verdict: DomainVerdict
    sender_domain: str | None
    imitated: str | None = None  # the known domain a lookalike imitates


def _decode_label(label: str) -> str:
    if label.startswith("xn--"):
        try:
            return label[4:].encode("ascii").decode("punycode")
        except (UnicodeError, ValueError):
            return label
    return label


def _clean_host(host: str) -> str:
    labels = [part for part in host.strip().strip(".").lower().split(".") if part]
    return ".".join(_decode_label(label) for label in labels)


def email_domain(address: str | None) -> str | None:
    """'Vodafone <faturas@Mail.Vodafone.pt>' -> 'mail.vodafone.pt' (punycode decoded)."""
    if not address:
        return None
    _, addr = parseaddr(address)
    if "@" not in addr:
        return None
    host = _clean_host(addr.rsplit("@", 1)[1])
    return host or None


def registrable_domain(host: str) -> str:
    """'mail.vodafone.pt' -> 'vodafone.pt'; 'faturas.empresa.com.pt' -> 'empresa.com.pt'."""
    labels = _clean_host(host).split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _brand(registrable: str) -> str:
    return registrable.split(".", 1)[0]


def skeleton(text: str) -> str:
    """Visual skeleton for comparison: accents, homoglyphs and separators removed."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = plain.translate(_CONFUSABLES)
    for pair, single in _MULTI_CHAR:
        folded = folded.replace(pair, single)
    return "".join(ch for ch in folded if ch.isalnum())


def _distance(a: str, b: str) -> int:
    """Optimal string alignment distance (Damerau-Levenshtein with adjacent swaps)."""
    rows = [list(range(len(b) + 1))]
    for i, ca in enumerate(a, start=1):
        row = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            row[j] = min(rows[-1][j] + 1, row[j - 1] + 1, rows[-1][j - 1] + cost)
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                row[j] = min(row[j], rows[-2][j - 2] + 1)
        rows.append(row)
    return rows[-1][-1]


def _imitates(sender_host: str, sender_reg: str, known_reg: str) -> bool:
    brand, sender_brand = _brand(known_reg), _brand(sender_reg)
    if sender_brand != brand and skeleton(sender_brand) == skeleton(brand):
        return True  # homoglyphs / character swaps
    if len(brand) >= 4:
        allowed = 1 if len(brand) < 9 else 2
        if 0 < _distance(skeleton(sender_brand), skeleton(brand)) <= allowed:
            return True  # typo-squatting
        if sender_brand != brand and skeleton(brand) in skeleton(sender_host):
            return True  # brand embedded in another domain
    return False


def _address(value: str) -> str | None:
    """'João <Joao.Canal@Gmail.com>' -> 'joao.canal@gmail.com' (domain punycode-decoded)."""
    _, addr = parseaddr(value)
    if "@" not in addr:
        return None
    local, _, host = addr.rpartition("@")
    domain = _clean_host(host)
    return f"{local.strip().casefold()}@{domain}" if local.strip() and domain else None


def check_sender_domain(
    sender: str | None,
    known_domains: Sequence[str],
    known_addresses: Sequence[str] = (),
) -> DomainCheck:
    """Classify the sender against the supplier's known domains and addresses.

    ``known_domains`` may also hold full addresses ("joao@gmail.com"); those
    count as known addresses and their domain as a known domain.
    """
    host = email_domain(sender) if sender and "@" in sender else (_clean_host(sender) if sender else None)
    entries = [d.strip() for d in known_domains if d and d.strip()]
    addresses = {a for a in (_address(x) for x in [*known_addresses, *(e for e in entries if "@" in e)]) if a}
    domains = [e for e in entries if "@" not in e] + [a.rpartition("@")[2] for a in addresses]
    known = sorted({registrable_domain(d) for d in domains})
    if not host:
        return DomainCheck(DomainVerdict.UNKNOWN, None)
    sender_reg = registrable_domain(host)
    if not known:
        return DomainCheck(DomainVerdict.UNKNOWN, host)
    if sender_reg in known:
        if sender_reg in FREE_MAIL_DOMAINS:
            address = _address(sender) if sender else None
            verdict = DomainVerdict.KNOWN if address in addresses else DomainVerdict.NEW_ADDRESS
            return DomainCheck(verdict, host)
        return DomainCheck(DomainVerdict.KNOWN, host)
    for known_reg in known:
        if _imitates(host, sender_reg, known_reg):
            return DomainCheck(DomainVerdict.LOOKALIKE, host, imitated=known_reg)
    if sender_reg in FREE_MAIL_DOMAINS:
        return DomainCheck(DomainVerdict.FREE_MAIL, host)
    return DomainCheck(DomainVerdict.CHANGED, host)
