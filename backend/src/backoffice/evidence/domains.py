"""Domain names: normalisation, lookalike detection, display names (§9, §26).

The registrable-domain split uses a small built-in list of multi-label public
suffixes, not the full Public Suffix List (not a dependency here). It is a
heuristic for grouping and display; it is never the sole basis of a trust
decision. Lookalike checks compare *skeletons* (homoglyphs and common
character swaps folded to ASCII) against known supplier domains.
"""

from __future__ import annotations

import ipaddress
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "LookalikeChecker",
    "LookalikeFinding",
    "LookalikeKind",
    "display_name_for_host",
    "registrable_domain",
    "to_ascii_host",
    "to_unicode_host",
]

# Second-level public suffixes common in our markets (PT, ES, UK, IL, BR, ...).
# Heuristic list, verified_as_of 2026-09 against the author's knowledge of the
# PSL; extend when a new country pack is added. Not a regulatory source.
_MULTI_LABEL_SUFFIXES = frozenset(
    {
        "com.pt", "org.pt", "gov.pt", "edu.pt", "int.pt", "net.pt", "publ.pt", "nome.pt",
        "com.es", "org.es", "gob.es", "nom.es", "edu.es",
        "co.uk", "org.uk", "gov.uk", "ac.uk", "ltd.uk", "plc.uk", "me.uk", "net.uk",
        "co.il", "org.il", "gov.il", "ac.il", "net.il", "muni.il",
        "com.br", "net.br", "org.br", "gov.br",
        "com.fr", "gouv.fr", "co.it", "gov.it",
        "com.au", "net.au", "org.au", "co.nz", "co.jp", "co.za", "com.mx", "com.tr",
    }
)  # fmt: skip

# Characters that render like ASCII letters (Cyrillic, Greek, Latin variants)
# and digit swaps. Subset of Unicode confusables relevant to Latin domains.
_CONFUSABLES = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i",
    "ј": "j", "ѕ": "s", "ԁ": "d", "һ": "h", "ӏ": "l", "ɡ": "g", "ո": "n", "ս": "u",
    "ԛ": "q", "ԝ": "w", "ү": "y", "в": "b", "к": "k", "м": "m", "н": "h", "т": "t",
    "α": "a", "ο": "o", "ρ": "p", "ν": "v", "ι": "i", "κ": "k", "τ": "t", "υ": "u",
    "χ": "x", "ε": "e", "β": "b", "η": "n", "ı": "i", "ɩ": "i", "ł": "l",
    "0": "o", "1": "l", "3": "e", "5": "s", "!": "i", "|": "l",
}  # fmt: skip
_SEQUENCE_SWAPS = (("rn", "m"), ("vv", "w"), ("cl", "d"), ("nn", "m"))
_SCRIPTS = ("LATIN", "CYRILLIC", "GREEK", "ARMENIAN", "CHEROKEE", "COPTIC")


def to_ascii_host(host: str) -> str | None:
    """Lower-case ASCII (punycode) form of ``host``, or ``None`` if invalid."""
    host = host.strip().rstrip(".").lower()
    if not host or len(host) > 253:
        return None
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    labels = ascii_host.split(".")
    if any(not label or len(label) > 63 for label in labels):
        return None
    return ascii_host


def to_unicode_host(host: str) -> str:
    """Unicode form of an ASCII host (``xn--`` labels decoded), best effort."""
    labels = []
    for label in host.lower().split("."):
        if label.startswith("xn--"):
            try:
                label = label.encode("ascii").decode("idna")
            except UnicodeError:
                pass
        labels.append(label)
    return ".".join(labels)


def registrable_domain(host: str) -> str:
    """``my.vodafone.pt`` -> ``vodafone.pt``; ``a.b.co.uk`` -> ``b.co.uk``."""
    labels = host.lower().rstrip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _brand_label(host: str) -> str:
    reg = registrable_domain(to_unicode_host(host))
    return reg.split(".")[0]


def display_name_for_host(host: str) -> str:
    """Plain supplier name from a host: ``faturas.vodafone.pt`` -> ``Vodafone``.

    Used only when the caller does not know the supplier's real name. An IP
    address or a numeric label is never shown as a name (§70: no raw IDs).
    """
    if _is_ip(host):
        return "The supplier"
    label = _brand_label(host).replace("-", " ").strip()
    if not label or not any(ch.isalpha() for ch in label):
        return "The supplier"
    return " ".join(word[:1].upper() + word[1:] for word in label.split())


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    return True


def _skeleton(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text.lower())
    out = "".join(_CONFUSABLES.get(ch, ch) for ch in folded)
    out = "".join(
        ch for ch in unicodedata.normalize("NFKD", out) if not unicodedata.combining(ch)
    )
    for seq, repl in _SEQUENCE_SWAPS:
        out = out.replace(seq, repl)
    return out


def _scripts(label: str) -> set[str]:
    found = set()
    for ch in label:
        if ch.isascii() and not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        script = name.split(" ", 1)[0]
        if script in _SCRIPTS:
            found.add(script)
    return found


def _within_one_edit(a: str, b: str) -> bool:
    """Damerau distance <= 1 (one insert, delete, substitute or adjacent swap)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diffs = [i for i in range(la) if a[i] != b[i]]
        if len(diffs) == 1:
            return True
        return len(diffs) == 2 and diffs[1] == diffs[0] + 1 and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]
    short, long_ = (a, b) if la < lb else (b, a)
    for i in range(len(long_)):
        if long_[:i] + long_[i + 1 :] == short:
            return True
    return False


class LookalikeKind(str, Enum):
    HOMOGLYPH = "homoglyph"  # renders like a known domain
    TYPO = "typo"  # one edit away from a known domain
    EMBEDDED = "embedded"  # vodafone.pt.example.com
    MIXED_SCRIPT = "mixed_script"  # Latin and Cyrillic in one label


@dataclass(frozen=True)
class LookalikeFinding:
    kind: LookalikeKind
    imitates: str | None  # the known domain being imitated, if any


class LookalikeChecker:
    """Flags hosts that imitate a known supplier domain (§9 step 2, §26)."""

    def __init__(self, known_domains: Iterable[str] = (), *, min_typo_length: int = 6) -> None:
        normalised = {to_ascii_host(d) for d in known_domains}
        self._known = frozenset(d for d in normalised if d)
        self._skeletons = {self._reg_skeleton(d): d for d in self._known}
        self._min_typo = min_typo_length

    @property
    def known(self) -> frozenset[str]:
        return self._known

    @staticmethod
    def _reg_skeleton(ascii_host: str) -> str:
        return _skeleton(registrable_domain(to_unicode_host(ascii_host)))

    def is_known(self, ascii_host: str) -> bool:
        return any(ascii_host == d or ascii_host.endswith("." + d) for d in self._known)

    def check(self, host: str) -> LookalikeFinding | None:
        """``None`` when ``host`` is known or unremarkable; otherwise a finding."""
        ascii_host = to_ascii_host(host)
        if ascii_host is None:
            return LookalikeFinding(LookalikeKind.HOMOGLYPH, None)
        if self.is_known(ascii_host):
            return None
        for known in sorted(self._known):
            if f".{known}." in f".{ascii_host}.":
                return LookalikeFinding(LookalikeKind.EMBEDDED, known)
        skeleton = self._reg_skeleton(ascii_host)
        if skeleton in self._skeletons:  # most specific: names the imitated domain
            return LookalikeFinding(LookalikeKind.HOMOGLYPH, self._skeletons[skeleton])
        unicode_host = to_unicode_host(ascii_host)
        if any(len(_scripts(label)) > 1 for label in unicode_host.split(".")):
            return LookalikeFinding(LookalikeKind.MIXED_SCRIPT, None)
        return self._typo(skeleton)

    def _typo(self, skeleton: str) -> LookalikeFinding | None:
        for known_skeleton, known in sorted(self._skeletons.items()):
            if len(known_skeleton.split(".")[0]) < self._min_typo:
                continue
            if _within_one_edit(skeleton, known_skeleton):
                return LookalikeFinding(LookalikeKind.TYPO, known)
        return None
