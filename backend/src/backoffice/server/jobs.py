"""The durable work queue: work one process hands another, with retries and dead letters (§45, §47).

Why PostgreSQL and not the SQS queues the infrastructure used to provision: the
work a queued job does is a change to one business's event log, which lives in
the same PostgreSQL. A job is enqueued in the transaction that decided it
(``jobs`` table, migration 0016), so nothing can be decided and then lost
between two systems; the worker claims due jobs with ``SELECT ... FOR UPDATE
SKIP LOCKED``; row-level security keeps each business's jobs its own (only the
scheduler role sees across businesses, to work the queue); and the volume (a
few jobs per mailbox per hour) is far inside what a table serves. It also runs
the same in tests (:class:`~backoffice.server.store.MemoryStore`), with no
second service to fake.

Kinds of job (``payload`` holds ids only, never a secret or a document):

``sync.connection``    read one mailbox now: a push notification said something
                       arrived (or the provider said notifications were missed)
``subscription.renew`` renew (or re-create) a mailbox's push subscription now:
                       the provider asked for it

:class:`JobRunner` claims due jobs and runs their handlers. A handler that raises
:class:`RetryLater` (or anything unexpected) is retried with exponential backoff;
after ``max_attempts`` the job is parked as ``dead`` (a dead letter), logged as
``job_dead_lettered`` (the alarm in infra/terraform/monitoring.tf counts those
lines) and shown on the team's internal dashboard. Retrying is safe: every
handler is idempotent (syncs continue from the cursor recorded in the business's
log, evidence is content-addressed, bank rows are keyed on the bank's own id).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .store import Job, StoreError

__all__ = ["JOB_LEASE", "JobRunner", "MAX_ATTEMPTS", "RetryLater", "RunReport", "SUBSCRIPTION_RENEW",
           "SYNC_CONNECTION", "backoff"]

log = logging.getLogger("backoffice.server.jobs")

SYNC_CONNECTION = "sync.connection"
SUBSCRIPTION_RENEW = "subscription.renew"
MAX_ATTEMPTS = 5
BACKOFF_BASE = timedelta(minutes=1)
BACKOFF_MAX = timedelta(hours=1)
JOB_LEASE = timedelta(minutes=10)  # a worker that died mid-job: the job is claimed again after this
DONE_KEPT = timedelta(days=7)  # finished jobs are kept this long (the dashboard, investigations)


class RetryLater(Exception):
    """The job could not be done now (an outage, a busy provider): try again after a pause.

    ``code`` is internal (logs, the dead letter's last error), never owner copy."""

    def __init__(self, code: str, *, retry_after: timedelta | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


def backoff(attempts: int, base: timedelta = BACKOFF_BASE, cap: timedelta = BACKOFF_MAX) -> timedelta:
    """1, 2, 4, 8 ... minutes after each failed attempt, at most an hour."""
    return min(cap, base * (2 ** max(0, attempts - 1)))


@dataclass
class RunReport:
    done: list[int] = field(default_factory=list)
    retried: list[int] = field(default_factory=list)
    dead: list[int] = field(default_factory=list)


Handler = Callable[[Job], None]


class JobRunner:
    """Claims due jobs and runs them; retries with backoff; parks what keeps failing as a dead letter."""

    def __init__(self, store: Any, handlers: Mapping[str, Handler], *, now: Callable[[], datetime],
                 lease: timedelta = JOB_LEASE, batch: int = 50) -> None:
        self.store = store
        self.handlers = dict(handlers)
        self.now = now
        self.lease = lease
        self.batch = batch
        self._pruned: datetime | None = None

    def run_due(self, should_stop: Callable[[], bool] | None = None) -> RunReport:
        report = RunReport()
        now = self.now()
        try:
            jobs = self.store.claim_jobs(now, limit=self.batch, lease=self.lease)
        except StoreError:
            log.warning("jobs_unavailable")
            return report
        for job in jobs:
            if should_stop is not None and should_stop():
                break  # claimed but not run: the lease runs out and the next pass takes it again
            self._run(job, report)
        self._prune(now)
        return report

    def _run(self, job: Job, report: RunReport) -> None:
        handler = self.handlers.get(job.kind)
        if handler is None:
            self._bury(job, "unknown_kind", report)
            return
        if job.attempts > job.max_attempts:  # claimed again after its worker died every time: never forever
            self._bury(job, job.last_error or "worker_lost", report)
            return
        try:
            handler(job)
        except RetryLater as exc:
            self._failed(job, exc.code, exc.retry_after, report)
            return
        except Exception as exc:  # a bug or an unexpected answer: retried like an outage, then a dead letter
            log.exception("job_failed", extra={"tenant": job.tenant_id, "reason": job.kind})
            self._failed(job, type(exc).__name__, None, report)
            return
        self.store.finish_job(job.id, self.now())
        report.done.append(job.id)

    def _failed(self, job: Job, code: str, retry_after: timedelta | None, report: RunReport) -> None:
        if job.attempts >= job.max_attempts:
            self._bury(job, code, report)
            return
        delay = backoff(job.attempts)
        if retry_after is not None:
            delay = max(delay, retry_after)
        now = self.now()
        self.store.retry_job(job.id, now, run_after=now + delay, error=code)
        report.retried.append(job.id)
        log.warning("job_retry", extra={"tenant": job.tenant_id, "reason": f"{job.kind}:{code}"})

    def _bury(self, job: Job, code: str, report: RunReport) -> None:
        self.store.bury_job(job.id, self.now(), error=code)
        report.dead.append(job.id)
        log.error("job_dead_lettered", extra={"tenant": job.tenant_id, "reason": f"{job.kind}:{code}"})

    def _prune(self, now: datetime) -> None:
        """Finished jobs older than a week are removed, once a day (dead letters stay until engineers act)."""
        if self._pruned is not None and now - self._pruned < timedelta(days=1):
            return
        self._pruned = now
        try:
            self.store.prune_jobs(now - DONE_KEPT)
        except StoreError:
            log.warning("jobs_prune_unavailable")
