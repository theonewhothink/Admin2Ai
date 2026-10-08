"""A country's own words, for the core's readers and writers (§49-50).

A core module that reads text (till reports, fraud phrases, the owner's chat, bank lines, payslips ...) or
writes it (an email to a supplier) holds English and the languages no pack covers. A country's own words
(Portugal's "fatura", "fecho de caixa", "alteração de IBAN", "Olá, ...") live in its pack, under a concept key
("tills.title", "fraud.bank_change", ...), in the mapping its ``CompanyPack.vocabulary()`` returns. Each key's
owner (the core module that asks for it) says what its entries are; most are regular-expression alternatives
written for that module's own pattern (over its folded text: lower case, no accents).

The core reads every text with the words of every company pack (:func:`pack_words`, in country order), so a
Portuguese till report or a Spanish invoice is read the same whichever country the business's company is in.
:class:`LazyPattern` builds a module's regular expression from them on first use, so importing a core module
never imports a pack (and a pack may import the core).

    from backoffice.countries import LazyPattern, pack_alternatives
    _TITLE = LazyPattern(lambda: rf"(?<![a-z])(?:{pack_alternatives('tills.title')}|z[\\s-]?report)(?![a-z])")
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from functools import lru_cache
from typing import Any

__all__ = ["PACK_WORDS", "LazyPattern", "pack_alternatives", "pack_text", "pack_words", "spliced"]

_NEVER = r"(?!)"  # an alternative that never matches: no pack names the concept
# In an ordered list of the core's words (CSV headings tried in order): where the packs' own words go.
PACK_WORDS = "<pack words>"


def pack_words(concept: str) -> tuple[str, ...]:
    """Every company pack's entries for ``concept``, in country order, without repeats (``()`` when none has it)."""
    return _words(concept)


@lru_cache(maxsize=8192)  # concepts are few, but a glossary is asked for word by word
def _words(concept: str) -> tuple[str, ...]:
    from .base import _BUILTIN_PACKS, CompanyPack, UnknownCountryError, available_countries, get_pack

    out: list[str] = []
    for country in available_countries():
        try:
            pack = get_pack(country)
        except UnknownCountryError:
            if country in _BUILTIN_PACKS:  # its module is still being imported: never cache words without it
                raise RuntimeError(f"the {country} pack's words were asked for while it was loading") from None
            continue
        if isinstance(pack, CompanyPack):
            out.extend(pack.vocabulary().get(concept, ()))
    return tuple(dict.fromkeys(out))


def spliced(concept: str, words: Sequence[str]) -> tuple[str, ...]:
    """``words`` with each :data:`PACK_WORDS` replaced by the packs' words: the first by ``concept``'s, the n-th by
    "<concept>:<n>"'s. Keeps an ordered list's order where the order matters (the first heading found wins)."""
    out: list[str] = []
    n = 0
    for word in words:
        if word == PACK_WORDS:
            n += 1
            out.extend(pack_words(concept if n == 1 else f"{concept}:{n}"))
        else:
            out.append(word)
    return tuple(dict.fromkeys(out))


def pack_alternatives(concept: str) -> str:
    """The packs' entries for ``concept`` as regular-expression alternatives ("a|b|c"); never matches when none."""
    return "|".join(pack_words(concept)) or _NEVER


def pack_text(concept: str, default: str = "") -> str:
    """The first pack's text for ``concept`` (a phrase or a template), or ``default``."""
    words = pack_words(concept)
    return words[0] if words else default


class LazyPattern:
    """A regular expression compiled on first use from ``build()`` (the pattern's source, which may ask the packs).

    It answers like the compiled ``re.Pattern`` (``search``, ``match``, ``finditer``, ``sub``, ``pattern`` ...)."""

    __slots__ = ("_build", "_compiled", "_flags")

    def __init__(self, build: Callable[[], str], flags: int | re.RegexFlag = 0) -> None:
        self._build = build
        self._flags = flags
        self._compiled: re.Pattern[str] | None = None

    def compiled(self) -> re.Pattern[str]:
        if self._compiled is None:
            self._compiled = re.compile(self._build(), self._flags)
        return self._compiled

    def __getattr__(self, name: str) -> Any:
        return getattr(self.compiled(), name)

    def __repr__(self) -> str:
        return f"LazyPattern({self.compiled().pattern!r})"
