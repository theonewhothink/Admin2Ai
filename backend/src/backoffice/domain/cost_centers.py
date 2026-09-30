"""Cost centers: the job, property, vehicle, outlet, event, course or client a cost belongs to.

A company that works by job (construction, plumbing, architecture), by
property (property management, short-term rentals), by vehicle (car rental,
couriers), by outlet (franchises), by event, course or client keeps a list of
**cost centers**. Each has a name, the business's own word for it ("Job",
"Property", "Vehicle", "Outlet", "Event", "Course", "Client") and the facts
that point to it: site addresses, license plates, card endings, project codes
and references, customer tax numbers, email aliases and keywords.

Every payment and document of such a company can carry a
:class:`~backoffice.domain.models.CostAllocation`: one cost center, a split
across several, or the company's general costs. A split always adds up to the
total to the cent, and to each VAT rate's net and VAT where the rates are
known. Rounding leftovers go to the largest share. A split that does not add
up is refused with a plain message (:class:`SplitError`), never stored.

Pure Python, no I/O (runs in the browser build too).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

from backoffice.language import format_money

from .models import AllocationShare, DocumentLine, VatPart

__all__ = [
    "DEFAULT_KIND",
    "CostCenter",
    "CostCenterIdentifiers",
    "SplitError",
    "split_by_amounts",
    "split_by_lines",
    "split_by_percent",
    "split_by_weights",
    "to_cents",
]

DEFAULT_KIND = "Job"
CENT = Decimal("0.01")
HUNDRED = Decimal("100")
_MAX_ENTRIES = 50
_MAX_TEXT = 120
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SplitError(ValueError):
    """A split that cannot be used. ``message`` is plain and safe to show the owner."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# --------------------------------------------------------------------------- cost centers


_WORDS = {"addresses": "addresses", "plates": "plates", "cards": "cards", "accounts": "accounts",
          "references": "references", "tax_ids": "tax numbers", "emails": "email addresses", "keywords": "keywords"}


def _clean_list(values: object, *, what: str) -> tuple[str, ...]:
    """Trimmed, de-duplicated text entries. Messages are plain: they reach the owner."""
    what = _WORDS.get(what, what)
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"give the {what} as a list")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"write the {what} as words or numbers")
        text = " ".join(value.split())
        if not text:
            continue
        if len(text) > _MAX_TEXT:
            raise ValueError(f"one of the {what} is too long")
        if text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)
    if len(out) > _MAX_ENTRIES:
        raise ValueError(f"that is too many {what}")
    return tuple(out)


class CostCenterIdentifiers(BaseModel):
    """Facts that point to one cost center when they appear on a payment or a document."""

    model_config = ConfigDict(frozen=True)

    addresses: tuple[str, ...] = ()  # site or property addresses ("Rua das Flores 12")
    plates: tuple[str, ...] = ()  # license plates ("12-AB-34")
    cards: tuple[str, ...] = ()  # last 4 digits of cards used only for it (a technician's or a vehicle's card)
    accounts: tuple[str, ...] = ()  # bank accounts used only for it (account ids)
    references: tuple[str, ...] = ()  # project codes, booking or property names, order references
    tax_ids: tuple[str, ...] = ()  # customer tax numbers (a reseller's customer, an agency's client)
    emails: tuple[str, ...] = ()  # email aliases invoices for it are sent to or from
    keywords: tuple[str, ...] = ()  # other words that only mean this cost center

    @field_validator("addresses", "plates", "accounts", "references", "keywords", mode="before")
    @classmethod
    def _texts(cls, value: object, info: ValidationInfo) -> tuple[str, ...]:
        return _clean_list(value, what=info.field_name or "entries")

    @field_validator("cards", mode="before")
    @classmethod
    def _cards(cls, value: object) -> tuple[str, ...]:
        cards = _clean_list(value, what="cards")
        for card in cards:
            if not re.fullmatch(r"\d{4}", card):
                raise ValueError("a card is the last 4 digits")
        return cards

    @field_validator("tax_ids", mode="before")
    @classmethod
    def _tax_ids(cls, value: object) -> tuple[str, ...]:
        ids = tuple(re.sub(r"[\s.\-]", "", v).upper() for v in _clean_list(value, what="tax_ids"))
        for tax_id in ids:
            if not re.fullmatch(r"(?:[A-Z]{2})?[0-9A-Z]{5,14}", tax_id) or not re.search(r"\d", tax_id):
                raise ValueError("that tax number doesn't look right")
        return tuple(dict.fromkeys(ids))

    @field_validator("emails", mode="before")
    @classmethod
    def _emails(cls, value: object) -> tuple[str, ...]:
        emails = tuple(e.lower() for e in _clean_list(value, what="emails"))
        for email in emails:
            if not _EMAIL.match(email):
                raise ValueError("that doesn't look like an email address")
        return tuple(dict.fromkeys(emails))

    def as_dict(self) -> dict[str, list[str]]:
        """As the API shows them (camelCase keys, like the rest of the web contract)."""
        return {API_NAMES.get(name, name): list(getattr(self, name)) for name in type(self).model_fields}

    @classmethod
    def field_for(cls, key: str) -> str | None:
        """The field an API key names ("taxIds" or "tax_ids"), or None."""
        name = {v: k for k, v in API_NAMES.items()}.get(key, key)
        return name if name in cls.model_fields else None


API_NAMES = {"tax_ids": "taxIds"}


class CostCenter(BaseModel):
    """One job, property, vehicle, outlet, event, course or client of one company."""

    model_config = ConfigDict(frozen=True)

    id: str
    tenant_id: str
    company_id: str
    name: str
    kind: str = DEFAULT_KIND  # the business's own word, shown to the owner
    identifiers: CostCenterIdentifiers = CostCenterIdentifiers()
    active: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        text = " ".join(str(value).split())
        if not text:
            raise ValueError("a cost center needs a name")
        return text

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        text = " ".join(str(value).split())
        if not text:
            return DEFAULT_KIND
        return text[:1].upper() + text[1:]

    @property
    def label(self) -> str:
        """'Job Rua das Flores', 'Apartment 2B' (a name that already starts with its kind stays as it is)."""
        name, kind = self.name.casefold(), self.kind.casefold()
        if name == kind or name.startswith(kind + " "):
            return self.name
        return f"{self.kind} {self.name}"


# --------------------------------------------------------------------------- exact splits


def to_cents(value: object, *, what: str = "amount") -> Decimal:
    """A Decimal amount in cents from a number or text, refusing floats' surprises and sub-cent amounts."""
    if isinstance(value, bool):
        raise SplitError(f"That {what} is not a number.")
    try:
        if isinstance(value, float):
            amount = Decimal(repr(value))
        else:
            amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise SplitError(f"That {what} is not a number.") from None
    if not amount.is_finite():
        raise SplitError(f"That {what} is not a number.")
    if amount != amount.quantize(CENT):
        raise SplitError("Use amounts in whole cents.")
    return amount.quantize(CENT)


def _largest(weights: Sequence[Decimal]) -> int:
    best = 0
    for i, w in enumerate(weights):
        if w > weights[best]:
            best = i
    return best


def _distribute(amount: Decimal, weights: Sequence[Decimal]) -> list[Decimal]:
    """``amount`` in cents over ``weights``: each share rounded down, the leftover cents to the largest weight."""
    if not weights:
        return []
    total_weight = sum(weights, Decimal(0))
    if total_weight <= 0:
        raise SplitError("Each share must be more than zero.")
    sign = Decimal(-1) if amount < 0 else Decimal(1)
    size = abs(amount)
    shares = [(size * w / total_weight).quantize(CENT, rounding=ROUND_DOWN) for w in weights]
    shares[_largest(weights)] += size - sum(shares, Decimal(0))
    return [sign * s for s in shares]


def _parts_total(parts: Sequence[VatPart]) -> Decimal:
    return sum((p.gross for p in parts), Decimal(0))


def _shares(ids: Sequence[str], amounts: Sequence[Decimal], parts: Sequence[Sequence[VatPart]] | None = None,
            percents: Sequence[Decimal | None] | None = None) -> tuple[AllocationShare, ...]:
    out = []
    for i, cid in enumerate(ids):
        if amounts[i] <= 0:
            raise SplitError("This split leaves one share with nothing. Give each share a real amount.")
        share_parts = tuple(p for p in (parts[i] if parts else ()) if p.net or p.vat)
        out.append(AllocationShare(cost_center_id=cid, amount=amounts[i], parts=share_parts,
                                   percent=percents[i] if percents else None))
    return tuple(out)


def _check_ids(ids: Sequence[str]) -> None:
    if not ids:
        raise SplitError("Choose at least one.")
    if len(set(ids)) != len(ids):
        raise SplitError("Each one can appear only once in a split.")


def split_by_weights(total: Decimal, weights: Sequence[tuple[str, Decimal]], parts: Sequence[VatPart] = (),
                     *, percents: bool = False) -> tuple[AllocationShare, ...]:
    """Split ``total`` by relative weights, exactly to the cent (and to each VAT rate when ``parts`` are given).

    With ``parts`` (which must add up to ``total``) each rate's net and VAT are
    split separately, so every rate still adds up exactly; a share's amount is
    the sum of its parts. Rounding leftovers go to the largest weight.
    """
    ids = [cid for cid, _ in weights]
    _check_ids(ids)
    ws = [Decimal(w) for _, w in weights]
    if any(w <= 0 for w in ws):
        raise SplitError("Each share must be more than zero.")
    if parts and _parts_total(parts) != total:
        parts = ()
    if not parts:
        amounts = _distribute(total, ws)
        return _shares(ids, amounts, percents=ws if percents else None)
    per_share: list[list[VatPart]] = [[] for _ in ids]
    for part in parts:
        nets = _distribute(part.net, ws)
        vats = _distribute(part.vat, ws)
        for i in range(len(ids)):
            per_share[i].append(VatPart(rate=part.rate, net=nets[i], vat=vats[i]))
    amounts = [sum((p.gross for p in share), Decimal(0)) for share in per_share]
    return _shares(ids, amounts, per_share, percents=ws if percents else None)


def split_by_percent(total: Decimal, percents: Sequence[tuple[str, Decimal]], parts: Sequence[VatPart] = ()
                     ) -> tuple[AllocationShare, ...]:
    """Split by percentages that must add up to exactly 100."""
    values = [p for _, p in percents]
    if any(p <= 0 for p in values):
        raise SplitError("Each share must be more than 0%.")
    added = sum(values, Decimal(0))
    if added != HUNDRED:
        raise SplitError(f"The shares add up to {_percent(added)}, not 100%.")
    return split_by_weights(total, percents, parts, percents=True)


def split_by_amounts(total: Decimal, amounts: Sequence[tuple[str, Decimal, Decimal | None]],
                     parts: Sequence[VatPart] = (), *, currency: str = "EUR") -> tuple[AllocationShare, ...]:
    """Split by amounts the owner gave: ``(cost center id, amount, VAT rate or None)``.

    The amounts must add up to ``total`` exactly. With one VAT rate (or none
    known) each share's VAT is worked out from its amount. When the document
    has more than one VAT rate, each amount must say its rate and the amounts
    at each rate must add up to that rate's total.
    """
    if not amounts:
        raise SplitError("Choose at least one.")
    for _, amount, _ in amounts:
        if amount <= 0:
            raise SplitError("Each amount must be more than zero.")
    added = sum((a for _, a, _ in amounts), Decimal(0))
    if added != total:
        raise SplitError(f"These amounts add up to {format_money(added, currency)}, but the total is "
                         f"{format_money(total, currency)}. They must match to the cent.")
    if parts and _parts_total(parts) != total:
        parts = ()
    rates = {p.rate for p in parts}
    if len(parts) > 1 and len(rates) != len(parts):
        parts = ()  # the same rate twice (two regions): no per-rate check is possible, the total still is
    if len(parts) <= 1:
        ids = [cid for cid, _, _ in amounts]
        _check_ids(ids)
        values = [a for _, a, _ in amounts]
        if not parts:
            return _shares(ids, values)
        vats = _distribute(parts[0].vat, values)
        share_parts = [[VatPart(rate=parts[0].rate, net=values[i] - vats[i], vat=vats[i])] for i in range(len(ids))]
        return _shares(ids, values, share_parts)
    if any(rate is None for _, _, rate in amounts):
        raise SplitError("This invoice has more than one VAT rate. Give the amount at each rate for each share.")
    by_rate = {p.rate: p for p in parts}
    ids: list[str] = []
    per_share: dict[str, list[VatPart]] = {}
    for rate, part in by_rate.items():
        entries = [(cid, a) for cid, a, r in amounts if r is not None and Decimal(r) == rate]
        entered = sum((a for _, a in entries), Decimal(0))
        if entered != part.gross:
            raise SplitError(f"At {_percent(rate)} VAT the amounts add up to {format_money(entered, currency)}, "
                             f"but the invoice shows {format_money(part.gross, currency)}.")
        if not entries:
            continue
        if len({cid for cid, _ in entries}) != len(entries):
            raise SplitError("Each one can appear only once at each VAT rate.")
        vats = _distribute(part.vat, [a for _, a in entries])
        for (cid, a), vat in zip(entries, vats, strict=True):
            if cid not in per_share:
                ids.append(cid)
                per_share[cid] = []
            per_share[cid].append(VatPart(rate=rate, net=a - vat, vat=vat))
    unknown = [r for _, _, r in amounts if r is not None and Decimal(r) not in by_rate]
    if unknown:
        raise SplitError(f"The invoice has no {_percent(Decimal(unknown[0]))} VAT rate.")
    values = [sum((p.gross for p in per_share[cid]), Decimal(0)) for cid in ids]
    return _shares(ids, values, [per_share[cid] for cid in ids])


def split_by_lines(lines: Sequence[DocumentLine], assignment: Mapping[str, str], parts: Sequence[VatPart],
                   total: Decimal) -> tuple[AllocationShare, ...] | None:
    """Split an invoice by its lines, each line assigned to one cost center.

    Each rate's VAT is spread over that rate's lines in proportion to their
    net amounts (leftover cents to the largest line), so every rate and the
    total add up exactly. Returns None when the lines do not add up to the
    invoice (a missing line, a discount line, an unknown rate): the owner is
    asked instead of guessing.
    """
    if not lines or any(line.id not in assignment for line in lines):
        return None
    if any(line.net_amount is None or line.net_amount <= 0 for line in lines):
        return None
    order: list[str] = list(dict.fromkeys(assignment[line.id] for line in lines))
    amounts: dict[str, list[VatPart]] = {cid: [] for cid in order}
    line_ids: dict[str, list[str]] = {cid: [] for cid in order}
    if not parts:
        if sum((line.net_amount for line in lines), Decimal(0)) != total:  # type: ignore[misc]
            return None
        for line in lines:
            cid = assignment[line.id]
            amounts[cid].append(VatPart(rate=line.vat_rate, net=line.net_amount, vat=Decimal("0.00")))  # type: ignore[arg-type]
            line_ids[cid].append(line.id)
    else:
        if _parts_total(parts) != total:
            return None
        rates = [p.rate for p in parts]
        if len(set(rates)) != len(rates):
            return None
        single = len(parts) == 1
        placed = 0
        for part in parts:
            rate_lines = [line for line in lines
                          if (single and (line.vat_rate is None or part.rate is None or line.vat_rate == part.rate))
                          or (not single and line.vat_rate is not None and line.vat_rate == part.rate)]
            if not rate_lines:
                if part.net or part.vat:
                    return None
                continue
            if sum((line.net_amount for line in rate_lines), Decimal(0)) != part.net:  # type: ignore[misc]
                return None
            vats = _distribute(part.vat, [line.net_amount for line in rate_lines])  # type: ignore[misc]
            for line, vat in zip(rate_lines, vats, strict=True):
                cid = assignment[line.id]
                amounts[cid].append(VatPart(rate=part.rate, net=line.net_amount, vat=vat))  # type: ignore[arg-type]
                line_ids[cid].append(line.id)
                placed += 1
        if placed != len(lines):
            return None
    shares = []
    for cid in order:
        merged: dict[Decimal | None, VatPart] = {}
        for p in amounts[cid]:
            old = merged.get(p.rate)
            merged[p.rate] = p if old is None else VatPart(rate=p.rate, net=old.net + p.net, vat=old.vat + p.vat)
        share_parts = tuple(merged.values()) if parts else ()
        amount = sum((p.gross for p in amounts[cid]), Decimal(0))
        shares.append(AllocationShare(cost_center_id=cid, amount=amount, parts=share_parts,
                                      line_ids=tuple(line_ids[cid])))
    if sum((s.amount for s in shares), Decimal(0)) != total:
        return None
    return tuple(shares)


def _percent(value: Decimal | None) -> str:
    if value is None:
        return "an unknown"
    text = f"{value.normalize():f}" if value == value.to_integral_value() else f"{value:f}".rstrip("0").rstrip(".")
    return f"{text}%"
