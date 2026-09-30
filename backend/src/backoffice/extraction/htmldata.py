"""schema.org Invoice / Order structured data in HTML (§8, §13 Stage 0).

Invoice emails and portal pages often carry machine-readable markup, either
JSON-LD (``<script type="application/ld+json">``) or microdata
(``itemscope`` / ``itemprop``). Both are read with the standard library
into the same node shape and mapped by one set of rules:

* Invoice: ``identifier`` -> invoice number, ``totalPaymentDue`` -> gross and
  currency, ``paymentDueDate`` -> due date, ``provider`` / ``customer``
  ``vatID`` or ``taxID`` -> tax ids. ``totalPaymentDue`` is what is still
  owed: on an invoice marked paid (``PaymentComplete``,
  ``PaymentAutomaticallyApplied``), or when it is zero, it says nothing
  about the invoice total, so it is kept as the extra ``amount_payable``
  and never claimed as the gross amount (§19). Otherwise it is taken as
  the total, which is how invoice email markup uses it.
* Order: ``price`` / ``priceSpecification`` -> gross and currency (lower
  confidence: an order total is not an invoice), ``seller`` / ``customer``
  tax ids, ``paymentDueDate``; ``orderNumber`` is kept as an extra, never as
  an invoice number.

JSON numbers are parsed as Decimal, never float.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any

from backoffice.domain.models import CriticalField, ExtractionMethod

from ._collect import Collector
from .fields import Stage0Result, StructuredDataError
from .values import normalize_currency, parse_amount, parse_date

__all__ = [
    "HTML_INVOICE_CONFIDENCE",
    "HTML_ORDER_TOTAL_CONFIDENCE",
    "MAX_HTML_BYTES",
    "extract_html_structured",
]

F = CriticalField

MAX_HTML_BYTES = 10 * 1024 * 1024
HTML_INVOICE_CONFIDENCE = 0.90
HTML_ORDER_TOTAL_CONFIDENCE = 0.70
_MAX_DEPTH = 32
_MAX_ELEMENT_DEPTH = 512
_MAX_TEXT_FRAMES = 16
_MAX_VALUE_CHARS = 1024
# Elements an author often leaves unclosed between siblings ("<li>a<li>b").
_SELF_CLOSING_SIBLINGS = frozenset({"p", "li", "td", "th", "tr", "option", "dt", "dd"})

# Elements without end tags.
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
     "param", "source", "track", "wbr"}
)  # fmt: skip
# Microdata value attributes by element (WHATWG HTML, "microdata property value").
_VALUE_ATTRIBUTE: Mapping[str, str] = {
    "meta": "content", "audio": "src", "embed": "src", "iframe": "src", "img": "src",
    "source": "src", "track": "src", "video": "src", "a": "href", "area": "href",
    "link": "href", "object": "data", "data": "value", "meter": "value", "time": "datetime",
}  # fmt: skip


def extract_html_structured(html: str | bytes, *, source: str) -> tuple[Stage0Result, ...]:
    """One Stage0Result per schema.org Invoice or Order found in ``html``."""
    size = len(html) if isinstance(html, bytes) else len(html.encode("utf-8"))
    if size > MAX_HTML_BYTES:
        raise StructuredDataError("html_too_large", f"{size} bytes")
    text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    parser = _MarkupParser()
    parser.feed(text)
    parser.close()

    results: list[Stage0Result] = []
    for i, block in enumerate(parser.jsonld):
        try:
            document = json.loads(block, parse_float=Decimal)
        except (ValueError, RecursionError):  # RecursionError: hostile nesting depth
            results.append(Stage0Result.build("html_jsonld", source, (), notes=["jsonld_unreadable"]))
            continue
        for path, node in _typed_nodes(document, f"json-ld[{i}]"):
            results.append(_map_node(node, path, "html_jsonld", source))
    for i, item in enumerate(parser.items):
        for path, node in _typed_nodes(item, f"microdata[{i}]"):
            results.append(_map_node(node, path, "html_microdata", source))
    if parser.too_deep:
        results.append(Stage0Result.build("html_microdata", source, (), notes=["microdata_too_deep"]))
    return tuple(results)


# --------------------------------------------------------------------------- parsing


@dataclass
class _Frame:
    tag: str
    item: dict[str, Any] | None = None
    props: tuple[str, ...] = ()
    text: list[str] | None = None
    size: int = 0


class _MarkupParser(HTMLParser):
    """Collects JSON-LD blocks and top-level microdata items in linear time.

    Hostile markup is bounded (§52): open elements are capped at
    ``_MAX_ELEMENT_DEPTH`` (beyond it microdata reading stops and
    ``too_deep`` is set; JSON-LD is still collected), at most
    ``_MAX_TEXT_FRAMES`` nested properties collect text, each up to
    ``_MAX_VALUE_CHARS``, and open tags are counted so an end tag never
    rescans the stack.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.jsonld: list[str] = []
        self.items: list[dict[str, Any]] = []
        self.too_deep = False
        self._stack: list[_Frame] = []
        self._open: Counter[str] = Counter()
        self._item_frames: list[_Frame] = []
        self._text_frames: list[_Frame] = []
        self._script: list[str] | None = None
        self._raw_text_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in ("script", "style"):
            self._raw_text_tag = tag
            is_jsonld = tag == "script" and (a.get("type") or "").strip().lower() == "application/ld+json"
            self._script = [] if is_jsonld else None
            return
        self._start(tag, a, void=tag in _VOID)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, dict(attrs), void=True)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._raw_text_tag:
            if self._script is not None:
                self.jsonld.append("".join(self._script))
            self._script, self._raw_text_tag = None, None
            return
        if self._open[tag] <= 0:
            return
        while self._stack:
            frame = self._pop()
            self._finish(frame)
            if frame.tag == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._raw_text_tag is not None:
            if self._script is not None:
                self._script.append(data)
            return
        for frame in self._text_frames:
            if frame.text is not None and frame.size < _MAX_VALUE_CHARS:
                frame.text.append(data)
                frame.size += len(data)

    def close(self) -> None:
        super().close()
        while self._stack:
            self._finish(self._pop())

    def _start(self, tag: str, attrs: dict[str, str | None], *, void: bool) -> None:
        if self.too_deep:
            return
        if self._stack and tag in _SELF_CLOSING_SIBLINGS and self._stack[-1].tag == tag:
            self._finish(self._pop())  # "<li>a<li>b": the second item closes the first
        if not void and len(self._stack) >= _MAX_ELEMENT_DEPTH:
            self._overflow()
            return
        props = tuple((attrs.get("itemprop") or "").split())
        if "itemscope" in attrs:
            types = (attrs.get("itemtype") or "").split()
            frame = _Frame(tag, item={"@type": types}, props=props)
        elif props and tag in _VALUE_ATTRIBUTE and attrs.get(_VALUE_ATTRIBUTE[tag]) is not None:
            self._assign(props, attrs[_VALUE_ATTRIBUTE[tag]])
            frame = _Frame(tag)
        elif props and len(self._text_frames) < _MAX_TEXT_FRAMES:
            frame = _Frame(tag, props=props, text=[])
        else:
            frame = _Frame(tag)
        if void:
            self._finish(frame)
            return
        self._stack.append(frame)
        self._open[tag] += 1
        if frame.item is not None:
            self._item_frames.append(frame)
        if frame.text is not None:
            self._text_frames.append(frame)

    def _pop(self) -> _Frame:
        frame = self._stack.pop()
        self._open[frame.tag] -= 1
        if self._item_frames and self._item_frames[-1] is frame:
            self._item_frames.pop()
        if self._text_frames and self._text_frames[-1] is frame:
            self._text_frames.pop()
        return frame

    def _overflow(self) -> None:
        """Stop reading microdata; items still open are incomplete and dropped."""
        self.too_deep = True
        self._stack.clear()
        self._open.clear()
        self._item_frames.clear()
        self._text_frames.clear()

    def _finish(self, frame: _Frame) -> None:
        if frame.item is not None:
            if frame.props and self._parent_item() is not None:
                self._assign(frame.props, frame.item)
            else:
                self.items.append(frame.item)
        elif frame.text is not None:
            self._assign(frame.props, " ".join("".join(frame.text).split())[:_MAX_VALUE_CHARS])

    def _parent_item(self) -> dict[str, Any] | None:
        return self._item_frames[-1].item if self._item_frames else None

    def _assign(self, props: tuple[str, ...], value: Any) -> None:
        parent = self._parent_item()
        if parent is None:
            return
        for prop in props:
            parent.setdefault(prop, []).append(value)


# --------------------------------------------------------------------------- mapping


def _type_names(node: Mapping[str, Any]) -> set[str]:
    raw = node.get("@type", ())
    types = [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, list) else []
    return {str(t).rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1] for t in types}


def _typed_nodes(document: Any, path: str, depth: int = 0) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Every Invoice or Order node, depth-first, with a readable path."""
    if depth > _MAX_DEPTH:
        return
    if isinstance(document, list):
        for i, item in enumerate(document):
            yield from _typed_nodes(item, f"{path}[{i}]", depth + 1)
    elif isinstance(document, Mapping):
        if _type_names(document) & {"Invoice", "Order"}:
            yield path, document
        for key, value in document.items():
            if isinstance(value, (list, Mapping)):
                yield from _typed_nodes(value, f"{path}.{key}", depth + 1)


def _values(node: Mapping[str, Any], name: str) -> list[Any]:
    """Property values, accepting "name", "schema:name" and full schema.org IRIs."""
    for key in (name, f"schema:{name}", f"http://schema.org/{name}", f"https://schema.org/{name}"):
        if key in node:
            raw = node[key]
            values = raw if isinstance(raw, list) else [raw]
            return [v["@value"] if isinstance(v, Mapping) and "@value" in v else v for v in values]
    return []


def _nodes(node: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    return [v for v in _values(node, name) if isinstance(v, Mapping)]


def _scalar(value: Any) -> Any:
    if isinstance(value, Mapping):  # PropertyValue
        return value.get("value")
    return value


def _map_node(node: Mapping[str, Any], path: str, kind: str, source: str) -> Stage0Result:
    c = Collector(kind, source, ExtractionMethod.HTML_STRUCTURED, HTML_INVOICE_CONFIDENCE)
    if "Invoice" in _type_names(node):
        _map_invoice(node, path, c)
        c.kind = f"{kind}_invoice"
    else:
        _map_order(node, path, c)
        c.kind = f"{kind}_order"
    return c.result()


# schema.org PaymentStatusType members meaning nothing is owed any more.
_SETTLED_PAYMENT_STATUSES = frozenset({"PaymentComplete", "PaymentAutomaticallyApplied"})


def _enum_name(value: Any) -> str:
    """"https://schema.org/PaymentComplete", "schema:PaymentComplete" or {"@id": ...} -> "PaymentComplete"."""
    if isinstance(value, Mapping):
        value = value.get("@id") or value.get("value") or value.get("name") or ""
    return str(value).strip().rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def _map_invoice(node: Mapping[str, Any], path: str, c: Collector) -> None:
    for value in _values(node, "identifier"):
        c.add(F.INVOICE_NUMBER, _scalar(value), f"{path}.identifier")
    settled = any(_enum_name(v) in _SETTLED_PAYMENT_STATUSES for v in _values(node, "paymentStatus"))
    for value in _values(node, "totalPaymentDue"):
        _total_due(c, value, f"{path}.totalPaymentDue", settled=settled)
    _due_dates(node, path, c)
    _party(node, "provider", "supplier", F.SUPPLIER_TAX_ID, path, c)
    _party(node, "customer", "customer", F.CUSTOMER_TAX_ID, path, c)
    for name in ("accountId", "paymentStatus", "confirmationNumber"):
        for value in _values(node, name):
            c.extra(name, _scalar(value))


def _map_order(node: Mapping[str, Any], path: str, c: Collector) -> None:
    currency_values = _values(node, "priceCurrency")
    for value in _values(node, "price"):
        currency = currency_values[0] if currency_values else None
        _money(c, {"price": value, "priceCurrency": currency}, f"{path}.price", HTML_ORDER_TOTAL_CONFIDENCE)
    for value in _values(node, "priceSpecification"):
        _money(c, value, f"{path}.priceSpecification", HTML_ORDER_TOTAL_CONFIDENCE)
    _due_dates(node, path, c)
    _party(node, "seller", "supplier", F.SUPPLIER_TAX_ID, path, c)
    _party(node, "customer", "customer", F.CUSTOMER_TAX_ID, path, c)
    for key, name in (("order_number", "orderNumber"), ("order_date", "orderDate")):
        for value in _values(node, name):
            c.extra(key, _scalar(value))


def _money_parts(value: Any) -> tuple[Any, Any]:
    """(amount, currency) of a MonetaryAmount (value/currency), PriceSpecification
    (price/priceCurrency) or a bare number."""
    if isinstance(value, Mapping):
        amount_raw = next(iter(_values(value, "value") or _values(value, "price")), None)
        currency_raw = next(iter(_values(value, "currency") or _values(value, "priceCurrency")), None)
        return amount_raw, currency_raw
    return value, None


def _money(c: Collector, value: Any, location: str, confidence: float) -> None:
    amount_raw, currency_raw = _money_parts(value)
    c.add(F.GROSS_AMOUNT, _amount(c, amount_raw, location), location, confidence=confidence)
    _currency(c, currency_raw, location, confidence)


def _total_due(c: Collector, value: Any, location: str, *, settled: bool) -> None:
    """An invoice's total due: the gross amount unless it is zero or the invoice is paid."""
    amount_raw, currency_raw = _money_parts(value)
    amount = _amount(c, amount_raw, location)
    if amount is not None:
        c.extra("amount_payable", amount)
        if settled or amount == 0:
            c.note("total_due_not_invoice_total")
        else:
            c.add(F.GROSS_AMOUNT, amount, location, confidence=HTML_INVOICE_CONFIDENCE)
    _currency(c, currency_raw, location, HTML_INVOICE_CONFIDENCE)


def _amount(c: Collector, raw: Any, location: str) -> Decimal | None:
    amount = parse_amount(raw)
    if raw is not None and amount is None:
        c.note(f"unreadable_amount:{location}")
    return amount


def _currency(c: Collector, raw: Any, location: str, confidence: float) -> None:
    currency = normalize_currency(raw)
    if raw is not None and currency is None:
        c.note(f"unreadable_currency:{location}")
    c.add(F.CURRENCY, currency, f"{location}.currency", confidence=confidence)


def _due_dates(node: Mapping[str, Any], path: str, c: Collector) -> None:
    for name in ("paymentDueDate", "paymentDue"):
        for value in _values(node, name):
            due = parse_date(_scalar(value))
            if due is None:
                c.note(f"unreadable_date:{path}.{name}")
            c.add(F.DUE_DATE, due, f"{path}.{name}")


def _party(
    node: Mapping[str, Any], prop: str, role: str, field: CriticalField, path: str, c: Collector
) -> None:
    for party in _nodes(node, prop):
        for value in _values(party, "name"):
            c.extra(f"{role}_name", _scalar(value))
        for id_prop in ("vatID", "taxID"):
            for value in _values(party, id_prop):
                c.add(field, _scalar(value), f"{path}.{prop}.{id_prop}")
