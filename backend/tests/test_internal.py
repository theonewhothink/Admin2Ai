"""The team's internal dashboard ("Admin OS"): every figure must be the engine's own."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backoffice import internal
from backoffice.api.app import create_app
from backoffice.closure import InteractionKind, OwnerInteraction
from backoffice.domain.lifecycle import TERMINAL, Stage
from backoffice.domain.models import Quality
from backoffice.orchestrator import TZ
from backoffice.pipeline import build_pipeline
from backoffice.readiness import READINESS, ReadinessItem, readiness
from backoffice.service import BackOfficeService


@pytest.fixture()
def svc() -> BackOfficeService:
    return BackOfficeService.demo()


def get(svc: BackOfficeService, path: str) -> dict:
    status, body = svc.dispatch("GET", path, None)
    assert status == 200, body
    return body


def overview(svc: BackOfficeService) -> dict:
    return get(svc, "/api/internal/overview")


def golden(svc: BackOfficeService) -> dict[str, dict]:
    return {g["id"]: g for g in overview(svc)["golden"]}


def month_items(svc: BackOfficeService) -> list:
    month = svc._current_month()
    return [i for c in sorted(svc.repo.companies) for i in svc.repo.items_for(c, month)]


def never_shown_to_owner(item) -> bool:  # noqa: ANN001
    return item.stage is not Stage.NEEDS_OWNER and all(
        t.to_stage is not Stage.NEEDS_OWNER and t.actor.split(":")[0] != "owner" for t in item.history)


# --------------------------------------------------------------------------- golden totals (§59)


def test_zero_touch_is_closed_without_owner_over_all_items(svc: BackOfficeService) -> None:
    items = month_items(svc)
    alone = [i for i in items if i.stage in TERMINAL and i.quality is Quality.GREEN and never_shown_to_owner(i)]
    g = golden(svc)["zero_touch"]
    assert (g["count"], len(items)) == (len(alone), 21) and g["count"] == 18
    assert g["countLabel"] == "of 21 items"
    assert g["value"] == math.floor(len(alone) * 1000 / len(items)) / 10 == 85.7  # rounded down, never flattering
    assert g["ring"] == g["value"] and g["ringLabel"] == "85.7%"
    assert g["onTarget"] is False
    # 20 of 21 is 95.2% (> 95%): two more items must finish on their own.
    assert g["remaining"] == 2 and g["remainingLabel"] == "2 more items to reach target"


def test_missing_invoices_recovered_are_the_closure_logs_own(svc: BackOfficeService) -> None:
    from backoffice.closure import ActivityKind, Actor

    month = svc._current_month()
    log = [a for a in svc.repo.closure_log if a.month(TZ) == month]
    detected = {a.subject_id for a in log if a.kind is ActivityKind.MISSING_DOCUMENT_DETECTED}
    recovered = {a.subject_id for a in log if a.kind is ActivityKind.MISSING_DOCUMENT_RETRIEVED
                 and a.actor is Actor.SYSTEM} & detected
    g = golden(svc)["recovered"]
    assert (g["count"], g["countLabel"]) == (len(recovered), f"of {len(detected)} missing") == (1, "of 2 missing")
    assert g["value"] == 50.0 and g["ringLabel"] == "50%" and g["onTarget"] is False
    assert g["remaining"] == 1  # both must be recovered to be above 90%


def test_unresolved_items_match_the_month_statuses(svc: BackOfficeService) -> None:
    month = svc._current_month()
    statuses = [svc._status(c, month) for c in svc.repo.companies]
    open_items = sum(s.items_total - s.items_done for s in statuses)
    total = sum(s.items_total for s in statuses)
    g = golden(svc)["unresolved"]
    assert (g["count"], total) == (open_items, len(month_items(svc))) and open_items == 3
    assert g["value"] == math.ceil(open_items * 1000 / total) / 10 == 14.3  # rounded up, never flattering
    assert g["remaining"] == 3 and g["onTarget"] is False  # below 1% of 21 means none may stay open


def test_owner_minutes_are_the_engines_estimate(svc: BackOfficeService) -> None:
    g = golden(svc)["owner_minutes"]
    stats = [svc.month(c, str(svc._current_month()))["stats"]["minutesSpent"] for c in svc.repo.companies]
    assert g["count"] == g["value"] == max(stats) == 0
    assert g["estimate"] is True and "40 seconds per answer" in g["detail"] and "October so far: 0 min" in g["detail"]
    assert g["onTarget"] is True and g["remainingLabel"] == "14 min to spare"

    # Answering today counts towards October, the calendar month it happened in.
    svc.dispatch("POST", "/api/needs-you/nd_ikea_418/answer", {"option_id": "entity:company-c"})
    g = golden(svc)["owner_minutes"]
    assert g["count"] == 0 and "October so far: 1 min" in g["detail"]

    # Owner time about September is the engine's September figure, rounded up to whole minutes.
    svc.repo.interactions.append(OwnerInteraction(at=datetime(2026, 9, 20, 10, 0, tzinfo=TZ), active_seconds=601,
                                                  kind=InteractionKind.ANSWER))
    g = golden(svc)["owner_minutes"]
    assert g["count"] == 11 and g["ring"] == round(11 * 100 / 15, 1) and g["remainingLabel"] == "3 min to spare"
    svc.repo.interactions.append(OwnerInteraction(at=datetime(2026, 9, 21, 10, 0, tzinfo=TZ), active_seconds=300,
                                                  kind=InteractionKind.ANSWER, entity_id="hazel-tree"))
    g = golden(svc)["owner_minutes"]
    assert g["count"] == 16 and g["onTarget"] is False and g["remainingLabel"] == "2 min over target"
    assert g["ring"] == 100


def test_answers_move_the_golden_totals(svc: BackOfficeService) -> None:
    before = golden(svc)
    svc.dispatch("POST", "/api/needs-you/nd_ikea_418/answer", {"option_id": "entity:company-c"})
    after = golden(svc)
    assert after["unresolved"]["count"] == before["unresolved"]["count"] - 2  # the payment and its receipt
    # The payment needed the owner, so it is not zero-touch; its receipt never did, so it is.
    assert after["zero_touch"]["count"] == before["zero_touch"]["count"] + 1
    items = month_items(svc)
    alone = [i for i in items if i.stage in TERMINAL and i.quality is Quality.GREEN and never_shown_to_owner(i)]
    assert after["zero_touch"]["count"] == len(alone) and after["zero_touch"]["countLabel"] == "of 21 items"


# --------------------------------------------------------------------------- health


def test_health_combines_targets_evidence_and_connections(svc: BackOfficeService) -> None:
    h = overview(svc)["health"]
    parts = {p["id"]: p for p in h["parts"]}
    finished = [i for i in svc.repo.items.values() if i.stage in TERMINAL]
    assert all(i.quality is Quality.GREEN and all(t.evidence_ids for t in i.history) for i in finished)
    assert parts["evidence"]["score"] == 100 and parts["evidence"]["detail"].startswith(f"{len(finished)} of")
    assert parts["connections"]["score"] == 100
    # Automation: each target's attainment, capped at 100%, averaged.
    attain = [min(1, (18 / 21) / 0.95), min(1, 0.5 / 0.9), min(1, (18 / 21) / 0.99), 1]
    assert parts["automation"]["score"] == math.floor(sum(attain) / 4 * 100) == 83
    assert h["score"] == math.floor((83 + 100 + 100) / 3) == 94 and h["tone"] == "good"

    svc.dispatch("POST", "/api/connections/gmail/stale", {})
    h = overview(svc)["health"]
    assert {p["id"]: p["score"] for p in h["parts"]}["connections"] == 75


# --------------------------------------------------------------------------- critical fixes


def test_critical_fixes_come_from_the_engine(svc: BackOfficeService) -> None:
    fixes = overview(svc)["fixes"]
    assert [(f["severity"], f["label"], f["href"]) for f in fixes] == [
        ("red", "Vodafone changed its bank details", "/needs-you#nd_vodafone_iban"),
        ("blue", "IKEA €418.00: waiting for the owner", "/needs-you#nd_ikea_418"),
    ]
    assert fixes[0]["company"] == "Hazel Tree" and "on hold" in fixes[0]["detail"]

    svc.dispatch("POST", "/api/connections/gmail/stale", {})
    svc.dispatch("POST", "/api/needs-you/nd_ikea_418/answer", {"option_id": "entity:company-c"})
    fixes = overview(svc)["fixes"]
    assert [f["severity"] for f in fixes] == ["red", "amber"]
    stale = fixes[1]
    assert stale["label"] == "Gmail is not syncing (laura@hazeltree.pt)" and stale["href"] == "/sources"
    assert "needs reconnecting" in stale["detail"]


def test_conflicts_are_red_fixes(svc: BackOfficeService) -> None:
    item = next(i for i in month_items(svc) if i.stage is Stage.UNDERSTOOD)
    evidence = item.history[-1].evidence_ids
    assert svc.orchestrator.advance(item, Stage.CONFLICT, evidence, agent="verification", quality=Quality.RED,
                                    note="The QR code and the text disagree.")
    fixes = [f for f in overview(svc)["fixes"] if f["label"].startswith("Sources disagree")]
    assert len(fixes) == 1 and fixes[0]["severity"] == "red" and fixes[0]["href"] == "/diagram"
    assert overview(svc)["pipeline"]["summary"]["conflicts"] == 1


def test_deadlines_within_a_week_are_fixes(svc: BackOfficeService) -> None:
    assert not [f for f in overview(svc)["fixes"] if "Tax payment" in f["label"]]  # 20 October is 18 days away
    svc.repo.clock.advance_to(datetime(2026, 10, 15, 9, 0, tzinfo=TZ))
    due = [f for f in overview(svc)["fixes"] if "Tax payment" in f["label"]]
    assert [(f["severity"], f["label"]) for f in due] == [("amber", "Tax payment due in 5 days")]
    assert "Due 2026-10-20" in due[0]["detail"]
    svc.repo.clock.advance_to(datetime(2026, 10, 22, 9, 0, tzinfo=TZ))
    due = [f for f in overview(svc)["fixes"] if "Tax payment" in f["label"]]
    assert [f["severity"] for f in due] == ["red"]


# --------------------------------------------------------------------------- pipeline, tenants, connections


def test_pipeline_and_quick_stats_are_the_engines(svc: BackOfficeService) -> None:
    o = overview(svc)
    p = build_pipeline(svc)
    assert o["pipeline"]["summary"] == p["summary"]
    assert o["pipeline"]["stages"] == p["stages"] and o["pipeline"]["side"] == p["side"]
    assert {a["id"]: a["count"] for a in o["pipeline"]["agents"]} == {a["id"]: a["count"] for a in p["agents"]}
    records = sum(1 for _ in svc.repo.audit_store.records(svc.repo.tenant_id))
    assert {q["id"]: q["value"] for q in o["quick"]} == {
        "tenants": 1, "companies": 3, "items": len(svc.repo.items), "audit": records}
    assert o["period"] == {"key": "2026-09", "label": "September 2026"} and o["today"] == "2026-10-02"


def test_tenants_and_companies_table(svc: BackOfficeService) -> None:
    [tenant] = overview(svc)["tenants"]
    assert (tenant["id"], tenant["owner"], tenant["monthLabel"]) == ("demo-laura", "Laura Medina", "September")
    listed = {c["id"]: c for c in svc.companies()["companies"]}
    month = svc._current_month()
    for row in tenant["companies"]:
        status = svc._status(row["id"], month)
        assert row["statusLabel"] == listed[row["id"]]["statusLabel"] == status.company_status
        assert (row["percentClosed"], row["itemsDone"], row["itemsTotal"]) == (
            status.percent_closed, status.items_done, status.items_total)
    by_id = {c["id"]: c for c in tenant["companies"]}
    assert by_id["company-b"]["statusLabel"] == "Closed" and by_id["company-b"]["closedOn"] == "2026-10-01"
    assert tenant["connections"] == {"healthy": 4, "total": 4} and tenant["audit"]["intact"] is True
    assert tenant["needsYou"] == 2


def test_connections_list_every_connector(svc: BackOfficeService) -> None:
    conns = overview(svc)["connections"]
    assert [c["id"] for c in conns] == [f"demo-laura:{c}" for c in svc.repo.connectors]
    gmail = conns[0]
    assert gmail["status"] == "healthy" and gmail["companies"] == ["Hazel Tree", "Company B", "Company C"]
    assert gmail["lastSyncedAt"] == svc.repo.connectors["gmail"].last_synced_at.isoformat()
    svc.dispatch("POST", "/api/connections/gmail/stale", {})
    gmail = overview(svc)["connections"][0]
    assert gmail["status"] == "stale" and gmail["message"]


def test_targets_list_every_success_metric(svc: BackOfficeService) -> None:
    o = overview(svc)
    targets = {t["id"]: t for t in o["targets"]}
    assert list(targets) == ["owner_minutes", "zero_touch", "recovered", "silent_errors", "unresolved",
                             "onboarding_minutes", "accountant_owner"]
    g = {x["id"]: x for x in o["golden"]}
    assert targets["zero_touch"]["value"] == g["zero_touch"]["value"]
    assert targets["zero_touch"]["evidence"] == "18 of 21 items"
    assert targets["silent_errors"]["onTarget"] is True and targets["onboarding_minutes"]["value"] is None
    assert targets["owner_minutes"]["estimate"] is True


def test_several_tenants_add_up() -> None:
    one, two = BackOfficeService.demo(), BackOfficeService.demo()
    single = internal.overview([one])
    both = internal.overview([one, two])
    g1 = {g["id"]: g for g in single["golden"]}
    g2 = {g["id"]: g for g in both["golden"]}
    assert g2["zero_touch"]["count"] == 2 * g1["zero_touch"]["count"]
    assert g2["zero_touch"]["value"] == g1["zero_touch"]["value"]
    assert g2["recovered"]["countLabel"] == "of 4 missing"
    assert both["pipeline"]["summary"]["items"] == 2 * single["pipeline"]["summary"]["items"]
    assert len(both["tenants"]) == 2 and len(both["fixes"]) == 2 * len(single["fixes"])
    assert {q["id"]: q["value"] for q in both["quick"]}["tenants"] == 2


def test_reading_the_dashboard_changes_nothing(svc: BackOfficeService) -> None:
    records = sum(1 for _ in svc.repo.audit_store.records(svc.repo.tenant_id))
    first = overview(svc)
    get(svc, "/api/internal/operations")
    assert overview(svc) == first
    assert sum(1 for _ in svc.repo.audit_store.records(svc.repo.tenant_id)) == records


# --------------------------------------------------------------------------- operations


def test_operations_show_activity_and_the_audit_trail(svc: BackOfficeService) -> None:
    ops = get(svc, "/api/internal/operations")
    assert len(ops["activity"]) == len(svc.repo.activity)
    assert [a["at"] for a in ops["activity"]] == sorted((a["at"] for a in ops["activity"]), reverse=True)
    audit = ops["audit"]
    records = list(svc.repo.audit_store.records(svc.repo.tenant_id))
    assert audit["records"] == len(records) and audit["intact"] is True
    assert audit["chains"][0]["head"] == records[-1].hash
    assert audit["shown"] == len(audit["entries"]) == internal.AUDIT_LIMIT
    assert [e["seq"] for e in audit["entries"]] == [r.seq for r in reversed(records)][: internal.AUDIT_LIMIT]
    assert sum(a["count"] for a in audit["agents"]) == len(records)
    closing = next(e for e in audit["entries"] if e["action"] == "transition" and "Closed" in e["summary"])
    assert closing["summary"].startswith("→ Closed · verified") and closing["evidence"] >= 1
    assert get(svc, "/api/internal/operations?limit=5")["audit"]["shown"] == 5
    assert get(svc, "/api/internal/operations?limit=nonsense")["audit"]["shown"] == internal.AUDIT_LIMIT
    assert get(svc, "/api/internal/operations?limit=100000")["audit"]["shown"] == len(records)


def test_operations_notice_a_broken_chain(svc: BackOfficeService) -> None:
    from backoffice.audit import AuditRecord

    chain = svc.repo.audit_store._chains[svc.repo.tenant_id]
    chain[3] = AuditRecord(chain[3].tenant_id, chain[3].seq, chain[3].body.replace("discovery", "d1scovery"),
                           chain[3].prev_hash, chain[3].hash)
    ops = get(svc, "/api/internal/operations")
    assert ops["audit"]["intact"] is False and ops["audit"]["chains"][0]["problem"] == "hash_mismatch"
    assert overview(svc)["tenants"][0]["audit"]["intact"] is False


# --------------------------------------------------------------------------- readiness


def test_readiness_is_the_owners_checklist() -> None:
    r = readiness()
    titles = [i["title"] for i in r["items"]]
    assert titles == [
        "Static demo site", "Automatic deploys and checks", "Sign-in and accounts", "Saved data / database",
        "Email connection (Gmail, Outlook)", "Bank connection", "Reading PDFs and photos", "Mobile app in stores",
        "Security and data protection review", "Monitoring and backups", "Payments and billing",
    ]
    assert [i["id"] for i in r["items"] if i["status"] == "live"] == ["static-site", "deploys"]
    assert (r["live"], r["pending"], r["total"]) == (2, 9, 11)
    assert r["percent"] == sum(i.percent for i in READINESS) // len(READINESS)
    assert all(i["detail"] and len(i["detail"]) <= 120 for i in r["items"])


def test_readiness_items_are_validated() -> None:
    with pytest.raises(ValueError):
        ReadinessItem("x", "X", "live", 90, "Nearly.", "site")
    with pytest.raises(ValueError):
        ReadinessItem("x", "X", "pending", 100, "Done?", "site")
    with pytest.raises(ValueError):
        ReadinessItem("x", "X", "shipped", 100, "Done.", "site")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        readiness((READINESS[0], READINESS[0]))


def test_readiness_endpoint_and_overview_agree(svc: BackOfficeService) -> None:
    assert get(svc, "/api/internal/readiness") == overview(svc)["readiness"] == readiness()


# --------------------------------------------------------------------------- routing and access


def test_internal_paths_are_admin_only() -> None:
    assert internal.admin_only("/api/internal/overview")
    assert internal.admin_only("/api/internal/operations?limit=5")
    assert internal.admin_only("/api/internal")
    assert not internal.admin_only("/api/home")
    assert not internal.admin_only("/api/internalized")


def test_unknown_internal_views_are_not_found(svc: BackOfficeService) -> None:
    assert svc.dispatch("GET", "/api/internal/secrets", None)[0] == 404
    assert svc.dispatch("POST", "/api/internal/overview", {})[0] == 405


def test_over_http() -> None:
    client = TestClient(create_app(BackOfficeService.demo()))
    for path in ("/api/internal/overview", "/api/internal/operations?limit=3", "/api/internal/readiness"):
        res = client.get(path)
        assert res.status_code == 200, (path, res.text)
    assert client.get("/api/internal/operations?limit=3").json()["audit"]["shown"] == 3


def test_period_follows_the_engine_clock(svc: BackOfficeService) -> None:
    svc.repo.clock.advance_to(svc._now() + timedelta(days=31))
    o = overview(svc)
    assert o["period"]["key"] == "2026-10" and o["today"] == "2026-11-02"
