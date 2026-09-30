"""Lazy loading of optional heavy dependencies (§13-15).

Heavy engines and parsers (paddleocr, pypdf, zxing-cpp, Pillow) are never
needed at import time or by the test suite. Adapters call
:func:`import_optional` at the moment they need one and get a clear,
typed error when it is not installed.
"""

from __future__ import annotations

import importlib
from types import ModuleType

__all__ = ["MissingDependencyError", "import_optional"]


class MissingDependencyError(ImportError):
    """An optional package needed for this feature is not installed."""

    def __init__(self, module: str, feature: str, package: str | None = None) -> None:
        self.module = module
        self.feature = feature
        self.package = package or module
        super().__init__(
            f"{feature} needs the optional package {self.package!r}; "
            f"install it with: pip install {self.package}"
        )


def import_optional(module: str, *, feature: str, package: str | None = None) -> ModuleType:
    """Import ``module`` or raise :class:`MissingDependencyError`."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise MissingDependencyError(module, feature, package) from exc
