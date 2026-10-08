"""Engines registered by name, so any one can be swapped instantly (§16).

The router refers to engines only by name ("pp-ocrv6-medium",
"paddleocr-vl", ...). Replacing an engine is one call, with no code change
and no redeploy of the routing logic:

    registry.replace(UNLIMITED_OCR, NewLongDocumentProvider(...))
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

from .base import OCRProviderInterface

__all__ = [
    "COMMERCIAL",
    "LOCAL_OCR",
    "PADDLEOCR_VL",
    "PP_OCR_V6_MEDIUM",
    "PP_OCR_V6_TINY",
    "UNLIMITED_OCR",
    "EngineRegistry",
    "UnknownEngineError",
]

# Conventional engine names used by the default routing roles.
PP_OCR_V6_TINY = "pp-ocrv6-tiny"
PP_OCR_V6_MEDIUM = "pp-ocrv6-medium"
PADDLEOCR_VL = "paddleocr-vl"
UNLIMITED_OCR = "unlimited-ocr"
COMMERCIAL = "commercial"
# PP-OCRv6 in the server's own process (RapidOCR + onnxruntime), the primary engine when no sidecar is configured.
LOCAL_OCR = "rapidocr"


class UnknownEngineError(KeyError):
    """No engine is registered under that name."""


class EngineRegistry:
    """Thread-safe name -> provider map."""

    def __init__(self, providers: Iterable[OCRProviderInterface] = ()) -> None:
        self._engines: dict[str, OCRProviderInterface] = {}
        self._lock = threading.RLock()
        for provider in providers:
            self.register(provider)

    def register(
        self, provider: OCRProviderInterface, *, name: str | None = None, replace: bool = False
    ) -> None:
        """Register ``provider`` under ``name`` (default: its own name)."""
        if not isinstance(provider, OCRProviderInterface):
            raise TypeError(f"{type(provider).__name__} does not implement OCRProviderInterface")
        key = (name or provider.name).strip()
        if not key:
            raise ValueError("engine name cannot be blank")
        with self._lock:
            if key in self._engines and not replace:
                raise ValueError(f"an engine is already registered as {key!r}")
            self._engines[key] = provider

    def replace(self, name: str, provider: OCRProviderInterface) -> OCRProviderInterface | None:
        """Swap the engine behind ``name``; returns the previous one, if any."""
        with self._lock:
            previous = self._engines.get(name)
            self.register(provider, name=name, replace=True)
            return previous

    def unregister(self, name: str) -> None:
        with self._lock:
            self._engines.pop(name, None)

    def get(self, name: str) -> OCRProviderInterface:
        with self._lock:
            try:
                return self._engines[name]
            except KeyError:
                raise UnknownEngineError(name) from None

    def find(self, name: str) -> OCRProviderInterface | None:
        with self._lock:
            return self._engines.get(name)

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._engines)

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return name in self._engines

    def __len__(self) -> int:
        with self._lock:
            return len(self._engines)
