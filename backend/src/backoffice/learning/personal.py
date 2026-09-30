"""Purchases that are clearly personal for a company (checklist X23).

A company card is strong evidence that a purchase belongs to that company, but
some shops are almost never a business cost: a streaming subscription, a
supermarket for a business that does not sell food, a clothes shop for a
business that does not deal in clothes. Such a purchase, with no history of
being the company's, becomes one plain question ("Was this Netflix payment for
Hazel Tree or personal?") instead of being booked to the company on the card's
word alone.

Only strong signals count. A merchant is flagged only when its name matches the
lists below as whole words, never for a supplier the business already knows,
and never for a sector that buys there for work (a café buys groceries, a
boutique buys clothes). Groceries are flagged only when the company's sector is
known and has nothing to do with food: without the sector a supermarket is not a
strong signal.

The lists are public brand names as they appear on Portuguese and European card
statements. They are conventions, not facts about any business (verified_as_of:
never against live card feeds); extend them as feeds are seen.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .keys import fold

__all__ = ["PERSONAL_MERCHANTS", "personal_signal"]

# Category -> (plain words for the owner, brand names as whole words, folded).
PERSONAL_MERCHANTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "streaming": ("a streaming service", (
        "netflix", "spotify", "disney plus", "disneyplus", "hbo max", "hbomax", "prime video", "primevideo",
        "apple tv", "dazn", "youtube premium", "crunchyroll", "deezer", "skyshowtime", "sky showtime",
        "paramount plus", "filmin",
    )),
    "fashion": ("a clothes shop", (
        "zara", "primark", "pull bear", "bershka", "stradivarius", "massimo dutti", "shein", "zalando",
        "uniqlo", "lefties", "oysho", "springfield", "cortefiel", "kiabi", "mango",
    )),
    "groceries": ("a supermarket", (
        "continente", "pingo doce", "lidl", "aldi", "minipreco", "intermarche", "mercadona", "auchan",
        "carrefour", "froiz", "tesco", "sainsbury", "sainsburys", "coviran",
    )),
}

# Sectors that buy in these shops for work: the purchase is then an ordinary business cost.
_BUSINESS_SECTORS: dict[str, tuple[str, ...]] = {
    "streaming": ("media", "film", "cinema", "music", "entertainment", "broadcast", "production"),
    "fashion": ("fashion", "clothing", "clothes", "apparel", "textile", "boutique", "stylist", "costume"),
    "groceries": ("food", "restaurant", "cafe", "coffee", "bar", "catering", "bakery", "grocery", "hotel",
                  "hospitality", "kitchen", "pastry", "takeaway"),
}
_H_AND_M = re.compile(r"(?<![a-z0-9])h\s*&\s*m(?![a-z0-9])")
_NON_WORD = re.compile(r"[^a-z0-9&]+")


def _words(text: str) -> str:
    return " " + " ".join(_NON_WORD.sub(" ", fold(text)).split()) + " "


def _mentions(words: str, brands: Iterable[str]) -> bool:
    return any(f" {brand} " in words for brand in brands)


def _sector_buys_there(category: str, sector: str | None) -> bool:
    folded = _words(sector or "")
    return any(f" {word} " in folded or f" {word}s " in folded for word in _BUSINESS_SECTORS[category])


def personal_signal(counterparty: str, description: str = "", *, sector: str | None = None,
                    known_supplier: bool = False) -> str | None:
    """The kind of shop, in plain words ("a streaming service"), when a purchase there is clearly personal
    for a company in ``sector``; None otherwise (module docstring).

    ``known_supplier``: the business already knows this merchant as one of its suppliers, so it is never
    flagged. ``sector``: the company's line of business in plain words ("interior design", "café"), or None
    when it is not known.
    """
    if known_supplier:
        return None
    text = f"{counterparty} {description}"
    words = _words(text)
    for category, (phrase, brands) in PERSONAL_MERCHANTS.items():
        hit = _mentions(words, brands) or (category == "fashion" and _H_AND_M.search(fold(text)) is not None)
        if not hit:
            continue
        if category == "groceries" and not (sector or "").strip():
            return None  # without the company's sector a supermarket is not a strong signal
        if _sector_buys_there(category, sector):
            return None
        return phrase
    return None
