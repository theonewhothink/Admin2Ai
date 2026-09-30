"""The build-time snapshot the static site shows while Python starts in the browser.

web/lib/engine.ts answers reads from it only until the engine is ready, so it
must be exactly what a freshly started demo engine would answer, whatever
order the pages ask in.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from backoffice.service import BackOfficeService

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "snapshot_engine.py"
_spec = importlib.util.spec_from_file_location("snapshot_engine", SCRIPT)
assert _spec and _spec.loader
snapshot_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(snapshot_engine)


def test_snapshot_covers_every_first_screen_read() -> None:
    replies = snapshot_engine.snapshot()
    assert set(snapshot_engine.STATIC_PATHS) <= set(replies)
    assert all(r["status"] == 200 for r in replies.values())
    companies = [c["id"] for c in replies["/api/companies"]["body"]["companies"]]
    assert companies and all(f"/api/companies/{c}" in replies for c in companies)
    assert any(p.startswith("/api/months/") for p in replies)


def test_snapshot_matches_a_fresh_engine_in_any_order() -> None:
    replies = snapshot_engine.snapshot()
    assert snapshot_engine.snapshot() == replies  # deterministic build
    shared = BackOfficeService.demo()
    for path in sorted(replies, reverse=True):
        assert snapshot_engine._reply(shared, path) == replies[path], path


def test_snapshot_file_is_what_the_browser_reads(tmp_path: Path) -> None:
    out = tmp_path / "engine" / "snapshot.json"
    assert snapshot_engine.main(["snapshot_engine.py", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["replies"] == snapshot_engine.snapshot()
