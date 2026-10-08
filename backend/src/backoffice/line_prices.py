"""Prices on invoice lines: what each product cost per unit, per supplier, and when that jumped (QA X20).

A bakery's flour, a restaurant's olive oil, a workshop's steel: raw-material prices move all the time, and an
invoice's total says little about it. The lines say what went up. This module reads the lines of purchase
invoices:

* e-invoices (UBL): each line's quantity, unit, unit price, line total, VAT rate and the supplier's product
  code (:func:`backoffice.extraction.invoicelines.read_priced_lines`);
* the text of a Portuguese, Spanish or English invoice (a PDF's text layer, an uploaded text, an invoice written
  in an email body) where the lines are laid out as a table: description, quantity, unit, unit price, discount,
  VAT rate and line total, in the orders invoicing programs print them.

Robustly, never guessed. In a text a row is a line only when its own numbers hold (quantity × unit price, less a
printed discount, is the line total, to the rounding of the printed price). Then the lines together must add up,
to the cent, to the invoice's own net at each VAT rate: its e-invoice subtotals, its fiscal QR code, or the
"Base tributável (6%)" / "Base imponible" / "Net" lines it prints. Lines that do not add up are not used at all,
so no price is ever read from a table that was misread. An invoice whose prices include VAT (a shop's receipt) is
used when its lines add up to the gross at each rate; its prices are then taken net of that rate.

From the lines of every purchase invoice this keeps a price history per supplier and product. The same product
is recognised across invoices by the supplier's product code when the invoice prints one, otherwise by its
normalised description (accents, case, packaging words and pack sizes aside). Prices are compared per kilo or
per litre whenever the unit or the pack size says so ("Farinha T65 saco 25 kg" at €20.50 a bag is €0.82 a kilo),
otherwise per the unit printed.

An unusual change is a purchase whose unit price is :data:`UNUSUAL_CHANGE` (15%) or more above the average of
the previous :data:`COMPARE_LAST` (3) purchases of that product from that supplier; both are parameters. It is
one plain line where price increases already appear (Ask "what got more expensive?", the accountant's view, the
business audit), never a hold and never a question.

Nothing here changes a tenant: the history is read from the documents whenever it is asked for (a read), and
what was read from each document is only cached. Pure standard library: it runs in the browser build too.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import cache
from typing import Any

from backoffice.countries import LazyPattern, pack_alternatives, pack_words
from backoffice.domain.models import DocumentType, EvidenceFormat, VatPart
from backoffice.extraction.invoicelines import read_invoice_details, read_priced_lines
from backoffice.learning import counterparty_key, display_name, fold, format_money

__all__ = [
    "COMPARE_LAST",
    "InvoiceLines",
    "LinePrices",
    "PriceChange",
    "PriceLine",
    "Purchase",
    "UNUSUAL_CHANGE",
    "check_lines",
    "parse_number",
    "product_concepts",
    "product_key",
    "rate_parts_from_text",
    "read_text_lines",
    "summarise",
    "threshold_words",
    "unit_money",
]

UNUSUAL_CHANGE = Decimal("0.15")  # +15% or more against the average of the last purchases is unusual
COMPARE_LAST = 3  # how many earlier purchases of the product the latest is compared with
RECENT_DAYS = 90  # "what got more expensive?" looks at purchases of the last three months

_CENT = Decimal("0.01")
_ZERO = Decimal("0")
# Purchase documents whose lines are prices paid. A credit note gives money back, a pro-forma or a delivery note
# is not a purchase: none of them is a price.
_PRICED = frozenset({DocumentType.INVOICE, DocumentType.INVOICE_RECEIPT, DocumentType.SIMPLIFIED_INVOICE,
                     DocumentType.RECEIPT, DocumentType.DEBIT_NOTE})
# VAT rates a table may print without a % sign (EU standard, reduced and regional rates).
_PLAIN_RATES = frozenset({0, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 27})

# ---------------------------------------------------------------------------------------------------- units

# Units as printed (folded, without a final dot) -> the unit a price is kept in. A pack adds its own words
# ("line_prices.unit:<unit>", plain: Portugal's "caixa", "garrafa"), see _units().
_UNIT_WORDS: tuple[tuple[str, str], ...] = (
    ("kg", "kg kgs kilo kilos kilogramo kilogramos kilogram kilograms"),
    ("g", "g gr grs gramo gramos gram grams"),
    ("ton", "ton tons tonelada toneladas tonne tonnes"),
    ("l", "l lt lts ltr litro litros litre litres liter liters"),
    ("ml", "ml"),
    ("cl", "cl"),
    ("unit", "un und uni unid unids unidad ud uds unit units pc pcs pz pza pce each ea"),
    ("box", "caja cajas box boxes ctn"),
    ("bag", "sc saco sacos saca bag bags"),
    ("m", "m mt mts metro metros metre metres meter meters"),
    ("m2", "m2"),
    ("m3", "m3"),
    ("hour", "h hr hrs hora horas hour hours"),
    ("dozen", "docena docenas dozen"),
    ("pack", "pack packs pk paq paquete"),
    ("bottle", "botella botellas bottle bottles"),
    ("can", "lata latas can cans"),
)


@cache
def _units() -> dict[str, str]:
    out: dict[str, str] = {}
    for unit, words in _UNIT_WORDS:
        for word in (*words.split(), *pack_words(f"line_prices.unit:{unit}")):
            out[word] = unit
    return out

# UN/ECE Recommendation 20 unit codes an e-invoice uses -> the same units.
_UNIT_CODES = {"KGM": "kg", "GRM": "g", "TNE": "ton", "LTR": "l", "MLT": "ml", "CLT": "cl", "H87": "unit",
               "C62": "unit", "EA": "unit", "XPP": "unit", "PCE": "unit", "NAR": "unit", "NPR": "unit",
               "MTR": "m", "MTK": "m2", "MTQ": "m3", "HUR": "hour", "DZN": "dozen", "XBX": "box", "XCT": "box",
               "XBG": "bag", "XSA": "bag", "XPK": "pack", "XBO": "bottle", "XCX": "can"}

# Kilos or litres in one of each unit: prices are compared per kilo or per litre whenever they can be.
_MASS = {"kg": Decimal(1), "g": Decimal("0.001"), "ton": Decimal(1000)}
_VOLUME = {"l": Decimal(1), "ml": Decimal("0.001"), "cl": Decimal("0.01")}
_PER = {"kg": "per kg", "l": "per litre", "unit": "per unit", "box": "per box", "bag": "per bag", "m": "per metre",
        "m2": "per square metre", "m3": "per cubic metre", "hour": "per hour", "dozen": "per dozen",
        "pack": "per pack", "bottle": "per bottle", "can": "per can", "": "each"}

# A pack size printed in a description: "saco 25 kg", "25kg", "garrafa 0,75 l", "6 x 1,5 l".
_PACK = re.compile(r"(?<![\w.,])(?:(\d{1,3})\s?x\s?)?(\d+(?:[.,]\d+)?)\s?(kgs?|grs?|g|lts?|l|ml|cl)(?![\w])")
_PACK_UNITS = {"kg": "kg", "kgs": "kg", "g": "g", "gr": "g", "grs": "g", "l": "l", "lt": "l", "lts": "l",
               "ml": "ml", "cl": "cl"}
# Words that say how a product is packed, not what it is (and a pack's: "line_prices.packing").
_PACKING_WORDS = frozenset({"x", "granel", "paquete", "fardo", "fardos", "pallet"})
_STOP_WORDS = frozenset("de del la el los las y con with and of the para for a en in".split())  # + "line_prices.stop"

# The same raw material in the owner's words and on a Spanish or English invoice; a pack adds its own words for
# it ("line_prices.material:<concept>": Portugal's "farinha").
_CONCEPTS: dict[str, tuple[str, ...]] = {
    "flour": ("flour", "harina"),
    "sugar": ("sugar", "azucar"),
    "butter": ("butter", "mantequilla"),
    "milk": ("milk", "leche"),
    "eggs": ("egg", "eggs", "huevo", "huevos"),
    "yeast": ("yeast", "levadura"),
    "salt": ("salt", "sal"),
    "oil": ("oil", "aceite"),
    "coffee": ("coffee", "cafe"),
    "cheese": ("cheese", "queso"),
    "cream": ("cream", "nata"),
    "chocolate": ("chocolate", "cacao", "cocoa"),
    "rice": ("rice", "arroz"),
    "meat": ("meat", "carne"),
    "chicken": ("chicken", "pollo"),
    "fish": ("fish", "pescado"),
    "tomatoes": ("tomato", "tomatoes", "tomate", "tomates"),
    "potatoes": ("potato", "potatoes", "patata", "patatas"),
    "onions": ("onion", "onions", "cebolla", "cebollas"),
    "almonds": ("almond", "almonds", "almendra", "almendras"),
    "wine": ("wine", "vino"),
    "beer": ("beer", "cerveza"),
    "water": ("water", "agua"),
    "diesel": ("diesel", "gasoleo", "gasoil"),
    "petrol": ("petrol", "gasoline", "gasolina"),
    "paper": ("paper", "papel"),
    "cement": ("cement", "cemento"),
    "steel": ("steel", "acero"),
}


@cache
def _packing() -> frozenset[str]:
    return frozenset(set(_units()) - {"m", "h"}) | _PACKING_WORDS | frozenset(pack_words("line_prices.packing"))


@cache
def _stop() -> frozenset[str]:
    return _STOP_WORDS | frozenset(pack_words("line_prices.stop"))


@cache
def _concept_of() -> dict[str, str]:
    return {word: concept for concept, words in _CONCEPTS.items()
            for word in (*words, *pack_words(f"line_prices.material:{concept}"))}


# ---------------------------------------------------------------------------------------------------- results


@dataclass(frozen=True)
class PriceLine:
    """One invoice line that holds: ``quantity`` of ``unit`` at ``unit_price`` (net of VAT, after any discount)."""

    description: str
    quantity: Decimal
    unit: str | None  # "kg", "l", "unit", "bag", ... ; None when the invoice prints no unit
    unit_price: Decimal
    net: Decimal  # the line total, net of VAT
    vat_rate: Decimal | None
    code: str | None = None  # the supplier's product code
    trusted: bool = True  # False: counts in the invoice's sum, but its own price did not hold (never used)


@dataclass(frozen=True)
class InvoiceLines:
    """The lines of one invoice that add up to its net (or gross) at each VAT rate."""

    lines: tuple[PriceLine, ...]
    source: str  # "e-invoice" | "text"
    check: str  # plain: how they add up


@dataclass(frozen=True)
class Purchase:
    """One product bought on one invoice, at one unit price (lines of the same product on it together)."""

    supplier_key: str
    supplier: str  # display name
    product_key: str  # "code:FAR65" or "name:farinha trigo t65"
    product: str  # the description as printed on the latest line
    code: str | None
    on: date
    basis: str  # "kg" | "l" | a unit ("unit", "bag", ...) | "" when the invoice prints no unit
    unit_price: Decimal  # net, per ``basis``
    quantity: Decimal  # in ``basis``
    net: Decimal
    document_id: str
    evidence_id: str
    company_id: str | None
    invoice: str  # the invoice's number, or its date when it has none

    @property
    def per(self) -> str:
        return _PER.get(self.basis, f"per {self.basis}")

    @property
    def group(self) -> tuple[str, str, str]:
        return self.supplier_key, self.product_key, self.basis


@dataclass(frozen=True)
class PriceChange:
    """A purchase whose unit price is unusual against the average of the product's previous purchases."""

    purchase: Purchase
    before: Decimal  # the average unit price of ``previous``
    previous: tuple[Purchase, ...]  # the earlier purchases compared with (the last few)
    change: Decimal  # a fraction: 0.18 is 18% up

    @property
    def increased(self) -> bool:
        return self.change > 0

    @property
    def after(self) -> Decimal:
        return self.purchase.unit_price

    @property
    def percent(self) -> int:
        return int((self.change * 100).copy_abs().quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @property
    def name(self) -> str:
        return f"{self.purchase.product} ({self.purchase.supplier})"

    def line(self) -> str:
        """'Farinha de trigo T65 (Moagem do Norte) €0.82 → €0.97 per kg, up 18%'."""
        word = "up" if self.increased else "down"
        return (f"{self.name} {unit_money(self.before)} → {unit_money(self.after)} {self.purchase.per}, "
                f"{word} {self.percent}%")

    def compared(self) -> str:
        """'the last 3 purchases averaged €0.82 per kg' / 'the purchase before was €0.82 per kg'."""
        n = len(self.previous)
        if n == 1:
            return f"the purchase before was {unit_money(self.before)} {self.purchase.per}"
        return f"the last {n} purchases averaged {unit_money(self.before)} {self.purchase.per}"


def threshold_words(threshold: Decimal = UNUSUAL_CHANGE) -> str:
    """'15%'."""
    return f"{(threshold * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP)}%"


def unit_money(value: Decimal, currency: str = "EUR") -> str:
    """A unit price: cents like any amount ('€20.50'), a third decimal when it matters ('€0.823')."""
    cents = value.quantize(_CENT, rounding=ROUND_HALF_UP)
    if value.copy_abs() >= 10 or (value - cents).copy_abs() < Decimal("0.0005"):
        return format_money(cents, currency)
    shown = format_money(cents, currency)
    symbol = shown[: -len(f"{cents.copy_abs():,.2f}")]  # the sign and currency, as every amount shows them
    return symbol + f"{value.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP).copy_abs():,.3f}"


# ---------------------------------------------------------------------------------------------------- numbers


def _decimal_mark(text: str) -> str:
    """The document's decimal separator: ',' (Portuguese, Spanish) or '.' (English), from its amounts."""
    commas = len(re.findall(r"\d,\d{2}(?![\d.,])", text))
    dots = len(re.findall(r"(?<![\d.])\d+\.\d{2}(?![\d.,])", text))
    return "." if dots > commas else ","


_NUMBER_SHAPE = re.compile(r"[+-]?\d[\d.,]*")


def parse_number(raw: str, mark: str = ",") -> tuple[Decimal, int] | None:
    """('1.250,50', ',') -> (Decimal('1250.50'), 2 decimals); None when it is not a number as printed.

    A lone separator followed by exactly three digits is a thousands separator unless it is the document's
    decimal mark (``mark``); both separators in one number: the last one is the decimal point.
    """
    s = raw.strip()
    if not _NUMBER_SHAPE.fullmatch(s) or s[-1] in ".,":
        return None
    sign = -1 if s.startswith("-") else 1
    s = s.lstrip("+-")
    dec: str | None
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        pieces = s.split(sep)
        if len(pieces) > 2:
            dec = None
        elif len(pieces[1]) == 3:
            dec = sep if sep == mark else None
        else:
            dec = sep
    else:
        dec = None
    if dec is not None:
        whole, frac = s.rsplit(dec, 1)
        if not frac.isdigit():
            return None
    else:
        whole, frac = s, ""
    group = "." if dec == "," else ","
    if dec is None:
        group = "," if "," in whole else "."
    groups = whole.split(group)
    if len(groups) > 1 and (not 1 <= len(groups[0]) <= 3 or any(len(g) != 3 for g in groups[1:])):
        return None
    digits = "".join(groups)
    if not digits.isdigit():
        return None
    try:
        value = Decimal(f"{digits}.{frac}" if frac else digits) * sign
    except InvalidOperation:
        return None
    return value, len(frac)


# ---------------------------------------------------------------------------------------------------- text rows


@dataclass(frozen=True)
class _Cell:
    kind: str  # "num" | "pct" | "unit"
    value: Decimal = _ZERO
    decimals: int = 0
    unit: str | None = None  # a unit printed with the number ("25kg"), or the unit of a unit cell
    raw: str = ""


_ATTACHED = re.compile(r"^(\d+(?:[.,]\d+)?)([a-z]{1,6}\d?)$")
_CURRENCY_TOKENS = frozenset({"€", "eur", "euros", "$", "£", "usd", "gbp"})


def _cell(token: str, mark: str) -> _Cell | None:
    t = fold(token)
    if t in _CURRENCY_TOKENS:
        return _Cell("skip", raw=token)
    t = t.strip("€$£")
    if t.endswith("%"):
        found = parse_number(t[:-1], mark)
        if found is None or not _ZERO <= found[0] <= 30:
            return None
        return _Cell("pct", found[0], found[1], raw=token)
    found = parse_number(t, mark)
    if found is not None:
        return _Cell("num", found[0], found[1], raw=token)
    attached = _ATTACHED.match(t)
    units = _units()
    if attached and attached.group(2) in units:
        value = parse_number(attached.group(1), mark)
        if value is not None:
            return _Cell("num", value[0], value[1], unit=units[attached.group(2)], raw=token)
    word = t.rstrip(".")
    if word in units and word not in ("t", "u"):
        return _Cell("unit", unit=units[word], raw=token)
    return None


@dataclass(frozen=True)
class _Row:
    tokens: tuple[str, ...]  # the description's tokens, as printed (code and line number still in)
    quantity: Decimal
    unit: str | None
    price: Decimal  # as printed, per unit
    discount: Decimal | None  # percent, or an amount when ``discount_amount``
    discount_amount: bool
    vat_rate: Decimal | None
    total: Decimal


def _tolerance(quantity: Decimal, price_decimals: int) -> Decimal:
    """How far quantity × a printed (rounded) price may be from the line total: the rounding of the price."""
    half = Decimal(5) / (Decimal(10) ** (max(price_decimals, 0) + 1))
    return _CENT + quantity.copy_abs() * half


def _vat_like(c: _Cell) -> bool:
    if c.kind == "pct":
        return True
    return c.kind == "num" and c.unit is None and c.value == c.value.to_integral_value() and int(c.value) in _PLAIN_RATES


def _parse_row(line: str, mark: str, price_first: bool) -> list[_Row]:
    """Every reading of ``line`` as a table row whose numbers hold (usually one; none for anything else)."""
    tokens = line.replace("%", "% ").replace(" %", "%").split()
    tokens = [t for t in tokens if t]
    cells: list[_Cell] = []
    i = len(tokens) - 1
    while i >= 0:
        c = _cell(tokens[i], mark)
        if c is None:
            break
        cells.append(c)
        i -= 1
    cells.reverse()
    desc = tokens[: i + 1]
    if not desc or sum(ch.isalpha() for ch in " ".join(desc)) < 2:
        return []
    cells = [c for c in cells if c.kind != "skip"]
    nums = [k for k, c in enumerate(cells) if c.kind in ("num", "pct")]
    if len(nums) < 3:
        return []
    out: list[_Row] = []
    for total_at in (nums[-1], nums[-2]):
        total = cells[total_at]
        if total.kind != "num" or total.unit is not None or total.value <= 0:
            continue
        after = [k for k in nums if k > total_at]
        vat: _Cell | None = None
        if after:
            if len(after) != 1 or not _vat_like(cells[after[0]]):
                continue
            vat = cells[after[0]]
        before = [k for k in nums if k < total_at]
        options: list[tuple[_Cell | None, list[int]]] = []
        if vat is None and before and _vat_like(cells[before[-1]]):
            options.append((cells[before[-1]], before[:-1]))  # the VAT column before the total
        options.append((vat, before))
        for rate, rest in options:
            for with_discount in (False, True):
                if len(rest) < (3 if with_discount else 2):
                    continue
                discount = cells[rest[-1]] if with_discount else None
                pair = rest[-3:-1] if with_discount else rest[-2:]
                q_at, p_at = (pair[1], pair[0]) if price_first else (pair[0], pair[1])
                qty, price = cells[q_at], cells[p_at]
                if qty.kind != "num" or price.kind != "num" or qty.value <= 0 or price.value <= 0:
                    continue
                if price.unit is not None or (discount is not None and discount.unit is not None):
                    continue
                if abs(q_at - p_at) != 1 and not (abs(q_at - p_at) == 2 and cells[min(q_at, p_at) + 1].kind == "unit"):
                    continue
                # Units: attached to the quantity, or one unit cell next to it; none anywhere else in the numbers.
                units = [k for k in range(min(pair), len(cells)) if cells[k].kind == "unit"]
                unit = qty.unit
                if units:
                    if len(units) != 1 or units[0] not in (q_at + 1, q_at - 1) or unit is not None:
                        continue
                    unit = cells[units[0]].unit
                elif q_at > 0 and cells[q_at - 1].kind == "unit" and unit is None:
                    unit = cells[q_at - 1].unit
                gross = qty.value * price.value
                tolerance = _tolerance(qty.value, price.decimals)
                # Without a discount; with a discount in percent; with a discount as an amount.
                readings = [(gross, False)] if discount is None else []
                if discount is not None and (discount.kind == "pct" or discount.value < 100):
                    readings.append((gross * (Decimal(100) - discount.value) / Decimal(100), False))
                if discount is not None and discount.kind == "num":
                    readings.append((gross - discount.value, True))
                fits = [amount for net, amount in readings if abs(net - total.value) <= tolerance]
                if not fits:
                    continue
                leftover = [cells[k].raw for k in range(0, min(pair))
                            if not (cells[k].kind == "unit" and k == q_at - 1 and unit == cells[k].unit)]
                out.append(_Row(tuple(desc + leftover), qty.value, unit, price.value,
                                discount.value if discount is not None and discount.value else None, fits[0],
                                rate.value if rate is not None else None, total.value))
    unique: dict[tuple[Any, ...], _Row] = {}
    for row in out:
        unique.setdefault((row.quantity, row.price, row.total, row.vat_rate, row.unit), row)
    return list(unique.values())


def _settle(readings: list[_Row], columns: Mapping[str, int] | None) -> _Row | None:
    """One reading of a row, or None when the readings disagree on what was bought and at which price.

    Readings that differ only in whether a small whole number is the VAT rate or a discount ("0", "6") are
    settled by the table's header (an IVA column and no discount column, or the reverse); otherwise the rate is
    left unknown, so the row can only count on an invoice with one VAT rate.
    """
    if len(readings) == 1:
        return readings[0]
    same = {(r.quantity, r.price, r.total, r.unit) for r in readings}
    if len(same) != 1:
        return None
    cols = set(columns or ())
    rated = [r for r in readings if r.vat_rate is not None]
    plain = [r for r in readings if r.vat_rate is None]
    if "vat" in cols and "disc" not in cols and len(rated) == 1:
        return rated[0]
    if "disc" in cols and "vat" not in cols and len(plain) == 1:
        return plain[0]
    base = plain[0] if plain else readings[0]
    return _Row(base.tokens, base.quantity, base.unit, base.price, None, False, None, base.total)


# A table's header: which columns it has, in which order ("Code Description Qty Unit Price VAT Total"; a pack's
# own words for each column: "line_prices.head:<column>", Portugal's "Código Descrição Qtd Un Preço IVA Total").
_HEAD_WORDS = {
    "code": r"cod|ref|referencia|reference|code|sku",
    "desc": r"descripcion|description|producto|product|concepto|item|articulo",
    "qty": r"qty|quantity|cant|cantidad|uds|unidades",
    "price": r"precio|price|p\s?unit|pr\s?unit|pu|valor unit|v\s?unit|unit price|rate|pvp",
    "total": r"total|valor|importe|amount",
    "vat": r"iva|vat|igic|tax|impuesto",
    "disc": r"desc|dto|descuento|discount|disc",
}


@cache
def _head_patterns() -> dict[str, re.Pattern[str]]:
    return {name: re.compile(rf"\b(?:{pack_alternatives(f'line_prices.head:{name}')}|{pattern})\b")
            for name, pattern in _HEAD_WORDS.items()}


def _header(line: str) -> dict[str, int] | None:
    t = re.sub(r"[^a-z ]+", " ", fold(line))
    found: dict[str, int] = {}
    for name, pattern in _head_patterns().items():
        m = pattern.search(t)
        if m is not None:
            found[name] = m.start()
    if {"qty", "price"} <= set(found) and ("desc" in found or "code" in found) and len(found) >= 3:
        return found
    return None


_CODE = re.compile(r"^(?=[A-Z0-9._/-]*\d)[A-Z0-9][A-Z0-9._/-]{2,}$")
_LINE_NO = re.compile(r"^\d{1,3}[.)]?$")


def read_text_lines(text: str) -> tuple[PriceLine, ...]:
    """The table rows of an invoice's text whose own numbers hold (not yet checked against its totals)."""
    if not text:
        return ()
    mark = _decimal_mark(text)
    price_first = False
    code_column = False
    columns: dict[str, int] | None = None
    rows: list[_Row] = []
    for raw in text.splitlines()[:2000]:
        line = raw.strip()
        if not line or len(line) > 300:
            continue
        head = _header(line)
        if head is not None and not rows:
            columns = head
            price_first = head["price"] < head["qty"]
            code_column = "code" in head and ("desc" not in head or head["code"] < head["desc"])
            continue
        found = _settle(_parse_row(line, mark, price_first), columns) if head is None else None
        if found is None:
            continue  # not a row, or readings that disagree: never guessed
        rows.append(found)
    if not rows:
        return ()
    # A leading product code: the header names one, or every row starts with one.
    firsts = [r.tokens[0] for r in rows if len(r.tokens) > 1]
    with_code = code_column or (len(firsts) == len(rows) and all(_CODE.match(t) and not _LINE_NO.match(t)
                                                                 for t in firsts))
    out: list[PriceLine] = []
    for r in rows:
        tokens = list(r.tokens)
        while len(tokens) > 1 and _LINE_NO.match(tokens[0]):
            tokens.pop(0)  # a line number
        code = None
        if with_code and len(tokens) > 1 and _CODE.match(tokens[0].upper()):
            code = tokens.pop(0).upper()
        description = " ".join(tokens).strip(" -:;|")
        if sum(ch.isalpha() for ch in description) < 2:
            continue
        out.append(PriceLine(description=description, quantity=r.quantity, unit=r.unit,
                             unit_price=(r.total / r.quantity), net=r.total, vat_rate=r.vat_rate, code=code))
    return tuple(out)


# ---------------------------------------------------------------------------------------------------- totals


_BASE_LABEL = LazyPattern(lambda: (rf"\b(?:{pack_alternatives('line_prices.base_label')}|base imponible|base imp|"
                                   r"taxable amount|taxable|net amount|net|base|subtotal|sin iva|excl(?:uding)? vat|"
                                   r"ex vat)\b"))
_VAT_LABEL = LazyPattern(lambda: rf"\b(?:iva|vat|igic|tax|{pack_alternatives('line_prices.vat_label')}|impuesto)\b")
_RATE = re.compile(r"(?<![\d.,])(\d{1,2}(?:[.,]\d{1,2})?)\s*%")
_AMOUNT = re.compile(r"(?<![\w.,])[+-]?\d[\d.,]*\d|(?<![\w.,])\d(?![\w.,])")


def rate_parts_from_text(text: str, gross: Decimal | None) -> tuple[VatPart, ...]:
    """The net and VAT at each rate an invoice prints ("Base tributável (6%): 472,00", "IVA 6%: 28,32", or a
    summary table "6% 472,00 28,32"), when together they make exactly its total ``gross``; else nothing."""
    if not text or gross is None:
        return ()
    mark = _decimal_mark(text)
    nets: dict[Decimal, Decimal] = {}
    vats: dict[Decimal, Decimal] = {}
    for raw in text.splitlines()[:2000]:
        folded = fold(raw)
        rates = _RATE.findall(folded)
        if len(rates) != 1:
            continue
        found_rate = parse_number(rates[0], mark)
        if found_rate is None:
            continue
        rate = found_rate[0].normalize()
        rest = _RATE.sub(" ", folded)
        amounts = [n[0] for n in (parse_number(a, mark) for a in _AMOUNT.findall(rest)) if n is not None]
        if not amounts:
            continue
        if len(amounts) >= 2:  # a summary row: rate, base and VAT (and its total)
            base, vat = amounts[-2], amounts[-1]
            if len(amounts) >= 3 and abs(amounts[-3] * rate / 100 - amounts[-2]) <= Decimal("0.02"):
                base, vat = amounts[-3], amounts[-2]  # rate, base, VAT, total
            if base > 0 and abs(base * rate / 100 - vat) <= Decimal("0.02"):
                nets.setdefault(rate, base)
                vats.setdefault(rate, vat)
                continue
        if _BASE_LABEL.search(rest):
            nets.setdefault(rate, amounts[-1])
        elif _VAT_LABEL.search(rest):
            vats.setdefault(rate, amounts[-1])
    rates_found = set(nets) | set(vats)
    if not rates_found or set(nets) != set(vats):
        return ()
    parts = tuple(VatPart(rate=r, net=nets[r], vat=vats[r]) for r in sorted(rates_found))
    if sum((p.gross for p in parts), _ZERO) != abs(gross):
        return ()
    return parts


def _same_rate(a: Decimal | None, b: Decimal | None) -> bool:
    return a is not None and b is not None and a.compare(b) == 0


def check_lines(lines: Sequence[PriceLine], parts: Sequence[VatPart]) -> tuple[tuple[PriceLine, ...], str] | None:
    """``lines`` as used, and how they add up, when they add up to ``parts`` (the invoice's net and VAT at each
    rate) to the cent: net at each rate, or (prices with VAT) gross at each rate. None when they do not."""
    parts = [p for p in parts if p.net or p.vat]
    if not lines or not parts:
        return None
    rated = list(lines)
    if len(parts) == 1:
        rates = {line.vat_rate for line in lines if line.vat_rate is not None}
        if len(rates) > 1:
            return None  # lines at several rates, the invoice at one: they cannot add up at each rate
        only = parts[0]
        if only.rate is not None and rates and not _same_rate(next(iter(rates)), only.rate):
            return None
        groups = [(only, list(lines))]
    else:
        if any(line.vat_rate is None for line in lines) or any(p.rate is None for p in parts):
            return None  # several rates: every line needs its own
        groups = []
        for p in parts:
            groups.append((p, [line for line in lines if _same_rate(line.vat_rate, p.rate)]))
        if sum(len(g) for _, g in groups) != len(lines):
            return None  # a line at a rate the invoice does not have
    for kind in ("net", "gross"):
        if all(g and sum((line.net for line in g), _ZERO) == (p.net if kind == "net" else p.gross)
               for p, g in groups):
            if kind == "gross":
                rated = []
                for p, g in groups:
                    factor = p.net / p.gross if p.gross else Decimal(1)
                    rated += [_net_of_vat(line, factor) for line in g]
            said = ", ".join(f"{_rate_word(p.rate)}{format_money(p.net if kind == 'net' else p.gross)}"
                             for p, _ in groups)
            what = "net" if kind == "net" else "total with VAT"
            return tuple(rated), f"The lines add up to the invoice's {what}: {said}."
    return None


def _rate_word(rate: Decimal | None) -> str:
    return "" if rate is None else f"{rate.normalize():f}% "


def _net_of_vat(line: PriceLine, factor: Decimal) -> PriceLine:
    return PriceLine(description=line.description, quantity=line.quantity, unit=line.unit,
                     unit_price=line.unit_price * factor, net=(line.net * factor).quantize(_CENT), vat_rate=line.vat_rate,
                     code=line.code, trusted=line.trusted)


def _einvoice_lines(xml: bytes) -> tuple[tuple[PriceLine, ...], tuple[VatPart, ...]]:
    priced = read_priced_lines(xml)
    if not priced:
        return (), ()
    lines: list[PriceLine] = []
    for p in priced:
        if p.net is None or p.quantity is None or p.quantity <= 0 or p.net <= 0:
            return (), ()  # a line without its amount or quantity: the sum cannot be checked
        unit = _UNIT_CODES.get(p.unit_code or "", (p.unit_code or "").lower() or None)
        per = p.net / p.quantity
        trusted = True
        if p.price is not None:  # its own price must give its amount (a line allowance would not)
            base = p.base_quantity if p.base_quantity and p.base_quantity > 0 else Decimal(1)
            exponent = p.price.as_tuple().exponent
            decimals = -exponent if isinstance(exponent, int) and exponent < 0 else 0
            trusted = abs(p.price * p.quantity / base - p.net) <= _tolerance(p.quantity / base, decimals)
        lines.append(PriceLine(description=p.description, quantity=p.quantity, unit=unit, unit_price=per, net=p.net,
                               vat_rate=p.vat_rate, code=p.code, trusted=trusted))
    details = read_invoice_details(xml)
    return tuple(lines), tuple(details.vat_parts) if details is not None else ()


# ---------------------------------------------------------------------------------------------------- products


def _pack_size(folded: str) -> tuple[str, Decimal] | None:
    """('kg', 25) for "farinha t65 saco 25 kg"; ('l', 9) for "agua 6 x 1,5 l"; None without exactly one size."""
    found = _PACK.findall(folded)
    if len(found) != 1:
        return None
    count, size, unit = found[0]
    value = parse_number(size, ",") or parse_number(size, ".")
    if value is None or value[0] <= 0:
        return None
    unit = _PACK_UNITS[unit]
    amount = value[0] * (Decimal(count) if count else Decimal(1))
    if unit in _MASS:
        return "kg", amount * _MASS[unit]
    return "l", amount * _VOLUME[unit]


def product_key(description: str) -> str:
    """'Farinha de Trigo T65 (saco 25 kg)' -> 'farinha trigo t65': what the product is, not how it is packed."""
    folded = _PACK.sub(" ", fold(description))
    words = [w for w in re.split(r"[^a-z0-9]+", folded) if w]
    stop, packing = _stop(), _packing()
    return " ".join(w for w in words if w not in stop and w not in packing)


def product_concepts(text: str) -> set[str]:
    """The raw materials a text names, in any of the three languages ("flour" and "farinha" are both flour)."""
    out: set[str] = set()
    for word in re.split(r"[^a-z]+", fold(text)):
        concept = _concept_of().get(word) or (_concept_of().get(word[:-1]) if word.endswith("s") else None)
        if concept:
            out.add(concept)
    return out


def _basis(line: PriceLine) -> tuple[str, Decimal] | None:
    """What a line's price is compared per (kg, litre, or its unit) and how many of those it bought."""
    unit = line.unit
    if unit in _MASS:
        return "kg", line.quantity * _MASS[unit]
    if unit in _VOLUME:
        return "l", line.quantity * _VOLUME[unit]
    size = _pack_size(fold(line.description))
    if size is not None:
        return size[0], line.quantity * size[1]
    return (unit or ""), line.quantity


# ---------------------------------------------------------------------------------------------------- the history


class LinePrices:
    """The price history of one tenant's purchase invoices, read from its documents (backoffice.orchestrator)."""

    def __init__(self, orchestrator: Any) -> None:
        self.o = orchestrator
        self._cache: dict[str, tuple[tuple[Any, ...], InvoiceLines | None]] = {}

    # -- one invoice -------------------------------------------------------------------------------------

    def invoice_lines(self, record: Any) -> InvoiceLines | None:
        """The lines of one purchase invoice that add up to its totals; None when it has none that do."""
        doc = record.document
        key = (tuple(record.evidence_ids), doc.gross_amount, doc.net_amount, doc.vat_amount, doc.doc_type,
               record.sales, len(record.text or ""))
        cached = self._cache.get(record.id)
        if cached is not None and cached[0] == key:
            return cached[1]
        found = self._read(record)
        self._cache[record.id] = (key, found)
        return found

    def _xml(self, record: Any) -> bytes | None:
        repo = self.o.repo
        for evidence_id in record.evidence_ids:
            evidence = repo.evidence(evidence_id)
            if evidence.format in (EvidenceFormat.UBL, EvidenceFormat.XML):
                return repo.registry.open(repo.tenant_id, evidence_id)
            outcome = repo.reads.get(evidence_id)
            for xml in getattr(outcome, "embedded_xml", ()) or ():
                return xml
        return None

    def _read(self, record: Any) -> InvoiceLines | None:
        doc = record.document
        if record.sales or doc.doc_type not in _PRICED or doc.gross_amount is None or doc.gross_amount <= 0:
            return None
        if getattr(record, "supporting", False):
            return None
        gross = abs(doc.gross_amount)
        xml = self._xml(record)
        if xml is not None:
            lines, parts = _einvoice_lines(xml)
            if lines:
                if sum((p.gross for p in parts), _ZERO) != gross:
                    parts = ()
                checked = check_lines(lines, parts or self.o.cost_centers.vat_parts(record, None))
                return InvoiceLines(checked[0], "e-invoice", checked[1]) if checked else None
        text = record.text or ""
        lines = read_text_lines(text)
        if not lines:
            return None
        # The invoice's own totals at each rate: its fiscal QR code or net and VAT as read, else its printed lines.
        for parts in (self.o.cost_centers.vat_parts(record, None), rate_parts_from_text(text, gross)):
            checked = check_lines(lines, parts)
            if checked:
                return InvoiceLines(checked[0], "text", checked[1])
        return None

    # -- every invoice ----------------------------------------------------------------------------------

    def _supplier(self, record: Any) -> tuple[str, str]:
        repo = self.o.repo
        doc = record.document
        if record.supplier_id and record.supplier_id in repo.suppliers:
            return f"id:{record.supplier_id}", display_name(repo.suppliers[record.supplier_id].name)
        name = display_name(doc.supplier_name)
        if doc.supplier_tax_id:
            return f"tax:{doc.supplier_tax_id}", name
        return f"name:{counterparty_key(doc.supplier_name or '')}", name

    def purchases(self) -> list[Purchase]:
        """Every product bought on a purchase invoice whose lines add up, oldest first."""
        out: list[Purchase] = []
        codes: dict[tuple[str, str], str] = {}  # (supplier, description key) -> the product code it was printed with
        pending: list[tuple[Any, ...]] = []
        for record in sorted(self.o.repo.documents.values(), key=lambda r: r.id):
            found = self.invoice_lines(record)
            if found is None:
                continue
            supplier_key, supplier = self._supplier(record)
            doc = record.document
            on = doc.issue_date or record.received_at.date()
            merged: dict[tuple[str, str, str], list[Any]] = {}
            for line in found.lines:
                if not line.trusted or line.quantity <= 0:
                    continue
                key = product_key(line.description)
                if not key and not line.code:
                    continue
                basis = _basis(line)
                if basis is None or basis[1] <= 0:
                    continue
                if line.code and key:
                    codes.setdefault((supplier_key, key), line.code)
                slot = (line.code or "", key, basis[0])
                entry = merged.setdefault(slot, [line, _ZERO, _ZERO])
                entry[0] = line
                entry[1] += basis[1]
                entry[2] += line.net
            for (code, key, basis), (line, quantity, net) in merged.items():
                pending.append((supplier_key, supplier, code, key, basis, line, quantity, net, record, on))
        for supplier_key, supplier, code, key, basis, line, quantity, net, record, on in pending:
            code = code or codes.get((supplier_key, key), "")
            doc = record.document
            out.append(Purchase(
                supplier_key=supplier_key, supplier=supplier, product_key=f"code:{code}" if code else f"name:{key}",
                product=_clean_description(line.description), code=code or None, on=on, basis=basis,
                unit_price=net / quantity, quantity=quantity, net=net, document_id=record.id,
                evidence_id=record.evidence_ids[0], company_id=doc.entity_id,
                invoice=doc.invoice_number or on.isoformat()))
        out.sort(key=lambda p: (p.on, p.document_id, p.product_key))
        return out

    def history(self) -> dict[tuple[str, str, str], list[Purchase]]:
        """Purchases by supplier, product and what the price is compared per, oldest first."""
        groups: dict[tuple[str, str, str], list[Purchase]] = {}
        for p in self.purchases():
            groups.setdefault(p.group, []).append(p)
        return groups

    def changes(self, *, threshold: Decimal = UNUSUAL_CHANGE, window: int = COMPARE_LAST,
                increases_only: bool = True) -> list[PriceChange]:
        """Every purchase whose unit price is ``threshold`` or more away from the average of the product's
        previous ``window`` purchases (increases only unless ``increases_only`` is False)."""
        out: list[PriceChange] = []
        for group in self.history().values():
            for i, p in enumerate(group):
                previous = tuple(group[max(0, i - window):i])
                if not previous:
                    continue
                before = sum((x.unit_price for x in previous), _ZERO) / len(previous)
                if before <= 0:
                    continue
                change = (p.unit_price - before) / before
                if change >= threshold or (not increases_only and change <= -threshold):
                    out.append(PriceChange(p, before, previous, change))
        out.sort(key=lambda c: (c.purchase.on, c.purchase.document_id, c.purchase.product_key))
        return out

    def recent_increases(self, today: date, *, days: int = RECENT_DAYS, threshold: Decimal = UNUSUAL_CHANGE,
                         window: int = COMPARE_LAST) -> list[PriceChange]:
        """Products whose latest purchase, in the last ``days``, cost unusually more than the ones before."""
        latest = {group[-1].group: group[-1] for group in self.history().values()}
        start = today - timedelta(days=days)
        return [c for c in self.changes(threshold=threshold, window=window)
                if latest.get(c.purchase.group) == c.purchase and start <= c.purchase.on <= today]

    def increases_in(self, company_id: str, start: date, end: date, *, threshold: Decimal = UNUSUAL_CHANGE,
                     window: int = COMPARE_LAST) -> list[PriceChange]:
        """Unusual increases on one company's invoices dated from ``start`` to ``end``."""
        return [c for c in self.changes(threshold=threshold, window=window)
                if c.purchase.company_id == company_id and start <= c.purchase.on <= end]

    def find(self, words: str) -> list[list[Purchase]]:
        """The product histories a question names: "flour" finds "Farinha de trigo T65", "farinha" too."""
        concepts = product_concepts(words)
        folded = set(re.split(r"[^a-z0-9]+", fold(words))) - {""}
        out: list[list[Purchase]] = []
        for group in self.history().values():
            names = set(group[-1].product_key.split(":", 1)[1].split()) | set(product_key(group[-1].product).split())
            if (concepts and concepts & product_concepts(" ".join(names))) or \
                    any(len(w) >= 4 and w in names for w in folded):
                out.append(group)
        return out


def _clean_description(description: str) -> str:
    text = " ".join(description.split())
    return text[:60].rstrip(" -,;:") or "Product"


def summarise(purchases: Iterable[Purchase]) -> Mapping[str, Any]:
    """Quantity, spend, average, lowest and highest unit price of some purchases (same basis)."""
    items = list(purchases)
    quantity = sum((p.quantity for p in items), _ZERO)
    net = sum((p.net for p in items), _ZERO)
    return {"quantity": quantity, "net": net, "average": net / quantity if quantity else _ZERO,
            "low": min(p.unit_price for p in items), "high": max(p.unit_price for p in items),
            "invoices": len({p.document_id for p in items}), "suppliers": sorted({p.supplier for p in items}),
            "latest": max(items, key=lambda p: (p.on, p.document_id))}
