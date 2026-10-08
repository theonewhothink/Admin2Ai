"""Suspicious payment-instruction language, English and the packs' (§26).

Classic invoice-redirection fraud reads the same everywhere: new bank
details, urgency, secrecy. A country's own phrases ("alteração de IBAN") are in its pack
(``fraud.<category>``, backoffice.countries.wording). Text is accent-folded and lower-cased before
matching, so "Alteração de IBAN", "ALTERACAO DE IBAN" and "alteração do iban"
all match; invisible characters are dropped and Cyrillic/Greek letters that
look Latin ("nеw bаnk details" with Cyrillic е, а) are read as the Latin
letters they imitate, so neither can hide a phrase. These are wording heuristics, not proof; the engine decides how
much weight each category carries.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from backoffice.countries import LazyPattern, pack_words
from backoffice.learning.keys import fold

from .domains import CROSS_SCRIPT_LETTERS

__all__ = ["PhraseCategory", "PhraseHit", "find_suspicious_phrases"]


class PhraseCategory(str, Enum):
    BANK_CHANGE = "bank_change"  # new or changed bank details
    URGENCY = "urgency"
    SECRECY = "secrecy"


_PATTERNS: dict[PhraseCategory, tuple[str, ...]] = {
    PhraseCategory.BANK_CHANGE: (
        r"new bank (?:details|account|information|info)",
        r"(?:updated|new|changed|different|alternative) "
        r"(?:bank(?:ing)? (?:details|account|information)|account (?:details|number)|payment details)",
        r"change (?:of|in|to) (?:our )?bank(?:ing)? (?:details|account|information)",
        r"(?:our|the) bank(?:ing)? (?:details|account) (?:has|have) (?:changed|been updated)",
        r"(?:changed|switched|moved) (?:our )?(?:banks?|banking provider)\b(?! holiday)",
        r"new iban",
        r"iban (?:has|was) (?:changed|updated)",
        r"(?:do not|don't|please don't|please do not) (?:pay|use) (?:into |to )?(?:the |our )?"
        r"old (?:account|iban|bank)",
        r"update (?:your|the) (?:payment|bank(?:ing)?|vendor|supplier) (?:details|information)",
        r"update your records with (?:our|the) new (?:bank|iban|account)",
    ),
    PhraseCategory.URGENCY: (
        r"urgent(?:ly)?",
        r"immediate(?:ly)?",
        r"without delay",
        r"as soon as possible|asap",
        r"right away",
        r"by (?:the )?end of (?:the )?day",
    ),
    PhraseCategory.SECRECY: (
        r"confidential(?:ly)?",
        r"keep (?:this|it) (?:between us|quiet|private)",
        r"(?:do not|don't) (?:call|phone|contact|discuss|share|tell)",
        r"discreet(?:ly)?",
    ),
}


def _pattern(category: PhraseCategory) -> LazyPattern:
    """The core's English phrases of ``category``, then the packs' ("fraud.bank_change": Portugal's "novo IBAN")."""
    return LazyPattern(lambda: r"\b(?:" + "|".join((*_PATTERNS[category], *pack_words(f"fraud.{category.value}")))
                       + r")\b")


_COMPILED: dict[PhraseCategory, LazyPattern] = {category: _pattern(category) for category in _PATTERNS}


@dataclass(frozen=True)
class PhraseHit:
    category: PhraseCategory
    phrase: str  # as matched in the folded text


def find_suspicious_phrases(text: str) -> list[PhraseHit]:
    """Every suspicious phrase in ``text``, grouped by category, in order of appearance."""
    folded = fold(text or "").translate(CROSS_SCRIPT_LETTERS)
    hits: list[PhraseHit] = []
    for category, pattern in _COMPILED.items():
        seen: set[str] = set()
        for match in pattern.finditer(folded):
            phrase = match.group(0)
            if phrase not in seen:
                seen.add(phrase)
                hits.append(PhraseHit(category, phrase))
    return hits
