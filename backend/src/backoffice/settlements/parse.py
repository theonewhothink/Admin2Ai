"""Reading payout reports: structured CSV and JSON only, never OCR (§13 Stage 0).

:func:`parse_settlement_reports` turns one file into zero or more
:class:`~.report.SettlementReport` objects (one per payout it describes). It
returns ``[]`` for anything that is not recognisably a payout report, so the
caller can treat the file as it would any other upload.

Layouts read (header names are compared folded, case- and accent-insensitive,
punctuation ignored; a provider's column order never matters):

* **Provider-neutral CSV**, two shapes:

  - one row per payout: ``provider, payout_id, payout_date, currency, gross,
    fees, refunds, chargebacks, adjustments, net``;
  - one row per sale / refund / fee / adjustment / payout, with a ``type``
    column: ``provider, payout_id, payout_date, currency, type, reference,
    date, amount, fee[, net]``. A ``payout`` row states the amount paid out.
    Fees are positive when charged; refund, dispute and fee rows may be
    written with either sign.

* **Provider-neutral JSON**: ``{"provider": ..., "payouts": [{"id", "date",
  "currency", "gross", "fees", "refunds", "chargebacks", "adjustments",
  "net", "lines": [{"type", "reference", "date", "gross", "fee", "net"}]}]}``
  (a single payout object at the root works too).
* **Stripe**: the itemized payout reconciliation / balance reports
  (``reporting_category, gross, fee, net``, grouped by
  ``automatic_payout_id``) and the API's balance transactions (JSON, minor
  units), with or without the payout object.
* **PayPal** activity download (``Date, Time, Type/Description, Status,
  Currency, Gross, Fee, Net, Transaction ID[, Balance]``): each withdrawal to
  the bank is one payout; the rows since the previous withdrawal are its
  breakdown, and the running balance before and after is what stayed in or
  came out of PayPal (an adjustment), so the arithmetic is still exact.
* **Booking.com** payout statement (reservation number, amount, commission,
  payments service fee, net, payout date / id).
* **Airbnb** transaction history (Type, Confirmation code, Amount, Paid out,
  Service fee, Gross earnings): each "Payout" row and the reservations of
  the same date.
* **Delivery platforms** (Glovo, Uber Eats, Bolt Food) per-order statements:
  order id, sales, commission / service fees, promotions, refunds, payout.
* **Card terminals** (SIBS / Multibanco, REDUNIQ, Comercia) in Portuguese:
  lote, data de liquidação, montante bruto, comissão, montante líquido, tipo
  (compra / devolução / chargeback).

The layouts follow the providers' documented exports as far as they are
public; they were NOT verified against live downloads (verified_as_of:
never). Extend the alias tables as real files are seen.
"""

from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from backoffice.domain.models import ExtractionMethod, FieldObservation
from backoffice.reconciliation import CARD_TERMINAL, PayoutProvider, provider_by_key, provider_named
from backoffice.reconciliation._text import format_money

from .report import LineKind, SettlementLine, SettlementReport, observation

__all__ = ["SettlementReportError", "looks_like_settlement_report", "parse_settlement_reports"]

_ZERO = Decimal("0")
_MAX_ROWS = 200_000


class SettlementReportError(ValueError):
    """A payout report whose figures cannot be read. ``str(error)`` is plain language."""


# --------------------------------------------------------------------------- small readers


def _norm(text: Any) -> str:
    """'Sales (incl. VAT)' -> 'sales incl vat'; 'Nº Lote' -> 'no lote'; 'reporting_category' -> 'reporting category'."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()
    return " ".join(re.findall(r"[a-z0-9]+", plain))


_CURRENCY_NOISE = re.compile(r"[€$£\s ]|\b(?:eur|usd|gbp)\b", re.I)


def _amount(raw: Any) -> Decimal | None:
    """'1.234,56' / '1,234.56' / '-12,30' / '(12.30)' / '€ 12,30' -> Decimal. Never float."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        return raw if raw.is_finite() else None
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(repr(raw))
    text = _CURRENCY_NOISE.sub("", str(raw)).strip()
    if not text:
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    if text.endswith("-"):
        negative, text = True, text[:-1]
    if text and text[0] in "+-":
        negative, text = negative or text[0] == "-", text[1:]
    if not re.fullmatch(r"\d[\d.,]*", text):
        return None
    comma, dot = text.rfind(","), text.rfind(".")
    if comma >= 0 and dot >= 0:
        text = text.replace(".", "").replace(",", ".") if comma > dot else text.replace(",", "")
    elif comma >= 0:
        fraction = text[comma + 1:]
        text = text.replace(",", ".") if text.count(",") == 1 and len(fraction) != 3 else text.replace(",", "")
    elif dot >= 0:
        fraction = text[dot + 1:]
        if text.count(".") > 1 or len(fraction) == 3:
            text = text.replace(".", "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -value if negative else value


_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_YMD = re.compile(r"^(\d{4})[/.](\d{1,2})[/.](\d{1,2})|^(\d{4})(\d{2})(\d{2})$")
_DMY = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")


def _date_order(values: Sequence[Any]) -> str:
    """'dmy' or 'mdy' for a column of d/m/y-looking dates (European unless a value proves otherwise)."""
    for value in values:
        m = _DMY.match(str(value or "").strip())
        if m:
            first, second = int(m[1]), int(m[2])
            if first > 12:
                return "dmy"
            if second > 12:
                return "mdy"
    return "dmy"


def _date(raw: Any, order: str = "dmy") -> date | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, (int, Decimal)) and not isinstance(raw, bool):
        try:
            return datetime.fromtimestamp(int(raw), timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    try:
        if m := _ISO.match(text) or _YMD.match(text):
            parts = [g for g in m.groups() if g is not None]
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        if m := _DMY.match(text):
            a, b = int(m[1]), int(m[2])
            day, month = (a, b) if order == "dmy" else (b, a)
            return date(int(m[3]), month, day)
    except ValueError:
        return None
    if text.isdigit() and len(text) >= 9:  # unix seconds
        return _date(int(text))
    return None


def _currency(raw: Any) -> str | None:
    text = str(raw or "").strip().upper()
    if text in ("€", "EURO", "EUROS"):
        return "EUR"
    return text if re.fullmatch(r"[A-Z]{3}", text) else None


def _decode(data: bytes) -> str | None:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


# --------------------------------------------------------------------------- building a report


@dataclass
class _Figure:
    """One summary figure with where it came from."""

    value: Decimal
    location: str
    method: ExtractionMethod = ExtractionMethod.API


@dataclass
class _Draft:
    """What one payout's rows (or summary) say, before it becomes a report."""

    provider: PayoutProvider | None
    currency: str
    fmt: str
    payout_id: tuple[str, str] | None = None  # (value, location)
    payout_date: tuple[date, str] | None = None
    lines: list[SettlementLine] = field(default_factory=list)
    stated: dict[str, _Figure] = field(default_factory=dict)  # summary figures the report states
    paid_out: list[tuple[Decimal, str]] = field(default_factory=list)  # payout rows
    carry: _Figure | None = None  # PayPal: balance used or kept (an adjustment)
    row_label: str = "row"
    net_column: str = ""  # the column holding each row's own net, as printed

    def build(self, source: str) -> SettlementReport | None:
        provider = self.provider
        if provider is None:
            return None
        body = [line for line in self.lines if line.kind is not LineKind.PAYOUT]
        if not body and not self.stated and self.carry is None:
            return None  # a bare payout amount says nothing about sales, fees or refunds
        obs: dict[str, list[FieldObservation]] = defaultdict(list)
        totals: dict[str, Decimal] = {}
        count = len(body)
        noun = self.row_label if count == 1 else f"{self.row_label}s"
        for name, attr in (("gross_sales", "sales"), ("fees", "fees"), ("refunds", "refunds"),
                           ("chargebacks", "chargebacks"), ("adjustments", "adjustments")):
            summed = sum((getattr(line, attr) for line in body), _ZERO)
            stated = self.stated.get(name)
            if stated is not None:
                totals[name] = stated.value
                obs[name].append(observation(stated.value, source, stated.location, method=stated.method))
                if body:
                    obs[name].append(observation(summed, source, f"arithmetic: sum of {count} {noun}",
                                                 method=ExtractionMethod.ARITHMETIC))
            else:
                totals[name] = summed
                if body and (summed or name == "gross_sales"):
                    obs[name].append(observation(summed, source, f"arithmetic: sum of {count} {noun}",
                                                 method=ExtractionMethod.ARITHMETIC))
        if self.carry is not None:
            totals["adjustments"] += self.carry.value
            obs["adjustments"].append(observation(self.carry.value, source, self.carry.location,
                                                  method=self.carry.method))
        problems: list[str] = []
        if body and self.stated:
            for name, attr, words in (("gross_sales", "sales", "sales"), ("fees", "fees", "fees"),
                                      ("refunds", "refunds", "refunds"), ("chargebacks", "chargebacks",
                                                                          "disputed payments")):
                stated = self.stated.get(name)
                summed = sum((getattr(line, attr) for line in body), _ZERO)
                if stated is not None and stated.value != summed:
                    problems.append(f"Its {noun} add up to {format_money(summed, self.currency)} in {words}, "
                                    f"but its total says {format_money(stated.value, self.currency)}.")
        computed = (totals["gross_sales"] - totals["fees"] - totals["refunds"] - totals["chargebacks"]
                    + totals["adjustments"])
        if "net" in self.stated:
            net, stated_net = self.stated["net"].value, True
            obs["net_amount"].append(observation(net, source, self.stated["net"].location,
                                                 method=self.stated["net"].method))
        elif self.paid_out:
            net, stated_net = sum((v for v, _ in self.paid_out), _ZERO), True
            for value, where in self.paid_out:
                obs["net_amount"].append(observation(value, source, where))
        elif body and all(line.stated_net is not None for line in body):
            net = sum((line.stated_net or _ZERO for line in body), _ZERO)
            stated_net = True
            where = (f"csv:sum of column '{self.net_column}' over {count} {noun}" if self.net_column
                     else f"sum of the net of {count} {noun}")
            obs["net_amount"].append(observation(net, source, where))
        else:
            net, stated_net = computed, False
        obs["net_amount"].append(observation(
            computed, source, "arithmetic: sales minus fees, refunds and disputes, plus or minus adjustments",
            method=ExtractionMethod.ARITHMETIC))
        payout_date = self.payout_date
        if payout_date is None:
            days = [line.on for line in self.lines if line.on is not None]
            if days:
                payout_date = (max(days), "the latest date in the report")
        if self.payout_id is not None:
            obs["payout_id"].append(observation(self.payout_id[0], source, self.payout_id[1]))
        if payout_date is not None:
            obs["payout_date"].append(observation(payout_date[0], source, payout_date[1],
                                                  confidence=0.99 if self.payout_date else 0.6))
        obs["currency"].append(observation(self.currency, source, "the report's currency"))
        report = SettlementReport(
            provider=provider, source=source, format=self.fmt, currency=self.currency,
            gross_sales=totals["gross_sales"], fees=totals["fees"], refunds=totals["refunds"],
            chargebacks=totals["chargebacks"], adjustments=totals["adjustments"], net=net, net_stated=stated_net,
            payout_id=self.payout_id[0] if self.payout_id else None,
            payout_date=payout_date[0] if payout_date else None, lines=tuple(self.lines),
            observations={k: tuple(v) for k, v in obs.items()}, problems=tuple(problems),
        )
        return report


# --------------------------------------------------------------------------- entry point


def parse_settlement_reports(data: bytes, *, source: str, filename: str | None = None,
                             hint: str = "") -> list[SettlementReport]:
    """Every payout a payout-report file describes, or ``[]`` when it is not one.

    ``hint`` is extra text naming the provider when the file itself does not
    (the sender and subject of the email it came in). Raises
    :class:`SettlementReportError` for a recognisable report whose figures
    cannot be read.
    """
    text = _decode(bytes(data))
    if text is None or not text.strip():
        return []
    head = text.lstrip()[:1]
    if head in ("{", "["):
        try:
            parsed = json.loads(text, parse_float=Decimal)
        except ValueError:
            return []
        drafts = _from_json(parsed, filename or "", hint)
    else:
        drafts = _from_csv(text, filename or "", hint)
    reports = [r for r in (d.build(source) for d in drafts) if r is not None]
    return sorted(reports, key=lambda r: (r.payout_date or date.min, r.payout_id or "", r.currency))


def looks_like_settlement_report(data: bytes, *, filename: str | None = None, hint: str = "") -> bool:
    try:
        return bool(parse_settlement_reports(data, source="probe", filename=filename, hint=hint))
    except SettlementReportError:
        return True


# --------------------------------------------------------------------------- CSV


def _from_csv(text: str, filename: str, hint: str) -> list[_Draft]:
    first = next((line for line in text.splitlines() if line.strip()), "")
    delimiters = sorted((",", ";", "\t"), key=lambda d: -first.count(d))
    for delimiter in delimiters:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        if len(rows) > _MAX_ROWS:
            raise SettlementReportError("This payout report is too large for me to read in one go.")
        for index, row in enumerate(rows[:15]):
            headers = [_norm(c) for c in row]
            found = frozenset(h for h in headers if h)
            if len(found) < 3:
                continue
            for layout in _LAYOUTS:
                if layout.detect(found):
                    body = [(index + 2 + i, r) for i, r in enumerate(rows[index + 1:])
                            if any(c.strip() for c in r) and not _is_total_row(r)]
                    # A rate column ('Commission %', 'Taxa (%)') is never an amount.
                    usable = ["" if "%" in raw or _RATE.search(h) else h for raw, h in zip(row, headers, strict=True)]
                    table = _Table(usable, [c.strip() for c in row], body)
                    return layout.read(table, filename, hint)
    return []


_RATE = re.compile(r"\b(?:rate|percent|percentage|percentagem|pct)\b")


@dataclass
class _Table:
    headers: list[str]  # normalized
    shown: list[str]  # as printed, for locations
    rows: list[tuple[int, list[str]]]  # (1-based line number, cells)

    def column(self, aliases: Sequence[str]) -> int | None:
        """The first column matching the aliases, in the aliases' order of preference."""
        for alias in aliases:
            if alias in self.headers:
                return self.headers.index(alias)
        return None

    def columns(self, aliases: Sequence[str]) -> list[int]:
        return [i for i, h in enumerate(self.headers) if h in aliases]

    @staticmethod
    def cell(row: list[str], index: int | None) -> str:
        if index is None or index >= len(row):
            return ""
        return row[index].strip()

    def where(self, line: int, index: int) -> str:
        return f"csv:row {line} column '{self.shown[index]}'"

    @property
    def date_order(self) -> str:
        """One file, one date convention: any date in any column decides (US exports use month first)."""
        return _date_order([cell for _, row in self.rows for cell in row])


def _has(found: frozenset[str], aliases: Sequence[str]) -> bool:
    return any(a in found for a in aliases)


# Header aliases by role (normalized). Order = preference when several are present.
_A: dict[str, tuple[str, ...]] = {
    "provider": ("provider", "platform", "acquirer", "rede", "adquirente", "entidade", "processador", "processor"),
    "payout_id": ("payout id", "automatic payout id", "payout reference id", "payout reference", "payout number",
                  "payout reference number", "settlement id", "settlement reference", "batch id", "batch number",
                  "batch", "no lote", "n lote", "numero lote", "numero do lote", "id lote", "lote", "id liquidacao",
                  "referencia liquidacao", "statement number"),
    "payout_date": ("payout date", "automatic payout effective at", "automatic payout effective at utc",
                    "payout effective at", "date of payout", "paid on", "settlement date", "value date",
                    "data liquidacao", "data de liquidacao", "data valor", "data credito", "data de credito",
                    "payment date", "transfer date", "arrival date"),
    "currency": ("currency", "moeda", "divisa", "currency code"),
    "type": ("type", "reporting category", "transaction type", "line type", "tipo", "tipo movimento",
             "tipo operacao", "tipo de operacao", "kind"),
    "reference": ("reference", "order id", "order code", "order number", "order reference", "reservation number",
                  "booking number", "reservation id", "reference number", "source id", "charge id",
                  "balance transaction id", "transaction id", "id transacao", "referencia", "numero autorizacao",
                  "codigo autorizacao", "autorizacao", "authorisation code", "authorization code"),
    "on": ("date", "order date", "transaction date", "created utc", "created", "data movimento", "data operacao",
           "data transacao", "data da transacao", "data", "check out", "checkout", "departure", "check in",
           "arrival"),
    "gross": ("gross", "gross amount", "gross sales", "sales incl vat", "sales including vat", "amount", "sales",
              "food sales", "order total", "order value", "total order value", "subtotal", "products price",
              "total sales", "reservation amount", "room sales", "total amount", "original amount",
              "montante bruto", "valor bruto", "montante", "valor", "valor transacao", "valor da transacao"),
    "fee": ("fee", "fees", "commission", "commission amount", "commissions", "payments service fee",
            "payment service fee", "payment charge", "transaction fee", "service fee", "uber service fee",
            "marketplace fee", "glovo commission", "bolt commission", "platform fee", "comissao", "comissoes",
            "taxa servico", "taxa de servico", "service charge"),
    "refund": ("refunds", "refund", "refunds incl vat", "customer refunds", "reembolsos"),
    "chargeback": ("chargebacks", "chargeback", "disputes"),
    "deduction": ("promotions", "promotion paid by partner", "promotions paid by partner", "promotions on items",
                  "partner funded discount", "partner funded promotions", "discounts"),
    "adjustment": ("adjustments", "adjustment", "other payments", "misc payments", "ajustes", "acertos"),
    "net": ("net", "net amount", "net payout", "total payout", "total to receive", "amount to receive",
            "payable amount", "payout amount", "earnings", "montante liquido", "valor liquido", "liquido"),
    "paid_out": ("paid out",),
}
_SUMMARY: dict[str, tuple[str, ...]] = {
    "gross_sales": ("gross sales", "gross", "sales", "total sales"),
    "fees": ("fees", "fee", "commission", "commissions", "fees and commissions"),
    "refunds": ("refunds", "refund"),
    "chargebacks": ("chargebacks", "chargeback", "disputes"),
    "adjustments": ("adjustments", "adjustment", "other"),
    "net": ("net", "net amount", "paid out", "payout amount", "net payout"),
}
_PT_TERMINAL = ("montante bruto", "valor bruto", "montante liquido", "valor liquido", "comissao", "comissoes",
                "taxa de servico", "taxa servico", "data liquidacao", "data de liquidacao", "lote", "no lote",
                "n lote", "numero lote", "numero do lote")
_DELIVERY = {"uber_eats": ("uber service fee", "marketplace fee", "payout reference id"),
             "glovo": ("glovo commission", "order code", "total to receive", "promotion paid by partner"),
             "bolt_food": ("bolt commission",)}


@dataclass(frozen=True)
class _Layout:
    name: str
    detect: Callable[[frozenset[str]], bool]
    read: Callable[[_Table, str, str], list[_Draft]]


# ------------------------------------------------------------------ generic row layouts

# (whole words in the row type, kind). First hit wins; see _row_kind.
_KIND_WORDS: tuple[tuple[tuple[str, ...], LineKind], ...] = (
    (("payout reversal", "payout failure", "hold", "holds", "release", "currency conversion"), LineKind.ADJUSTMENT),
    (("payout", "payouts", "withdrawal", "withdraw", "transfer to bank", "liquidacao", "pagamento ao comerciante"),
     LineKind.PAYOUT),
    (("chargeback fee", "dispute fee"), LineKind.FEE),
    (("partial capture reversal",), LineKind.REFUND),
    (("chargeback", "chargebacks", "dispute", "disputes", "payment reversal", "contestacao", "retrocessao",
      "disputa"), LineKind.CHARGEBACK),
    (("refund", "refunds", "refunded", "devolucao", "devolucoes", "estorno", "anulacao", "reembolso"),
     LineKind.REFUND),
    (("fee", "fees", "commission", "commissions", "comissao", "comissoes", "tax", "network cost"), LineKind.FEE),
    (("adjustment", "adjustments", "ajuste", "ajustes", "acerto", "acertos", "correction", "correcao",
      "promotion", "promotions", "reserve", "reserved", "transfer", "topup", "contribution", "conversion"),
     LineKind.ADJUSTMENT),
    (("sale", "sales", "order", "orders", "booking", "reservation", "charge", "payment", "venda", "vendas",
      "compra", "compras", "pagamento", "purchase"), LineKind.SALE),
)
_TOTAL_ROW = frozenset({"total", "totals", "totais", "grand total", "subtotal", "sum"})


def _row_kind(text: str, amount: Decimal | None, *, signed: bool) -> LineKind:
    for words, kind in _KIND_WORDS:
        if any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", text) for w in words):
            return kind
    if signed:
        return LineKind.ADJUSTMENT if text else LineKind.SALE
    return LineKind.SALE if amount is None or amount >= 0 else LineKind.REFUND


def _is_total_row(row: Sequence[str]) -> bool:
    """A totals line at the end of an export: never a sale of its own."""
    first = next((c for c in row if c.strip()), "")
    return _norm(first) in _TOTAL_ROW


@dataclass(frozen=True)
class _RowRules:
    """How one layout's rows turn into payout contributions.

    ``fee_sign``: +1 a positive fee is charged (Stripe, neutral), -1 a negative
    fee is charged (PayPal), 0 fees are printed either way and are charged
    unless the row's own net shows otherwise. ``signed``: refund and dispute
    amounts carry their real sign (Stripe); otherwise they are magnitudes.
    """

    fmt: str
    provider: str | None = None
    default_provider: PayoutProvider | None = None
    fee_sign: int = 0
    signed: bool = False
    row_label: str = "row"


def _generic(rules: _RowRules) -> Callable[[_Table, str, str], list[_Draft]]:
    def read(table: _Table, filename: str, hint: str) -> list[_Draft]:
        col = {role: table.column(aliases) for role, aliases in _A.items()}
        multi = {role: table.columns(_A[role]) for role in ("fee", "refund", "chargeback", "deduction", "adjustment")}
        if col["gross"] is None and not multi["fee"] and col["net"] is None:
            return []
        on_order = pay_order = table.date_order
        fixed = provider_by_key(rules.provider)
        drafts: dict[tuple[str, str], _Draft] = {}
        for line_no, row in table.rows:
            gross = _amount(table.cell(row, col["gross"]))
            kind_text = _norm(table.cell(row, col["type"]))
            kind = _row_kind(kind_text, gross, signed=rules.signed)
            currency = _currency(table.cell(row, col["currency"])) or "EUR"
            payout_id = table.cell(row, col["payout_id"])
            payout_day = _date(table.cell(row, col["payout_date"]), pay_order)
            key = (payout_id or (payout_day.isoformat() if payout_day else ""), currency)
            draft = drafts.get(key)
            if draft is None:
                provider = fixed or provider_named(table.cell(row, col["provider"])) or \
                    provider_named(hint, filename) or rules.default_provider
                draft = drafts[key] = _Draft(provider=provider, currency=currency, fmt=rules.fmt,
                                             row_label=rules.row_label,
                                             net_column=table.shown[col["net"]] if col["net"] is not None else "")
            if payout_id and draft.payout_id is None:
                draft.payout_id = (payout_id, table.where(line_no, col["payout_id"]))  # type: ignore[arg-type]
            if payout_day and draft.payout_date is None:
                draft.payout_date = (payout_day, table.where(line_no, col["payout_date"]))  # type: ignore[arg-type]
            net = _amount(table.cell(row, col["net"]))
            if kind is LineKind.PAYOUT:
                amount = net if net is not None else gross
                if amount is None:
                    raise SettlementReportError("I can't read the amount of a payout in this report.")
                where = table.where(line_no, col["net"] if net is not None else col["gross"])  # type: ignore[arg-type]
                draft.paid_out.append((abs(amount), where))
                reference = table.cell(row, col["reference"])
                if reference and draft.payout_id is None:
                    draft.payout_id = (reference, table.where(line_no, col["reference"]))  # type: ignore[arg-type]
                continue
            fees = [_amount(table.cell(row, i)) for i in multi["fee"]]
            if gross is None and net is None and not any(f is not None for f in fees):
                continue
            line = _line(rules, kind, gross or _ZERO, [f for f in fees if f is not None], row, table, multi, net)
            draft.lines.append(SettlementLine(
                kind=line.kind, reference=table.cell(row, col["reference"]) or None,
                on=_date(table.cell(row, col["on"]), on_order), sales=line.sales, fees=line.fees,
                refunds=line.refunds, chargebacks=line.chargebacks, adjustments=line.adjustments,
                stated_net=net, location=f"csv:row {line_no}"))
        return [d for d in drafts.values() if d.provider is not None]

    return read


def _line(rules: _RowRules, kind: LineKind, gross: Decimal, fees: list[Decimal], row: list[str], table: _Table,
          multi: Mapping[str, list[int]], net: Decimal | None) -> SettlementLine:
    """One row's contributions (see _RowRules for the sign conventions)."""
    sales = refunds = chargebacks = adjustments = fee_row = _ZERO
    if kind is LineKind.SALE and gross < 0 and not rules.signed:
        kind = LineKind.REFUND
    if kind is LineKind.SALE:
        sales = gross
    elif kind is LineKind.REFUND:
        refunds = -gross if rules.signed else abs(gross)
    elif kind is LineKind.CHARGEBACK:
        chargebacks = -gross if rules.signed else abs(gross)
    elif kind is LineKind.FEE:
        fee_row = -gross if rules.signed else abs(gross)
    else:
        adjustments = gross
    refunds += sum((abs(v) for v in (_amount(table.cell(row, i)) for i in multi["refund"]) if v is not None), _ZERO)
    chargebacks += sum((abs(v) for v in (_amount(table.cell(row, i)) for i in multi["chargeback"]) if v is not None),
                       _ZERO)
    adjustments -= sum((abs(v) for v in (_amount(table.cell(row, i)) for i in multi["deduction"]) if v is not None),
                       _ZERO)
    adjustments += sum((v for v in (_amount(table.cell(row, i)) for i in multi["adjustment"]) if v is not None), _ZERO)
    if rules.fee_sign > 0:
        fee = sum(fees, _ZERO)
    elif rules.fee_sign < 0:
        fee = -sum(fees, _ZERO)
    else:
        fee = sum((abs(f) for f in fees), _ZERO)
        if net is not None and fees:
            # Printed either way round: the row's own net says whether the fee was charged or given back.
            derived = sales - refunds - chargebacks + adjustments - fee_row - net
            if abs(derived) == fee:
                fee = derived
    return SettlementLine(kind=kind, sales=sales, fees=fee + fee_row, refunds=refunds, chargebacks=chargebacks,
                          adjustments=adjustments)


# ------------------------------------------------------------------ one row per payout (neutral summary)


def _read_summary(table: _Table, filename: str, hint: str) -> list[_Draft]:
    col = {role: table.column(_A[role]) for role in ("provider", "payout_id", "payout_date", "currency")}
    figures = {name: table.column(aliases) for name, aliases in _SUMMARY.items()}
    order = table.date_order
    drafts = []
    for line_no, row in table.rows:
        provider = provider_named(table.cell(row, col["provider"])) or provider_named(hint, filename)
        draft = _Draft(provider=provider, currency=_currency(table.cell(row, col["currency"])) or "EUR",
                       fmt="neutral_summary_csv")
        if value := table.cell(row, col["payout_id"]):
            draft.payout_id = (value, table.where(line_no, col["payout_id"]))  # type: ignore[arg-type]
        if day := _date(table.cell(row, col["payout_date"]), order):
            draft.payout_date = (day, table.where(line_no, col["payout_date"]))  # type: ignore[arg-type]
        for name, index in figures.items():
            if index is None or not table.cell(row, index):
                continue
            value = _amount(table.cell(row, index))
            if value is None:
                raise SettlementReportError("I can't read one of the amounts in this payout report.")
            magnitude = name in ("fees", "refunds", "chargebacks")
            draft.stated[name] = _Figure(abs(value) if magnitude else value, table.where(line_no, index))
        if "net" not in draft.stated or "gross_sales" not in draft.stated:
            raise SettlementReportError("This payout report does not say what was sold and what was paid out.")
        if draft.provider is not None:
            drafts.append(draft)
    return drafts


# ------------------------------------------------------------------ PayPal


def _read_paypal(table: _Table, filename: str, hint: str) -> list[_Draft]:
    c = {role: table.column(aliases) for role, aliases in {
        "on": ("date",), "time": ("time",), "type": ("type", "description"), "status": ("status",),
        "currency": ("currency",), "gross": ("gross",), "fee": ("fee",), "net": ("net",),
        "reference": ("transaction id",), "balance": ("balance",), "impact": ("balance impact",)}.items()}
    order = table.date_order
    rows = []
    for position, (line_no, row) in enumerate(table.rows):
        status = _norm(table.cell(row, c["status"]))
        if status and status not in ("completed", "concluido", "concluida", "completado", "cleared"):
            continue
        if _norm(table.cell(row, c["impact"])) == "memo":
            continue
        gross, fee, net = (_amount(table.cell(row, c[k])) for k in ("gross", "fee", "net"))
        if gross is None:
            continue
        fee = fee or _ZERO
        net = net if net is not None else gross + fee
        rows.append((_date(table.cell(row, c["on"]), order) or date.min, table.cell(row, c["time"]), position,
                     line_no, row, gross, fee, net))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    drafts: list[_Draft] = []
    pending: dict[str, list[tuple]] = defaultdict(list)
    for entry in rows:
        day, _, _, line_no, row, gross, fee, net = entry
        currency = _currency(table.cell(row, c["currency"])) or "EUR"
        kind = _row_kind(_norm(table.cell(row, c["type"])), gross, signed=True)
        if kind is not LineKind.PAYOUT:
            pending[currency].append(entry)
            continue
        if gross >= 0:  # money added to PayPal from the bank, not a payout
            pending[currency].append(entry)
            continue
        segment = pending.pop(currency, [])
        draft = _Draft(provider=provider_by_key("paypal"), currency=currency, fmt="paypal_activity_csv",
                       row_label="transaction")
        draft.paid_out.append((-gross, table.where(line_no, c["gross"])))  # type: ignore[arg-type]
        if ref := table.cell(row, c["reference"]):
            draft.payout_id = (ref, table.where(line_no, c["reference"]))  # type: ignore[arg-type]
        if day != date.min:
            draft.payout_date = (day, table.where(line_no, c["on"]))  # type: ignore[arg-type]
        for s_day, _, _, s_line, s_row, s_gross, s_fee, s_net in segment:
            s_kind = _row_kind(_norm(table.cell(s_row, c["type"])), s_gross, signed=True)
            if s_kind is LineKind.SALE and s_gross < 0:
                s_kind = LineKind.ADJUSTMENT  # something bought with the PayPal balance
            draft.lines.append(_paypal_line(s_kind, s_gross, s_fee, s_net, table.cell(s_row, c["reference"]),
                                            s_day, s_line))
        balance_col = c["balance"]
        if balance_col is not None:
            first_row, first_net = (segment[0][4], segment[0][7]) if segment else (row, net)
            before = _amount(table.cell(first_row, balance_col))
            after = _amount(table.cell(row, balance_col))
            if before is not None and after is not None:
                opening = before - first_net
                draft.carry = _Figure(opening - after, f"csv:PayPal balance before these rows and after row {line_no}")
        drafts.append(draft)
    return drafts


def _paypal_line(kind: LineKind, gross: Decimal, fee: Decimal, net: Decimal, reference: str, on: date,
                 line_no: int) -> SettlementLine:
    charged = -fee  # PayPal prints a charged fee as a negative number
    parts: dict[str, Decimal] = {"sales": _ZERO, "refunds": _ZERO, "chargebacks": _ZERO, "adjustments": _ZERO}
    fee_row = _ZERO
    if kind is LineKind.SALE:
        parts["sales"] = gross
    elif kind is LineKind.REFUND:
        parts["refunds"] = -gross
    elif kind is LineKind.CHARGEBACK:
        parts["chargebacks"] = -gross
    elif kind is LineKind.FEE:
        fee_row = -gross
    else:
        parts["adjustments"] = gross
    return SettlementLine(kind=kind, reference=reference or None, on=on if on != date.min else None,
                          fees=charged + fee_row, stated_net=net, location=f"csv:row {line_no}", **parts)


# ------------------------------------------------------------------ Airbnb


def _read_airbnb(table: _Table, filename: str, hint: str) -> list[_Draft]:
    c = {role: table.column(aliases) for role, aliases in {
        "on": ("date",), "type": ("type",), "reference": ("confirmation code", "reference code"),
        "currency": ("currency",), "amount": ("amount",), "paid_out": ("paid out",),
        "gross": ("gross earnings",)}.items()}
    fee_cols = table.columns(("service fee", "fast pay fee", "host fee"))
    order = table.date_order
    payouts: dict[tuple[date | None, str], _Draft] = {}
    others: list[tuple[date | None, str, SettlementLine]] = []
    for line_no, row in table.rows:
        day = _date(table.cell(row, c["on"]), order)
        currency = _currency(table.cell(row, c["currency"])) or "EUR"
        kind_text = _norm(table.cell(row, c["type"]))
        amount = _amount(table.cell(row, c["amount"]))
        if "payout" in kind_text and "resolution" not in kind_text:
            paid = _amount(table.cell(row, c["paid_out"]))
            value = paid if paid is not None else amount
            if value is None:
                continue
            draft = payouts.setdefault((day, currency), _Draft(
                provider=provider_by_key("airbnb"), currency=currency, fmt="airbnb_transactions_csv",
                row_label="booking"))
            where = table.where(line_no, c["paid_out"] if paid is not None else c["amount"])  # type: ignore[arg-type]
            draft.paid_out.append((abs(value), where))
            if day and draft.payout_date is None:
                draft.payout_date = (day, table.where(line_no, c["on"]))  # type: ignore[arg-type]
            continue
        if amount is None:
            continue
        fees = sum((abs(v) for v in (_amount(table.cell(row, i)) for i in fee_cols) if v is not None), _ZERO)
        reference = table.cell(row, c["reference"]) or None
        if "reservation" in kind_text:
            gross = _amount(table.cell(row, c["gross"]))
            line = SettlementLine(LineKind.SALE, reference, day, sales=gross if gross is not None else amount + fees,
                                  fees=fees, stated_net=amount if gross is not None else None,
                                  location=f"csv:row {line_no}")
        elif "fee" in kind_text:
            line = SettlementLine(LineKind.FEE, reference, day, fees=abs(amount), stated_net=-abs(amount),
                                  location=f"csv:row {line_no}")
        else:
            line = SettlementLine(LineKind.ADJUSTMENT, reference, day, adjustments=amount, stated_net=amount,
                                  location=f"csv:row {line_no}")
        others.append((day, currency, line))
    for day, currency, line in others:
        target = payouts.get((day, currency))
        if target is None and len(payouts) == 1:
            target = next(iter(payouts.values()))
        if target is not None:
            target.lines.append(line)
    return list(payouts.values())


# ------------------------------------------------------------------ layouts, most specific first


def _is_stripe(h: frozenset[str]) -> bool:
    return "reporting category" in h and {"gross", "fee", "net"} <= h


def _is_paypal(h: frozenset[str]) -> bool:
    return {"gross", "fee", "net", "transaction id"} <= h and _has(h, ("type", "description")) and \
        _has(h, ("balance", "status"))


def _is_airbnb(h: frozenset[str]) -> bool:
    return "confirmation code" in h and "type" in h and _has(h, ("paid out", "amount"))


def _is_booking(h: frozenset[str]) -> bool:
    return _has(h, ("reservation number", "booking number", "reservation id")) and \
        _has(h, ("commission", "commission amount", "payments service fee"))


def _delivery_provider(h: frozenset[str]) -> str | None:
    for key, marks in _DELIVERY.items():
        if _has(h, marks):
            return key
    return None


def _is_delivery(h: frozenset[str]) -> bool:
    orders = _has(h, ("order id", "order code", "order number", "order reference"))
    return orders and _has(h, _A["gross"]) and (_has(h, _A["fee"]) or _has(h, _A["net"]))


def _is_terminal(h: frozenset[str]) -> bool:
    return _has(h, _PT_TERMINAL) and _has(h, _A["gross"]) and (_has(h, _A["fee"]) or _has(h, _A["net"]))


def _is_neutral_lines(h: frozenset[str]) -> bool:
    return "type" in h and _has(h, ("amount", "gross")) and _has(h, ("payout id", "payout date", "payout reference"))


def _is_neutral_summary(h: frozenset[str]) -> bool:
    return ("type" not in h and _has(h, _SUMMARY["gross_sales"]) and _has(h, _SUMMARY["fees"])
            and _has(h, _SUMMARY["net"]) and _has(h, ("payout id", "payout date", "payout reference")))


def _read_delivery(table: _Table, filename: str, hint: str) -> list[_Draft]:
    key = _delivery_provider(frozenset(table.headers))
    rules = _RowRules(fmt=f"{key or 'delivery'}_statement_csv", provider=key, row_label="order")
    return _generic(rules)(table, filename, hint)


_LAYOUTS: tuple[_Layout, ...] = (
    _Layout("stripe", _is_stripe, _generic(_RowRules("stripe_itemized_csv", "stripe", fee_sign=1, signed=True,
                                                     row_label="payment"))),
    _Layout("paypal", _is_paypal, _read_paypal),
    _Layout("airbnb", _is_airbnb, _read_airbnb),
    _Layout("booking", _is_booking, _generic(_RowRules("booking_payout_csv", "booking", row_label="booking"))),
    _Layout("delivery", _is_delivery, _read_delivery),
    _Layout("card_terminal", _is_terminal, _generic(_RowRules("card_terminal_csv", default_provider=CARD_TERMINAL,
                                                              row_label="card payment"))),
    _Layout("neutral_lines", _is_neutral_lines, _generic(_RowRules("neutral_lines_csv", fee_sign=1,
                                                                   row_label="row"))),
    _Layout("neutral_summary", _is_neutral_summary, _read_summary),
)


# --------------------------------------------------------------------------- JSON


def _from_json(parsed: Any, filename: str, hint: str) -> list[_Draft]:
    stripe = _stripe_json(parsed)
    if stripe is not None:
        return stripe
    root_provider: Any = None
    if isinstance(parsed, list):
        found = [(item, f"$[{i}]") for i, item in enumerate(parsed)]
    elif isinstance(parsed, dict):
        keys = {_norm(k): k for k in parsed}
        root_provider = parsed.get(keys["provider"]) if "provider" in keys else None
        if "payouts" in keys and isinstance(parsed[keys["payouts"]], list):
            found = [(item, f"$.{keys['payouts']}[{i}]") for i, item in enumerate(parsed[keys["payouts"]])]
        elif "payout" in keys and isinstance(parsed[keys["payout"]], dict):
            merged = {**parsed[keys["payout"]]}  # {"payout": {...}, "lines": [...]}: the lines belong to it
            inner = {_norm(k) for k in merged}
            for extra in ("lines", "orders", "transactions", "items", "bookings"):
                if extra in keys and extra not in inner:
                    merged[keys[extra]] = parsed[keys[extra]]
            found = [(merged, f"$.{keys['payout']}")]
        else:
            found = [(parsed, "$")]
    else:
        return []
    drafts = []
    for item, path in found:
        if isinstance(item, dict):
            draft = _neutral_payout(item, root_provider, path, filename, hint)
            if draft is not None:
                drafts.append(draft)
    return drafts


def _pick(obj: Mapping[str, Any], aliases: Sequence[str]) -> tuple[Any, str] | None:
    keys = {_norm(k): k for k in obj}
    for alias in aliases:
        if alias in keys and obj[keys[alias]] not in (None, ""):
            return obj[keys[alias]], keys[alias]
    return None


_JSON: dict[str, tuple[str, ...]] = {
    "provider": ("provider", "platform", "acquirer"),
    "id": ("payout id", "id", "payout reference", "reference", "batch id", "settlement id"),
    "date": ("payout date", "date", "arrival date", "paid on", "settlement date"),
    "currency": ("currency",),
    "lines": ("lines", "orders", "transactions", "items", "bookings"),
    "line_type": ("type", "kind", "reporting category", "category"),
    "line_reference": ("reference", "order id", "id", "booking id", "reservation number", "transaction id"),
    "line_date": ("date", "order date", "created"),
    "line_gross": ("gross", "amount", "sales", "gross amount", "total"),
    "line_fee": ("fee", "fees", "commission", "commissions"),
    "line_refund": ("refund", "refunds"),
    "line_net": ("net", "net amount", "payout"),
}


def _neutral_payout(obj: Mapping[str, Any], root_provider: Any, path: str, filename: str,
                    hint: str) -> _Draft | None:
    lines = _pick(obj, _JSON["lines"])
    if not any(_pick(obj, aliases) for aliases in _SUMMARY.values()) and not (lines and isinstance(lines[0], list)):
        return None  # no figures at all: some other JSON file
    named = _pick(obj, _JSON["provider"])
    provider = provider_named(str(named[0])) if named else None
    provider = provider or (provider_named(str(root_provider)) if root_provider else None) or \
        provider_named(hint, filename)
    if provider is None:
        return None
    currency = _pick(obj, _JSON["currency"])
    draft = _Draft(provider=provider, currency=_currency(currency[0]) if currency else "EUR", fmt="neutral_json")
    draft.currency = draft.currency or "EUR"
    if found := _pick(obj, _JSON["id"]):
        draft.payout_id = (str(found[0]), f"json:{path}.{found[1]}")
    if found := _pick(obj, _JSON["date"]):
        day = _date(found[0])
        if day:
            draft.payout_date = (day, f"json:{path}.{found[1]}")
    for name, aliases in _SUMMARY.items():
        if found := _pick(obj, aliases):
            value = _amount(found[0])
            if value is None:
                raise SettlementReportError("I can't read one of the amounts in this payout report.")
            magnitude = name in ("fees", "refunds", "chargebacks")
            draft.stated[name] = _Figure(abs(value) if magnitude else value, f"json:{path}.{found[1]}")
    rules = _RowRules("neutral_json", fee_sign=1)
    if lines and isinstance(lines[0], list):
        draft.row_label = "line"
        for i, item in enumerate(lines[0]):
            if not isinstance(item, dict):
                continue
            gross = _amount((_pick(item, _JSON["line_gross"]) or (None,))[0])
            net = _amount((_pick(item, _JSON["line_net"]) or (None,))[0])
            kind = _row_kind(_norm((_pick(item, _JSON["line_type"]) or ("",))[0]), gross, signed=False)
            if kind is LineKind.PAYOUT:
                continue
            fee = _amount((_pick(item, _JSON["line_fee"]) or (None,))[0])
            refund = _amount((_pick(item, _JSON["line_refund"]) or (None,))[0])
            base = _line(rules, kind, gross or _ZERO, [fee] if fee is not None else [], [], _Table([], [], []),
                         {"refund": [], "chargeback": [], "deduction": [], "adjustment": []}, net)
            ref = _pick(item, _JSON["line_reference"])
            when = _pick(item, _JSON["line_date"])
            draft.lines.append(SettlementLine(
                kind=base.kind, reference=str(ref[0]) if ref else None, on=_date(when[0]) if when else None,
                sales=base.sales, fees=base.fees, refunds=base.refunds + (abs(refund) if refund else _ZERO),
                chargebacks=base.chargebacks, adjustments=base.adjustments, stated_net=net,
                location=f"json:{path}.{lines[1]}[{i}]"))
    if "net" not in draft.stated and not draft.lines:
        raise SettlementReportError("This payout report does not say what was paid out.")
    if "gross_sales" not in draft.stated and not draft.lines:
        raise SettlementReportError("This payout report does not say what was sold.")
    return draft


def _minor(value: Decimal, scale: Decimal) -> Decimal:
    """Minor units (cents) to money: 95100 -> 951.00; zero-decimal currencies unchanged."""
    return value if scale == 1 else value.scaleb(-2)


_ZERO_DECIMAL_CURRENCIES = frozenset({"JPY", "KRW", "VND", "CLP", "ISK", "HUF", "TWD", "UGX", "XOF", "XAF"})


def _stripe_json(parsed: Any) -> list[_Draft] | None:
    """Stripe balance transactions (``GET /v1/balance_transactions?payout=...``), amounts in minor units."""
    payout: Mapping[str, Any] | None = None
    items: Any = None
    wrapped = parsed.get("payout") if isinstance(parsed, dict) else None
    if isinstance(wrapped, dict) and wrapped.get("object") == "payout":
        payout = wrapped
        items = parsed.get("balance_transactions") or parsed.get("data") or []
    elif isinstance(parsed, dict) and parsed.get("object") == "list":
        items = parsed.get("data")
    elif isinstance(parsed, list):
        items = parsed
    if isinstance(items, dict):
        items = items.get("data")
    if not isinstance(items, list) or not items or \
            not all(isinstance(i, dict) and i.get("object") == "balance_transaction" for i in items):
        return None if payout is None else []
    groups: dict[str, _Draft] = {}
    for index, item in enumerate(items):
        currency = _currency(item.get("currency")) or "EUR"
        draft = groups.get(currency)
        if draft is None:
            draft = groups[currency] = _Draft(provider=provider_by_key("stripe"), currency=currency,
                                              fmt="stripe_balance_json", row_label="payment")
        scale = Decimal(1) if currency in _ZERO_DECIMAL_CURRENCIES else Decimal(100)
        gross, fee, net = (_amount(item.get(k)) for k in ("amount", "fee", "net"))
        if gross is None:
            raise SettlementReportError("I can't read one of the amounts in this Stripe report.")
        gross, fee = _minor(gross, scale), _minor(fee or _ZERO, scale)
        net = _minor(net, scale) if net is not None else gross - fee
        category = _norm(item.get("reporting_category") or item.get("type") or "")
        kind = _row_kind(category, gross, signed=True)
        where = f"json:$.data[{index}]"
        if kind is LineKind.PAYOUT:
            draft.paid_out.append((-gross, f"{where}.amount"))
            if item.get("source") and draft.payout_id is None:
                draft.payout_id = (str(item["source"]), f"{where}.source")
            if day := _date(item.get("available_on") or item.get("created")):
                draft.payout_date = draft.payout_date or (day, f"{where}.available_on")
            continue
        line = _line(_RowRules("stripe_balance_json", fee_sign=1, signed=True), kind, gross, [fee], [],
                     _Table([], [], []), {"refund": [], "chargeback": [], "deduction": [], "adjustment": []}, net)
        draft.lines.append(SettlementLine(
            kind=line.kind, reference=str(item.get("source") or item.get("id") or "") or None,
            on=_date(item.get("created")), sales=line.sales, fees=line.fees, refunds=line.refunds,
            chargebacks=line.chargebacks, adjustments=line.adjustments, stated_net=net, location=where))
    if payout is not None:
        currency = _currency(payout.get("currency")) or "EUR"
        draft = groups.setdefault(currency, _Draft(provider=provider_by_key("stripe"), currency=currency,
                                                   fmt="stripe_balance_json", row_label="payment"))
        scale = Decimal(1) if currency in _ZERO_DECIMAL_CURRENCIES else Decimal(100)
        amount = _amount(payout.get("amount"))
        if amount is not None:
            draft.paid_out = [(_minor(amount, scale), "json:$.payout.amount")]
        if payout.get("id"):
            draft.payout_id = (str(payout["id"]), "json:$.payout.id")
        if day := _date(payout.get("arrival_date")):
            draft.payout_date = (day, "json:$.payout.arrival_date")
    return list(groups.values())
