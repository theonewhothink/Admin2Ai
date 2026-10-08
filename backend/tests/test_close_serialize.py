"""JSON-safe conversion of closure outputs for the API layer (money as strings)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from backoffice.closure import (
    DueItem,
    Month,
    compute_month_status,
    home_summary,
    to_jsonable,
)
from backoffice.domain.models import Evidence, EvidenceFormat, Obligation, ObligationKind, SourceKind

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)


class Covered:
    name = "Gmail"
    healthy = False
    covered_from = datetime(2026, 1, 1, tzinfo=timezone.utc)
    covered_until = datetime(2026, 10, 2, 14, 42, tzinfo=timezone.utc)


def test_month_status_serialises_to_plain_json():
    status = compute_month_status("ent_1", Month(2026, 9), [], now=NOW, connectors=[Covered()])
    data = to_jsonable(status)
    json.dumps(data)  # fully JSON-safe
    assert data["month"] == "2026-09"
    assert data["state"] == "needs_owner"
    assert data["as_of"] == "2026-10-03T09:00:00+00:00"
    assert data["blockers"][0]["message"] == "Gmail has not synced since 14:42 yesterday."


def test_money_becomes_exact_strings():
    item = DueItem("obl_1", "ent_1", "Tax payment", date(2026, 10, 20), 17, Decimal("1234.50"), "owner")
    assert to_jsonable(item)["amount"] == "1234.50"
    assert to_jsonable(Decimal("1E+2")) == "100"


def test_home_summary_and_domain_models_serialise():
    status = compute_month_status("ent_1", Month(2026, 9), [], now=NOW, connectors=[Covered()])
    ob = Obligation(tenant_id="t1", entity_id="ent_1", kind=ObligationKind.RENT, title="Rent payment",
                    due_on=date(2026, 10, 5), amount=Decimal("900.00"))  # fmt: skip
    home = home_summary([status], {"ent_1": "Hazel Tree"}, today=date(2026, 10, 3), obligations=[ob])
    data = to_jsonable(home)
    json.dumps(data)
    assert data["due_soon"][0]["amount"] == "900.00"
    ev = Evidence(tenant_id="t1", source_kind=SourceKind.EMAIL, format=EvidenceFormat.PDF, sha256="a" * 64,
                  metadata={"ocr_confidence": 0.93})  # fmt: skip
    assert to_jsonable({"evidence": ev})["evidence"]["metadata"] == {"ocr_confidence": 0.93}
    assert to_jsonable(ob)["amount"] == "900.00"


def test_bare_floats_bytes_and_unknown_types_are_refused():
    with pytest.raises(TypeError):
        to_jsonable(1.5)
    with pytest.raises(TypeError):
        to_jsonable(b"raw")
    with pytest.raises(TypeError):
        to_jsonable(object())


def test_sets_are_sorted_for_determinism():
    assert to_jsonable({"b", "a"}) == ["a", "b"]
