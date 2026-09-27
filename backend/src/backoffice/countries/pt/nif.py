"""Portuguese tax number (NIF / NIPC) handling (§4, §18, §50).

A NIF has 9 digits. The last one is a mod-11 check digit over the first 8
(weights 9..2; remainder 0 or 1 gives check digit 0, otherwise 11 - remainder).
The leading digit(s) say what kind of taxpayer holds it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backoffice.countries.base import TaxIdCheck, TaxIdKind

# Generic number printed on invoices issued to a final consumer without a NIF
# (the AT QR specification uses it for field B). Valid by check digit, but it
# never identifies a real taxpayer.
FINAL_CONSUMER_NIF = "999999990"


@dataclass(frozen=True)
class NIFPrefix:
    prefix: str
    kind: TaxIdKind
    category: str  # Portuguese category label


# Valid leading digits and what they mean.
# Source: AT numbering ranges as published on pt.wikipedia.org "Número de
# identificação fiscal" and AT/press notes on the "3" range for individuals
# (assigned since mid-2019). verified_as_of: 2026-09-27.
# "8" (old sole-trader numbers) is no longer issued but still appears on
# historical documents, so it is accepted and flagged as SOLE_TRADER.
NIF_PREFIXES: tuple[NIFPrefix, ...] = (
    NIFPrefix("1", TaxIdKind.PERSON, "Pessoa singular"),
    NIFPrefix("2", TaxIdKind.PERSON, "Pessoa singular"),
    NIFPrefix("3", TaxIdKind.PERSON, "Pessoa singular"),
    NIFPrefix("45", TaxIdKind.NON_RESIDENT, "Pessoa singular não residente"),
    NIFPrefix("5", TaxIdKind.COMPANY, "Pessoa coletiva"),
    NIFPrefix("6", TaxIdKind.PUBLIC_BODY, "Administração pública"),
    NIFPrefix("70", TaxIdKind.OTHER_ENTITY, "Herança indivisa"),
    NIFPrefix("71", TaxIdKind.NON_RESIDENT, "Pessoa coletiva não residente"),
    NIFPrefix("72", TaxIdKind.OTHER_ENTITY, "Fundo de investimento"),
    NIFPrefix("74", TaxIdKind.OTHER_ENTITY, "Herança indivisa"),
    NIFPrefix("75", TaxIdKind.OTHER_ENTITY, "Herança indivisa"),
    NIFPrefix("77", TaxIdKind.OTHER_ENTITY, "Atribuição oficiosa"),
    NIFPrefix("78", TaxIdKind.NON_RESIDENT, "Não residente (reembolso de IVA)"),
    NIFPrefix("79", TaxIdKind.OTHER_ENTITY, "Regime excecional"),
    NIFPrefix("8", TaxIdKind.SOLE_TRADER, "Empresário em nome individual (numeração antiga)"),
    NIFPrefix("90", TaxIdKind.OTHER_ENTITY, "Condomínio ou sociedade irregular"),
    NIFPrefix("91", TaxIdKind.OTHER_ENTITY, "Condomínio ou sociedade irregular"),
    NIFPrefix("98", TaxIdKind.NON_RESIDENT, "Não residente sem estabelecimento estável"),
    NIFPrefix("99", TaxIdKind.OTHER_ENTITY, "Sociedade civil sem personalidade jurídica"),
)

_WEIGHTS = (9, 8, 7, 6, 5, 4, 3, 2)
# Optional "PT" (VIES form), then digits with the usual printed separators.
_SHAPE = re.compile(r"^(?:PT[ \-]?)?([0-9 .\-]+)$", re.IGNORECASE)


def normalize_nif(raw: str) -> str | None:
    """"PT 123 456 789" -> "123456789". None when it is not 9 digits.

    Only formatting is removed (spaces, dots, dashes, a leading "PT"); the
    check digit is not verified here.
    """
    if not isinstance(raw, str):
        return None
    match = _SHAPE.match(" ".join(raw.split()))
    if match is None:
        return None
    digits = re.sub(r"[ .\-]", "", match.group(1))
    return digits if len(digits) == 9 and digits.isdigit() else None


def nif_check_digit(first_eight: str) -> int:
    """Mod-11 check digit for the first 8 digits of a NIF."""
    if len(first_eight) != 8 or not (first_eight.isascii() and first_eight.isdigit()):
        raise ValueError("expected exactly 8 digits")
    remainder = sum(int(d) * w for d, w in zip(first_eight, _WEIGHTS)) % 11
    return 0 if remainder < 2 else 11 - remainder


def nif_prefix(nif: str) -> NIFPrefix | None:
    """The numbering range a 9-digit NIF belongs to, or None if unassigned."""
    for entry in sorted(NIF_PREFIXES, key=lambda p: -len(p.prefix)):
        if nif.startswith(entry.prefix):
            return entry
    return None


def entity_kind_hint(raw: str) -> TaxIdKind:
    """Person vs company (and other kinds) from the leading digits."""
    nif = normalize_nif(raw)
    if nif is None:
        return TaxIdKind.UNKNOWN
    if nif == FINAL_CONSUMER_NIF:
        return TaxIdKind.PLACEHOLDER
    entry = nif_prefix(nif)
    return entry.kind if entry else TaxIdKind.UNKNOWN


def is_final_consumer(raw: str) -> bool:
    return normalize_nif(raw) == FINAL_CONSUMER_NIF


def validate_nif(raw: str, *, allow_placeholder: bool = False) -> TaxIdCheck:
    """Validate shape, leading digits and check digit.

    The final-consumer placeholder is rejected unless ``allow_placeholder``:
    it is correct on a consumer receipt but never a business's own NIF.
    """
    text = raw if isinstance(raw, str) else ""
    if not text.strip():
        return _invalid(text, None, "empty", "I need the NIF. It has 9 digits.")
    nif = normalize_nif(text)
    if nif is None:
        digits = re.sub(r"[^0-9]", "", text)
        if _SHAPE.match(" ".join(text.split())) is None:
            return _invalid(text, None, "characters", "A NIF only has digits. Please check it.")
        return _invalid(
            text, None, "length", f"That NIF has {len(digits)} digits. It needs 9."
        )
    entry = nif_prefix(nif)
    if entry is None:
        return _invalid(
            text, nif, "prefix", "That NIF doesn't look right. Please check the first digits."
        )
    if int(nif[8]) != nif_check_digit(nif[:8]):
        return _invalid(
            text, nif, "check_digit", "That NIF doesn't add up. Please check the digits."
        )
    if nif == FINAL_CONSUMER_NIF:
        if not allow_placeholder:
            return _invalid(
                text,
                nif,
                "placeholder",
                "That's the generic number used when there is no NIF. I need the real one.",
                kind=TaxIdKind.PLACEHOLDER,
            )
        return TaxIdCheck(
            raw=text,
            normalized=nif,
            valid=True,
            kind=TaxIdKind.PLACEHOLDER,
            category="Consumidor final",
        )
    return TaxIdCheck(raw=text, normalized=nif, valid=True, kind=entry.kind, category=entry.category)


def is_valid_nif(raw: str, *, allow_placeholder: bool = False) -> bool:
    return validate_nif(raw, allow_placeholder=allow_placeholder).valid


def _invalid(
    raw: str,
    normalized: str | None,
    problem: str,
    message: str,
    *,
    kind: TaxIdKind = TaxIdKind.UNKNOWN,
) -> TaxIdCheck:
    return TaxIdCheck(
        raw=raw, normalized=normalized, valid=False, kind=kind, problem=problem, message=message
    )
