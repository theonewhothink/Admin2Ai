"""A disposable local PostgreSQL for tests and local schema checks.

:class:`TemporaryPostgres` runs ``initdb`` + ``pg_ctl`` from the local
PostgreSQL installation into a fresh temporary directory, listening on a Unix
socket only (no TCP port, so parallel runs never collide), and removes it all
on exit. PostgreSQL refuses to run as root; when started as root it runs the
server as the ``postgres`` system user through ``runuser``.

Nothing here is used in production.
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import TracebackType

from .executor import ConnectionParams, PsqlExecutor

__all__ = ["PostgresUnavailable", "TemporaryPostgres", "find_pg_bindir"]

_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class PostgresUnavailable(RuntimeError):
    """No usable local PostgreSQL server binaries (tests skip on this)."""


def find_pg_bindir() -> Path | None:
    """Directory holding initdb, pg_ctl, postgres and psql, newest version first."""
    candidates: list[Path] = []
    if env := os.environ.get("BACKOFFICE_PG_BINDIR"):
        candidates.append(Path(env))
    if pg_config := shutil.which("pg_config"):
        out = subprocess.run([pg_config, "--bindir"], capture_output=True, text=True, check=False)
        if out.returncode == 0 and out.stdout.strip():
            candidates.append(Path(out.stdout.strip()))
    debian = sorted(
        Path("/usr/lib/postgresql").glob("*/bin"),
        key=lambda p: int(p.parent.name) if p.parent.name.isdigit() else 0,
        reverse=True,
    )
    candidates.extend(debian)
    for directory in candidates:
        if all((directory / tool).is_file() for tool in ("initdb", "pg_ctl", "postgres", "psql")):
            return directory
    return None


class TemporaryPostgres:
    """Context manager: a throwaway PostgreSQL cluster reachable as superuser ``postgres``."""

    def __init__(self, bindir: Path | None = None, *, startup_timeout: int = 60) -> None:
        found = bindir or find_pg_bindir()
        if found is None:
            raise PostgresUnavailable("PostgreSQL server binaries not found")
        self.bindir = found
        self._timeout = startup_timeout
        self._run_as = self._server_user()
        self.root: Path | None = None

    @staticmethod
    def _server_user() -> str | None:
        if os.geteuid() != 0:
            return None
        if not shutil.which("runuser"):
            raise PostgresUnavailable("running as root without runuser")
        try:
            pwd.getpwnam("postgres")
        except KeyError:
            raise PostgresUnavailable("running as root and there is no postgres user") from None
        return "postgres"

    # -------------------------------------------------------------- lifecycle

    def __enter__(self) -> TemporaryPostgres:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def start(self) -> None:
        # /tmp itself: the server user must be able to reach the socket directory.
        self.root = Path(tempfile.mkdtemp(prefix="bo_pg_", dir="/tmp"))
        if self._run_as:
            entry = pwd.getpwnam(self._run_as)
            os.chown(self.root, entry.pw_uid, entry.pw_gid)
        data = self.root / "data"
        try:
            self._server_cmd(
                "initdb", "-D", str(data), "-U", "postgres", "--auth=trust",
                "-E", "UTF8", "--locale=C.UTF-8", "--no-sync",
            )
            options = f"-c listen_addresses='' -k {self.root} -c fsync=off -c full_page_writes=off"
            self._server_cmd(
                "pg_ctl", "-D", str(data), "-l", str(self.root / "server.log"),
                "-o", options, "-w", "-t", str(self._timeout), "start",
            )
        except (subprocess.CalledProcessError, OSError) as err:
            self.stop()
            raise PostgresUnavailable(f"could not start PostgreSQL: {err}") from err

    def stop(self) -> None:
        if self.root is None:
            return
        data = self.root / "data"
        if (data / "postmaster.pid").exists():
            try:
                self._server_cmd("pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop")
            except (subprocess.CalledProcessError, OSError):
                pass
        shutil.rmtree(self.root, ignore_errors=True)
        self.root = None

    def _server_cmd(self, tool: str, *args: str) -> None:
        argv = [str(self.bindir / tool), *args]
        if self._run_as:
            argv = ["runuser", "-u", self._run_as, "--", *argv]
        subprocess.run(argv, check=True, capture_output=True, text=True, timeout=self._timeout)

    # -------------------------------------------------------------- access

    def url(self, dbname: str = "postgres", user: str = "postgres") -> str:
        if self.root is None:
            raise RuntimeError("the server is not running")
        return f"postgresql://{user}@/{dbname}?host={self.root}"

    def executor(self, dbname: str = "postgres", user: str = "postgres") -> PsqlExecutor:
        return PsqlExecutor(
            ConnectionParams.from_url(self.url(dbname, user)),
            psql=str(self.bindir / "psql"),
            timeout=120,
        )

    def create_database(self, name: str, *, template: str | None = None) -> None:
        """Create database ``name`` (optionally copied from ``template``)."""
        for value in (name, template):
            if value is not None and not _NAME.fullmatch(value):
                raise ValueError("database names must be plain lower-case identifiers")
        suffix = f" TEMPLATE {template}" if template else ""
        self.executor().query(f"CREATE DATABASE {name}{suffix}")

    def create_login(self, user: str, *member_of: str) -> None:
        """A password-less (trust) login role, member of the given group roles."""
        for value in (user, *member_of):
            if not _NAME.fullmatch(value):
                raise ValueError("role names must be plain lower-case identifiers")
        groups = f" IN ROLE {', '.join(member_of)}" if member_of else ""
        self.executor().query(f"CREATE ROLE {user} LOGIN NOSUPERUSER NOBYPASSRLS{groups}")
