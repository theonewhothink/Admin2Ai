"""Altered-document signals for the fraud engine (§26 "altered document").

This module only *reports*; it never decides that a document is forged.
Signals come from two places:

* **File metadata** (the PDF Info dictionary as read by the extraction
  layer: ``Producer``, ``Creator``, ``CreationDate``, ``ModDate``):
  saved by an editing program, changed after it was first made *and* after
  the invoice date, or dates that contradict each other.
* **Disagreeing readings of one document**: the hidden text layer against
  what a scan of the page shows (amounts), and the QR code against the
  printed values. Printed text can be edited without touching the QR code,
  and a pasted-over number leaves the old text layer behind.

Unlike field verification, which ignores unclear readings, every readable
value takes part here. A signal is ``STRONG`` only when both sides are
firm: confident, and either not an OCR/VLM reading or read the same way by
a second engine. One engine's misread is ``WEAK``; the field itself is
still a conflict in verification.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from itertools import product
from typing import Any

from backoffice.domain.models import ExtractionMethod, FieldObservation

from ._display import day, field_label, join, show
from .arithmetic import derive_observations
from .fields import DEFAULT_POLICY, READING_METHODS, VerificationPolicy, lineage, order_key
from .normalize import (
    AMOUNT_FIELDS,
    NormalizeHints,
    as_critical_field,
    comparison_key,
    field_name,
    normalize_value,
)

__all__ = [
    "EDITING_TOOLS",
    "PdfMetadata",
    "SignalStrength",
    "TamperKind",
    "TamperSignal",
    "detect_tampering",
    "metadata_signals",
    "observation_signals",
    "parse_pdf_date",
]


class TamperKind(str, Enum):
    EDITED_AFTER_ISSUE = "edited_after_issue"
    EDITING_SOFTWARE = "editing_software"
    METADATA_INCONSISTENT = "metadata_inconsistent"
    TEXT_LAYER_MISMATCH = "text_layer_mismatch"
    QR_MISMATCH = "qr_mismatch"
    QR_INCONSISTENT = "qr_inconsistent"


class SignalStrength(str, Enum):
    WEAK = "weak"  # worth a look together with other signals
    STRONG = "strong"  # enough on its own to stop and ask (§26 hard stop is the fraud engine's call)


@dataclass(frozen=True)
class TamperSignal:
    kind: TamperKind
    strength: SignalStrength
    detail: str  # plain language, safe to show
    field: str | None = None
    values: tuple[str, ...] = ()
    observations: tuple[FieldObservation, ...] = ()


# --------------------------------------------------------------------------- metadata

# Programs mostly used to edit existing PDFs or images, with the name shown
# to the owner. A heuristic list, not proof of anything: a signal from it
# alone is WEAK. Owner text names the program from this table, never the
# file's own Producer string, which whoever made the file controls.
_TOOLS: tuple[tuple[str, str], ...] = (
    (r"photoshop", "Photoshop"),
    (r"gimp", "GIMP"),
    (r"illustrator", "Illustrator"),
    (r"inkscape", "Inkscape"),
    (r"pixelmator", "Pixelmator"),
    (r"affinity photo", "Affinity Photo"),
    (r"affinity designer", "Affinity Designer"),
    (r"paint\.net", "Paint.NET"),
    (r"pdfescape", "PDFescape"),
    (r"sejda", "Sejda"),
    (r"ilovepdf", "iLovePDF"),
    (r"smallpdf", "Smallpdf"),
    (r"pdf-?xchange editor", "PDF-XChange Editor"),
    (r"foxit (?:phantompdf|pdf editor)", "Foxit PDF Editor"),
    (r"nitro (?:pro|pdf pro)", "Nitro PDF"),
    (r"pdfelement", "PDFelement"),
    (r"pdffiller", "pdfFiller"),
    (r"dochub", "DocHub"),
    (r"soda ?pdf", "Soda PDF"),
    (r"pdf candy", "PDF Candy"),
)
EDITING_TOOLS = re.compile("|".join(pattern for pattern, _ in _TOOLS), re.IGNORECASE)
_TOOL_NAMES = tuple((re.compile(pattern, re.IGNORECASE), name) for pattern, name in _TOOLS)
_RAW_LIMIT = 200  # how much of a file's own program name is passed on to the fraud engine


def _bounded(raw: str) -> str:
    return "".join(c for c in raw if c.isprintable())[:_RAW_LIMIT]


_PDF_DATE = re.compile(
    r"(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
    r"(?:(Z)(?:00'?(?:00'?)?)?|([+-])(\d{2})'?(?:(\d{2})'?)?)?"
)


def parse_pdf_date(value: object) -> datetime | None:
    """A timezone-aware datetime from a PDF date ("D:20260918143000+01'00'") or ISO text.

    A PDF date without an offset is read as UTC; callers compare calendar
    days with a grace period, so the assumption cannot create a signal.
    """
    if isinstance(value, datetime):
        return value if value.utcoffset() is not None else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    match = _PDF_DATE.fullmatch(text)
    if match is None:
        return _iso_datetime(text)
    year, month, dd, hh, mm, ss = (int(g) if g else d for g, d in zip(match.groups()[:6], (0, 1, 1, 0, 0, 0)))
    try:
        return datetime(year, month, dd, hh, mm, ss, tzinfo=_offset(match))
    except ValueError:
        return None


def _offset(match: re.Match[str]) -> timezone:
    sign = match.group(8)
    if not sign:
        return timezone.utc
    minutes = int(match.group(9)) * 60 + int(match.group(10) or 0)
    return timezone(timedelta(minutes=-minutes if sign == "-" else minutes))


def _iso_datetime(text: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class PdfMetadata:
    producer: str | None = None
    creator: str | None = None
    created_at: datetime | None = None
    modified_at: datetime | None = None

    @classmethod
    def from_mapping(cls, metadata: Mapping[str, Any]) -> PdfMetadata:
        """From an Info dictionary; keys may carry a leading "/" and any case."""
        found = {str(k).lstrip("/").lower(): v for k, v in metadata.items()}

        def text(key: str) -> str | None:
            value = found.get(key)
            if value is None:
                return None
            return str(value).strip() or None

        return cls(
            producer=text("producer"),
            creator=text("creator"),
            created_at=parse_pdf_date(found.get("creationdate")),
            modified_at=parse_pdf_date(found.get("moddate")),
        )

    @property
    def editing_tool(self) -> str | None:
        """The Producer/Creator text (as the file states it) that names an editing program."""
        for name in (self.producer, self.creator):
            if name and EDITING_TOOLS.search(name):
                return name
        return None

    @property
    def editing_tool_name(self) -> str | None:
        """The editing program's own name from our table, safe to show."""
        raw = self.editing_tool
        if raw is None:
            return None
        return next(name for pattern, name in _TOOL_NAMES if pattern.search(raw))


def metadata_signals(
    metadata: PdfMetadata | Mapping[str, Any],
    issue_date: date | datetime | None = None,
    *,
    grace_days: int = 1,
    edit_gap: timedelta = timedelta(minutes=10),
) -> tuple[TamperSignal, ...]:
    """Signals from file metadata.

    "Changed after issue" needs both: a modification more than ``edit_gap``
    after creation (a portal that renders the PDF on download creates and
    modifies it at once) and a modification day later than the issue date
    plus ``grace_days``.
    """
    meta = metadata if isinstance(metadata, PdfMetadata) else PdfMetadata.from_mapping(metadata)
    if isinstance(issue_date, datetime):
        issue_date = issue_date.date()
    signals: list[TamperSignal] = []
    tool = meta.editing_tool
    if tool:
        signals.append(TamperSignal(
            TamperKind.EDITING_SOFTWARE, SignalStrength.WEAK,
            f"The file was saved with {meta.editing_tool_name}, a program often used to edit documents.",
            values=(_bounded(tool),),
        ))  # fmt: skip
    created, modified = meta.created_at, meta.modified_at
    if created and modified and modified < created - edit_gap:
        signals.append(TamperSignal(
            TamperKind.METADATA_INCONSISTENT, SignalStrength.WEAK,
            "The file's dates don't make sense: it says it was changed before it was made.",
        ))  # fmt: skip
    if issue_date and modified and (created is None or modified - created > edit_gap):
        changed_on = modified.date()
        if changed_on > issue_date + timedelta(days=grace_days):
            strength = SignalStrength.STRONG if tool and created else SignalStrength.WEAK
            signals.append(TamperSignal(
                TamperKind.EDITED_AFTER_ISSUE, strength,
                f"The file was changed on {day(changed_on)}, after the document date of {day(issue_date)}.",
                values=(changed_on.isoformat(), issue_date.isoformat()),
            ))  # fmt: skip
    return tuple(signals)


# --------------------------------------------------------------------------- readings


@dataclass(frozen=True, eq=False)
class _Value:
    observation: FieldObservation
    keys: frozenset[Any]
    shown: str
    confident: bool


@dataclass(frozen=True)
class _Reader:
    hints: NormalizeHints | None
    policy: VerificationPolicy
    currency: str | None

    def values(self, name: str, observations: Iterable[FieldObservation]) -> list[_Value]:
        found = []
        for obs in sorted(observations, key=order_key):  # details never depend on arrival order
            normalized = normalize_value(name, obs.value, self.hints)
            if normalized.readable:
                keys = frozenset(comparison_key(name, c) for c in normalized.candidates)
                shown = " or ".join(show(name, c, self.currency) for c in normalized.candidates)
                found.append(_Value(obs, keys, shown, obs.confidence >= self.policy.min_confidence))
        return found


def _qr_derived(v: _Value) -> bool:
    return v.observation.method is ExtractionMethod.ARITHMETIC and lineage(v.observation) == {"qr"}


def _firm(value: _Value, side: list[_Value]) -> bool:
    """Confident, and not a lone OCR/VLM reading: one engine's misread is not an alteration."""
    if not value.confident:
        return False
    if value.observation.method not in READING_METHODS:
        return True
    own = lineage(value.observation)
    return any(
        other is not value
        and other.confident
        and not other.keys.isdisjoint(value.keys)
        and lineage(other.observation).isdisjoint(own)
        for other in side
    )


def _mismatch(
    kind: TamperKind, name: str, left: list[_Value], right: list[_Value], detail: str
) -> TamperSignal | None:
    pairs = [(a, b) for a, b in product(left, right) if a.keys.isdisjoint(b.keys)]
    if not pairs:
        return None
    strong = any(_firm(a, left) and _firm(b, right) for a, b in pairs)
    involved = list(dict.fromkeys(v for pair in pairs for v in pair))
    return TamperSignal(
        kind,
        SignalStrength.STRONG if strong else SignalStrength.WEAK,
        detail.format(
            field=field_label(name), a=join(a.shown for a, _ in pairs), b=join(b.shown for _, b in pairs)
        ),
        field=name,
        values=tuple(dict.fromkeys(v.shown for v in involved)),
        observations=tuple(v.observation for v in involved),
    )


def _field_signals(name: str, values: list[_Value]) -> list[TamperSignal]:
    qr = [v for v in values if v.observation.method is ExtractionMethod.QR]
    printed = [
        v for v in values if v.observation.method in (ExtractionMethod.EMBEDDED_TEXT, *READING_METHODS)
    ]
    checks = [
        _mismatch(TamperKind.QR_MISMATCH, name, qr, printed,
                  "For {field}, the QR code says {a}, but the printed document shows {b}."),
    ]  # fmt: skip
    if as_critical_field(name) in AMOUNT_FIELDS:
        text_layer = [v for v in values if v.observation.method is ExtractionMethod.EMBEDDED_TEXT]
        scanned = [v for v in values if v.observation.method in READING_METHODS]
        qr_sums = [v for v in values if _qr_derived(v)]
        checks.append(
            _mismatch(
                TamperKind.TEXT_LAYER_MISMATCH,
                name,
                text_layer,
                scanned,
                "For {field}, the file's hidden text says {a}, but the visible page shows {b}.",
            )
        )
        checks.append(
            _weak(
                _mismatch(
                    TamperKind.QR_INCONSISTENT,
                    name,
                    qr,
                    qr_sums,
                    "The QR code's own amounts don't add up: {field} is {a}, its parts give {b}.",
                )
            )
        )
    return [s for s in checks if s is not None]


def _weak(signal: TamperSignal | None) -> TamperSignal | None:
    """A QR code that contradicts itself may be a generator bug: never STRONG alone."""
    return None if signal is None else replace(signal, strength=SignalStrength.WEAK)


def observation_signals(
    observations_by_field: Mapping[Any, Iterable[FieldObservation]],
    *,
    policy: VerificationPolicy = DEFAULT_POLICY,
    hints: NormalizeHints | None = None,
    currency: str | None = None,
) -> tuple[TamperSignal, ...]:
    """Text layer vs scan (amounts), QR vs printed (every field), QR vs its own sums.

    ``currency`` only affects how amounts are written in the details.
    """
    grouped: dict[str, list[FieldObservation]] = {}
    for key, observations in observations_by_field.items():
        grouped.setdefault(field_name(key), []).extend(observations)
    for field, derived in derive_observations(grouped, hints=hints).items():
        grouped.setdefault(field.value, []).extend(derived)
    reader = _Reader(hints, policy, currency)
    signals: list[TamperSignal] = []
    for name, observations in grouped.items():
        signals += _field_signals(name, reader.values(name, observations))
    return tuple(signals)


def detect_tampering(
    *,
    metadata: PdfMetadata | Mapping[str, Any] | None = None,
    issue_date: date | None = None,
    observations_by_field: Mapping[Any, Iterable[FieldObservation]] | None = None,
    policy: VerificationPolicy = DEFAULT_POLICY,
    hints: NormalizeHints | None = None,
    currency: str | None = None,
) -> tuple[TamperSignal, ...]:
    """All altered-document signals for one document, for the fraud engine."""
    found: list[TamperSignal] = []
    if metadata:
        found += metadata_signals(metadata, issue_date)
    if observations_by_field:
        found += observation_signals(observations_by_field, policy=policy, hints=hints, currency=currency)
    return tuple(found)
