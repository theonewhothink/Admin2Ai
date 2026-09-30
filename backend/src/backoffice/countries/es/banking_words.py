"""How Spanish bank statements word payments (§21, §49): the Spain pack's :class:`BankWording`.

Consecutive folded words (upper case, no accents), as the core's expected-evidence engine
(:mod:`backoffice.reconciliation.expected`) matches them on the bank lines of Spanish companies.
Bank-statement conventions compiled from public naming of the institutions, not legal facts; NOT verified
against live bank feeds (verified_as_of: never). Extend them as feeds are seen.
"""

from __future__ import annotations

from backoffice.countries.base import BankWording

__all__ = ["BANK_WORDING"]

BANK_WORDING = BankWording(
    # The Agencia Tributaria and the Seguridad Social named outright ("AEAT MODELO 303", "SEG SOCIAL TGSS").
    tax_authorities=(
        "AGENCIA TRIBUTARIA",
        "AGENCIA ESTATAL DE ADMINISTRACION TRIBUTARIA",
        "AEAT",
        "TESORERIA GENERAL DE LA SEGURIDAD SOCIAL",
        "TGSS",
        "SEGURIDAD SOCIAL",
        "SEG SOCIAL",
    ),
    # Tax abbreviations only count next to a word naming the state as payee ('HACIENDA IVA 3T').
    tax_words=frozenset({"IVA", "IRPF", "IBI", "IAE"}),
    state_words=frozenset({"HACIENDA", "IMPUESTO", "IMPUESTOS", "TRIBUTOS", "ESTADO"}),
    # Local tourist taxes (Catalonia's, the Balearic "ecotasa", checklist X26).
    tourist_tax=("IMPUESTO TURISTICO", "IMPUESTO ESTANCIAS TURISTICAS", "IMPUESTO SOBRE ESTANCIAS TURISTICAS",
                 "TASA TURISTICA", "ECOTASA"),
    # Grants and subsidies paid to the business (checklist X30).
    grants=("SUBVENCION", "SUBVENCIONES", "AYUDA FONDO PERDIDO", "AYUDAS FONDO PERDIDO", "KIT DIGITAL"),
    bank_fees=("COMISION", "COMISIONES", "COMISION MANTENIMIENTO", "COM MANTENIMIENTO", "INTERESES"),
    payroll_words=frozenset({"NOMINA", "NOMINAS", "SUELDO", "SUELDOS", "SALARIO", "SALARIOS"}),
    loans=("PRESTAMO", "CUOTA PRESTAMO", "AMORTIZACION PRESTAMO"),
)
