"""Spanish invoice QR codes: Verifactu and TicketBAI (§13 Stage 0, §19 for Spain).

**Verifactu** (Real Decreto 1007/2023, Orden HAC/1177/2024): every invoice issued with invoicing
software carries a QR code whose content is a URL on the tax agency's site::

    https://www2.agenciatributaria.gob.es/wlpl/TIKE-CONT/ValidarQR?nif=89890001K
        &numserie=12345678%26G33&fecha=01-09-2024&importe=241.4

``nif`` is the issuer's NIF, ``numserie`` the series and number (URL-encoded), ``fecha`` the issue
date (dd-mm-aaaa) and ``importe`` the total with a point for decimals. ``ValidarQRNoVerifactu`` is
the same for software that keeps its records itself; ``prewww2.aeat.es`` is the test site.

**TicketBAI** (the Basque provinces: Bizkaia, Gipuzkoa, Araba)::

    https://batuz.eus/QRTBAI/?id=TBAI-00000006Y-251019-btFpwP8dcLGAF-237&s=T86&nf=270&i=4.70&cr=007

``id`` carries the issuer's NIF and the issue date (ddmmyy), ``s`` the series, ``nf`` the number,
``i`` the total and ``cr`` a CRC-8 of the rest (not checked here: reported in the notes).

Neither code gives the VAT, the buyer or the kind of document, so the document's own words still
decide those; the code is one independent source for the issuer, number, date and total (§18).
A code that claims to be one of these but is malformed raises :class:`ESQRError`.
verified_as_of: 2026-09 (author knowledge of the published specifications).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, unquote, urlsplit

from backoffice.countries.base import FiscalQRError, NamedObservation
from backoffice.domain.models import CriticalField, ExtractionMethod

from . import nif as es_nif

__all__ = ["ESQRCode", "ESQRError", "find_qr_url", "looks_like_es_qr", "parse_es_qr", "qr_to_observations"]

_VERIFACTU_HOSTS = ("www2.agenciatributaria.gob.es", "prewww2.aeat.es", "www2.aeat.es", "www1.agenciatributaria.gob.es")
_VERIFACTU_PATH = re.compile(r"^/wlpl/TIKE-CONT/ValidarQR(?:NoVerifactu)?/?$", re.I)
_TBAI_HOSTS = ("batuz.eus", "tbai.egoitza.gipuzkoa.eus", "ticketbai.araba.eus", "tbai.prep.gipuzkoa.eus",
               "pruebas-ticketbai.araba.eus", "tbai.egoitza.gipuzkoa.net")
_URL = re.compile(
    r"https?://(?:" + "|".join(re.escape(h) for h in (*_VERIFACTU_HOSTS, *_TBAI_HOSTS)) + r")/[^\s\"'<>]+", re.I)
_TBAI_ID = re.compile(r"^TBAI-([0-9A-Z]{9})-(\d{2})(\d{2})(\d{2})-[0-9A-Za-z]{13}-\d{3}$")
_CONFIDENCE = 0.95  # structured data the issuer's certified software printed


class ESQRError(FiscalQRError):
    """A Verifactu or TicketBAI code that cannot be read."""


@dataclass(frozen=True)
class ESQRCode:
    system: str  # "verifactu" | "ticketbai"
    issuer_nif: str
    number: str
    issue_date: date
    total: Decimal
    url: str
    notes: tuple[str, ...] = ()

    @property
    def currency(self) -> str:
        return "EUR"  # both systems state amounts in euro


def find_qr_url(line: str) -> str | None:
    """The Verifactu or TicketBAI URL in a decoded-QR text line, or None."""
    m = _URL.search(line or "")
    if m is None:
        return None
    url = m.group(0).rstrip(".,;)")
    return url if looks_like_es_qr(url) else None


def looks_like_es_qr(payload: str) -> bool:
    """Cheap sniff: a URL on the tax agency's QR check page, or on a TicketBAI one."""
    try:
        parts = urlsplit((payload or "").strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if host in _VERIFACTU_HOSTS:
        return bool(_VERIFACTU_PATH.match(parts.path))
    return host in _TBAI_HOSTS and "id=" in parts.query


def _one(query: dict[str, list[str]], key: str) -> str:
    values = [v for v in query.get(key, []) if v.strip()]
    if len(values) != 1:
        raise ESQRError(f"the code has {len(values)} values for {key!r}")
    return values[0].strip()


def _amount(raw: str) -> Decimal:
    try:
        value = Decimal(raw.replace(",", "."))
    except InvalidOperation:
        raise ESQRError(f"the total {raw!r} is not a number") from None
    if not value.is_finite():
        raise ESQRError(f"the total {raw!r} is not a number")
    return value


def _nif(raw: str) -> str:
    check = es_nif.validate_nif(raw)
    if not check.valid or check.normalized is None:
        raise ESQRError("the issuer's NIF in the code is not valid")
    return check.normalized


def parse_es_qr(payload: str) -> ESQRCode:
    """Read a Verifactu or TicketBAI code; ESQRError when it is one but cannot be read."""
    url = (payload or "").strip()
    if not looks_like_es_qr(url):
        raise ESQRError("not a Verifactu or TicketBAI code")
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=False)
    host = (parts.hostname or "").lower()
    if host in _VERIFACTU_HOSTS:
        day = _one(query, "fecha")
        m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", day)
        if m is None:
            raise ESQRError(f"the date {day!r} is not dd-mm-yyyy")
        try:
            issued = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            raise ESQRError(f"the date {day!r} does not exist") from None
        number = unquote(_one(query, "numserie")).strip()
        return ESQRCode("verifactu", _nif(_one(query, "nif")), number, issued, _amount(_one(query, "importe")), url,
                        ("verifactu:not_verifiable" if "NoVerifactu" in parts.path else "verifactu",))
    tbai = _TBAI_ID.match(_one(query, "id"))
    if tbai is None:
        raise ESQRError("the TicketBAI id is malformed")
    try:
        issued = date(2000 + int(tbai.group(4)), int(tbai.group(3)), int(tbai.group(2)))
    except ValueError:
        raise ESQRError("the TicketBAI id's date does not exist") from None
    series = query.get("s", [""])[0].strip()
    number = _one(query, "nf")
    return ESQRCode("ticketbai", _nif(tbai.group(1)), f"{series}-{number}" if series else number, issued,
                    _amount(_one(query, "i")), url, ("ticketbai:crc_not_checked",))


def qr_to_observations(code: ESQRCode, evidence_id: str) -> list[NamedObservation]:
    where = f"qr:{code.system}"

    def obs(field: CriticalField, value: object, what: str) -> NamedObservation:
        return NamedObservation(field=field, value=value, source=evidence_id, method=ExtractionMethod.QR,
                                confidence=_CONFIDENCE, location=f"{where}:{what}")

    return [obs(CriticalField.SUPPLIER_TAX_ID, code.issuer_nif, "nif"),
            obs(CriticalField.INVOICE_NUMBER, code.number, "number"),
            obs(CriticalField.ISSUE_DATE, code.issue_date, "date"),
            obs(CriticalField.GROSS_AMOUNT, code.total, "total")]
