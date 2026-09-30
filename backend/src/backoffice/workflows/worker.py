"""Temporal worker for the back-office workflows (§44-45).

Library use::

    client = await connect_client(WorkerSettings.from_env(os.environ))
    worker = build_worker(client, "backoffice", services)
    await worker.run()

Command line::

    TEMPORAL_ADDRESS=temporal:7233 TEMPORAL_NAMESPACE=backoffice \\
    TEMPORAL_TASK_QUEUE=backoffice \\
    BACKOFFICE_WORKFLOW_SERVICES=myapp.wiring:build_services \\
    python -m backoffice.workflows.worker

``BACKOFFICE_WORKFLOW_SERVICES`` names a zero-argument callable (sync or
async) returning :class:`~.services.WorkflowServices`; the worker refuses to
start without it rather than run with stand-in services. Connection settings
use Temporal's standard variable names: ``TEMPORAL_ADDRESS``,
``TEMPORAL_NAMESPACE``, ``TEMPORAL_API_KEY``, ``TEMPORAL_TLS``,
``TEMPORAL_TLS_CLIENT_CERT_PATH``, ``TEMPORAL_TLS_CLIENT_KEY_PATH``,
``TEMPORAL_TLS_SERVER_CA_CERT_PATH``, ``TEMPORAL_TLS_SERVER_NAME``.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import logging
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from temporalio.client import Client, TLSConfig
from temporalio.contrib.pydantic import PydanticPayloadConverter, pydantic_data_converter
from temporalio.converter import DataConverter
from temporalio.worker import Worker

from .activities import BackofficeActivities
from .approval import HardApprovalWorkflow
from .missing_invoice import MissingInvoiceWorkflow
from .month_close import MonthCloseWorkflow
from .services import WorkflowServices

__all__ = [
    "DATA_CONVERTER",
    "DEFAULT_TASK_QUEUE",
    "SERVICES_ENV",
    "WORKFLOWS",
    "ConfigError",
    "WorkerSettings",
    "build_worker",
    "connect_client",
    "load_services",
    "main",
    "run_worker",
    "worker_options",
]

log = logging.getLogger(__name__)

# Pydantic JSON keeps Decimal money exact and datetimes timezone-aware.
DATA_CONVERTER: DataConverter = pydantic_data_converter
DEFAULT_TASK_QUEUE = "backoffice"
SERVICES_ENV = "BACKOFFICE_WORKFLOW_SERVICES"
WORKFLOWS: tuple[type, ...] = (
    MissingInvoiceWorkflow,
    HardApprovalWorkflow,
    MonthCloseWorkflow,
)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(ValueError):
    """The worker is misconfigured (developer-facing, never shown to owners)."""


def _flag(env: Mapping[str, str], name: str) -> bool | None:
    raw = env.get(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _opt(env: Mapping[str, str], name: str) -> str | None:
    value = (env.get(name) or "").strip()
    return value or None


@dataclass(frozen=True)
class WorkerSettings:
    """Explicit connection and wiring configuration."""

    address: str = "localhost:7233"
    namespace: str = "default"
    task_queue: str = DEFAULT_TASK_QUEUE
    api_key: str | None = None
    tls: bool = False
    tls_client_cert_path: str | None = None
    tls_client_key_path: str | None = None
    tls_server_ca_cert_path: str | None = None
    tls_server_name: str | None = None
    services_factory: str | None = None

    def __post_init__(self) -> None:
        for name in ("address", "namespace", "task_queue"):
            if not getattr(self, name).strip():
                raise ConfigError(f"{name} cannot be empty")
        if bool(self.tls_client_cert_path) != bool(self.tls_client_key_path):
            raise ConfigError("client certificate and key must be given together")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> WorkerSettings:
        api_key = _opt(env, "TEMPORAL_API_KEY")
        cert = _opt(env, "TEMPORAL_TLS_CLIENT_CERT_PATH")
        key = _opt(env, "TEMPORAL_TLS_CLIENT_KEY_PATH")
        ca = _opt(env, "TEMPORAL_TLS_SERVER_CA_CERT_PATH")
        tls = _flag(env, "TEMPORAL_TLS")
        if tls is None:  # Temporal Cloud API keys and mTLS both imply TLS
            tls = bool(api_key or cert or ca)
        return cls(
            address=_opt(env, "TEMPORAL_ADDRESS") or cls.address,
            namespace=_opt(env, "TEMPORAL_NAMESPACE") or cls.namespace,
            task_queue=_opt(env, "TEMPORAL_TASK_QUEUE") or cls.task_queue,
            api_key=api_key,
            tls=tls,
            tls_client_cert_path=cert,
            tls_client_key_path=key,
            tls_server_ca_cert_path=ca,
            tls_server_name=_opt(env, "TEMPORAL_TLS_SERVER_NAME"),
            services_factory=_opt(env, SERVICES_ENV),
        )

    def tls_config(self) -> bool | TLSConfig:
        """``False``, ``True`` (system roots) or a :class:`TLSConfig` from files."""
        if not self.tls:
            return False
        files = (
            self.tls_client_cert_path,
            self.tls_client_key_path,
            self.tls_server_ca_cert_path,
        )
        if not any(files) and not self.tls_server_name:
            return True

        def read(path: str | None) -> bytes | None:
            if not path:
                return None
            try:
                return Path(path).read_bytes()
            except OSError as err:
                raise ConfigError(f"cannot read TLS file {path!r}: {err.strerror}") from err

        return TLSConfig(
            client_cert=read(self.tls_client_cert_path),
            client_private_key=read(self.tls_client_key_path),
            server_root_ca_cert=read(self.tls_server_ca_cert_path),
            domain=self.tls_server_name,
        )


async def connect_client(settings: WorkerSettings) -> Client:
    """Connect with the workflow data converter (use it for starters too)."""
    return await Client.connect(
        settings.address,
        namespace=settings.namespace,
        api_key=settings.api_key,
        tls=settings.tls_config(),
        data_converter=DATA_CONVERTER,
    )


def worker_options(services: WorkflowServices) -> dict[str, Any]:
    """Workflows and bound activities to register (pure; no connection)."""
    return {
        "workflows": list(WORKFLOWS),
        "activities": BackofficeActivities(services).definitions(),
    }


def _check_converter(client: Client) -> None:
    converter = client.data_converter.payload_converter_class
    if not (isinstance(converter, type) and issubclass(converter, PydanticPayloadConverter)):
        raise ConfigError(
            "the Temporal client must use backoffice.workflows.DATA_CONVERTER "
            "(pydantic) so money stays Decimal; connect with connect_client()"
        )


def build_worker(
    client: Client,
    task_queue: str,
    services: WorkflowServices,
    **worker_kwargs: Any,
) -> Worker:
    """A worker running every back-office workflow and activity on ``task_queue``.

    ``worker_kwargs`` pass through to :class:`temporalio.worker.Worker`
    (concurrency limits, identity, interceptors, ...).
    """
    if not task_queue.strip():
        raise ConfigError("task_queue cannot be empty")
    _check_converter(client)
    return Worker(client, task_queue=task_queue, **worker_options(services), **worker_kwargs)


def load_services(factory_path: str) -> Callable[[], Any]:
    """Resolve ``'package.module:callable'`` to the services factory."""
    module_name, sep, attr = factory_path.partition(":")
    if not sep or not module_name.strip() or not attr.strip():
        raise ConfigError(f"{SERVICES_ENV} must look like 'package.module:factory'")
    try:
        module = importlib.import_module(module_name.strip())
    except ImportError as err:
        raise ConfigError(f"cannot import {module_name!r} for {SERVICES_ENV}") from err
    factory = getattr(module, attr.strip(), None)
    if not callable(factory):
        raise ConfigError(f"{factory_path!r} is not a callable")
    return factory


async def _make_services(factory: Callable[[], Any]) -> WorkflowServices:
    services = factory()
    if inspect.isawaitable(services):
        services = await services
    if not isinstance(services, WorkflowServices):
        raise ConfigError("the services factory must return a WorkflowServices")
    return services


async def run_worker(
    settings: WorkerSettings,
    services: WorkflowServices | None = None,
    *,
    stop: asyncio.Event | None = None,
) -> None:
    """Connect, build and run the worker until ``stop`` is set (or SIGINT/SIGTERM)."""
    if services is None:
        if not settings.services_factory:
            raise ConfigError(f"set {SERVICES_ENV}=package.module:factory")
        services = await _make_services(load_services(settings.services_factory))
    client = await connect_client(settings)
    worker = build_worker(client, settings.task_queue, services)
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):  # Windows / thread
            loop.add_signal_handler(sig, stop.set)
    log.info(
        "Back-office worker polling %s on %s (%s)",
        settings.task_queue,
        settings.address,
        settings.namespace,
    )
    async with worker:
        await stop.wait()
    log.info("Back-office worker stopped")


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    del argv  # configuration comes from the environment only
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = WorkerSettings.from_env(os.environ if env is None else env)
        if not settings.services_factory:
            raise ConfigError(f"set {SERVICES_ENV}=package.module:factory")
        asyncio.run(run_worker(settings))
    except ConfigError as err:
        print(f"backoffice worker: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
