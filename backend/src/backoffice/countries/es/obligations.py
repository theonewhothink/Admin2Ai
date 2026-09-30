"""What Spanish letters say, and the deadlines Spain's calendar sets (§24 for Spain).

**Wording** (folded phrases by category, added to the core's English and Portuguese ones by
:mod:`backoffice.closure.obligations`): the Agencia Tributaria and the Seguridad Social, "plazo",
"importe a ingresar", "modelo 303", "justificante de presentación"... Unverified letter
conventions (verified_as_of: never); extend as letters are seen.

**Quarterly VAT return (modelo 303)**: every company that charges IVA files it each quarter, from
the 1st to the 20th of April, July and October, and from the 1st to the 30th of January for the
fourth quarter (Orden HAC/3625/2003 as amended; Reglamento del IVA art. 71). A deadline that falls
on a Saturday or Sunday moves to the Monday; public holidays are not encoded, so the obligation
says the date may move. It is the accountant's to file (§25), proven by the filing receipt.
verified_as_of: 2026-09 (author knowledge, not re-checked online).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from types import MappingProxyType

from backoffice.countries.base import PeriodicObligation

__all__ = ["VAT_RETURN_TITLE", "VOCABULARY", "quarterly_vat_return", "vat_return_due"]

VAT_RETURN_TITLE = "Quarterly VAT return (modelo 303)"
_VAT_RETURN = "vat_return"

VOCABULARY: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "issuer:tax_authority": ("agencia tributaria", "agencia estatal de administracion tributaria", "aeat",
                             "hacienda", "sede electronica de la agencia tributaria"),
    "issuer:social_security": ("seguridad social", "tesoreria general de la seguridad social", "tgss"),
    "issuer:bank": ("su banco", "su entidad bancaria"),
    "issuer:landlord": ("arrendador", "casero", "contrato de alquiler", "contrato de arrendamiento"),
    "issuer:insurer": ("aseguradora", "compania de seguros"),
    "kyc": ("conozca a su cliente", "actualizacion de datos", "actualice sus datos", "verificacion de identidad",
            "titular real"),
    "payment": ("pago", "pagar", "a pagar", "a ingresar", "importe a ingresar", "ingreso", "domiciliacion",
                "liquidacion", "cuota"),
    "filing": ("declaracion", "autoliquidacion", "presentacion de la declaracion", "modelo 303", "modelo 111",
               "modelo 115", "modelo 130", "modelo 390", "modelo 200"),
    "vat_return": ("modelo 303", "declaracion trimestral de iva", "declaracion trimestral del iva",
                   "autoliquidacion del iva", "autoliquidacion de iva", "iva trimestral"),
    "request": ("requerimiento", "notificacion", "solicitamos", "aportar", "documentacion requerida",
                "tramite de audiencia"),
    "debt": ("reclamacion de deuda", "deuda pendiente", "via de apremio", "providencia de apremio", "embargo",
             "ultimo aviso", "recobro"),
    "renewal": ("renovacion", "renovar", "se renueva", "caduca", "caducidad", "vencimiento de la poliza"),
    "insurance": ("seguro", "poliza", "aseguradora"),
    "license": ("licencia", "licencia de actividad", "permiso"),
    "rent": ("alquiler", "renta mensual", "arrendamiento", "arrendador"),
    "bank_request": ("documentacion", "documentos pendientes", "solicitud de documentacion"),
    "payment_deadline": ("fecha limite de pago", "pagar antes del", "vence el", "fecha de cargo"),
    "auto_renew": ("se renovara automaticamente", "renovacion automatica", "renovacion tacita"),
    "strong_date": ("plazo", "fecha limite", "plazo de presentacion", "plazo de pago", "hasta el dia",
                    "hasta el", "antes del", "fecha de vencimiento", "vence el"),
    "weak_date": ("hasta", "antes de"),
    "strong_amount": ("importe a ingresar", "total a ingresar", "importe a pagar", "total a pagar",
                      "importe pendiente", "deuda pendiente"),
    "weak_amount": ("importe", "cuota", "total"),
    "submitted": ("justificante de presentacion", "presentacion realizada", "declaracion presentada",
                  "ha sido presentada", "presentada correctamente", "presentacion correcta"),
    "answered": ("hemos recibido su documentacion", "documentacion recibida", "hemos recibido los documentos",
                 "tramite finalizado", "datos actualizados"),
    "still_asking": ("todavia necesitamos", "aun necesitamos", "falta aportar", "le rogamos aporte",
                     "si no paga", "en caso de no"),
    "renewed": ("ha sido renovada", "ha sido renovado", "renovada hasta", "renovado hasta"),
    "decided": ("ha sido cancelada", "ha sido cancelado", "baja confirmada", "no se renovara"),
    "title:vat_return": (VAT_RETURN_TITLE,),
})  # fmt: skip


def _weekday(day: date) -> date:
    while day.weekday() >= 5:  # Saturday or Sunday: the next Monday
        day += timedelta(days=1)
    return day


def vat_return_due(year: int, quarter: int) -> date:
    """The last day to file the modelo 303 for one quarter (weekends moved to Monday)."""
    if quarter not in (1, 2, 3, 4):
        raise ValueError("a quarter is 1 to 4")
    if quarter == 4:
        return _weekday(date(year + 1, 1, 30))
    return _weekday(date(year, 3 * quarter + 1, 20))


def quarterly_vat_return(company_id: str, today: date) -> tuple[PeriodicObligation, ...]:
    """The modelo 303 of the last quarter that ended, while its filing window is still ahead or open.

    Nothing before the filing window opens, and nothing for a quarter whose deadline already
    passed (a company set up late is not told it missed a return it may well have filed).
    """
    quarter = (today.month - 1) // 3  # the last quarter that ended (0: the fourth of last year)
    year = today.year if quarter else today.year - 1
    quarter = quarter or 4
    due = vat_return_due(year, quarter)
    if today > due:
        return ()
    period = f"{year}-Q{quarter}"
    names = {1: "January to March", 2: "April to June", 3: "July to September", 4: "October to December"}
    return (PeriodicObligation(
        key=f"es-303-{company_id}-{period}", kind=_VAT_RETURN, title=VAT_RETURN_TITLE, period=period, due_on=due,
        responsible="accountant", consequence="Filing late may lead to a fine and a surcharge.",
        required_evidence="The filing receipt for the modelo 303.",
        reasons=(f"VAT for {names[quarter]} {year}", "Filed every quarter in Spain",
                 "If the last day is a public holiday, the deadline moves to the next working day."),
    ),)
