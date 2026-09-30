"""Reading uploaded PDFs and photos (§13-19, §53).

Public API
----------
    DocumentReader(registry=, budget=, qr_decoder=AUTO, external_ai=False).read(ReadRequest) -> ReadOutcome
    reader_from_env(env=os.environ) -> DocumentReader | None   (server configuration, see .config)
    read_pdf_stage0(data) / read_image_stage0(data, decoder) -> Stage0
    find_qr_decoder() -> QRDecoder | None      (zxing-cpp or pyzbar, when installed)
    ReadStep / StepState                       (what each step did: done, nothing found,
                                                not available, switched off, skipped, failed)
    run_sync(factory)                          (async engines from synchronous code)

Importing this package needs only pydantic: every optional reader (pypdf,
zxing-cpp, pyzbar, Pillow, pypdfium2) and the engine clients (httpx) are
imported when a file is actually read. The browser demo never creates a
reader, so uploads there are stored and wait, as before.
"""

from .config import external_ai_enabled, reader_from_env
from .reader import AUTO, DocumentReader, ReadOutcome, ReadRequest, run_sync
from .stage0 import (
    PyzbarQRDecoder,
    QRDecoder,
    ReadStep,
    Stage0,
    StepState,
    ZXingQRDecoder,
    find_qr_decoder,
    fiscal_qr_payloads,
    read_image_stage0,
    read_pdf_stage0,
)

__all__ = [
    "AUTO",
    "DocumentReader",
    "PyzbarQRDecoder",
    "QRDecoder",
    "ReadOutcome",
    "ReadRequest",
    "ReadStep",
    "Stage0",
    "StepState",
    "ZXingQRDecoder",
    "external_ai_enabled",
    "find_qr_decoder",
    "fiscal_qr_payloads",
    "read_image_stage0",
    "read_pdf_stage0",
    "reader_from_env",
    "run_sync",
]
