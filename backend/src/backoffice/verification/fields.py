"""Field-level verification: GREEN, AMBER or RED for one critical value (§18, §19, §57).

Rules, applied after :func:`~.normalize.normalize_value`:

* **RED** (conflict) when the *confident* observations (confidence at or
  above ``policy.min_confidence``) cannot all be the same value. Both values
  are reported and none is picked (§19: "Never guess").
* **GREEN** (verified) only when at least two *independent* observations
  agree and at least one of the agreeing pair is *high rank* (structured
  XML, API, QR, barcode, embedded text, web data, bank, arithmetic, human),
  and no weaker reading contradicts them.
* **AMBER** (likely) otherwise: a single source, readings that only agree
  with themselves, an ambiguous reading, or an unclear dissent. AMBER is
  never promoted to make numbers look better (§57).

Two disagreements are easy to miss and are treated like any other:

* an amount printed with a currency ("$483.60") that no other confident
  reading's currency can match ("€483.60") is a conflict, although the
  numbers agree;
* a value that fails its own check digits (IBAN, RF reference) in a
  confident *document* source (anything but a scan, where it is most
  likely a misread) is what that source says, so it contradicts every
  valid value instead of being skipped as unreadable.

Readings are ordered strongest first with a full tie-break
(:func:`order_key`), so the result and its wording never depend on the
order in which observations arrived.

Independence is judged by *channel*, the path a value took into the system:

* OCR / VLM readings: one channel per engine. The engine is the part after
  "@" in ``source`` ("ev_1@pp-ocrv6" -> "pp-ocrv6"), else the source itself.
  The same engine twice is one channel, even on two scans.
* Every other method is one channel: the QR code read twice, or the same
  e-invoice received twice, says the same thing once.
* ARITHMETIC observations belong to the channels of the numbers they add
  up. :func:`derived_location` records them (``";from=qr,ocr:x"``); a
  foreign arithmetic observation whose location starts with a method name
  ("qr:...") belongs to that method; otherwise it is its own channel.

Two observations are independent when their channel sets do not overlap,
so a QR code's own subtotal check never confirms the QR code, and a high
rank needs every contributing channel to be high rank (arithmetic over OCR
numbers is still OCR-grade).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from itertools import combinations
from typing import Any

from backoffice.domain.models import (
    METHOD_RANK,
    CriticalField,
    ExtractionMethod,
    FieldObservation,
    Quality,
    VerifiedField,
)

from ._display import field_label, join, method_label, show, show_many
from .normalize import (
    AMOUNT_FIELDS,
    CurrencyMark,
    NormalizeHints,
    Normalized,
    NormNote,
    as_critical_field,
    comparison_key,
    currency_mark,
    fails_check_digits,
    field_name,
    normalize_value,
)

__all__ = [
    "DEFAULT_POLICY",
    "HIGH_RANK_THRESHOLD",
    "LINEAGE_MARKER",
    "READING_METHODS",
    "FieldAssessment",
    "VerificationPolicy",
    "assess_field",
    "channel_token",
    "derived_location",
    "engine_of",
    "independent",
    "is_high_rank",
    "lineage",
    "order_key",
    "severity",
    "verify_field",
]

READING_METHODS: frozenset[ExtractionMethod] = frozenset({ExtractionMethod.OCR, ExtractionMethod.VLM})
# "High rank" = structured / QR / API / embedded text / bank / arithmetic / human (§13, §18).
HIGH_RANK_THRESHOLD = METHOD_RANK[ExtractionMethod.ARITHMETIC]
LINEAGE_MARKER = ";from="

_SEVERITY = {Quality.GREEN: 0, Quality.AMBER: 1, Quality.RED: 2}


def severity(quality: Quality) -> int:
    """GREEN 0 < AMBER 1 < RED 2."""
    return _SEVERITY[quality]


@dataclass(frozen=True)
class VerificationPolicy:
    """Tunable only through the golden dataset (§56).

    ``min_confidence`` 0.4 is the lowest confidence any in-repo producer
    gives a deliberate reading (weak text labels, a fiscal QR whose own
    totals fail). Below it an observation neither supports nor contradicts;
    at or above it, a disagreement is a conflict.
    """

    min_confidence: float = 0.4

    def __post_init__(self) -> None:
        if not 0.0 < self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be in (0, 1]")


DEFAULT_POLICY = VerificationPolicy()


# --------------------------------------------------------------------------- channels


def engine_of(source: str) -> str:
    """'ev_1@pp-ocrv6' -> 'pp-ocrv6'; a bare source is its own engine name."""
    text = source.strip().lower()
    return text.rsplit("@", 1)[1] if "@" in text else text


def _clean_token(text: str) -> str:
    return re.sub(r"[\s,;]+", "_", text.strip())


def channel_token(method: ExtractionMethod, source: str) -> str:
    if method in READING_METHODS:
        return f"{method.value}:{_clean_token(engine_of(source))}"
    return method.value


def derived_location(formula: str, tokens: Iterable[str]) -> str:
    """Location of a derived observation: formula plus the channels it came from."""
    return f"arithmetic:{formula}{LINEAGE_MARKER}{','.join(sorted(set(tokens)))}"


def _declared_lineage(location: object) -> frozenset[str] | None:
    if not isinstance(location, str) or LINEAGE_MARKER not in location:
        return None
    tokens = frozenset(t.strip() for t in location.split(LINEAGE_MARKER, 1)[1].split(","))
    return (tokens - {""}) or None


def _location_method(location: object) -> ExtractionMethod | None:
    if not isinstance(location, str) or ":" not in location:
        return None
    try:
        return ExtractionMethod(location.split(":", 1)[0].strip().lower())
    except ValueError:
        return None


def lineage(observation: FieldObservation) -> frozenset[str]:
    """The channels an observation's value depends on (module docstring)."""
    if observation.method is ExtractionMethod.ARITHMETIC:
        declared = _declared_lineage(observation.location)
        if declared is not None:
            return declared
        origin = _location_method(observation.location)
        if origin is not None and origin is not ExtractionMethod.ARITHMETIC:
            return frozenset({channel_token(origin, observation.source)})
    return frozenset({channel_token(observation.method, observation.source)})


def _token_rank(token: str) -> int:
    try:
        return METHOD_RANK.get(ExtractionMethod(token.split(":", 1)[0]), 0)
    except ValueError:
        return 0


def _token_label(token: str) -> str:
    try:
        return method_label(ExtractionMethod(token.split(":", 1)[0]))
    except ValueError:
        return "another source"


def is_high_rank(observation: FieldObservation) -> bool:
    return all(_token_rank(t) >= HIGH_RANK_THRESHOLD for t in lineage(observation))


def order_key(observation: FieldObservation) -> tuple[Any, ...]:
    """Strongest method first, then most confident, then a total order on the rest.

    The tail (method, source, value, location) only breaks ties, so two
    engines that finish in a different order give the same result.
    """
    return (
        -METHOD_RANK.get(observation.method, 0),
        -observation.confidence,
        observation.method.value,
        observation.source,
        type(observation.value).__name__,
        str(observation.value),
        str(observation.location),
    )


def independent(a: FieldObservation, b: FieldObservation) -> bool:
    return lineage(a).isdisjoint(lineage(b))


# --------------------------------------------------------------------------- result


@dataclass(frozen=True)
class FieldAssessment:
    """Verification of one field, with the detail a :class:`VerifiedField` has no room for.

    ``conflicting_values`` lists every value in a RED conflict; ``possible_values``
    every reading of an ambiguous AMBER. ``value`` is None whenever picking
    one would be a guess.
    """

    name: str
    quality: Quality
    value: Any
    observations: tuple[FieldObservation, ...]
    reasons: tuple[str, ...]
    supporting: tuple[FieldObservation, ...] = ()
    conflicting_values: tuple[Any, ...] = ()
    possible_values: tuple[Any, ...] = ()

    @property
    def verified(self) -> VerifiedField:
        return VerifiedField(
            name=self.name,
            value=self.value,
            quality=self.quality,
            observations=list(self.observations),
            reasons=list(self.reasons),
        )

    def demote(self, quality: Quality, reason: str) -> FieldAssessment:
        """Lower the quality (never raise it, §57); RED drops the value (§19)."""
        worst = max(self.quality, quality, key=severity)
        value = None if worst is Quality.RED else self.value
        reasons = self.reasons if reason in self.reasons else (*self.reasons, reason)
        return replace(self, quality=worst, value=value, reasons=reasons)


# --------------------------------------------------------------------------- readings


@dataclass(frozen=True, eq=False)
class _Reading:
    observation: FieldObservation
    normalized: Normalized
    keys: tuple[Any, ...]
    lineage: frozenset[str]
    high_rank: bool
    label: str
    mark: CurrencyMark | None = None  # currency printed with an amount
    fails_check: bool = False

    @property
    def clear(self) -> bool:
        return len(self.keys) == 1

    @property
    def is_scan(self) -> bool:
        return self.observation.method in READING_METHODS

    @property
    def invalid(self) -> bool:
        """Stated in full but failing its own check digits (not a partial read)."""
        return self.normalized.note is NormNote.INVALID and self.fails_check

    @property
    def raw(self) -> str:
        """Compact form of a value that failed its checks, for review."""
        return re.sub(r"[^0-9A-Za-z]", "", str(self.observation.value)).upper()


@dataclass(frozen=True)
class _Context:
    name: str
    observations: tuple[FieldObservation, ...]
    currency: str | None
    notes: tuple[str, ...]
    invalid_notes: tuple[str, ...] = ()

    def show(self, value: Any) -> str:
        return show(self.name, value, self.currency)

    def show_many(self, values: Sequence[Any]) -> list[str]:
        return show_many(self.name, values, self.currency)

    def result(
        self,
        quality: Quality,
        value: Any,
        reasons: Sequence[str],
        *,
        invalid_noted: bool = True,
        **extra: Any,
    ) -> FieldAssessment:
        notes = (*self.notes, *self.invalid_notes) if invalid_noted else self.notes
        return FieldAssessment(
            name=self.name,
            quality=quality,
            value=value,
            observations=self.observations,
            reasons=tuple(dict.fromkeys((*reasons, *notes))),
            **extra,
        )


def _read(name: str, observation: FieldObservation, hints: NormalizeHints | None) -> _Reading:
    normalized = normalize_value(name, observation.value, hints)
    keys = tuple(dict.fromkeys(comparison_key(name, c) for c in normalized.candidates))
    label = method_label(observation.method)
    mark = currency_mark(observation.value) if as_critical_field(name) in AMOUNT_FIELDS else None
    return _Reading(
        observation,
        normalized,
        keys,
        lineage(observation),
        is_high_rank(observation),
        label,
        mark,
        fails_check_digits(name, observation.value),
    )


_ORDINALS = ("first", "second", "third", "fourth", "fifth")


def _number_scans(readings: list[_Reading]) -> list[_Reading]:
    """'the first scan', 'the second scan'... when several engines read the document."""
    engines = list(dict.fromkeys(r.lineage for r in readings if r.is_scan))
    if len(engines) < 2:
        return readings
    names = {
        engine: f"the {_ORDINALS[i]} scan" if i < len(_ORDINALS) else f"scan {i + 1}"
        for i, engine in enumerate(engines)
    }
    return [replace(r, label=names[r.lineage]) if r.is_scan else r for r in readings]


def _ranked(readings: list[_Reading]) -> list[_Reading]:
    """Strongest first, fully tie-broken: never depends on input order."""
    return sorted(readings, key=lambda r: order_key(r.observation))


def _common_keys(readings: Sequence[_Reading]) -> list[Any]:
    first, rest = readings[0], readings[1:]
    return [k for k in first.keys if all(k in r.keys for r in rest)]


def _value_for(name: str, key: Any, readings: Iterable[_Reading]) -> Any:
    for reading in readings:
        for candidate in reading.normalized.candidates:
            if comparison_key(name, candidate) == key:
                return candidate
    return key


def _either(parts: Sequence[str]) -> str:
    items = list(dict.fromkeys(parts))
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _unreadable_notes(
    name: str, readings: Sequence[_Reading], firm_invalid: Sequence[_Reading]
) -> tuple[str, ...]:
    labels = [r.label for r in readings if not r.normalized.readable and r not in firm_invalid]
    if not labels:
        return ()
    return (f"I couldn't read {field_label(name)} from {join(labels)}.",)


def _invalid_notes(name: str, firm_invalid: Sequence[_Reading]) -> tuple[str, ...]:
    if not firm_invalid:
        return ()
    return (f"What {join(r.label for r in firm_invalid)} shows for {field_label(name)} isn't valid.",)


def _firm_invalid(readings: Sequence[_Reading], policy: VerificationPolicy) -> list[_Reading]:
    """Values a confident document source states that fail their own checks (not scan misreads)."""
    return [
        r
        for r in readings
        if r.invalid and not r.is_scan and r.observation.confidence >= policy.min_confidence
    ]


# --------------------------------------------------------------------------- outcomes


def _unconfirmed(ctx: _Context, usable: list[_Reading]) -> FieldAssessment:
    label = field_label(ctx.name)
    if not usable:
        reasons = () if ctx.notes or ctx.invalid_notes else (f"I haven't found {label} yet.",)
        return ctx.result(Quality.AMBER, None, reasons)
    common = _common_keys(usable)
    if len(common) == 1:
        value = _value_for(ctx.name, common[0], usable)
        return ctx.result(Quality.AMBER, value, [f"I only have an unclear reading of {label} so far."])
    return ctx.result(Quality.AMBER, None, [f"The readings of {label} I have are unclear and don't agree."])


_WONT_GUESS = "I won't guess. A person needs to check which is right."


def _conflict(ctx: _Context, confident: list[_Reading], invalid: Sequence[_Reading] = ()) -> FieldAssessment:
    """RED: every value on the table, none picked (§19). ``invalid`` values are named as such."""
    groups: dict[Any, list[_Reading]] = {}
    for reading in confident:
        if reading.clear:
            groups.setdefault(reading.keys[0], []).append(reading)
    unclear = [r for r in confident if not r.clear]
    values = [_value_for(ctx.name, key, rs) for key, rs in groups.items()]
    candidates = [list(r.normalized.candidates) for r in unclear]
    raws = [r.raw for r in invalid]
    shown = iter(ctx.show_many([*values, *(c for cs in candidates for c in cs), *raws]))
    parts = [f"{next(shown)} ({join(r.label for r in rs)})" for rs in groups.values()]
    parts += [f"{_either([next(shown) for _ in cs])} ({r.label})" for r, cs in zip(unclear, candidates)]
    parts += [f"{next(shown)} ({r.label}), which isn't valid" for r in invalid]
    reasons = [f"The sources disagree on {field_label(ctx.name)}: {' vs '.join(parts)}.", _WONT_GUESS]
    every = (*values, *(c for cs in candidates for c in cs), *raws)
    return ctx.result(
        Quality.RED, None, reasons, invalid_noted=not invalid, conflicting_values=tuple(dict.fromkeys(every))
    )


def _marks_agree(readings: Iterable[_Reading]) -> bool:
    marks = [r.mark for r in readings if r.mark is not None]
    return all(a.compatible(b) for a, b in combinations(marks, 2))


def _currency_clash(ctx: _Context, confident: list[_Reading]) -> FieldAssessment:
    """RED: the same number printed in currencies that cannot be the same (§19)."""
    groups: dict[str, list[_Reading]] = {}
    for reading in confident:
        if reading.mark is not None:
            groups.setdefault(reading.mark.written, []).append(reading)
    parts = [f"{written} ({join(r.label for r in rs)})" for written, rs in groups.items()]
    reason = f"The sources disagree on the currency of {field_label(ctx.name)}: {' vs '.join(parts)}."
    printed = tuple(dict.fromkeys(str(r.observation.value).strip() for rs in groups.values() for r in rs))
    return ctx.result(Quality.RED, None, [reason, _WONT_GUESS], conflicting_values=printed)


def _ambiguous(ctx: _Context, confident: list[_Reading], common: list[Any]) -> FieldAssessment:
    values = tuple(_value_for(ctx.name, key, confident) for key in common)
    reason = (
        f"{_capital(field_label(ctx.name))} could be {_either([ctx.show(v) for v in values])}. "
        "I need one more source to be sure."
    )
    return ctx.result(Quality.AMBER, None, [reason], possible_values=values)


def _has_pair(readings: Sequence[_Reading], *, need_high_rank: bool) -> bool:
    return any(
        a.lineage.isdisjoint(b.lineage) and (a.high_rank or b.high_rank or not need_high_rank)
        for a, b in combinations(readings, 2)
    )


def _settled(
    ctx: _Context, confident: list[_Reading], tentative: list[_Reading], key: Any
) -> FieldAssessment:
    supporters = [r for r in confident if r.clear and r.keys[0] == key]
    value = _value_for(ctx.name, key, supporters or confident)
    dissent = [r for r in tentative if key not in r.keys]
    other_currency = [r for r in tentative if key in r.keys and not _marks_agree([*confident, r])]
    support = tuple(r.observation for r in supporters)
    if dissent:
        shown = _either([ctx.show(c) for r in dissent for c in r.normalized.candidates])
        reason = f"An unclear reading from {join(r.label for r in dissent)} showed {shown}, so I can't confirm this yet."
        return ctx.result(Quality.AMBER, value, [reason], supporting=support)
    if other_currency:
        marks = join(r.mark.written for r in other_currency if r.mark is not None)
        reason = (
            f"An unclear reading from {join(r.label for r in other_currency)} showed it in another "
            f"currency ({marks}), so I can't confirm this yet."
        )
        return ctx.result(Quality.AMBER, value, [reason], supporting=support)
    if not supporters:
        reason = f"Every source could be read more than one way; only {ctx.show(value)} fits them all."
        return ctx.result(Quality.AMBER, value, [reason])
    if _has_pair(supporters, need_high_rank=True):
        reason = f"Confirmed by {join(r.label for r in supporters)}."
        return ctx.result(Quality.GREEN, value, [reason], supporting=support)
    return ctx.result(Quality.AMBER, value, [_why_not_green(supporters)], supporting=support)


def _why_not_green(supporters: Sequence[_Reading]) -> str:
    if not _has_pair(supporters, need_high_rank=False):
        channels = set().union(*(r.lineage for r in supporters))
        if len({_token_label(t) for t in channels}) == 1:
            return f"Only {_token_label(next(iter(channels)))} shows this so far."
        return "I only have one independent source for this so far."
    if all(r.is_scan for r in supporters):
        return "The scans agree, but I still need a more reliable source."
    return f"{_capital(join(r.label for r in supporters))} agree, but I still need a more reliable source."


# --------------------------------------------------------------------------- entry points


def assess_field(
    name: CriticalField | str,
    observations: Iterable[FieldObservation],
    *,
    policy: VerificationPolicy = DEFAULT_POLICY,
    hints: NormalizeHints | None = None,
    currency: str | None = None,
) -> FieldAssessment:
    """Verify one field (rules in the module docstring).

    ``currency`` only affects how amounts are written in the reasons.
    """
    label = field_name(name)
    observed = tuple(observations)
    readings = _number_scans(_ranked([_read(label, o, hints) for o in observed]))
    firm_invalid = _firm_invalid(readings, policy)
    ctx = _Context(
        label,
        observed,
        currency,
        _unreadable_notes(label, readings, firm_invalid),
        _invalid_notes(label, firm_invalid),
    )
    usable = [r for r in readings if r.normalized.readable]
    confident = [r for r in usable if r.observation.confidence >= policy.min_confidence]
    if not confident:
        return _unconfirmed(ctx, usable)
    common = _common_keys(confident)
    if not common or firm_invalid:
        return _conflict(ctx, confident, firm_invalid)
    if not _marks_agree(confident):
        return _currency_clash(ctx, confident)
    if len(common) > 1:
        return _ambiguous(ctx, confident, common)
    tentative = [r for r in usable if r.observation.confidence < policy.min_confidence]
    return _settled(ctx, confident, tentative, common[0])


def verify_field(
    name: CriticalField | str,
    observations: Iterable[FieldObservation],
    *,
    policy: VerificationPolicy = DEFAULT_POLICY,
    hints: NormalizeHints | None = None,
    currency: str | None = None,
) -> VerifiedField:
    """Domain :class:`VerifiedField` for one field; see :func:`assess_field`."""
    return assess_field(name, observations, policy=policy, hints=hints, currency=currency).verified
