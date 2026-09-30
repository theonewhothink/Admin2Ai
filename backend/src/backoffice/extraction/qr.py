"""QR and barcode payloads as Stage 0 evidence (§13, §19).

Decoding pixels and understanding payloads are separate steps:

* :class:`ZXingDecoder` turns an image into decoded strings with the optional
  ``zxing-cpp`` and ``Pillow`` packages (imported lazily). Phones usually
  decode on-device (§11) and send the strings instead.
* :class:`QRHook` turns decoded strings into observations through
  registered handlers. The built-in handler reads the EPC069-12 "SEPA
  credit transfer" payment QR. A country's fiscal QR (the Portuguese invoice
  QR, §19) plugs in with :func:`fiscal_qr_handler` without this package
  importing the country pack.

A payload that claims a format but is malformed never becomes an
observation; it is noted so verification can react (§19 "never guess").
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from backoffice.domain.models import CriticalField, DocumentType, ExtractionMethod

from ._collect import Collector
from ._optional import import_optional
from .fields import Stage0Result, group_named
from .values import iban_is_valid

__all__ = [
    "QR_CONFIDENCE",
    "DecodedCode",
    "QRHandler",
    "QRHook",
    "ZXingDecoder",
    "fiscal_qr_handler",
    "parse_epc_qr",
]

F = CriticalField

QR_CONFIDENCE = 0.95

QRHandler = Callable[[str, str], Stage0Result | None]
"""``handler(payload, source)``: a result when the payload is this handler's
format, None when it is not. Raises ValueError when it is, but malformed."""


# --------------------------------------------------------------------------- EPC QR

_EPC_AMOUNT = re.compile(r"EUR(\d{1,9}(?:\.\d{1,2})?)")


def parse_epc_qr(payload: str, source: str) -> Stage0Result | None:
    """EPC069-12 SEPA credit-transfer QR ("GiroCode"): IBAN, currency, reference.

    The amount is an amount *to pay*, which can differ from the invoice total
    (for example after withholding), so it is kept as the extra
    ``amount_payable`` rather than claimed as the gross amount.
    Layout (one field per line): BCD / version 001|002 / character set /
    SCT|INST / BIC / name / IBAN / EUR amount / purpose / structured
    reference / unstructured remittance / information.
    """
    lines = payload.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "BCD":
        return None
    lines += [""] * (12 - len(lines))
    version, identification = lines[1].strip(), lines[3].strip()
    if version not in ("001", "002") or identification not in ("SCT", "INST"):
        raise ValueError("epc_qr_header")
    c = Collector("epc_qr", source, ExtractionMethod.QR, QR_CONFIDENCE)
    iban = lines[6].strip()
    if iban_is_valid(iban):
        c.add(F.IBAN, iban, "epc_qr:iban")
    else:
        c.note("epc_qr_invalid_iban")
    amount = lines[7].strip()
    if amount:
        m = _EPC_AMOUNT.fullmatch(amount)
        if m is None:
            raise ValueError("epc_qr_amount")
        c.add(F.CURRENCY, "EUR", "epc_qr:amount")
        c.extra("amount_payable", Decimal(m[1]))
    c.add(F.PAYMENT_REFERENCE, lines[9].strip() or None, "epc_qr:reference")
    c.extra("beneficiary_name", lines[5].strip() or None)
    c.extra("bic", lines[4].strip() or None)
    c.extra("remittance_text", lines[10].strip() or None)
    return c.result()


# --------------------------------------------------------------------------- hook


def fiscal_qr_handler(parse: Callable[[str, str], Any]) -> QRHandler:
    """Adapt a country pack's ``parse_fiscal_qr(payload, evidence_id)``.

    The parser returns None for foreign payloads, raises ValueError for
    malformed ones, and otherwise an object with ``observations`` (each
    naming its ``field``), ``consistent``, ``usable``, ``doc_type``,
    ``country`` and ``notes``. An inconsistent code keeps its observations:
    the disagreement must surface as a conflict, not be hidden (§19).
    """

    def handler(payload: str, source: str) -> Stage0Result | None:
        parsed = parse(payload, source)
        if parsed is None:
            return None
        notes = [str(n) for n in getattr(parsed, "notes", ())]
        if not getattr(parsed, "consistent", True):
            notes.append("fiscal_qr_inconsistent")
        if not getattr(parsed, "usable", True):
            notes.append("fiscal_qr_not_usable")
        doc_type = getattr(parsed, "doc_type", None)
        country = str(getattr(parsed, "country", "") or "").lower()
        grouped = group_named(parsed.observations)
        return Stage0Result.build(
            f"fiscal_qr_{country}" if country else "fiscal_qr",
            source,
            ((f, o) for f, obs in grouped.items() for o in obs),
            doc_type=doc_type if isinstance(doc_type, DocumentType) else None,
            notes=notes,
        )

    return handler


class QRHook:
    """Runs decoded QR payloads through registered handlers, first match wins."""

    def __init__(self, handlers: Sequence[QRHandler] = (parse_epc_qr,)) -> None:
        self._handlers: list[QRHandler] = list(handlers)

    def register(self, handler: QRHandler, *, first: bool = False) -> None:
        if first:
            self._handlers.insert(0, handler)
        else:
            self._handlers.append(handler)

    def extract(self, payloads: Iterable[str], *, source: str) -> tuple[Stage0Result, ...]:
        """One result per recognised payload; malformed ones become a noted, empty result."""
        results: list[Stage0Result] = []
        for payload in dict.fromkeys(p for p in payloads if p and p.strip()):
            result = self._one(payload, source)
            if result is not None:
                results.append(result)
        return tuple(results)

    def _one(self, payload: str, source: str) -> Stage0Result | None:
        for handler in self._handlers:
            try:
                result = handler(payload, source)
            except ValueError as exc:
                code = str(exc.args[0]) if exc.args and isinstance(exc.args[0], str) else "malformed"
                return Stage0Result.build("qr", source, (), notes=[f"qr_unreadable:{code[:60]}"])
            except Exception as exc:  # a buggy handler must not stop Stage 0; OCR still runs
                return Stage0Result.build("qr", source, (), notes=[f"qr_handler_failed:{type(exc).__name__}"])
            if result is not None:
                return result
        return None


# --------------------------------------------------------------------------- image decoding


@dataclass(frozen=True)
class DecodedCode:
    format: str
    text: str


class ZXingDecoder:
    """Decode QR codes (and optionally barcodes) from an image with zxing-cpp."""

    def __init__(self, *, qr_only: bool = True) -> None:
        self._qr_only = qr_only

    def decode(self, image: bytes) -> tuple[DecodedCode, ...]:
        zxingcpp = import_optional("zxingcpp", feature="Reading QR codes from images", package="zxing-cpp")
        pil_image = import_optional("PIL.Image", feature="Reading QR codes from images", package="Pillow")
        with pil_image.open(io.BytesIO(image)) as img:
            found = zxingcpp.read_barcodes(img)
        codes = []
        for item in found:
            if not getattr(item, "valid", True) or not getattr(item, "text", ""):
                continue
            fmt = str(getattr(item, "format", ""))
            if self._qr_only and "qr" not in fmt.lower():
                continue
            codes.append(DecodedCode(format=fmt, text=item.text))
        return tuple(codes)
