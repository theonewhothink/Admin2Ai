"""Reading uploaded PDFs and photos, replay-safe (§13–19, §53).

The document reader (``backoffice.reading``) may call an OCR sidecar or the
Claude vision fallback: external, paid and not deterministic. A replay must
rebuild exactly what the owner saw, so a file is never read while an event
is applied:

1. **Before** the event is recorded, :func:`pre_read` finds every PDF or photo
   the upload contains (the file itself, email attachments, archive members,
   found with the engine's own intake on a scratch registry) and reads each
   one with the live reader, as the Document agent would.
2. The event stores every :class:`~backoffice.reading.ReadOutcome` in full
   (:func:`encode_outcome`: text, observations with their exact value types,
   steps, cost, embedded XML), keyed by the SHA-256 of the file's bytes.
3. Live **and** on replay the engine reads through a :class:`RecordedReader`
   that returns exactly the recorded outcome. A file that was not read in
   advance gets a fixed "not read" outcome, the same every time.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import importlib
import logging
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

__all__ = ["RecordedReader", "decode_outcome", "encode_outcome", "not_read", "pre_read", "readable_files"]

log = logging.getLogger("backoffice.server.reads")

# Classes an encoded outcome may name: only the engine's own value types.
_ALLOWED_MODULES = ("backoffice.",)


def _path(cls: type) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def _load(path: str) -> type:
    module, _, name = path.partition(":")
    if not module.startswith(_ALLOWED_MODULES):
        raise ValueError(f"refusing to load {path!r} from an event")
    obj: Any = importlib.import_module(module)
    for part in name.split("."):
        obj = getattr(obj, part)
    if not isinstance(obj, type):
        raise ValueError(f"{path!r} is not a class")
    return obj


def encode(value: Any) -> Any:
    """JSON that keeps every Python type the reader produces (Decimal, date, bytes, enums, models)."""
    if isinstance(value, Enum):  # before str/int: StepState and ExtractionMethod are str enums
        return {"$enum": _path(type(value)), "v": encode(value.value)}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return {"$float": repr(value)}
    if isinstance(value, Decimal):
        return {"$dec": str(value)}
    if isinstance(value, datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, (bytes, bytearray)):
        return {"$bytes": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, BaseModel):
        return {"$model": _path(type(value)),
                "v": {name: encode(getattr(value, name)) for name in type(value).model_fields}}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {"$dc": _path(type(value)),
                "v": {f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)}}
    if isinstance(value, tuple):
        return {"$tuple": [encode(v) for v in value]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, Mapping):
        return {"$map": [[encode(k), encode(v)] for k, v in value.items()]}
    raise TypeError(f"cannot record a {type(value).__name__} in an event")


def decode(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, Mapping):
        raise ValueError("malformed recorded value")
    if "$float" in value:
        return float(value["$float"])
    if "$dec" in value:
        return Decimal(value["$dec"])
    if "$dt" in value:
        return datetime.fromisoformat(value["$dt"])
    if "$date" in value:
        return date.fromisoformat(value["$date"])
    if "$bytes" in value:
        return base64.b64decode(value["$bytes"])
    if "$enum" in value:
        cls = _load(value["$enum"])
        if not issubclass(cls, Enum):
            raise ValueError(f"{value['$enum']!r} is not an enum")
        return cls(decode(value["v"]))
    if "$model" in value:
        cls = _load(value["$model"])
        if not issubclass(cls, BaseModel):
            raise ValueError(f"{value['$model']!r} is not a model")
        return cls.model_validate({k: decode(v) for k, v in value["v"].items()})
    if "$dc" in value:
        cls = _load(value["$dc"])
        if not dataclasses.is_dataclass(cls):
            raise ValueError(f"{value['$dc']!r} is not a dataclass")
        return cls(**{k: decode(v) for k, v in value["v"].items()})
    if "$tuple" in value:
        return tuple(decode(v) for v in value["$tuple"])
    if "$map" in value:
        return {decode(k): decode(v) for k, v in value["$map"]}
    raise ValueError("malformed recorded value")


def encode_outcome(outcome: Any) -> dict[str, Any]:
    from backoffice.reading import ReadOutcome

    if not isinstance(outcome, ReadOutcome):
        raise TypeError("not a ReadOutcome")
    return encode(outcome)


def decode_outcome(data: Mapping[str, Any]) -> Any:
    from backoffice.reading import ReadOutcome

    outcome = decode(data)
    if not isinstance(outcome, ReadOutcome):
        raise ValueError("the recorded value is not a ReadOutcome")
    return outcome


def not_read(detail: str = "not read before it was recorded", *, failed: bool = False) -> Any:
    """The fixed outcome for a file that has no recorded reading (the same live and on every replay)."""
    from backoffice.reading import ReadOutcome, ReadStep, StepState

    return ReadOutcome(steps=(ReadStep("read", StepState.FAILED if failed else StepState.NOT_AVAILABLE, detail),))


def sha(data: bytes) -> str:
    return hashlib.sha256(bytes(data)).hexdigest()


class RecordedReader:
    """The engine's document reader during an apply: what was read before the event was recorded."""

    external_ai = False

    def __init__(self, outcomes: Mapping[str, Any] | None = None) -> None:
        self._outcomes = dict(outcomes or {})

    @classmethod
    def from_event(cls, recorded: Mapping[str, Any] | None) -> RecordedReader:
        return cls({digest: decode_outcome(data) for digest, data in (recorded or {}).items()})

    def engines(self) -> tuple[str, ...]:
        return ()

    def read(self, request: Any) -> Any:
        found = self._outcomes.get(sha(request.data))
        return found if found is not None else not_read()


# --------------------------------------------------------------------------- before recording


_READABLE = ("pdf", "image", "screenshot")


def readable_files(tenant_id: str, data: bytes, *, filename: str | None, mime_type: str | None,
                   is_screenshot: bool = False) -> list[tuple[str, bytes, str | None]]:
    """``(evidence id, bytes, mime type)`` of every PDF or photo inside one upload.

    Runs the engine's own intake (sniffing, email parsing, archive expansion)
    against a scratch registry, so what is found is what the pipeline will
    read, and nothing is stored for the tenant.
    """
    from backoffice.evidence import EvidenceRegistry, ShareIntake
    from backoffice.orchestrator import MemoryObjectStore

    registry = EvidenceRegistry(MemoryObjectStore())
    intake = ShareIntake(registry)
    try:
        outcome = intake.ingest_file(tenant_id, bytes(data), filename=filename, mime_type=mime_type,
                                     is_screenshot=is_screenshot)
    except Exception:  # whatever the intake refuses is refused again when the event applies
        return []
    evidence = [r.evidence for r in outcome.registrations]
    if outcome.email is not None:
        evidence += outcome.email.all_evidence()
    found: dict[str, tuple[str, bytes, str | None]] = {}
    for ev in evidence:
        if ev.format.value in _READABLE and ev.id not in found:
            found[ev.id] = (ev.id, registry.open(tenant_id, ev.id), ev.mime_type)
    return list(found.values())


def pre_read(svc: Any, reader: Any, uploads: Sequence[tuple[bytes, str | None, str | None]]) -> dict[str, Any]:
    """Read every PDF or photo in ``uploads`` (bytes, filename, mime type) with the live reader.

    Returns ``{sha256: encoded ReadOutcome}`` for the event. Files the tenant
    has already read keep their earlier reading (no second OCR bill).
    """
    if reader is None:
        return {}
    from backoffice.reading import ReadRequest

    repo = svc.repo
    documents = svc.orchestrator.documents
    out: dict[str, Any] = {}
    for data, filename, mime_type in uploads:
        for evidence_id, blob, mime in readable_files(repo.tenant_id, data, filename=filename, mime_type=mime_type):
            digest = sha(blob)
            if digest in out or evidence_id in repo.reads:
                continue
            request = ReadRequest(
                tenant_id=repo.tenant_id, evidence_id=evidence_id, data=blob, mime_type=mime,
                stage0_fields=lambda text, method, _id=evidence_id: documents.stage0_fields(text, _id, method),
                extractor=documents.text_extractor(),
            )
            try:
                outcome = reader.read(request)
            except Exception as exc:  # a reader failure never loses the upload: it is stored, unread
                log.warning("read_failed", extra={"exc_type": type(exc).__name__})
                outcome = not_read(f"reading failed: {type(exc).__name__}", failed=True)
            out[digest] = encode_outcome(outcome)
    return out
