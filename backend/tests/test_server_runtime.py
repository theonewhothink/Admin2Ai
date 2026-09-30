"""Event-sourced tenants: record then apply, replay to the identical state, refuse divergence (§55)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _server_support import (
    PASSWORD,
    bearer,
    build_business,
    harness,
    read_paths,
    signup,
)

from backoffice.evidence.store import LocalObjectStore
from backoffice.server.events import Event, chain_hash, state_digest
from backoffice.server.runtime import ReplayDiverged, TenantManager
from backoffice.server.store import MemoryStore, StoredEvent


def _reads(h, token: str, paths: list[str]) -> dict[str, tuple[int, object]]:
    out = {}
    for p in paths:
        res = h.client.get(p, headers=bearer(token))
        out[p] = (res.status_code, res.json())
    return out


def _companies(h, token: str) -> list[str]:
    return [c["id"] for c in h.client.get("/api/companies", headers=bearer(token)).json()["companies"]]


def test_replay_rebuilds_every_read_exactly(tmp_path: Path) -> None:
    h = harness(tmp_path)
    account = signup(h.client)
    token, tenant = account["token"], account["tenant"]["id"]
    seen = build_business(h, token)
    h.clock.step = h.clock.step * 0  # freeze time: both reads see the same "now"
    paths = read_paths(_companies(h, token), seen["documents"])
    before = _reads(h, token, paths)
    with h.manager.open(tenant) as rt:
        digest_before = state_digest(rt.svc)
    h.manager.evict(tenant)
    assert not h.manager.cached(tenant)
    after = _reads(h, token, paths)
    for p in paths:
        assert after[p] == before[p], p
    with h.manager.open(tenant) as rt:
        assert state_digest(rt.svc) == digest_before
    assert all(status == 200 for status, _ in before.values())


def test_writes_append_then_apply_and_keep_no_file_bytes_or_passwords(tmp_path: Path) -> None:
    h = harness(tmp_path)
    account = signup(h.client)
    seen = build_business(h, account["token"])
    rows = h.store.events(account["tenant"]["id"])
    kinds = [r.kind for r in rows]
    assert kinds[:2] == ["tenant.created", "company.added"]
    assert {"request", "accountant.set", "api_key.created", "chat.rule", "company.added"} <= set(kinds)
    text = "\n".join(r.body for r in rows)
    assert "app-password-123" not in text and "$redacted" in text
    assert seen["api_key"] not in text  # the API key itself is never recorded, only prefix and fingerprint
    uploads = [json.loads(r.body)["data"]["body"] for r in rows
               if r.kind == "request" and json.loads(r.body)["data"]["path"] == "/api/evidence"]
    refs = [u["dataBase64"]["$object"] for u in uploads if isinstance(u.get("dataBase64"), dict)]
    assert refs and all(set(ref) == {"key", "sha256", "size"} for ref in refs)
    for ref in refs:
        assert ref["key"].startswith(account["tenant"]["id"] + "/sha256/") and ref["key"].endswith(ref["sha256"])
        assert h.objects.get(ref["key"])  # the bytes are in the object store, hash-verified
    # times never go backwards and every event links to the one before
    for prev, row in zip(rows, rows[1:], strict=False):
        assert row.prev_hash == prev.hash and row.at >= prev.at


def test_reads_do_not_change_the_tenant(tmp_path: Path) -> None:
    h = harness(tmp_path)
    account = signup(h.client)
    tenant = account["tenant"]["id"]
    count = len(h.store.events(tenant))
    for p in read_paths(_companies(h, account["token"])):
        h.client.get(p, headers=bearer(account["token"]))
    assert len(h.store.events(tenant)) == count


def test_a_tampered_event_is_refused_not_served(tmp_path: Path) -> None:
    h = harness(tmp_path)
    account = signup(h.client)
    token, tenant = account["token"], account["tenant"]["id"]
    h.client.post("/api/tasks", json={"title": "Call the bank"}, headers=bearer(token))
    rows = h.store.events(tenant)
    last = rows[-1]
    forged = last.body.replace("Call the bank", "Pay the attacker")
    h.store.tamper(tenant, last.seq, StoredEvent(last.seq, last.at, last.kind, last.actor, forged, last.prev_hash,
                                                 last.hash))
    h.manager.evict(tenant)
    res = h.client.get("/api/tasks", headers=bearer(token))
    assert res.status_code == 503
    assert res.json()["message"] == "Your data is safe, but I can't open it right now. The team has been alerted."
    assert "Traceback" not in res.text


def test_a_rechained_log_with_changed_data_diverges(tmp_path: Path) -> None:
    """Even a log whose hashes were all recomputed is caught: the state digests no longer match."""
    h = harness(tmp_path)
    account = signup(h.client)
    token, tenant = account["token"], account["tenant"]["id"]
    H = bearer(token)
    h.client.post("/api/tasks", json={"title": "Call the bank"}, headers=H)
    h.client.post("/api/tasks", json={"title": "Pay rent"}, headers=H)
    rows = h.store.events(tenant)
    target = len(rows) - 2  # the first task: its successor carries the digest of the honest state
    rebuilt: list[StoredEvent] = list(rows[:target])
    for row in rows[target:]:
        body = row.body.replace("Call the bank", "Pay the attacker")
        prev = rebuilt[-1].hash
        rebuilt.append(StoredEvent(row.seq, row.at, row.kind, row.actor, body, prev, chain_hash(prev, body)))
    for row in rebuilt[target:]:
        h.store.tamper(tenant, row.seq, row)
    manager = TenantManager(h.store, h.objects, now=h.clock)
    with pytest.raises(ReplayDiverged) as err:
        with manager.open(tenant):
            pass
    assert err.value.seq == rows[-1].seq and "state before the event" in err.value.reason
    with pytest.raises(ReplayDiverged):  # refused again without replaying
        with manager.open(tenant):
            pass


def test_two_processes_share_one_log(tmp_path: Path) -> None:
    """Two API processes (two managers, one store): each catches up before reading or writing."""
    h = harness(tmp_path)
    account = signup(h.client)
    token, tenant = account["token"], account["tenant"]["id"]
    other = TenantManager(h.store, h.objects, now=h.clock)
    h.client.post("/api/tasks", json={"title": "From process one"}, headers=bearer(token))
    status, body = other.command(tenant, "usr_x", "POST", "/api/tasks", {"title": "From process two"})
    assert status == 200 and [t["title"] for t in body["tasks"]] == ["From process one", "From process two"]
    # Process one has not seen process two's write yet; its next write loses the race, catches up, retries.
    status, body = h.manager.command(tenant, "usr_x", "POST", "/api/tasks", {"title": "Again from one"})
    assert status == 200 and len(body["tasks"]) == 3
    tasks = h.client.get("/api/tasks", headers=bearer(token)).json()["tasks"]
    assert [t["title"] for t in tasks] == ["From process one", "From process two", "Again from one"]
    with other.open(tenant) as a, h.manager.open(tenant) as b:
        assert state_digest(a.svc) == state_digest(b.svc)


def test_a_failed_live_apply_is_voided_and_left_out(tmp_path: Path) -> None:
    class BrokenMailer:
        def send(self, *args: object) -> None:
            raise ConnectionError("smtp down")

    h = harness(tmp_path, mailer=BrokenMailer())
    account = signup(h.client)
    token, tenant = account["token"], account["tenant"]["id"]
    H = bearer(token)
    reply = h.client.post("/api/chat/tool", json={"name": "draft_email", "input": {
        "to": ["marc@vidal.pt"], "subject": "Hello", "body": "Monthly package"}}, headers=H).json()
    draft = reply["result"]["draft_id"]
    res = h.client.post(f"/api/chat/outbox/{draft}/send", json={}, headers=H)
    assert res.status_code == 500 and res.json()["error"] == "server_error" and "smtp" not in res.text
    rows = h.store.events(tenant)
    assert rows[-1].kind == "void" and json.loads(rows[-1].body)["data"]["seq"] == rows[-2].seq
    # Rebuilt without the failed send: the draft is still a draft, and the tenant opens fine.
    with h.manager.open(tenant) as rt:
        assert rt.svc.assistant.outbox[draft].status == "draft"
    ok = h.client.post("/api/tasks", json={"title": "Still works"}, headers=H)
    assert ok.status_code == 200


def test_the_day_turns_once_on_the_first_request(tmp_path: Path) -> None:
    h = harness(tmp_path)
    account = signup(h.client)
    token, tenant = account["token"], account["tenant"]["id"]
    h.client.get("/api/home", headers=bearer(token))
    assert [r.kind for r in h.store.events(tenant)].count("tick") == 0
    h.clock.advance(days=1)
    h.client.get("/api/home", headers=bearer(token))
    h.client.get("/api/home", headers=bearer(token))
    assert [r.kind for r in h.store.events(tenant)].count("tick") == 1


def test_replay_is_the_same_in_another_process(tmp_path: Path) -> None:
    """Ids and times come from the events, so a fresh interpreter (other hash seed) rebuilds the same state."""
    h = harness(tmp_path)
    account = signup(h.client)
    token, tenant = account["token"], account["tenant"]["id"]
    build_business(h, token)
    with h.manager.open(tenant) as rt:
        expected = state_digest(rt.svc)
    dump = tmp_path / "events.json"
    dump.write_text(json.dumps([[r.seq, r.at.isoformat(), r.kind, r.actor, r.body, r.prev_hash, r.hash]
                                for r in h.store.events(tenant)]))
    script = f"""
import json, sys
from datetime import datetime
from backoffice.evidence.store import LocalObjectStore
from backoffice.server.events import state_digest
from backoffice.server.runtime import TenantManager
from backoffice.server.store import MemoryStore, StoredEvent, Tenant, User
store = MemoryStore()
rows = [StoredEvent(s, datetime.fromisoformat(a), k, ac, b, p, h) for s, a, k, ac, b, p, h in json.load(open({str(dump)!r}))]
store.create_account(user=User("usr_x", "x@x.pt", "X"), password_hash="scrypt$1$1$1$a$b",
                     tenant=Tenant({tenant!r}, "X"), roles=["owner"], events=rows, at=rows[0].at)
m = TenantManager(store, LocalObjectStore({str(tmp_path / 'objects')!r}))
with m.open({tenant!r}) as rt:
    print(state_digest(rt.svc))
"""
    src = str(Path(__file__).resolve().parents[1] / "src")
    for seed in ("1", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": src}
        out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        assert out.stdout.strip() == expected


def test_signup_events_replay_on_their_own(tmp_path: Path) -> None:
    store, objects = MemoryStore(), LocalObjectStore(tmp_path / "o")
    h = harness(tmp_path, store=store, objects=objects)
    account = signup(h.client)
    rows = store.events(account["tenant"]["id"])
    assert [Event.parse(r).kind for r in rows] == ["tenant.created", "company.added"]
    fresh = TenantManager(store, objects, now=h.clock)
    with fresh.open(account["tenant"]["id"]) as rt:
        assert list(rt.svc.repo.companies) == ["padaria-lda"]
        assert rt.svc.repo.companies["padaria-lda"].tax_id == "516123459"
        assert rt.svc.repo.owner.email == "ana@example.pt"
    assert h.client.post("/api/auth/login", json={"email": "ana@example.pt", "password": PASSWORD}).status_code == 200
