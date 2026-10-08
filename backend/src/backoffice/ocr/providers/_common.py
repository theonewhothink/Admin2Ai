"""Helpers shared by the engine adapters: timing, result assembly, fan-out."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from decimal import Decimal
from typing import Any, TypeVar

from backoffice.domain.models import ExtractionMethod

from ..base import LayoutSignals, OCRLine, OCRPage, OCRResult

T = TypeVar("T")

Clock = Callable[[], float]
DEFAULT_CLOCK: Clock = time.perf_counter


def confidence(value: Any, scale: float = 1.0) -> float | None:
    """A 0-1 score from an engine value, or None when absent or out of range."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or scale <= 0:
        return None
    score = float(value) / scale
    if not math.isfinite(score) or score < 0.0 or score > 1.0 + 1e-6:
        return None
    return min(score, 1.0)


def text_lines(text: str) -> tuple[OCRLine, ...]:
    """Unboxed, unscored lines from a plain transcription."""
    return tuple(OCRLine(text=line.strip()) for line in text.splitlines() if line.strip())


def build_result(
    *,
    engine: str,
    version: str,
    method: ExtractionMethod,
    pages: Iterable[OCRPage],
    cost_per_page: Decimal,
    pages_billed: int,
    started: float,
    clock: Clock,
    warnings: Iterable[str] = (),
    from_prior_text: bool = False,
) -> OCRResult:
    ordered = tuple(sorted(pages, key=lambda p: p.number))
    elapsed_ms = max(0, round((clock() - started) * 1000))
    return OCRResult(
        engine=engine,
        model_version=version,
        method=method,
        pages=ordered,
        layout=LayoutSignals.combine(p.layout for p in ordered),
        duration_ms=elapsed_ms,
        cost=cost_per_page * pages_billed,
        pages_billed=pages_billed,
        warnings=tuple(dict.fromkeys(warnings)),
        from_prior_text=from_prior_text,
    )


async def gather_ordered(
    factories: Sequence[Callable[[], Awaitable[T]]], *, limit: int
) -> list[T]:
    """Run coroutines with bounded concurrency; results keep input order.

    On the first failure the remaining work is cancelled and the error is
    raised unchanged (no ExceptionGroup), so callers see the typed OCRError.
    """
    semaphore = asyncio.Semaphore(max(1, limit))

    async def run(factory: Callable[[], Awaitable[T]]) -> T:
        async with semaphore:
            return await factory()

    tasks = [asyncio.ensure_future(run(f)) for f in factories]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
