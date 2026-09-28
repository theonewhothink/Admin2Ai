"""Tenant scope statement, id validation and exact money parameters."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "db") not in sys.path:
    sys.path.insert(0, str(REPO / "db"))

from backoffice.domain.models import new_id  # noqa: E402
from backoffice_db import (  # noqa: E402
    GROUP_ROLES,
    MAX_ABS_AMOUNT,
    SCOPE_SQL,
    db_amount,
    db_currency,
    ensure_login_sql,
    scope_params,
    scope_sql,
    validate_id,
    validate_tenant_id,
)


def test_scope_statement_is_transaction_local() -> None:
    assert SCOPE_SQL == (
        "SELECT set_config('app.tenant_id', %s, true), set_config('app.user_id', %s, true)"
    )
    assert scope_sql("$1").endswith("set_config('app.user_id', $2, true)")
    assert scope_sql("?").count("?") == 2
    with pytest.raises(ValueError):
        scope_sql(":tenant")


def test_scope_params_validate_and_clear_the_user() -> None:
    assert scope_params("acme-pt") == ("acme-pt", "")
    assert scope_params("acme-pt", "usr_1") == ("acme-pt", "usr_1")
    with pytest.raises(ValueError):
        scope_params("")
    with pytest.raises(ValueError):
        scope_params("acme pt")


@pytest.mark.parametrize("value", ["t1", "acme.pt", "A-1_b", "x" * 128])
def test_valid_tenant_ids(value: str) -> None:
    assert validate_tenant_id(value) == value


@pytest.mark.parametrize(
    "value", ["", "-lead", "has space", "a/b", "a:b", "x" * 129, "ação", None, 7, "t1\n", "t1\n\n"]
)
def test_invalid_tenant_ids(value: object) -> None:
    with pytest.raises(ValueError):
        validate_tenant_id(value)


def test_model_ids_are_valid_record_ids() -> None:
    for prefix in ("ev", "doc", "tx", "item", "obl", "sup", "ent"):
        assert validate_id(new_id(prefix))
    assert validate_id("owner:usr_1")
    with pytest.raises(ValueError):
        validate_id("ev 1")


def test_group_roles_are_distinct_and_named_for_the_product() -> None:
    assert len(set(GROUP_ROLES)) == 4 and all(r.startswith("backoffice_") for r in GROUP_ROLES)


# --------------------------------------------------------------------------- money


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("483.60"), Decimal("483.60")),
        (Decimal("483.6"), Decimal("483.60")),
        (Decimal("-117.20"), Decimal("-117.20")),
        (Decimal("0"), Decimal("0.00")),
        (12, Decimal("12.00")),
        (MAX_ABS_AMOUNT, MAX_ABS_AMOUNT),
        (-MAX_ABS_AMOUNT, -MAX_ABS_AMOUNT),
    ],
)
def test_exact_amounts_pass(value: Decimal | int, expected: Decimal) -> None:
    result = db_amount(value)
    assert result == expected and result.as_tuple().exponent == -2


@pytest.mark.parametrize(
    "value",
    [
        Decimal("10.005"),  # PostgreSQL would silently store 10.01
        Decimal("0.001"),
        Decimal("10000000000000000.00"),
        Decimal("1E+40"),
        Decimal("NaN"),
        Decimal("Infinity"),
    ],
)
def test_inexact_or_out_of_range_amounts_are_refused(value: Decimal) -> None:
    with pytest.raises(ValueError):
        db_amount(value)


@pytest.mark.parametrize("value", [483.6, True, "483.60", None])
def test_non_decimal_money_is_refused(value: object) -> None:
    with pytest.raises(TypeError):
        db_amount(value)  # type: ignore[arg-type]


def test_currency_codes() -> None:
    assert db_currency("EUR") == "EUR"
    for bad in ("eur", "EURO", "E1R", ""):
        with pytest.raises(ValueError):
            db_currency(bad)


def test_trailing_newlines_never_validate() -> None:
    with pytest.raises(ValueError):
        db_currency("EUR\n")
    with pytest.raises(ValueError):
        validate_id("usr_1\n")


# --------------------------------------------------------------------------- service logins


def test_login_script_creates_a_plain_member_and_quotes_the_password() -> None:
    sql = ensure_login_sql("backoffice_api", "it's-a-long-pass$$word\\x", ["backoffice_app"])
    assert "PASSWORD 'it''s-a-long-pass$$word\\x';" in sql
    assert "NOSUPERUSER NOBYPASSRLS" in sql and "GRANT backoffice_app TO backoffice_api;" in sql
    assert sql.index("DO $$") < sql.index("END $$;") < sql.index("PASSWORD")  # secret stays outside $$


@pytest.mark.parametrize(
    ("user", "password", "groups"),
    [
        ("Bad-Name", "x" * 20, ["backoffice_app"]),
        ("backoffice_app", "x" * 20, ["backoffice_app"]),  # a group is not a login
        ("pg_monitor_x", "x" * 20, ["backoffice_app"]),
        ("svc", "short", ["backoffice_app"]),
        ("svc", "x" * 20 + "\x00", ["backoffice_app"]),
        ("svc", "x" * 20, []),
        ("svc", "x" * 20, ["postgres"]),
    ],
)
def test_login_script_refuses_unsafe_input(user: str, password: str, groups: list[str]) -> None:
    with pytest.raises(ValueError):
        ensure_login_sql(user, password, groups)
