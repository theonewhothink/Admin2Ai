"""The Diagram page's data: what the system is doing, read from the engine itself."""
import pytest

from backoffice.domain.lifecycle import ORDER, Stage
from backoffice.service import BackOfficeService


@pytest.fixture()
def svc():
    return BackOfficeService.demo()


def pipeline(svc):
    status, body = svc.dispatch("GET", "/api/pipeline", None)
    assert status == 200, body
    return body


def test_counts_match_the_engines_items(svc):
    p = pipeline(svc)
    items = list(svc.repo.items.values())
    assert p["summary"]["items"] == len(items) == len(p["items"])
    assert [s["id"] for s in p["stages"]] == [s.value for s in ORDER]
    assert [s["label"] for s in p["stages"]] == ["Found", "Collected", "Read", "Checked", "Matched", "Acted",
                                                 "Confirmed", "Closed"]
    now = {s["id"]: s["now"] for s in p["stages"] + p["side"]}
    assert sum(now.values()) == len(items)  # every item is somewhere, exactly once
    assert now["closed"] == sum(i.stage is Stage.CLOSED for i in items) == p["summary"]["closed"]
    assert now["needs_owner"] == 2 == p["summary"]["waiting"]
    passed = {s["id"]: s["passed"] for s in p["stages"]}
    assert passed["discovered"] == len(items) >= passed["verified"] >= passed["closed"]
    assert p["summary"]["steps"] == sum(len(i.history) for i in items)
    assert sum(s["count"] for s in p["sources"]) == len(items)


def test_every_journey_is_the_real_history_with_its_agents(svc):
    p = pipeline(svc)
    by_id = {r["id"]: r for r in p["items"]}
    for item in svc.repo.items.values():
        row = by_id[item.id]
        assert [j["stage"] for j in row["journey"]] == [t.to_stage.value for t in item.history]
        assert all(j["evidence"] >= 1 for j in row["journey"])  # no step without evidence (§3)
        assert row["stage"] == item.stage.value
    closed = [r for r in p["items"] if r["stage"] == "closed"]
    assert closed and all(r["quality"] == "verified" for r in closed)  # closure requires GREEN
    assert [r["open"] for r in p["items"]] == sorted((r["open"] for r in p["items"]), reverse=True)  # open first


def test_what_is_happening_now_is_said_plainly(svc):
    rows = {(r["title"], r["detail"]): r for r in pipeline(svc)["items"]}
    vodafone = rows[("Vodafone", "Invoice FT VF2026/1290")]
    assert vodafone["stage"] == "needs_owner" and vodafone["href"] == "/needs-you#nd_vodafone_iban"
    assert vodafone["reason"] == "Waiting for you: Vodafone changed the IBAN shown on its invoice."
    assert [j["agentLabel"] for j in vodafone["journey"]] == ["Discovery", "Document reader", "Fraud check"]
    ikea = rows[("IKEA", "Card payment •••• 4817")]
    assert ikea["reason"] == "Waiting for you: We aren't sure which company this belongs to."
    edp = rows[("EDP Comercial", "Direct debit")]
    assert edp["reason"] == "Asked EDP for the invoice for the €64.10 payment. Waiting for their reply."
    agents = {a["id"]: a for a in pipeline(svc)["agents"]}
    assert agents["fraud"]["count"] == 1 and agents["missing"]["unit"] == "supplier asked"


def test_answers_move_items_on_the_diagram(svc):
    before = pipeline(svc)["summary"]
    svc.dispatch("POST", "/api/needs-you/nd_ikea_418/answer", {"option_id": "entity:company-c"})
    after = pipeline(svc)
    assert after["summary"]["waiting"] == before["waiting"] - 1
    assert after["summary"]["closed"] == before["closed"] + 2  # the payment and its receipt
    ikea = next(r for r in after["items"] if r["title"] == "IKEA" and r["detail"].startswith("Card"))
    assert ikea["stage"] == "closed"
    steps = [(j["stage"], j["agentLabel"]) for j in ikea["journey"]]
    assert ("needs_owner", "Company assignment") in steps and ("understood", "You") in steps
    assert steps[-1] == ("closed", "Closure")
