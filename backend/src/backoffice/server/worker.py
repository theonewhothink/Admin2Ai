"""The sync worker: ``python -m backoffice.server.worker [--once]`` (production only).

Reads connected mailboxes and banks into every tenant, runs each tenant's
day and completes account erasures (see :mod:`backoffice.server.sync`). It uses the same settings as the API
(``BACKOFFICE_MODE=production``, ``DATABASE_URL``, ``S3_BUCKET``, the vault key,
OAuth apps, GoCardless keys, SMTP, Expo, Anthropic, document reading), plus:

=============================  ==========================================================
``BACKOFFICE_SYNC_INTERVAL``   seconds between syncs of one mailbox (default 900); banks
                               sync at most every 6 hours (GoCardless allows 4 a day)
``BACKOFFICE_HISTORY_DAYS``    how far back a first sync reads (default 90, up to 365, §6)
``S3_ERASURE_ROLE_ARN``        the evidence-deletion role it assumes to delete an erased
                               business's files (AWS; its task role must be trusted)
``S3_REPLICA_BUCKET``          the evidence bucket's disaster-recovery copy, erased too
                               (``S3_REPLICA_REGION``)
``BACKOFFICE_GMAIL_PUSH_TOPIC`` the Pub/Sub topic Gmail watches publish to (unset: Gmail
                               is polled only)
``BACKOFFICE_GRAPH_PUSH``      Microsoft Graph subscriptions (default: on when
                               ``BACKOFFICE_API_URL`` is https)
=============================  ==========================================================

Every pass first works the durable job queue (server/jobs.py): syncs a push
notification asked for, subscriptions a provider asked to renew.

Its database login must be a member of ``backoffice_app`` and
``backoffice_scheduler`` (to list tenant ids; everything else stays under
row-level security). ``--once`` runs a single pass and exits (for a scheduler).
Stops cleanly on SIGTERM / SIGINT after the tenant it is working on.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from .config import PRODUCTION, ConfigError, ServerConfig

__all__ = ["build_worker", "main", "push_settings"]

log = logging.getLogger("backoffice.server.worker")

PASS_EVERY_MAX = 300  # seconds: days roll over and due connections are picked up at least this often


def push_settings(config: ServerConfig) -> Any:
    """Where the providers push (server/webhooks.py): Gmail's Pub/Sub topic, Graph's public endpoints."""
    from .sync import PushSettings

    graph = f"{config.api_url}/api/webhooks/microsoft" if config.graph_push_enabled else None
    return PushSettings(gmail_topic=config.gmail_push_topic or None, graph_url=graph,
                        graph_lifecycle_url=f"{graph}/lifecycle" if graph else None)


def build_worker(config: ServerConfig, services: dict[str, Any] | None = None) -> Any:
    """The worker on the configured services (tests pass their own, like ``build_production_app``)."""
    from backoffice.connectors.authorize import app_from_env

    from .erasure import purger_from_config
    from .http import build_manager, production_services
    from .portals import PortalWorker
    from .sync import SyncWorker

    services = production_services(config, services or {})
    manager = build_manager(config, services)
    apps = services.get("oauth_apps")
    if apps is None:
        apps = {p: a for p in ("google", "microsoft") if (a := app_from_env(p)) is not None}
    portals = services.get("portal_worker")
    if portals is None and services.get("vault") is not None:
        portals = PortalWorker(manager, vault=services["vault"], factory=services.get("portal_factory"),
                               history_days=config.history_days)
    return SyncWorker(manager, vault=services.get("vault"), aggregator_factory=services.get("aggregator"),
                      oauth_apps=apps, http_client=services.get("http_client"),
                      imap_factory=services.get("imap_factory"),
                      interval=timedelta(seconds=config.sync_interval_s), history_days=config.history_days,
                      purger=services.get("purger") or purger_from_config(config, services["objects"]),
                      push=services.get("push") or push_settings(config), portals=portals)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backoffice.server.worker", description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    args = parser.parse_args(argv)
    try:
        config = ServerConfig.from_env()
        if config.mode != PRODUCTION:  # the demo business has nothing connected: stop cleanly
            print("backoffice sync worker: nothing to sync with BACKOFFICE_MODE=demo (production only)",
                  file=sys.stderr)
            return 0
        config.require_production()
    except ConfigError as exc:
        print(f"backoffice sync worker: {exc}", file=sys.stderr)
        return 2
    from .http import configure_logging

    configure_logging(config.log_level)
    worker = build_worker(config)
    if worker.vault is None:  # no stored sign-ins: only each business's day runs
        log.warning("sync_without_vault")
    stop = threading.Event()

    def _stop(signum: int, frame: Any) -> None:
        log.info("sync_worker_stopping")
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    every = min(config.sync_interval_s, PASS_EVERY_MAX)
    log.info("sync_worker_started")
    while not stop.is_set():
        started = time.monotonic()
        try:
            worker.run_once(should_stop=stop.is_set)
        except Exception:  # one bad pass never stops the worker
            log.exception("sync_pass_failed")
        if args.once:
            break
        stop.wait(max(5.0, every - (time.monotonic() - started)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
