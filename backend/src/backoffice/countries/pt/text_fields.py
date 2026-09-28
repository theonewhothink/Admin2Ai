"""Candidate fields from Portuguese OCR or plain text (§13, §17, §18).

Everything here is a *candidate*: low-confidence observations that other
sources (QR, structured XML, bank) confirm or contradict. The rules are
deliberately conservative (§19 "never guess"):

* a value is only taken when a label introduces it ("Total", "IVA",
  "Data de emissão", "NIF"...), and the label is followed by exactly one value;
* when the best label tier yields different values, the field is reported as
  ambiguous and no observation is emitted;
* NIFs are only taken next to a tax-number label and must pass the check digit;
  who they belong to (supplier / customer) comes from the text or from ids the
  caller already knows, never from position alone.

Portuguese number format: "1.492,30" (also "1 492,30" and "1492,30"). A plain
"483.60" with exactly two decimals is accepted too.
"""

from __future__ import annotations

import re
from bisect import bisect_left, insort
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import MappingProxyType

from backoffice.countries.base import NamedObservation
from backoffice.domain.models import CriticalField, ExtractionMethod

from ._text import clean, fold
from .atcud import ATCUD, ATCUDError, parse_atcud, parse_document_number
from .banking import MultibancoError, MultibancoReference, find_ibans, parse_multibanco
from .nif import FINAL_CONSUMER_NIF, normalize_nif, validate_nif

# Confidence of text candidates (method OCR by default, so they already rank
# below QR / XML). Tier 1 labels are explicit ("Total do documento"), tier 2
# generic ("Total"), tier 3 weak ("A pagar").
_TIER_CONFIDENCE = {1: 0.60, 2: 0.50, 3: 0.40}
_NIF_CONFIDENCE = 0.65  # labelled + check digit passed
_NIF_KNOWN_CONFIDENCE = 0.70  # matches an id the caller already knows
_NIF_DEDUCED_CONFIDENCE = 0.50  # the only other valid NIF on the document
_IBAN_CONFIDENCE = 0.70  # mod-97 passed
_MULTIBANCO_CONFIDENCE = 0.60
_DOC_NUMBER_CONFIDENCE = 0.60
_DOC_NUMBER_BY_ATCUD_CONFIDENCE = 0.55

# --------------------------------------------------------------------------- #
# Numbers and dates
# --------------------------------------------------------------------------- #

_AMOUNT = re.compile(
    r"(?<![0-9.,])(?P<sign>-)?"
    r"(?:(?P<int>[0-9]{1,3}(?:\.[0-9]{3})+|[0-9]{1,3}(?: [0-9]{3})+|[0-9]+),(?P<cents>[0-9]{2})"
    r"|(?P<dint>[0-9]+)\.(?P<dcents>[0-9]{2}))"
    r"(?![0-9]|[.,][0-9])(?!\s*%)"
)


def parse_pt_amount(text: str) -> Decimal | None:
    """"1.492,30" -> Decimal("1492.30"). None when the text is not one amount."""
    match = _AMOUNT.fullmatch(clean(text).strip()) if isinstance(text, str) else None
    return _amount_from_match(match) if match else None


def _amount_from_match(match: re.Match[str]) -> Decimal:
    sign = match.group("sign") or ""
    if match.group("int") is not None:
        whole = match.group("int").replace(".", "").replace(" ", "")
        return Decimal(f"{sign}{whole}.{match.group('cents')}")
    return Decimal(f"{sign}{match.group('dint')}.{match.group('dcents')}")


_MONTHS: Mapping[str, int] = MappingProxyType({
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
})
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
# Works on folded (lower-case, accent-free) text.
_DATE = re.compile(
    r"(?<![0-9])(?:"
    r"(?P<d>[0-9]{1,2})(?P<s>[/.\-])(?P<m>[0-9]{1,2})(?P=s)(?P<y>[0-9]{4})"
    r"|(?P<y2>[0-9]{4})(?P<s2>[/.\-])(?P<m2>[0-9]{1,2})(?P=s2)(?P<d2>[0-9]{1,2})"
    rf"|(?P<d3>[0-9]{{1,2}})(?:\s+de\s+|[\s/.\-]+)(?P<mn>{_MONTH_ALT})\.?(?![a-z])"
    r"(?:\s+de\s+|[\s/.\-]+)(?P<y3>[0-9]{4})"
    r")(?![0-9])"
)


def parse_pt_date(text: str) -> date | None:
    """dd/mm/yyyy, dd-mm-yyyy, dd.mm.yyyy, yyyy-mm-dd, "18 de setembro de 2026"."""
    if not isinstance(text, str):
        return None
    match = _DATE.fullmatch(fold(clean(text)).strip())
    return _date_from_match(match) if match else None


def _date_from_match(match: re.Match[str]) -> date | None:
    if match.group("d"):
        d, m, y = match.group("d"), match.group("m"), match.group("y")
    elif match.group("y2"):
        d, m, y = match.group("d2"), match.group("m2"), match.group("y2")
    else:
        d, m, y = match.group("d3"), str(_MONTHS[match.group("mn")]), match.group("y3")
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Labels (on folded text)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _LabelSpec:
    key: str  # gross | payable | net | vat | issue_date | due_date | withholding
    tier: int
    pattern: re.Pattern[str]


def _specs(key: str, tier: int, *patterns: str) -> list[_LabelSpec]:
    return [
        _LabelSpec(key, tier, re.compile(rf"(?<![a-z0-9])(?:{p})(?![a-z0-9])")) for p in patterns
    ]


_NO = r"(?:n\.?\s*[ºo°]\.?|numero|nr\.?)"
_LABELS: tuple[_LabelSpec, ...] = tuple(
    _specs("gross", 1, r"total\s+(?:do\s+)?documento", r"total\s+(?:da\s+)?fatura",
           r"total\s+(?:c/|com)\s*(?:o\s+)?iva(?:\s+incluido)?", r"total\s+iva\s+incluido",
           r"valor\s+total\s+(?:c/|com)\s*iva")
    + _specs("gross", 2, r"total(?:\s+geral)?", r"valor\s+total")
    + _specs("payable", 3, r"(?:total|valor|montante)\s+a\s+pagar", r"a\s+pagar")
    + _specs("net", 1, r"total\s+(?:s/|sem)\s*iva", r"valor\s+(?:s/|sem)\s*iva",
             r"total\s+liquido", r"total\s+(?:da\s+)?(?:base\s+tributavel|incidencia)")
    + _specs("net", 2, r"base\s+tributavel", r"base\s+de\s+incidencia",
             r"valor\s+tributavel", r"incidencia")
    + _specs("vat", 1, r"total\s+(?:de\s+|do\s+)?iva", r"iva\s+total",
             r"valor\s+(?:de\s+|do\s+)?iva", r"montante\s+(?:de\s+)?iva")
    + _specs("vat", 2, r"iva")
    + _specs("issue_date", 1, r"data\s+(?:de\s+)?emissao", r"data\s+(?:do\s+)?documento",
             r"data\s+(?:da\s+)?fatura", r"emitid[ao]\s+em", r"data\s+(?:de\s+)?faturacao")
    + _specs("issue_date", 2, r"data")
    + _specs("due_date", 1, r"data\s+(?:de\s+)?vencimento", r"vencimento",
             r"data\s+limite(?:\s+(?:de|para)\s+pagamento)?", r"(?:pagamento|pagar)\s+ate")
    + _specs("withholding", 1, r"retencao\s+(?:na\s+)?fonte(?:\s+(?:de\s+)?irs)?",
             r"retencao\s+(?:de\s+)?irs", r"ret\.?\s+irs", r"irs\s+retido")
)
_AMOUNT_KEYS = frozenset({"gross", "payable", "net", "vat", "withholding"})

_AMOUNT_LEAD = re.compile(
    r"[\s:=]*"
    r"(?:\(?\s*(?:a\s+taxa\s+(?:de\s+)?)?[0-9]{1,2}(?:[.,][0-9]{1,2})?\s*%\s*\)?[\s:=]*)?"
    r"(?:\(\s*(?:eur|€)\s*\)|eur|€)?[\s:=]*(?:eur|€)?\s*"
)
_DATE_LEAD = re.compile(r"[\s:=.\-]*")
_ONLY_AMOUNT = re.compile(r"\s*(?:€|eur)?\s*(?P<a>.+?)\s*(?:€|eur)?\s*")


@dataclass(frozen=True)
class _Hit:
    spec: _LabelSpec
    start: int
    end: int


def _label_hits(folded_line: str) -> list[_Hit]:
    """Labels on a line; on overlap the longest label wins ("total c/ iva" > "iva").

    O(h log h) in the number of hits: text comes from untrusted attachments,
    so a line repeating a label thousands of times must not stall extraction.
    """
    hits = [
        _Hit(spec, m.start(), m.end()) for spec in _LABELS for m in spec.pattern.finditer(folded_line)
    ]
    hits.sort(key=lambda h: (h.start - h.end, h.start))
    starts: list[int] = []  # chosen hits are disjoint; kept sorted by start
    chosen: dict[int, _Hit] = {}
    for hit in hits:
        before = bisect_left(starts, hit.end) - 1  # last chosen hit starting before hit.end
        if before >= 0 and chosen[starts[before]].end > hit.start:
            continue
        insort(starts, hit.start)
        chosen[hit.start] = hit
    return [chosen[start] for start in starts]


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TextFieldsResult:
    """Candidates found in one text. ``ambiguous`` lists fields left out on purpose."""

    observations: tuple[NamedObservation, ...]
    ambiguous: Mapping[CriticalField, tuple[str, ...]] = field(default_factory=dict)
    unassigned_tax_ids: tuple[str, ...] = ()
    buyer_is_final_consumer: bool = False
    document_numbers: tuple[str, ...] = ()
    atcud: ATCUD | None = None
    ibans: tuple[str, ...] = ()
    multibanco: MultibancoReference | None = None
    withholding: Decimal | None = None

    def get(self, field_: CriticalField) -> NamedObservation | None:
        return next((o for o in self.observations if o.field == field_), None)


@dataclass(frozen=True)
class _Candidate:
    tier: int
    value: object
    line: int  # 1-based


@dataclass
class _Builder:
    source: str
    method: ExtractionMethod
    observations: list[NamedObservation] = field(default_factory=list)
    ambiguous: dict[CriticalField, tuple[str, ...]] = field(default_factory=dict)

    def add(self, field_: CriticalField, value: object, confidence: float, line: int | str) -> None:
        where = f"text:line {line}" if isinstance(line, int) else f"text:{line}"
        self.observations.append(NamedObservation(
            field=field_, value=value, source=self.source, method=self.method,
            confidence=confidence, location=where,
        ))

    def add_selected(self, field_: CriticalField, candidates: list[_Candidate]) -> None:
        chosen, conflicting = _select(candidates)
        if chosen is not None:
            self.add(field_, chosen.value, _TIER_CONFIDENCE[chosen.tier], chosen.line)
        elif conflicting:
            self.ambiguous[field_] = conflicting


def _select(candidates: list[_Candidate]) -> tuple[_Candidate | None, tuple[str, ...]]:
    """Best-tier candidate if its values agree; otherwise the disagreeing values."""
    if not candidates:
        return None, ()
    best = min(c.tier for c in candidates)
    top = [c for c in candidates if c.tier == best]
    distinct = list(dict.fromkeys(c.value for c in top))
    if len(distinct) == 1:
        return top[0], ()
    return None, tuple(str(v) for v in distinct)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def extract_text_fields(
    text: str,
    source: str,
    *,
    method: ExtractionMethod = ExtractionMethod.OCR,
    known_customer_tax_ids: Collection[str] = (),
) -> TextFieldsResult:
    """Extract candidate observations from Portuguese document text.

    ``source`` is the evidence id (or engine name) recorded on every
    observation. Use ``method=EMBEDDED_TEXT`` for a PDF's own text layer.
    ``known_customer_tax_ids`` are NIFs known to be the recipient (e.g. the
    business's own NIFs when reading purchase invoices).
    """
    original = clean(text or "")
    lines = original.split("\n")
    folded = [fold(line) for line in lines]
    out = _Builder(source=source, method=method)

    labelled = _labelled_values(folded)
    withholding = _single_value(labelled["withholding"])
    gross = labelled["gross"] or ([] if withholding is not None else labelled["payable"])
    out.add_selected(CriticalField.GROSS_AMOUNT, gross)
    out.add_selected(CriticalField.NET_AMOUNT, labelled["net"])
    out.add_selected(CriticalField.VAT_AMOUNT, labelled["vat"])
    out.add_selected(CriticalField.ISSUE_DATE, labelled["issue_date"])
    out.add_selected(CriticalField.DUE_DATE, labelled["due_date"])

    atcud = _find_atcud(lines, folded)
    doc_numbers = _document_numbers(lines, folded, out, atcud)
    unassigned, final_consumer = _tax_ids(folded, out, known_customer_tax_ids)
    ibans = _ibans(original, out)
    multibanco = _multibanco(folded, out)

    return TextFieldsResult(
        observations=tuple(out.observations),
        ambiguous=MappingProxyType(dict(out.ambiguous)),
        unassigned_tax_ids=unassigned,
        buyer_is_final_consumer=final_consumer,
        document_numbers=doc_numbers,
        atcud=atcud,
        ibans=ibans,
        multibanco=multibanco,
        withholding=withholding,
    )


# --------------------------------------------------------------------------- #
# Labelled amounts and dates
# --------------------------------------------------------------------------- #


def _labelled_values(folded: list[str]) -> dict[str, list[_Candidate]]:
    found: dict[str, list[_Candidate]] = {s.key: [] for s in _LABELS}
    for index, line in enumerate(folded):
        hits = _label_hits(line)
        for pos, hit in enumerate(hits):
            end = hits[pos + 1].start if pos + 1 < len(hits) else len(line)
            segment = line[hit.end:end]
            value = _value_in_segment(hit.spec.key, segment)
            line_no = index + 1
            if value is None and len(hits) == 1 and not line[: hit.start].strip():
                value, line_no = _value_on_next_line(hit.spec.key, folded, index, segment)
            if value is not None:
                found[hit.spec.key].append(_Candidate(hit.spec.tier, value, line_no))
    return found


def _value_in_segment(key: str, segment: str) -> object | None:
    """The single value right after a label, or None."""
    if key in _AMOUNT_KEYS:
        lead = _AMOUNT_LEAD.match(segment)
        start = lead.end() if lead else 0
        match = _AMOUNT.match(segment, start)
        if match is None or len(_AMOUNT.findall(segment)) != 1:
            return None
        return _amount_from_match(match)
    lead = _DATE_LEAD.match(segment)
    start = lead.end() if lead else 0
    match = _DATE.match(segment, start)
    if match is None or len(list(_DATE.finditer(segment))) != 1:
        return None
    return _date_from_match(match)


def _value_on_next_line(
    key: str, folded: list[str], index: int, segment: str
) -> tuple[object | None, int]:
    """"Total" alone on a line, the value alone on the next non-empty line."""
    if segment.strip(" :=€") not in ("", "eur", "(eur)"):
        return None, index + 1
    for nxt in range(index + 1, min(index + 3, len(folded))):
        candidate = folded[nxt].strip()
        if not candidate:
            continue
        if _label_hits(candidate):
            return None, index + 1
        value: object | None
        if key in _AMOUNT_KEYS:
            inner = _ONLY_AMOUNT.fullmatch(candidate)
            value = parse_pt_amount(inner.group("a")) if inner else None
        else:
            match = _DATE.fullmatch(candidate)
            value = _date_from_match(match) if match else None
        return value, nxt + 1
    return None, index + 1


def _single_value(candidates: list[_Candidate]) -> Decimal | None:
    chosen, _ = _select(candidates)
    return chosen.value if chosen is not None and isinstance(chosen.value, Decimal) else None


# --------------------------------------------------------------------------- #
# Document number and ATCUD
# --------------------------------------------------------------------------- #

# Unlabelled numbers are only trusted with a common fiscal code as prefix;
# legacy two-letter codes (DA, TD...) collide with ordinary upper-case words.
_DOC_NUMBER = re.compile(
    r"(?<![A-Za-z0-9])(FT|FS|FR|NC|ND|RC|RG|VD) +([^\s/]{1,40})/([0-9]{1,12})(?![0-9/])"
)
_DOC_LABEL = re.compile(
    r"(?<![a-z])(?:fatura(?:[\s\-]+recibo)?|fatura\s+simplificada|nota\s+de\s+(?:credito|debito)"
    rf"|recibo|documento)\s*(?:{_NO})?\s*:?\s*"
)
_GENERIC_DOC_NUMBER = re.compile(r"([^\s/:]+) ([^\s/]+)/([0-9]{1,12})(?![0-9/])")
_ATCUD_LABEL = re.compile(r"(?<![a-z])atcud\s*:?\s*([a-z0-9]{8,}-[0-9]+)(?![0-9])")


def _find_atcud(lines: list[str], folded: list[str]) -> ATCUD | None:
    found: dict[str, ATCUD] = {}
    for line, low in zip(lines, folded, strict=True):
        for match in _ATCUD_LABEL.finditer(low):
            try:
                atcud = parse_atcud(line[match.start(1):match.end(1)].upper())
            except ATCUDError:
                continue
            found.setdefault(str(atcud), atcud)
    return next(iter(found.values())) if len(found) == 1 else None


def _document_numbers(
    lines: list[str], folded: list[str], out: _Builder, atcud: ATCUD | None
) -> tuple[str, ...]:
    seen: dict[str, int] = {}
    for index, (line, low) in enumerate(zip(lines, folded, strict=True)):
        for match in _DOC_NUMBER.finditer(line):
            seen.setdefault(f"{match.group(1)} {match.group(2)}/{match.group(3)}", index + 1)
        for label in _DOC_LABEL.finditer(low):
            generic = _GENERIC_DOC_NUMBER.match(line, label.end())
            if generic:
                seen.setdefault(f"{generic.group(1)} {generic.group(2)}/{generic.group(3)}", index + 1)
    numbers = tuple(seen)
    if len(numbers) == 1:
        out.add(CriticalField.INVOICE_NUMBER, numbers[0], _DOC_NUMBER_CONFIDENCE, seen[numbers[0]])
    elif len(numbers) > 1:
        # The ATCUD's sequence must equal the document's own number (§50):
        # that rule, not position, may single one out.
        matching = [n for n in numbers if atcud and parse_document_number(n).number == atcud.sequence]
        if len(matching) == 1:
            out.add(CriticalField.INVOICE_NUMBER, matching[0], _DOC_NUMBER_BY_ATCUD_CONFIDENCE,
                    seen[matching[0]])
        else:
            out.ambiguous[CriticalField.INVOICE_NUMBER] = numbers
    return numbers


# --------------------------------------------------------------------------- #
# Tax numbers
# --------------------------------------------------------------------------- #

_NIF_LABEL = (
    r"(?:n\.?\s*[ºo°]?\.?\s*(?:de\s+)?)?"
    r"(?:nif(?:\s*/\s*nipc)?|nipc|n\.\s*i\.\s*f\.?|contribuinte|contrib\.?"
    r"|numero\s+de\s+identificacao\s+fiscal|n\.?\s*[ºo°]\.?\s*(?:de\s+)?iva|vat(?:\s+(?:no\.?|number))?"
    r"|tax\s+id)"
)
_NIF_NEAR_LABEL = re.compile(
    rf"(?<![a-z]){_NIF_LABEL}(?![a-z])[^0-9\n]{{0,30}}?(?:pt\s?)?"
    r"(?P<n>[0-9]{3}[ .]?[0-9]{3}[ .]?[0-9]{3})(?![0-9]|[.,][0-9])"
)
_NIF_VIES = re.compile(r"(?<![a-z0-9])pt\s?(?P<n>[0-9]{3} ?[0-9]{3} ?[0-9]{3})(?![0-9]|[.,][0-9])")
_CUSTOMER_WORDS = re.compile(r"(?<![a-z])(?:cliente|adquirente|comprador|destinatario|consumidor)")
_SUPPLIER_WORDS = re.compile(r"(?<![a-z])(?:fornecedor|emitente|vendedor|prestador|emissor)")
_ROLE_HEADER = re.compile(
    r"^\s*(?:dados\s+do\s+)?(?P<role>cliente|adquirente|fornecedor|emitente)\s*:?\s*$"
)
_FINAL_CONSUMER_WORDS = re.compile(r"(?<![a-z])consumidor\s+final(?![a-z])")
# "V/ Contribuinte" (vosso: yours = the customer) and "N/ Contribuinte"
# (nosso: ours = the issuer), a long-standing Portuguese invoice convention.
_POSSESSIVE_LABEL = re.compile(
    r"(?<![a-z0-9])(?P<who>[vn])\s*/\s*(?:n\.?\s*[ºo°]\.?\s*(?:de\s+)?)?"
    r"(?:contribuinte|contrib\.?|nif|nipc)(?![a-z])"
)
_CUSTOMER, _SUPPLIER = "customer", "supplier"


def _tax_ids(
    folded: list[str], out: _Builder, known_customers: Collection[str]
) -> tuple[tuple[str, ...], bool]:
    known = {n for n in (normalize_nif(k) for k in known_customers) if n}
    roles: dict[str, set[str | None]] = {}
    first_line: dict[str, int] = {}
    final_consumer = False
    for index, low in enumerate(folded):
        final_consumer = final_consumer or _FINAL_CONSUMER_WORDS.search(low) is not None
        previous_end = 0
        for match in _nif_matches(low):
            nif = normalize_nif(match.group("n"))
            context = (previous_end, match.start("n"))
            previous_end = match.end("n")
            if nif == FINAL_CONSUMER_NIF:
                final_consumer = True
                continue
            if nif is None or not validate_nif(nif).valid:
                continue
            first_line.setdefault(nif, index + 1)
            roles.setdefault(nif, set()).add(_role_for(folded, index, context))
    return _assign_roles(roles, first_line, known, out), final_consumer


def _nif_matches(folded_line: str) -> list[re.Match[str]]:
    """Labelled and "PT"-prefixed NIF matches on a line, left to right, no repeats."""
    by_start: dict[int, re.Match[str]] = {}
    for pattern in (_NIF_NEAR_LABEL, _NIF_VIES):
        for match in pattern.finditer(folded_line):
            by_start.setdefault(match.start("n"), match)
    return [by_start[k] for k in sorted(by_start)]


def _role_for(folded: list[str], index: int, context: tuple[int, int]) -> str | None:
    """Role from the words leading to the NIF on its line, else a section header above."""
    before = folded[index][context[0]:context[1]]
    possessive = {m.group("who") for m in _POSSESSIVE_LABEL.finditer(before)}
    is_customer = bool(_CUSTOMER_WORDS.search(before)) or "v" in possessive
    is_supplier = bool(_SUPPLIER_WORDS.search(before)) or "n" in possessive
    if is_customer != is_supplier:
        return _CUSTOMER if is_customer else _SUPPLIER
    if is_customer:
        return None
    for back in range(index - 1, max(index - 5, -1), -1):
        if _NIF_NEAR_LABEL.search(folded[back]):
            break
        header = _ROLE_HEADER.match(folded[back])
        if header:
            return _CUSTOMER if header.group("role") in ("cliente", "adquirente") else _SUPPLIER
    return None


def _assign_roles(
    roles: dict[str, set[str | None]],
    first_line: dict[str, int],
    known: set[str],
    out: _Builder,
) -> tuple[str, ...]:
    customers: list[str] = []
    suppliers: list[str] = []
    unlabelled: list[str] = []
    contradictory: list[str] = []
    for nif, seen_roles in roles.items():
        labelled = {r for r in seen_roles if r is not None}
        if (nif in known and labelled - {_CUSTOMER}) or len(labelled) > 1:
            contradictory.append(nif)  # e.g. a known customer id labelled as supplier
        elif nif in known or labelled == {_CUSTOMER}:
            customers.append(nif)
        elif labelled == {_SUPPLIER}:
            suppliers.append(nif)
        else:
            unlabelled.append(nif)

    if len(customers) == 1 and not suppliers and not contradictory and len(unlabelled) == 1:
        # A Portuguese invoice must show the supplier's NIF; if the customer is
        # identified, the only other valid NIF is the supplier's.
        out.add(CriticalField.SUPPLIER_TAX_ID, unlabelled[0], _NIF_DEDUCED_CONFIDENCE,
                first_line[unlabelled[0]])
        unlabelled = []
    _emit_role(CriticalField.CUSTOMER_TAX_ID, customers, known, first_line, out)
    _emit_role(CriticalField.SUPPLIER_TAX_ID, suppliers, set(), first_line, out)
    return tuple(nif for nif in roles if nif in unlabelled or nif in contradictory)


def _emit_role(
    field_: CriticalField, nifs: list[str], known: set[str], first_line: dict[str, int], out: _Builder
) -> None:
    if len(nifs) == 1:
        confidence = _NIF_KNOWN_CONFIDENCE if nifs[0] in known else _NIF_CONFIDENCE
        out.add(field_, nifs[0], confidence, first_line[nifs[0]])
    elif len(nifs) > 1:
        out.ambiguous[field_] = tuple(nifs)


# --------------------------------------------------------------------------- #
# Bank details
# --------------------------------------------------------------------------- #

_MB_ENTITY = re.compile(r"(?<![a-z])entidade\s*:?\s*([0-9]{5})(?![0-9])")
_MB_REFERENCE = re.compile(
    r"(?<![a-z])(?:referencia(?:\s+(?:mb|multibanco))?|ref\.?\s*(?:mb|multibanco))\s*:?\s*"
    r"([0-9]{3} ?[0-9]{3} ?[0-9]{3})(?![0-9])"
)
_MB_AMOUNT = re.compile(r"(?<![a-z])montante\s*:?\s*(?:€|eur)?\s*")


def _ibans(original: str, out: _Builder) -> tuple[str, ...]:
    ibans = tuple(find_ibans(original))
    if len(ibans) == 1:
        out.add(CriticalField.IBAN, ibans[0], _IBAN_CONFIDENCE, "iban")
    elif len(ibans) > 1:
        out.ambiguous[CriticalField.IBAN] = ibans
    return ibans


def _multibanco(folded: list[str], out: _Builder) -> MultibancoReference | None:
    text = "\n".join(folded)
    entities = list(dict.fromkeys(m.group(1) for m in _MB_ENTITY.finditer(text)))
    references = list(dict.fromkeys(m.group(1).replace(" ", "") for m in _MB_REFERENCE.finditer(text)))
    if len(entities) != 1 or len(references) != 1:
        if entities or references:
            out.ambiguous[CriticalField.PAYMENT_REFERENCE] = tuple(entities + references)
        return None
    amounts = {
        _amount_from_match(a)
        for m in _MB_AMOUNT.finditer(text)
        if (a := _AMOUNT.match(text, m.end())) is not None
    }
    amount = amounts.pop() if len(amounts) == 1 else None
    try:
        reference = parse_multibanco(entities[0], references[0], amount)
    except MultibancoError:
        return None
    out.add(CriticalField.PAYMENT_REFERENCE, reference.payment_reference, _MULTIBANCO_CONFIDENCE,
            "multibanco")
    return reference
