"""Banks the owner may name or paste an account of, for "Something missing?" on Sources (backoffice.source_intake).

Neutral data, not one country's wording: banks' own brand names (as the owner writes them) and where each one sits
in the IBANs of the countries it serves. An IBAN carries its bank's national code right after the country and check
digits (ISO 13616: Portugal's 4 digits, Spain's 4, Lithuania's 5, Germany's 8, Israel's 3, Ireland's and the UK's
4 letters of the bank's BIC, Belgium's 3), so ``PT50 0033 …`` is a Millennium BCP account.

Honesty: only banks whose national code is published by the country's central bank or the bank itself are listed;
any other IBAN gives no bank name and the owner is asked which bank it is, never a guess.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = ["KnownBank", "BANKS", "bank_for_iban", "banks_named_in", "fold"]


@dataclass(frozen=True)
class KnownBank:
    name: str  # as the owner reads it ("Millennium BCP")
    names: tuple[str, ...]  # how people write it, folded (lower case, no accents), whole words
    codes: tuple[tuple[str, str], ...] = ()  # (IBAN country, the bank's national code in the IBAN)


BANKS: tuple[KnownBank, ...] = (
    # Portugal: the Banco de Portugal's 4-digit bank codes.
    KnownBank("Millennium BCP", ("millennium bcp", "millennium", "millenium", "bcp"), (("PT", "0033"),)),
    KnownBank("Caixa Geral de Depósitos", ("caixa geral de depositos", "caixa geral", "cgd"), (("PT", "0035"),)),
    KnownBank("Novo Banco", ("novo banco", "novobanco"), (("PT", "0007"),)),
    KnownBank("Santander", ("santander totta", "santander"), (("PT", "0018"), ("ES", "0049"))),
    KnownBank("BPI", ("banco bpi", "bpi"), (("PT", "0010"),)),
    KnownBank("Montepio", ("banco montepio", "montepio"), (("PT", "0036"),)),
    KnownBank("ActivoBank", ("activobank", "activo bank"), (("PT", "0023"),)),
    KnownBank("Crédito Agrícola", ("credito agricola",), (("PT", "0045"),)),
    KnownBank("Bankinter", ("bankinter",), (("PT", "0269"), ("ES", "0128"))),
    KnownBank("Banco CTT", ("banco ctt",), (("PT", "0193"),)),
    # Spain: the Banco de España's 4-digit codes.
    KnownBank("CaixaBank", ("caixabank", "la caixa"), (("ES", "2100"),)),
    KnownBank("BBVA", ("bbva",), (("ES", "0182"),)),
    KnownBank("Banco Sabadell", ("banco sabadell", "sabadell"), (("ES", "0081"),)),
    KnownBank("Openbank", ("openbank",), (("ES", "0073"),)),
    KnownBank("ING", ("ing direct", "ing"), (("ES", "1465"),)),
    # Banks without branches, for businesses in several countries.
    KnownBank("Revolut", ("revolut business", "revolut"), (("LT", "32500"), ("IE", "REVO"), ("GB", "REVO"))),
    KnownBank("Wise", ("transferwise", "wise"), (("BE", "967"), ("GB", "TRWI"))),
    KnownBank("N26", ("n26",), (("DE", "10011001"),)),
    KnownBank("Qonto", ("qonto",)),
    KnownBank("Monzo", ("monzo",), (("GB", "MONZ"),)),
    KnownBank("bunq", ("bunq",)),
    # Israel: the Bank of Israel's 3-digit bank codes.
    KnownBank("Bank Leumi", ("bank leumi", "leumi"), (("IL", "010"),)),
    KnownBank("Bank Hapoalim", ("bank hapoalim", "hapoalim", "poalim"), (("IL", "012"),)),
    KnownBank("Discount Bank", ("israel discount bank", "discount bank"), (("IL", "011"),)),
    KnownBank("Mizrahi-Tefahot", ("mizrahi tefahot", "mizrahi", "tefahot"), (("IL", "020"),)),
    KnownBank("First International Bank", ("first international bank", "first international"), (("IL", "031"),)),
)

# Where the bank's code sits in each country's IBAN (after the 2 letters and 2 check digits), and how long it is.
_CODE_LENGTH = {"PT": 4, "ES": 4, "LT": 5, "DE": 8, "IL": 3, "IE": 4, "GB": 4, "BE": 3}


def fold(text: str) -> str:
    """Lower case, no accents, single spaces: how bank names are compared."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^0-9a-z]+", " ", plain.casefold()).split())


def bank_for_iban(iban: str) -> KnownBank | None:
    """The bank an IBAN (already without spaces, upper case) belongs to, or None when its code is not listed."""
    country = iban[:2]
    length = _CODE_LENGTH.get(country)
    if length is None or len(iban) < 4 + length:
        return None
    code = iban[4:4 + length]
    return next((b for b in BANKS if (country, code) in b.codes), None)


_NAMES = sorted(((name, bank) for bank in BANKS for name in bank.names), key=lambda p: -len(p[0]))


def banks_named_in(text: str) -> list[KnownBank]:
    """The banks ``text`` names (whole words, any case or accents), longest names first, each once."""
    folded = f" {fold(text)} "
    found: list[KnownBank] = []
    for name, bank in _NAMES:
        if f" {name} " in folded and bank not in found:
            found.append(bank)
            folded = folded.replace(f" {name} ", " ")
    return found
