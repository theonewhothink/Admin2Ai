"""psql executor, connection URLs and the CLI's offline paths (no database needed)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "db") not in sys.path:
    sys.path.insert(0, str(REPO / "db"))

from backoffice_db import ConnectionParams, PsqlError, PsqlExecutor  # noqa: E402
from backoffice_db.__main__ import run  # noqa: E402
from backoffice_db.executor import _NULL  # noqa: E402


# --------------------------------------------------------------------------- URLs


def test_url_with_password_host_port_and_ssl() -> None:
    params = ConnectionParams.from_url("postgresql://bo%40api:s%3Acr%2Ft@db.internal:6432/backoffice?sslmode=verify-full")
    assert params.env() == {
        "PGHOST": "db.internal",
        "PGPORT": "6432",
        "PGUSER": "bo@api",
        "PGPASSWORD": "s:cr/t",
        "PGDATABASE": "backoffice",
        "PGSSLMODE": "verify-full",
    }
    assert "s:cr/t" not in params.redacted() and "user=bo@api" in params.redacted()


def test_unix_socket_host_goes_in_the_query() -> None:
    params = ConnectionParams.from_url("postgresql://postgres@/app?host=/run/postgresql")
    assert params.env() == {"PGHOST": "/run/postgresql", "PGUSER": "postgres", "PGDATABASE": "app"}


def test_postgres_scheme_alias_is_accepted() -> None:
    assert ConnectionParams.from_url("postgres://u@h/d").env()["PGHOST"] == "h"


@pytest.mark.parametrize(
    "url",
    ["mysql://u@h/d", "postgresql://u@h/d?sslmod=require", "http://h/d"],
)
def test_bad_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        ConnectionParams.from_url(url)


# --------------------------------------------------------------------------- executor with a fake psql


class Recorder:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = (returncode, stdout, stderr)

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"argv": argv, **kwargs})
        code, out, err = self.result
        return subprocess.CompletedProcess(argv, code, out, err)


def _executor(recorder: Recorder, **kwargs: Any) -> PsqlExecutor:
    return PsqlExecutor(
        ConnectionParams.from_url("postgresql://app:hunter2@db/backoffice"),
        psql="/usr/bin/psql",
        runner=recorder,
        base_env={"PATH": "/usr/bin", "PGPASSWORD": "leaked", "PGHOST": "elsewhere", "HOME": "/root"},
        **kwargs,
    )


def test_execute_runs_one_transaction_and_keeps_the_password_off_argv() -> None:
    rec = Recorder()
    _executor(rec).execute("CREATE TABLE t (a int);")
    call = rec.calls[0]
    argv = call["argv"]
    assert argv[0] == "/usr/bin/psql"
    assert {"-X", "--no-password", "--single-transaction", "--file=-", "--set=ON_ERROR_STOP=1"} <= set(argv)
    assert not any("hunter2" in a for a in argv)
    assert call["input"] == "CREATE TABLE t (a int);"
    env = call["env"]
    assert env["PGPASSWORD"] == "hunter2" and env["PGHOST"] == "db"  # ambient PG* dropped
    assert env["PATH"] == "/usr/bin" and "PGOPTIONS" not in env


def test_query_parses_csv_with_nulls_commas_and_newlines() -> None:
    out = f'name,note,missing\r\n"a,b","line1\nline2",{_NULL}\r\nplain,,x\r\n'
    rec = Recorder(stdout=out)
    rows = _executor(rec).query("SELECT 1")
    assert rows == [
        {"name": "a,b", "note": "line1\nline2", "missing": None},
        {"name": "plain", "note": "", "missing": "x"},
    ]
    assert "--csv" in rec.calls[0]["argv"] and "--command=SELECT 1" in rec.calls[0]["argv"]


def test_query_with_no_output_is_empty() -> None:
    assert _executor(Recorder(stdout="")).query("SELECT 1 WHERE false") == []


def test_errors_carry_the_sqlstate() -> None:
    rec = Recorder(returncode=3, stderr="psql:<stdin>:4: ERROR:  23001: evidence is immutable: UPDATE is not allowed\n")
    with pytest.raises(PsqlError) as err:
        _executor(rec).execute("UPDATE evidence SET filename = 'x'")
    assert err.value.sqlstate == "23001" and err.value.returncode == 3
    assert "immutable" in str(err.value)


def test_connection_errors_have_no_sqlstate() -> None:
    rec = Recorder(returncode=2, stderr="psql: error: connection to server failed\n")
    with pytest.raises(PsqlError) as err:
        _executor(rec).query("SELECT 1")
    assert err.value.sqlstate is None


def test_session_settings_travel_in_pgoptions() -> None:
    rec = Recorder()
    ex = _executor(rec).with_settings(role="backoffice_app", app__tenant_id="t-1")
    ex.execute("SELECT 1")
    assert rec.calls[0]["env"]["PGOPTIONS"] == "-c app.tenant_id=t-1 -c role=backoffice_app"


@pytest.mark.parametrize(
    ("name", "value"),
    [("app.tenant_id", "a b"), ("app.tenant_id", "x;DROP"), ("Bad-Name", "x"), ("a.b.c", "x")],
)
def test_unsafe_session_settings_are_refused(name: str, value: str) -> None:
    with pytest.raises(ValueError):
        _executor(Recorder(), session_settings={name: value})


def test_missing_psql_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(FileNotFoundError):
        PsqlExecutor(ConnectionParams.from_url("postgresql://u@h/d"))


# --------------------------------------------------------------------------- CLI without a database


def test_cli_lint_needs_no_database(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["lint"], env={}) == 0
    assert "migrations OK" in capsys.readouterr().out


def test_cli_without_a_database_url_is_a_configuration_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["status"], env={}) == 2
    assert "MIGRATION_DATABASE_URL" in capsys.readouterr().err


def test_cli_reports_broken_migration_layout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "0002_only.sql").write_text("SELECT 1;")
    assert run(["lint", "--dir", str(tmp_path)], env={}) == 1
    assert "without gaps" in capsys.readouterr().err


def test_platform_injected_password_is_used_when_the_url_has_none() -> None:
    rec = Recorder()
    PsqlExecutor(
        ConnectionParams.from_url("postgresql://owner@db/backoffice?sslmode=require"),
        psql="/usr/bin/psql",
        runner=rec,
        base_env={"PGPASSWORD": "from-secrets-manager", "PGHOST": "elsewhere", "PGPASSFILE": "/run/pgpass"},
    ).execute("SELECT 1")
    env = rec.calls[0]["env"]
    assert env["PGPASSWORD"] == "from-secrets-manager" and env["PGPASSFILE"] == "/run/pgpass"
    assert env["PGHOST"] == "db"  # other ambient PG* settings never leak in


def test_cli_ensure_login_needs_the_password_variable(capsys: pytest.CaptureFixture[str]) -> None:
    code = run(["ensure-login", "backoffice_api", "--database-url", "postgresql://u@h/d"], env={})
    assert code == 2
    assert "APP_DB_PASSWORD" in capsys.readouterr().err


def test_cli_rejects_unknown_group_roles() -> None:
    with pytest.raises(SystemExit):
        run(["ensure-login", "svc", "--member-of", "postgres"], env={})
