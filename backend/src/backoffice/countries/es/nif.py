"""Spanish tax numbers: NIF (DNI), NIE and CIF, with their check characters (§50 for Spain).

* **DNI / NIF of a Spanish person**: 8 digits and a control letter,
  ``"TRWAGMYFPDXBNJZSQVHLCKE"[number % 23]`` ("12345678Z").
* **NIE** (a foreign resident): X, Y or Z, 7 digits and the same letter, X/Y/Z
  standing for 0/1/2 ("X1234567L").
* **K, L, M** (people without a DNI: under 14, non-residents, foreigners without a
  NIE): the letter, 7 digits and the same control letter over the 7 digits.
* **CIF / NIF of an entity**: a letter for the kind of entity, 7 digits and a
  control character. The digits in odd positions are doubled (digits of the
  product added), the even ones added; the control is ``(10 - total % 10) % 10``,
  printed as that digit or as ``"JABCDEFGHI"[control]``. Entities of kinds P, Q, R,
  S, N and W print the letter; A, B, E and H the digit; the others either.

Source: Orden EHA/451/2008 (entity letters) and the Ministry of the Interior's DNI
control letter; verified_as_of: 2026-09 (author knowledge, not re-checked online).
Owner-facing messages are plain English (§36, §70).
"""

from __future__ import annotations

import re

from backoffice.countries.base import TaxIdCheck, TaxIdKind

__all__ = ["cif_control", "dni_letter", "entity_kind_hint", "is_valid_nif", "normalize_nif", "validate_nif"]

_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
_CIF_LETTERS = "JABCDEFGHI"
_ENTITY_LETTERS = "ABCDEFGHJNPQRSUVW"
_LETTER_ONLY = frozenset("PQRSNW")  # these kinds print the control as a letter
_DIGIT_ONLY = frozenset("ABEH")  # these print it as a digit

# What the first letter of a CIF says about who holds it.
_ENTITY_KINDS: dict[str, tuple[TaxIdKind, str]] = {
    "A": (TaxIdKind.COMPANY, "public limited company (S.A.)"),
    "B": (TaxIdKind.COMPANY, "limited company (S.L.)"),
    "C": (TaxIdKind.COMPANY, "general partnership"),
    "D": (TaxIdKind.COMPANY, "limited partnership"),
    "E": (TaxIdKind.OTHER_ENTITY, "joint ownership (comunidad de bienes)"),
    "F": (TaxIdKind.COMPANY, "cooperative"),
    "G": (TaxIdKind.OTHER_ENTITY, "association or foundation"),
    "H": (TaxIdKind.OTHER_ENTITY, "owners' association"),
    "J": (TaxIdKind.COMPANY, "civil partnership"),
    "N": (TaxIdKind.NON_RESIDENT, "foreign entity"),
    "P": (TaxIdKind.PUBLIC_BODY, "local public body"),
    "Q": (TaxIdKind.PUBLIC_BODY, "public body"),
    "R": (TaxIdKind.OTHER_ENTITY, "religious body"),
    "S": (TaxIdKind.PUBLIC_BODY, "state administration body"),
    "U": (TaxIdKind.COMPANY, "temporary business association (UTE)"),
    "V": (TaxIdKind.OTHER_ENTITY, "other entity"),
    "W": (TaxIdKind.NON_RESIDENT, "permanent establishment of a non-resident"),
}

_WRONG = "That NIF doesn't look right. Please check it."
_SHAPE = "That doesn't look like a Spanish NIF, NIE or CIF. It has 9 characters, like B12345674 or 12345678Z."


def normalize_nif(raw: str | None) -> str | None:
    """"ES B-1234567-4" -> "B12345674"; None when it cannot be a Spanish tax number."""
    if not raw:
        return None
    compact = re.sub(r"[\s.\-/]", "", str(raw)).upper()
    if compact.startswith("ES") and len(compact) == 11:
        compact = compact[2:]
    if not re.fullmatch(r"[0-9A-Z]{9}", compact):
        return None
    return compact


def dni_letter(number: int) -> str:
    """The control letter of a DNI or NIE number."""
    return _LETTERS[number % 23]


def cif_control(digits: str) -> int:
    """The control number (0-9) of a CIF's seven digits."""
    total = 0
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == 0:  # positions 1, 3, 5, 7: doubled, digits added
            d = sum(divmod(d * 2, 10))
        total += d
    return (10 - total % 10) % 10


def _check(n: str) -> tuple[bool, TaxIdKind, str]:
    """(valid, kind, category) for a normalized 9-character number."""
    if n[:8].isdigit() and n[8].isalpha():
        return dni_letter(int(n[:8])) == n[8], TaxIdKind.PERSON, "DNI"
    if n[0] in "XYZ" and n[1:8].isdigit() and n[8].isalpha():
        return dni_letter(int(str("XYZ".index(n[0])) + n[1:8])) == n[8], TaxIdKind.PERSON, "NIE"
    if n[0] in "KLM" and n[1:8].isdigit() and n[8].isalpha():
        return dni_letter(int(n[1:8])) == n[8], TaxIdKind.PERSON, "NIF"
    if n[0] in _ENTITY_LETTERS and n[1:8].isdigit() and n[8].isalnum():
        control = cif_control(n[1:8])
        letter, digit = _CIF_LETTERS[control], str(control)
        if n[0] in _LETTER_ONLY:
            ok = n[8] == letter
        elif n[0] in _DIGIT_ONLY:
            ok = n[8] == digit
        else:
            ok = n[8] in (letter, digit)
        kind, category = _ENTITY_KINDS[n[0]]
        return ok, kind, category
    return False, TaxIdKind.UNKNOWN, ""


def validate_nif(raw: str | None) -> TaxIdCheck:
    """Check a Spanish NIF, NIE or CIF by its format and control character."""
    text = str(raw or "")
    n = normalize_nif(text)
    if n is None:
        return TaxIdCheck(raw=text, normalized=None, valid=False, problem="format", message=_SHAPE)
    ok, kind, category = _check(n)
    if kind is TaxIdKind.UNKNOWN:
        return TaxIdCheck(raw=text, normalized=None, valid=False, problem="format", message=_SHAPE)
    if not ok:
        return TaxIdCheck(raw=text, normalized=None, valid=False, kind=kind, category=category,
                          problem="check_character", message=_WRONG)
    return TaxIdCheck(raw=text, normalized=n, valid=True, kind=kind, category=category)


def is_valid_nif(raw: str | None) -> bool:
    return validate_nif(raw).valid


def entity_kind_hint(raw: str | None) -> TaxIdKind:
    """Who holds the number (person, company, public body ...), from its shape alone."""
    n = normalize_nif(raw)
    return _check(n)[1] if n else TaxIdKind.UNKNOWN
