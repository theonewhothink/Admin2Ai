"""Running SQL against PostgreSQL through ``psql`` (§44 PostgreSQL).

The migration runner and the catalog checks only need two operations, so they
depend on the small :class:`SqlExecutor` protocol. :class:`PsqlExecutor`
implements it with the ``psql`` client (the same approach Sqitch uses), which
keeps this package free of Python database drivers; tests pass fakes.

Connection settings come from a ``postgresql://`` URL and reach ``psql``
through libpq environment variables, so a password never appears on a command
line or in a process listing.
"""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qsl, unquote, urlsplit

__all__ = [
    "ConnectionParams",
    "PsqlError",
    "PsqlExecutor",
    "Row",
    "SqlExecutor",
]

Row = dict[str, str | None]

# libpq query parameters accepted in a URL, and the variable each maps to.
_QUERY_ENV = {
    "host": "PGHOST",
    "port": "PGPORT",
    "user": "PGUSER",
    "password": "PGPASSWORD",
    "dbname": "PGDATABASE",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "application_name": "PGAPPNAME",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "options": "PGOPTIONS",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
}
# Kept from the environment when the URL itself carries no password.
_AMBIENT_CREDENTIALS = ("PGPASSWORD", "PGPASSFILE")
# Printed for SQL NULL in query output; control characters keep it apart from data.
_NULL = "\x1eNULL\x1e"
_SQLSTATE = re.compile(r"\b(?:ERROR|FATAL|PANIC):\s+([0-9A-Z]{5}):")


class PsqlError(RuntimeError):
    """``psql`` failed. Developer-facing: never shown to business owners (§48, §70)."""

    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr.strip()
        match = _SQLSTATE.search(self.stderr)
        self.sqlstate: str | None = match.group(1) if match else None
        super().__init__(self.stderr or f"psql exited with status {returncode}")


@runtime_checkable
class SqlExecutor(Protocol):
    """What the runner and the catalog checks need from a database."""

    def execute(self, sql: str) -> None:
        """Run a script in one transaction, stopping at the first error."""
        ...

    def query(self, sql: str) -> list[Row]:
        """Rows returned by one statement, values as text (SQL NULL as ``None``)."""
        ...


@dataclass(frozen=True)
class ConnectionParams:
    """libpq connection settings, parsed from a ``postgresql://`` URL."""

    settings: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_url(cls, url: str) -> ConnectionParams:
        """Parse ``postgresql://user:password@host:port/dbname?sslmode=...``.

        A Unix socket directory goes in the query (``?host=/run/postgresql``),
        as libpq itself allows. Unknown query parameters are refused rather
        than silently ignored.
        """
        parts = urlsplit(url)
        if parts.scheme not in ("postgresql", "postgres"):
            raise ValueError("database URL must start with postgresql://")
        settings: dict[str, str] = {}
        if parts.hostname:
            settings["host"] = unquote(parts.hostname)
        if parts.port is not None:
            settings["port"] = str(parts.port)
        if parts.username:
            settings["user"] = unquote(parts.username)
        if parts.password:
            settings["password"] = unquote(parts.password)
        dbname = unquote(parts.path.lstrip("/"))
        if dbname:
            settings["dbname"] = dbname
        for key, value in parse_qsl(parts.query, keep_blank_values=False, strict_parsing=False):
            if key not in _QUERY_ENV:
                raise ValueError(f"unsupported database URL parameter {key!r}")
            settings[key] = value
        return cls(settings)

    def env(self) -> dict[str, str]:
        """The libpq environment variables for these settings."""
        return {_QUERY_ENV[key]: value for key, value in self.settings.items()}

    def redacted(self) -> str:
        """A printable description without the password."""
        shown = {k: v for k, v in self.settings.items() if k != "password"}
        return " ".join(f"{k}={v}" for k, v in sorted(shown.items())) or "(libpq defaults)"


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class PsqlExecutor:
    """:class:`SqlExecutor` backed by the ``psql`` command-line client.

    ``psql`` runs with ``-X`` (no ``~/.psqlrc``), ``--no-password`` (never
    prompts), ``ON_ERROR_STOP`` and verbose errors so :class:`PsqlError`
    carries the SQLSTATE. Variables from the parent environment that start
    with ``PG`` are dropped so only the given settings apply, except
    ``PGPASSWORD``/``PGPASSFILE`` when the URL has no password of its own.
    """

    def __init__(
        self,
        params: ConnectionParams,
        *,
        psql: str | None = None,
        timeout: float | None = 600.0,
        runner: Runner = subprocess.run,
        base_env: Mapping[str, str] | None = None,
        session_settings: Mapping[str, str] | None = None,
    ) -> None:
        resolved = psql or shutil.which("psql")
        if not resolved:
            raise FileNotFoundError("psql is not installed")
        self._psql = resolved
        self._params = params
        self._timeout = timeout
        self._runner = runner
        self._base_env = dict(os.environ if base_env is None else base_env)
        self._session = dict(session_settings or {})
        for name, value in self._session.items():
            _check_setting(name, value)

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        psql: str | None = None,
        timeout: float | None = 600.0,
        runner: Runner = subprocess.run,
        session_settings: Mapping[str, str] | None = None,
    ) -> PsqlExecutor:
        return cls(
            ConnectionParams.from_url(url),
            psql=psql,
            timeout=timeout,
            runner=runner,
            session_settings=session_settings,
        )

    @property
    def params(self) -> ConnectionParams:
        return self._params

    def with_settings(self, **settings: str) -> PsqlExecutor:
        """A copy whose sessions start with these settings (``app__tenant_id`` -> ``app.tenant_id``).

        Settings travel in ``PGOPTIONS`` and last for the whole ``psql``
        session, which is exactly one :meth:`execute` or :meth:`query`.
        """
        merged = {**self._session, **{k.replace("__", "."): v for k, v in settings.items()}}
        return PsqlExecutor(
            self._params,
            psql=self._psql,
            timeout=self._timeout,
            runner=self._runner,
            base_env=self._base_env,
            session_settings=merged,
        )

    def execute(self, sql: str) -> None:
        self._run(["--single-transaction", "--file=-"], stdin=sql)

    def query(self, sql: str) -> list[Row]:
        """Rows of ``sql``, which must be ONE statement (psql prints every result)."""
        out = self._run(["--csv", "--pset", f"null={_NULL}", f"--command={sql}"], stdin=None)
        return _parse_csv(out)

    def _run(self, args: Sequence[str], *, stdin: str | None) -> str:
        argv = [
            self._psql,
            "-X",
            "--no-password",
            "--quiet",
            "--set=ON_ERROR_STOP=1",
            "--set=VERBOSITY=verbose",
            "--set=SHOW_CONTEXT=never",
            *args,
        ]
        env = {k: v for k, v in self._base_env.items() if not k.startswith("PG")}
        if "password" not in self._params.settings:
            # Credentials the platform injects (Secrets Manager -> PGPASSWORD) still apply.
            env.update({k: self._base_env[k] for k in _AMBIENT_CREDENTIALS if k in self._base_env})
        env.update(self._params.env())
        if self._session:
            options = " ".join(f"-c {k}={v}" for k, v in sorted(self._session.items()))
            env["PGOPTIONS"] = f"{env['PGOPTIONS']} {options}" if env.get("PGOPTIONS") else options
        env.setdefault("PGAPPNAME", "backoffice_db")
        env["PGCLIENTENCODING"] = "UTF8"
        proc = self._runner(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=self._timeout,
            check=False,
        )
        if proc.returncode != 0:
            raise PsqlError(proc.returncode, proc.stderr or "")
        return proc.stdout


_SETTING_NAME = re.compile(r"^[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?$")
_SETTING_VALUE = re.compile(r"^[A-Za-z0-9_.:@/+-]*$")


def _check_setting(name: str, value: str) -> None:
    """Setting names and values that need no quoting inside PGOPTIONS."""
    if not _SETTING_NAME.fullmatch(name):
        raise ValueError(f"session setting {name!r} is not a setting name")
    if not isinstance(value, str) or not _SETTING_VALUE.fullmatch(value):
        raise ValueError(f"session setting {name!r} has an unsupported value")


def _parse_csv(text: str) -> list[Row]:
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    return [
        {name: (None if value == _NULL else value) for name, value in zip(header, row, strict=True)}
        for row in body
    ]
