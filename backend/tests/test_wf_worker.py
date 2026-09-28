"""Worker wiring, configuration from the environment and the CLI entry point."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from temporalio.client import TLSConfig
from temporalio.converter import DataConverter

import backoffice.workflows as wf
from backoffice.workflows import worker as w
from backoffice.workflows.testing import ScriptedServices

BACKEND = Path(__file__).resolve().parents[1]


def make_services():
    """Factory referenced by tests below as '<this module>:make_services'."""
    return ScriptedServices().services()


async def make_services_async():
    return ScriptedServices().services()


def not_services():
    return object()


NOT_CALLABLE = 42


# ---------- settings


def test_defaults_when_the_environment_is_empty():
    s = w.WorkerSettings.from_env({})
    assert (s.address, s.namespace, s.task_queue) == ("localhost:7233", "default", "backoffice")
    assert s.tls is False and s.tls_config() is False
    assert s.services_factory is None


def test_reads_temporal_standard_variables():
    s = w.WorkerSettings.from_env(
        {
            "TEMPORAL_ADDRESS": " temporal.eu:7233 ",
            "TEMPORAL_NAMESPACE": "backoffice-prod",
            "TEMPORAL_TASK_QUEUE": "backoffice-eu",
            "TEMPORAL_API_KEY": "secret",
            w.SERVICES_ENV: "pkg.wiring:build",
        }
    )
    assert s.address == "temporal.eu:7233"
    assert (s.namespace, s.task_queue, s.api_key) == ("backoffice-prod", "backoffice-eu", "secret")
    assert s.tls is True  # an API key implies TLS
    assert s.tls_config() is True
    assert s.services_factory == "pkg.wiring:build"


def test_explicit_tls_flag_wins_and_bad_flags_are_rejected():
    assert (
        w.WorkerSettings.from_env({"TEMPORAL_API_KEY": "k", "TEMPORAL_TLS": "false"}).tls is False
    )
    assert w.WorkerSettings.from_env({"TEMPORAL_TLS": "YES"}).tls is True
    with pytest.raises(w.ConfigError, match="TEMPORAL_TLS"):
        w.WorkerSettings.from_env({"TEMPORAL_TLS": "maybe"})


def test_mtls_files_are_read_into_a_tls_config(tmp_path):
    cert, key, ca = (tmp_path / n for n in ("c.pem", "k.pem", "ca.pem"))
    cert.write_bytes(b"CERT")
    key.write_bytes(b"KEY")
    ca.write_bytes(b"CA")
    s = w.WorkerSettings.from_env(
        {
            "TEMPORAL_TLS_CLIENT_CERT_PATH": str(cert),
            "TEMPORAL_TLS_CLIENT_KEY_PATH": str(key),
            "TEMPORAL_TLS_SERVER_CA_CERT_PATH": str(ca),
            "TEMPORAL_TLS_SERVER_NAME": "temporal.internal",
        }
    )
    config = s.tls_config()
    assert isinstance(config, TLSConfig)
    assert (config.client_cert, config.client_private_key, config.server_root_ca_cert) == (
        b"CERT",
        b"KEY",
        b"CA",
    )
    assert config.domain == "temporal.internal"


def test_unreadable_tls_file_is_a_configuration_error(tmp_path):
    s = w.WorkerSettings.from_env(
        {"TEMPORAL_TLS_SERVER_CA_CERT_PATH": str(tmp_path / "missing-ca.pem")}
    )
    with pytest.raises(w.ConfigError, match="cannot read TLS file"):
        s.tls_config()


def test_certificate_without_key_is_a_configuration_error():
    with pytest.raises(w.ConfigError, match="together"):
        w.WorkerSettings.from_env({"TEMPORAL_TLS_CLIENT_CERT_PATH": "/c.pem"})
    with pytest.raises(w.ConfigError, match="task_queue"):
        w.WorkerSettings(task_queue=" ")


# ---------- services factory


def test_loads_a_services_factory_by_dotted_path():
    assert w.load_services(f"{__name__}:make_services") is make_services


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("no_colon_here", "package.module:factory"),
        ("module.that.does.not.exist:f", "cannot import"),
        (f"{__name__}:NOT_CALLABLE", "not a callable"),
        (f"{__name__}:missing", "not a callable"),
    ],
)
def test_bad_factory_paths_are_explained(path, message):
    with pytest.raises(w.ConfigError, match=message):
        w.load_services(path)


def test_factories_may_be_async_and_must_return_services():
    assert isinstance(asyncio.run(w._make_services(make_services_async)), wf.WorkflowServices)
    with pytest.raises(w.ConfigError, match="WorkflowServices"):
        asyncio.run(w._make_services(not_services))


# ---------- worker


def test_worker_options_register_every_workflow_and_activity():
    options = w.worker_options(make_services())
    assert options["workflows"] == [
        wf.MissingInvoiceWorkflow,
        wf.HardApprovalWorkflow,
        wf.MonthCloseWorkflow,
    ]
    assert [fn.__name__ for fn in options["activities"]] == list(wf.ACTIVITY_NAMES)


def test_build_worker_refuses_a_client_without_the_decimal_safe_converter():
    client = SimpleNamespace(data_converter=DataConverter.default)
    with pytest.raises(w.ConfigError, match="DATA_CONVERTER"):
        w.build_worker(client, "backoffice", make_services())  # type: ignore[arg-type]


def test_build_worker_passes_everything_to_the_temporal_worker(monkeypatch):
    seen = {}

    class FakeWorker:
        def __init__(self, client, **kwargs):
            seen.update(client=client, **kwargs)

    monkeypatch.setattr(w, "Worker", FakeWorker)
    client = SimpleNamespace(data_converter=w.DATA_CONVERTER)
    w.build_worker(client, "backoffice-eu", make_services(), max_concurrent_activities=7)  # type: ignore[arg-type]
    assert seen["client"] is client
    assert seen["task_queue"] == "backoffice-eu"
    assert seen["max_concurrent_activities"] == 7
    assert len(seen["workflows"]) == 3 and len(seen["activities"]) == len(wf.ACTIVITY_NAMES)
    with pytest.raises(w.ConfigError):
        w.build_worker(client, "", make_services())  # type: ignore[arg-type]


def test_run_worker_connects_runs_and_stops_cleanly(monkeypatch):
    events: list[str] = []

    class FakeWorker:
        def __init__(self, client, **kwargs):
            events.append(f"built:{kwargs['task_queue']}")

        async def __aenter__(self):
            events.append("running")
            return self

        async def __aexit__(self, *exc):
            events.append("stopped")

    async def fake_connect(settings):
        events.append(f"connect:{settings.address}")
        return SimpleNamespace(data_converter=w.DATA_CONVERTER)

    monkeypatch.setattr(w, "Worker", FakeWorker)
    monkeypatch.setattr(w, "connect_client", fake_connect)

    async def scenario():
        stop = asyncio.Event()
        settings = w.WorkerSettings(
            address="temporal:7233", services_factory=f"{__name__}:make_services"
        )
        task = asyncio.create_task(w.run_worker(settings, stop=stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    assert events == ["connect:temporal:7233", "built:backoffice", "running", "stopped"]


def test_run_worker_needs_services():
    with pytest.raises(w.ConfigError, match=w.SERVICES_ENV):
        asyncio.run(w.run_worker(w.WorkerSettings()))


def test_main_reports_configuration_problems_without_a_traceback(capsys):
    assert w.main([], env={}) == 2
    assert w.SERVICES_ENV in capsys.readouterr().err
    assert w.main([], env={"TEMPORAL_TLS": "sometimes"}) == 2
    assert "TEMPORAL_TLS" in capsys.readouterr().err


def test_cli_entry_point_runs_as_a_module():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TEMPORAL_", "BACKOFFICE_"))}
    env["PYTHONPATH"] = str(BACKEND / "src")
    done = subprocess.run(
        [sys.executable, "-m", "backoffice.workflows.worker"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 2
    assert "BACKOFFICE_WORKFLOW_SERVICES" in done.stderr
    assert "Traceback" not in done.stderr
    assert "RuntimeWarning" not in done.stderr


# ---------- package surface


def test_public_api_resolves_lazily_and_completely():
    for name in wf.__all__:
        assert getattr(wf, name) is not None, name
    assert "build_worker" in dir(wf)
    with pytest.raises(AttributeError):
        wf.not_a_thing  # noqa: B018


def test_workflow_modules_import_without_the_worker_machinery():
    code = (
        "import sys; import backoffice.workflows.missing_invoice; "
        "print('backoffice.workflows.worker' in sys.modules)"
    )
    env = {**os.environ, "PYTHONPATH": str(BACKEND / "src")}
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60
    )
    assert out.stdout.strip() == "False", out.stderr


def test_all_matches_the_lazy_export_table():
    assert sorted(wf.__all__) == sorted(wf._EXPORTS)
    assert len(set(wf.__all__)) == len(wf.__all__)
