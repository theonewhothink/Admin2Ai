#!/usr/bin/env python3
"""Snapshot the demo engine's read-only replies for the in-browser build.

Usage: python backend/scripts/snapshot_engine.py [output.json]
Default output: web/public/engine/snapshot.json

On the static site the Python engine takes several seconds to start in the
browser (tens of seconds on slower machines). The demo it starts from is fixed
(frozen clock, same data every time), so its first answers are known at build
time. web/lib/engine.ts shows these while the engine starts, as long as the
visitor has not changed anything yet; everything after that comes from the
engine itself.

Each reply is serialised exactly as public/engine/worker.js does it
(``json.dumps(..., default=str)``), so the snapshot and the engine agree.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from backoffice.service import BackOfficeService  # noqa: E402

DEFAULT_OUT = HERE.parent.parent / "web" / "public" / "engine" / "snapshot.json"

# Every GET the web app sends without a body or a query string.
STATIC_PATHS = (
    "/api/home",
    "/api/needs-you",
    "/api/activity",
    "/api/companies",
    "/api/sources",
    "/api/connections",
    "/api/documents",
    "/api/settings/report",
    "/api/accountant/api-keys",
    "/api/accountant/clients",
    "/api/audit",
    "/api/pipeline",
    "/api/internal/overview",
    "/api/internal/operations",
    "/api/internal/readiness",
    "/api/internal/acceptance",
    "/api/obligations",
    "/api/billing",
    "/api/settings/automation",
    "/api/employees",
    "/api/expense-claims",
)


def _reply(service: BackOfficeService, path: str) -> dict[str, Any]:
    status, payload = service.dispatch("GET", path, None)
    # Same round trip as the worker: Python -> JSON text -> JavaScript value.
    return json.loads(json.dumps({"status": status, "body": payload}, default=str))


def snapshot() -> dict[str, dict[str, Any]]:
    service = BackOfficeService.demo()
    replies = {path: _reply(service, path) for path in STATIC_PATHS}

    companies = replies["/api/companies"]["body"].get("companies", [])
    for company in companies:
        cid = company["id"]
        replies[f"/api/companies/{cid}"] = _reply(service, f"/api/companies/{cid}")
        months = {company.get("currentMonth")} | {
            m.get("key") if isinstance(m, dict) else m for m in company.get("months", [])
        }
        for month in sorted(m for m in months if isinstance(m, str)):
            replies[f"/api/months/{cid}/{month}"] = _reply(service, f"/api/months/{cid}/{month}")

    for client in replies["/api/accountant/clients"]["body"].get("clients", []):
        path = f"/api/accountant/clients/{client['id']}"
        replies[path] = _reply(service, path)

    # Sources: each bank account's and card's payments, shown when the owner opens one.
    for group in replies["/api/sources"]["body"].get("groups", []):
        if group.get("id") in ("banks", "cards"):
            for item in group.get("items", []):
                path = f"/api/sources/{item['id']}/payments"
                replies[path] = _reply(service, path)

    # Only successful replies: anything else is left to the engine.
    return {path: r for path, r in sorted(replies.items()) if r["status"] == 200}


def main(argv: list[str]) -> int:
    out = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_OUT
    replies = snapshot()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"replies": replies}, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    print(f"snapshot: {len(replies)} replies into {out} ({out.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
