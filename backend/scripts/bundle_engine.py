#!/usr/bin/env python3
"""Zip the backoffice engine (Python sources only) for the in-browser build.

Usage: python backend/scripts/bundle_engine.py [output.zip]
Default output: web/public/engine/backoffice.zip

The archive holds ``backoffice/**.py`` (plus any non-code data files the
package reads at runtime, such as ``.json``/``.txt``/``.eml``/``.xml``), and
nothing from ``__pycache__``. Entries are written in sorted order with a fixed
timestamp so two builds of the same sources give byte-identical archives.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
PACKAGE = SRC / "backoffice"
DEFAULT_OUT = HERE.parent.parent / "web" / "public" / "engine" / "backoffice.zip"
DATA_SUFFIXES = {".py", ".json", ".txt", ".eml", ".xml", ".csv", ".typed"}
# Server-only parts that need fastapi/uvicorn/temporalio; the browser never imports them.
# backoffice/server is the production API (sign-in, PostgreSQL, push): never in the browser.
SKIP_DIRS = {"__pycache__", "server"}
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def bundle(out: Path) -> int:
    files = sorted(
        p for p in PACKAGE.rglob("*")
        if p.is_file() and p.suffix in DATA_SUFFIXES and not (SKIP_DIRS & set(p.relative_to(SRC).parts))
    )
    if not files:
        raise SystemExit(f"no sources found under {PACKAGE}")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(SRC).as_posix(), date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    tmp.replace(out)
    return len(files)


def main(argv: list[str]) -> int:
    out = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_OUT
    n = bundle(out)
    print(f"bundled {n} files into {out} ({out.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
