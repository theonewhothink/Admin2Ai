"""Offline workflow simulator and scriptable service doubles (development / CI).

Temporal's own test server is a downloaded binary. Where it cannot be
downloaded (air-gapped CI, restricted sandboxes) this module still runs the
**real workflow code inside the real Temporal Python SDK runtime** with no
server:

* each workflow task is executed by :class:`temporalio.worker.Replayer`
  over a history this simulator builds event by event;
* the commands the SDK emits (schedule activity, start timer, start child,
  signal external, complete, continue-as-new, ...) are captured from the
  workflow runner and turned into the matching history events;
* activities run in :class:`temporalio.testing.ActivityEnvironment` against
  the injected services;
* time only moves when the test says so, jumping straight to the next timer
  (time skipping), so "wait 6 days" takes milliseconds;
* every history is marked with the SDK's id/type determinism flag, and
  :meth:`WorkflowSimulator.verify_replay` re-runs it in the default
  *sandboxed* runner, so non-determinism or sandbox violations fail tests.

Queries are answered by calling the query method on the live workflow object
(captured with ``workflow.instance()``); query handlers must therefore be pure
reads of workflow state, which is what the Temporal docs require anyway.

Test doubles only: never wire :class:`ScriptedServices` into a real worker.
"""

from __future__ import annotations

import copy
import inspect
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from google.protobuf.duration_pb2 import Duration
from google.protobuf.timestamp_pb2 import Timestamp
from temporalio import activity as t_activity
from temporalio import workflow as t_workflow
from temporalio.api.common.v1 import (
    ActivityType,
    Payload,
    Payloads,
    WorkflowExecution,
    WorkflowType,
)
from temporalio.api.enums.v1 import (
    EventType,
    RetryState,
    StartChildWorkflowExecutionFailedCause,
)
from temporalio.api.failure.v1 import Failure
from temporalio.api.history.v1 import (
    ActivityTaskCompletedEventAttributes,
    ActivityTaskFailedEventAttributes,
    ActivityTaskScheduledEventAttributes,
    ActivityTaskStartedEventAttributes,
    ChildWorkflowExecutionStartedEventAttributes,
    ExternalWorkflowExecutionSignaledEventAttributes,
    HistoryEvent,
    SignalExternalWorkflowExecutionInitiatedEventAttributes,
    StartChildWorkflowExecutionFailedEventAttributes,
    StartChildWorkflowExecutionInitiatedEventAttributes,
    TimerCanceledEventAttributes,
    TimerFiredEventAttributes,
    TimerStartedEventAttributes,
    WorkflowExecutionCanceledEventAttributes,
    WorkflowExecutionCancelRequestedEventAttributes,
    WorkflowExecutionCompletedEventAttributes,
    WorkflowExecutionContinuedAsNewEventAttributes,
    WorkflowExecutionFailedEventAttributes,
    WorkflowExecutionSignaledEventAttributes,
    WorkflowExecutionStartedEventAttributes,
    WorkflowTaskCompletedEventAttributes,
    WorkflowTaskScheduledEventAttributes,
    WorkflowTaskStartedEventAttributes,
)
from temporalio.api.sdk.v1 import WorkflowTaskCompletedMetadata
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.client import WorkflowHistory
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import (
    ExecuteWorkflowInput,
    Interceptor,
    Replayer,
    UnsandboxedWorkflowRunner,
    WorkflowInboundInterceptor,
    WorkflowInstance,
    WorkflowInstanceDetails,
    WorkflowInterceptorClassInput,
    WorkflowRunner,
)
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from backoffice.domain.models import Quality

from .contracts import (
    ApprovalAuditEvent,
    ApprovalRequest,
    ApproverCheck,
    ApproverCheckRequest,
    AuthorizationDecision,
    AuthorizationRequest,
    ClosureRequest,
    ClosureVerdict,
    CompletenessReport,
    CompletenessRequest,
    DeliveryCheckRequest,
    DeliveryConfirmation,
    DeliveryReceipt,
    DeliveryRequest,
    GatedAction,
    ItemOutcome,
    MatchOutcome,
    MonthSummary,
    NeedsYouItem,
    OwnerItemRef,
    OwnerItemResolution,
    PackageRef,
    PackageRequest,
    QueryHandlingRequest,
    QueryResolution,
    ReconcileRequest,
    SearchOutcome,
    SearchRequest,
    SupplierMessageReceipt,
    SupplierRequest,
    ThreadCheckRequest,
    ThreadCheckResult,
    VerificationOutcome,
    VerifyRequest,
)
from .services import WorkflowServices

__all__ = [
    "ActivityCall",
    "ChildStart",
    "ExternalSignal",
    "ScriptedServices",
    "SimulationError",
    "TimerRecord",
    "WorkflowSimulator",
]

# sdk-core internal flag 1 ("IdAndTypeDeterminismChecks"): with it, replay
# rejects an activity whose type or id differs from history.
_CORE_FLAGS = [1]
_WFT_TIMEOUT = Duration(seconds=10)


class SimulationError(RuntimeError):
    """The simulated workflow failed, stalled or used something unsupported."""


@dataclass(frozen=True)
class ActivityCall:
    name: str
    arg: Any
    at: datetime
    result: Any = None
    error: str | None = None


@dataclass(frozen=True)
class TimerRecord:
    seq: int
    started_at: datetime
    duration: timedelta

    @property
    def fire_at(self) -> datetime:
        return self.started_at + self.duration


@dataclass(frozen=True)
class ChildStart:
    workflow_id: str
    workflow_type: str
    arg: Any
    at: datetime
    already_running: bool


@dataclass(frozen=True)
class ExternalSignal:
    workflow_id: str
    signal_name: str
    payloads: tuple[Payload, ...]
    at: datetime


# ---------- SDK hooks


@dataclass
class _Capture:
    commands: list[Any] = field(default_factory=list)
    flags: set[int] = field(default_factory=set)
    failures: list[str] = field(default_factory=list)
    instance: Any = None
    snapshot: Any = None  # the workflow object as of the last non-eviction activation


class _CapturingInstance(WorkflowInstance):
    """Delegates to the SDK instance; keeps commands of non-replay activations."""

    def __init__(self, inner: WorkflowInstance, capture: _Capture) -> None:
        self._inner, self._capture = inner, capture

    def activate(self, act: Any) -> Any:
        evicting = any(job.HasField("remove_from_cache") for job in act.jobs)
        completion = self._inner.activate(act)
        if not evicting and self._capture.instance is not None:
            # Eviction tears the instance down (tasks are cancelled); queries
            # must see the state before that, as a live worker would.
            self._capture.snapshot = copy.copy(self._capture.instance)
        if not act.is_replaying and completion.HasField("successful"):
            self._capture.commands.extend(completion.successful.commands)
            self._capture.flags.update(completion.successful.used_internal_flags)
        if completion.HasField("failed"):
            self._capture.failures.append(completion.failed.failure.message)
        return completion

    def get_serialization_context(self, command_info: Any) -> Any:
        return self._inner.get_serialization_context(command_info)

    def get_external_store_context(self, command_info: Any) -> Any:
        return self._inner.get_external_store_context(command_info)

    def get_info(self) -> Any:
        return self._inner.get_info()

    def get_thread_id(self) -> int | None:
        return self._inner.get_thread_id()


class _CapturingRunner(WorkflowRunner):
    def __init__(self, inner: WorkflowRunner, capture: _Capture) -> None:
        self._inner, self._capture = inner, capture

    def prepare_workflow(self, defn: Any) -> None:
        self._inner.prepare_workflow(defn)

    def create_instance(self, det: WorkflowInstanceDetails) -> WorkflowInstance:
        return _CapturingInstance(self._inner.create_instance(det), self._capture)

    def set_worker_level_failure_exception_types(
        self, types: Sequence[type[BaseException]]
    ) -> None:
        self._inner.set_worker_level_failure_exception_types(types)


class _InstanceGrabber(Interceptor):
    """Captures the live workflow object so queries can be answered."""

    def __init__(self, capture: _Capture) -> None:
        self._capture = capture

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor]:
        capture = self._capture

        class _Inbound(WorkflowInboundInterceptor):
            async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
                capture.instance = t_workflow.instance()
                return await super().execute_workflow(input)

        return _Inbound


# ---------- simulator


@dataclass
class _Run:
    run_id: str
    workflow_type: str
    events: list[HistoryEvent] = field(default_factory=list)
    timers: dict[int, tuple[int, TimerRecord]] = field(default_factory=dict)
    activities: list[tuple[int, Any]] = field(default_factory=list)
    children: list[tuple[int, int, Any]] = field(default_factory=list)
    signals_out: list[tuple[int, Any]] = field(default_factory=list)
    wft: tuple[int, int] = (0, 0)
    closed: bool = False


def _ts(value: datetime) -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(value)
    return ts


class WorkflowSimulator:
    """Drive one workflow execution through time, offline. See module docs."""

    def __init__(
        self,
        *,
        workflows: Sequence[type],
        activities: Sequence[Callable[..., Any]],
        start_time: datetime,
        data_converter: DataConverter | None = None,
        task_queue: str = "simulated",
        namespace: str = "default",
        running_workflow_ids: Iterable[str] = (),
        activity_attempts: int = 3,
        max_workflow_tasks: int = 5000,
        suggest_continue_as_new_after: int | None = None,
    ) -> None:
        if start_time.tzinfo is None:
            raise ValueError("start_time must be timezone-aware")
        if data_converter is None:
            from .worker import DATA_CONVERTER

            data_converter = DATA_CONVERTER
        self._classes = list(workflows)
        # The SDK's own definition objects (private API, fine for a test tool)
        # give names and argument types exactly as the worker sees them.
        workflow_defs = [t_workflow._Definition.must_from_class(c) for c in self._classes]
        self._defs = {defn.name: defn for defn in workflow_defs}
        activity_defs = [(fn, t_activity._Definition.must_from_callable(fn)) for fn in activities]
        self._activities = {defn.name: (fn, defn) for fn, defn in activity_defs}
        self._converter = data_converter
        self._now = start_time
        self._task_queue, self._namespace = task_queue, namespace
        self._running = set(running_workflow_ids)
        self._attempts = activity_attempts
        self._max_tasks = max_workflow_tasks
        # Like the server: suggest continue-as-new once a run's history is long.
        self._suggest_can_after = suggest_continue_as_new_after
        self._workflow_id = ""
        self._runs: list[_Run] = []
        self._capture = _Capture()
        self._result: Payload | None = None
        self._failure: Failure | None = None
        self._cancelled = False
        # Observations for assertions.
        self.activity_calls: list[ActivityCall] = []
        self.timers: list[TimerRecord] = []
        self.cancelled_timers: list[TimerRecord] = []
        self.children: list[ChildStart] = []
        self.external_signals: list[ExternalSignal] = []

    @classmethod
    def for_services(
        cls, services: WorkflowServices, *, start_time: datetime, **kwargs: Any
    ) -> WorkflowSimulator:
        """A simulator with every back-office workflow and activity registered."""
        from .activities import BackofficeActivities
        from .worker import WORKFLOWS

        return cls(
            workflows=list(WORKFLOWS),
            activities=BackofficeActivities(services).definitions(),
            start_time=start_time,
            **kwargs,
        )

    # ---------- public API

    @property
    def now(self) -> datetime:
        return self._now

    @property
    def completed(self) -> bool:
        return bool(self._runs) and self._runs[-1].closed

    @property
    def cancelled(self) -> bool:
        """True when the workflow ended as cancelled (after :meth:`request_cancel`)."""
        return self._cancelled

    @property
    def run_count(self) -> int:
        return len(self._runs)

    @property
    def workflow(self) -> Any:
        """The workflow object as it stood after the latest workflow task."""
        return self._capture.snapshot or self._capture.instance

    def histories(self) -> list[WorkflowHistory]:
        return [WorkflowHistory(self._workflow_id, list(r.events)) for r in self._runs]

    def pending_timers(self) -> list[TimerRecord]:
        if not self._runs:
            return []
        return sorted((r for _, r in self._runs[-1].timers.values()), key=lambda r: r.fire_at)

    async def start(self, workflow: Any, arg: Any, *, id: str) -> WorkflowSimulator:
        if self._runs:
            raise SimulationError("already started")
        name = self._workflow_name(workflow)
        self._workflow_id = id
        self._new_run(name, self._converter.payload_converter.to_payloads([arg]))
        await self._drive()
        return self

    async def signal(self, signal: Callable[..., Any] | str, arg: Any = None) -> None:
        run = self._open_run()
        name = t_workflow._SignalDefinition.must_name_from_fn_or_str(signal)
        payloads = self._converter.payload_converter.to_payloads([arg]) if arg is not None else []
        self._append(
            run,
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED,
            workflow_execution_signaled_event_attributes=WorkflowExecutionSignaledEventAttributes(
                signal_name=name, input=Payloads(payloads=payloads), identity="simulator"
            ),
        )
        self._open_workflow_task(run)
        await self._drive()

    async def request_cancel(self, reason: str = "cancelled by the simulator") -> None:
        """Cancel the execution the way ``WorkflowHandle.cancel()`` does."""
        run = self._open_run()
        self._append(
            run,
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED,
            workflow_execution_cancel_requested_event_attributes=(
                WorkflowExecutionCancelRequestedEventAttributes(cause=reason, identity="simulator")
            ),
        )
        self._open_workflow_task(run)
        await self._drive()

    async def advance(self, delta: timedelta) -> None:
        """Move time forward by ``delta``, firing every timer due on the way."""
        if delta < timedelta(0):
            raise ValueError("time only moves forward")
        target = self._now + delta
        while not self.completed:
            due = [t for t in self.pending_timers() if t.fire_at <= target]
            if not due:
                break
            await self._fire(due[0])
        self._now = max(self._now, target)

    async def advance_to_next_timer(self) -> TimerRecord | None:
        timers = self.pending_timers()
        if not timers or self.completed:
            return None
        await self._fire(timers[0])
        return timers[0]

    async def run_until_complete(self, limit: timedelta = timedelta(days=400)) -> Any:
        """Fire timers until the workflow finishes; fail if it needs a signal."""
        deadline = self._now + limit
        while not self.completed:
            timers = self.pending_timers()
            if not timers:
                raise SimulationError("the workflow is waiting for a signal")
            if timers[0].fire_at > deadline:
                raise SimulationError("the workflow did not finish within the limit")
            await self._fire(timers[0])
        return self.result()

    def result(self, result_type: type | None = None) -> Any:
        if self._cancelled:
            raise SimulationError("workflow was cancelled")
        if self._failure is not None:
            raise SimulationError(f"workflow failed: {self._failure.message}")
        if not self.completed or self._result is None:
            raise SimulationError("workflow has not completed")
        defn = self._defs[self._runs[-1].workflow_type]
        hint = result_type or defn.ret_type
        return self._converter.payload_converter.from_payloads(
            [self._result], [hint] if hint else None
        )[0]

    def query(self, method: Callable[..., Any] | str, *args: Any) -> Any:
        """Answer a query from the live workflow object (pure reads only)."""
        obj = self.workflow
        if obj is None:
            raise SimulationError("workflow has not started")
        fn = getattr(obj, method) if isinstance(method, str) else method.__get__(obj)
        return fn(*args)

    def calls(self, name: str) -> list[ActivityCall]:
        return [c for c in self.activity_calls if c.name == name]

    async def verify_replay(self, *, sandboxed: bool = True) -> None:
        """Replay every recorded history with the standard runner; raise on any
        non-determinism or sandbox violation."""
        runner = SandboxedWorkflowRunner() if sandboxed else UnsandboxedWorkflowRunner()
        replayer = Replayer(
            workflows=self._classes,
            data_converter=self._converter,
            workflow_runner=runner,
            namespace=self._namespace,
        )
        for history in self.histories():
            await replayer.replay_workflow(history, raise_on_replay_failure=True)

    # ---------- driving

    def _workflow_name(self, workflow: Any) -> str:
        if isinstance(workflow, str):
            name = workflow
        elif isinstance(workflow, type):
            name = t_workflow._Definition.must_from_class(workflow).name
        else:
            cls = getattr(workflow, "__qualname__", "").split(".")[0]
            matches = [n for n, d in self._defs.items() if d.cls.__name__ == cls]
            name = matches[0] if matches else ""
        if name not in self._defs:
            raise SimulationError(f"unknown workflow {workflow!r}")
        return name

    def _open_run(self) -> _Run:
        if not self._runs or self.completed:
            raise SimulationError("no running workflow")
        return self._runs[-1]

    def _append(self, run: _Run, event_type: int, **attrs: Any) -> int:
        event = HistoryEvent(
            event_id=len(run.events) + 1,
            event_type=event_type,
            event_time=_ts(self._now),
            **attrs,
        )
        run.events.append(event)
        return event.event_id

    def _new_run(self, workflow_type: str, payloads: Sequence[Payload], previous: str = "") -> None:
        run = _Run(run_id=f"sim-run-{len(self._runs) + 1}", workflow_type=workflow_type)
        first = self._runs[0].run_id if self._runs else run.run_id
        self._runs.append(run)
        self._append(
            run,
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
            workflow_execution_started_event_attributes=WorkflowExecutionStartedEventAttributes(
                workflow_type=WorkflowType(name=workflow_type),
                task_queue=TaskQueue(name=self._task_queue),
                input=Payloads(payloads=list(payloads)),
                workflow_task_timeout=_WFT_TIMEOUT,
                continued_execution_run_id=previous,
                original_execution_run_id=run.run_id,
                first_execution_run_id=first,
                workflow_id=self._workflow_id,
                attempt=1,
            ),
        )
        self._open_workflow_task(run)

    def _open_workflow_task(self, run: _Run) -> None:
        scheduled = self._append(
            run,
            EventType.EVENT_TYPE_WORKFLOW_TASK_SCHEDULED,
            workflow_task_scheduled_event_attributes=WorkflowTaskScheduledEventAttributes(
                task_queue=TaskQueue(name=self._task_queue),
                start_to_close_timeout=_WFT_TIMEOUT,
                attempt=1,
            ),
        )
        started = self._append(
            run,
            EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED,
            workflow_task_started_event_attributes=WorkflowTaskStartedEventAttributes(
                scheduled_event_id=scheduled,
                identity="simulator",
                suggest_continue_as_new=(
                    self._suggest_can_after is not None
                    and len(run.events) >= self._suggest_can_after
                ),
            ),
        )
        run.wft = (scheduled, started)

    async def _fire(self, timer: TimerRecord) -> None:
        run = self._open_run()
        started_id, _ = run.timers.pop(timer.seq)
        self._now = max(self._now, timer.fire_at)
        self._append(
            run,
            EventType.EVENT_TYPE_TIMER_FIRED,
            timer_fired_event_attributes=TimerFiredEventAttributes(
                timer_id=str(timer.seq), started_event_id=started_id
            ),
        )
        self._open_workflow_task(run)
        await self._drive()

    async def _drive(self) -> None:
        for _ in range(self._max_tasks):
            run = self._runs[-1]
            capture = await self._execute_workflow_task(run)
            follow_up = self._complete_workflow_task(run, capture)
            if follow_up is not None:  # continue-as-new
                self._new_run(*follow_up)
                continue
            if run.closed or not await self._settle(run):
                return
            self._open_workflow_task(run)
        raise SimulationError("too many workflow tasks; is the workflow looping?")

    async def _execute_workflow_task(self, run: _Run) -> _Capture:
        capture = _Capture()
        replayer = Replayer(
            workflows=self._classes,
            data_converter=self._converter,
            workflow_runner=_CapturingRunner(UnsandboxedWorkflowRunner(), capture),
            interceptors=[_InstanceGrabber(capture)],
            namespace=self._namespace,
        )
        outcome = await replayer.replay_workflow(
            WorkflowHistory(self._workflow_id, list(run.events)),
            raise_on_replay_failure=False,
        )
        if outcome.replay_failure is not None:
            raise SimulationError(
                f"workflow task failed: {outcome.replay_failure}"
            ) from outcome.replay_failure
        if capture.failures:
            raise SimulationError(f"workflow task failed: {capture.failures[0]}")
        if capture.instance is not None:
            self._capture = capture
        return capture

    def _complete_workflow_task(
        self, run: _Run, capture: _Capture
    ) -> tuple[str, Sequence[Payload], str] | None:
        scheduled, started = run.wft
        wft = self._append(
            run,
            EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED,
            workflow_task_completed_event_attributes=WorkflowTaskCompletedEventAttributes(
                scheduled_event_id=scheduled,
                started_event_id=started,
                identity="simulator",
                sdk_metadata=WorkflowTaskCompletedMetadata(
                    core_used_flags=_CORE_FLAGS, lang_used_flags=sorted(capture.flags)
                ),
            ),
        )
        follow_up = None
        for command in capture.commands:
            follow_up = self._apply_command(run, wft, command) or follow_up
        return follow_up

    def _apply_command(
        self, run: _Run, wft: int, command: Any
    ) -> tuple[str, Sequence[Payload], str] | None:
        variant = command.WhichOneof("variant")
        if variant == "start_timer":
            c = command.start_timer
            duration = c.start_to_fire_timeout.ToTimedelta()
            record = TimerRecord(seq=c.seq, started_at=self._now, duration=duration)
            event_id = self._append(
                run,
                EventType.EVENT_TYPE_TIMER_STARTED,
                timer_started_event_attributes=TimerStartedEventAttributes(
                    timer_id=str(c.seq),
                    start_to_fire_timeout=c.start_to_fire_timeout,
                    workflow_task_completed_event_id=wft,
                ),
            )
            run.timers[c.seq] = (event_id, record)
            self.timers.append(record)
        elif variant == "cancel_timer":
            seq = command.cancel_timer.seq
            started_id, record = run.timers.pop(seq)
            self._append(
                run,
                EventType.EVENT_TYPE_TIMER_CANCELED,
                timer_canceled_event_attributes=TimerCanceledEventAttributes(
                    timer_id=str(seq),
                    started_event_id=started_id,
                    workflow_task_completed_event_id=wft,
                ),
            )
            self.cancelled_timers.append(record)
        elif variant == "schedule_activity":
            c = command.schedule_activity
            event_id = self._append(
                run,
                EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED,
                activity_task_scheduled_event_attributes=ActivityTaskScheduledEventAttributes(
                    activity_id=c.activity_id,
                    activity_type=ActivityType(name=c.activity_type),
                    task_queue=TaskQueue(name=c.task_queue or self._task_queue),
                    input=Payloads(payloads=list(c.arguments)),
                    start_to_close_timeout=c.start_to_close_timeout,
                    workflow_task_completed_event_id=wft,
                ),
            )
            run.activities.append((event_id, c))
        elif variant == "start_child_workflow_execution":
            c = command.start_child_workflow_execution
            event_id = self._append(
                run,
                EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED,
                start_child_workflow_execution_initiated_event_attributes=(
                    StartChildWorkflowExecutionInitiatedEventAttributes(
                        namespace=c.namespace or self._namespace,
                        workflow_id=c.workflow_id,
                        workflow_type=WorkflowType(name=c.workflow_type),
                        task_queue=TaskQueue(name=c.task_queue or self._task_queue),
                        input=Payloads(payloads=list(c.input)),
                        parent_close_policy=int(c.parent_close_policy),
                        workflow_id_reuse_policy=int(c.workflow_id_reuse_policy),
                        workflow_task_completed_event_id=wft,
                    )
                ),
            )
            run.children.append((event_id, wft, c))
        elif variant == "signal_external_workflow_execution":
            c = command.signal_external_workflow_execution
            target = (
                c.workflow_execution.workflow_id
                if c.WhichOneof("target") == "workflow_execution"
                else c.child_workflow_id
            )
            event_id = self._append(
                run,
                EventType.EVENT_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED,
                signal_external_workflow_execution_initiated_event_attributes=(
                    SignalExternalWorkflowExecutionInitiatedEventAttributes(
                        namespace=self._namespace,
                        workflow_execution=WorkflowExecution(workflow_id=target),
                        signal_name=c.signal_name,
                        input=Payloads(payloads=list(c.args)),
                        workflow_task_completed_event_id=wft,
                    )
                ),
            )
            run.signals_out.append((event_id, target))
            self.external_signals.append(
                ExternalSignal(target, c.signal_name, tuple(c.args), self._now)
            )
        elif variant == "complete_workflow_execution":
            c = command.complete_workflow_execution
            payloads = [c.result] if c.HasField("result") else []
            self._append(
                run,
                EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
                workflow_execution_completed_event_attributes=WorkflowExecutionCompletedEventAttributes(
                    result=Payloads(payloads=payloads), workflow_task_completed_event_id=wft
                ),
            )
            self._result = c.result if payloads else None
            run.closed = True
        elif variant == "fail_workflow_execution":
            c = command.fail_workflow_execution
            self._append(
                run,
                EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED,
                workflow_execution_failed_event_attributes=WorkflowExecutionFailedEventAttributes(
                    failure=c.failure,
                    retry_state=RetryState.RETRY_STATE_RETRY_POLICY_NOT_SET,
                    workflow_task_completed_event_id=wft,
                ),
            )
            self._failure = c.failure
            run.closed = True
        elif variant == "continue_as_new_workflow_execution":
            c = command.continue_as_new_workflow_execution
            new_type = c.workflow_type or run.workflow_type
            next_run = f"sim-run-{len(self._runs) + 1}"
            self._append(
                run,
                EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW,
                workflow_execution_continued_as_new_event_attributes=(
                    WorkflowExecutionContinuedAsNewEventAttributes(
                        new_execution_run_id=next_run,
                        workflow_type=WorkflowType(name=new_type),
                        task_queue=TaskQueue(name=c.task_queue or self._task_queue),
                        input=Payloads(payloads=list(c.arguments)),
                        workflow_task_completed_event_id=wft,
                    )
                ),
            )
            run.closed = True
            return new_type, list(c.arguments), run.run_id
        elif variant == "cancel_workflow_execution":
            self._append(
                run,
                EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED,
                workflow_execution_canceled_event_attributes=WorkflowExecutionCanceledEventAttributes(
                    workflow_task_completed_event_id=wft
                ),
            )
            self._cancelled = True
            run.closed = True
        elif variant == "respond_to_query":
            pass
        else:
            raise SimulationError(f"the simulator does not support {variant!r}")
        return None

    async def _settle(self, run: _Run) -> bool:
        """Resolve everything the 'server' can resolve now. True if anything did."""
        progressed = False
        activities, run.activities = run.activities, []
        for scheduled_id, command in activities:
            await self._run_activity(run, scheduled_id, command)
            progressed = True
        children, run.children = run.children, []
        for initiated_id, wft, command in children:
            self._resolve_child(run, initiated_id, wft, command)
            progressed = True
        signals, run.signals_out = run.signals_out, []
        for initiated_id, target in signals:
            self._append(
                run,
                EventType.EVENT_TYPE_EXTERNAL_WORKFLOW_EXECUTION_SIGNALED,
                external_workflow_execution_signaled_event_attributes=(
                    ExternalWorkflowExecutionSignaledEventAttributes(
                        initiated_event_id=initiated_id,
                        namespace=self._namespace,
                        workflow_execution=WorkflowExecution(workflow_id=target),
                    )
                ),
            )
            progressed = True
        return progressed

    async def _run_activity(self, run: _Run, scheduled_id: int, command: Any) -> None:
        name = command.activity_type
        if name not in self._activities:
            raise SimulationError(f"no activity registered as {name!r}")
        fn, defn = self._activities[name]
        args = self._converter.payload_converter.from_payloads(
            list(command.arguments), defn.arg_types
        )
        started_id = self._append(
            run,
            EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED,
            activity_task_started_event_attributes=ActivityTaskStartedEventAttributes(
                scheduled_event_id=scheduled_id, attempt=1, identity="simulator"
            ),
        )
        non_retryable = set(command.retry_policy.non_retryable_error_types)
        last_error: BaseException | None = None
        for _ in range(self._attempts):
            try:
                result = ActivityEnvironment().run(fn, *args)
                if inspect.isawaitable(result):
                    result = await result
            except ApplicationError as err:
                last_error = err
                if err.non_retryable or (err.type or "") in non_retryable:
                    self._record_activity_failure(run, scheduled_id, started_id, err, name, args)
                    return
            except Exception as err:  # retryable: try again, like Temporal would
                last_error = err
            else:
                self.activity_calls.append(
                    ActivityCall(name, args[0] if len(args) == 1 else args, self._now, result)
                )
                self._append(
                    run,
                    EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED,
                    activity_task_completed_event_attributes=ActivityTaskCompletedEventAttributes(
                        result=Payloads(
                            payloads=self._converter.payload_converter.to_payloads([result])
                        ),
                        scheduled_event_id=scheduled_id,
                        started_event_id=started_id,
                    ),
                )
                return
        raise SimulationError(f"activity {name!r} kept failing: {last_error!r}") from last_error

    def _record_activity_failure(
        self,
        run: _Run,
        scheduled_id: int,
        started_id: int,
        error: BaseException,
        name: str,
        args: Sequence[Any],
    ) -> None:
        failure = Failure()
        self._converter.failure_converter.to_failure(
            error, self._converter.payload_converter, failure
        )
        self.activity_calls.append(
            ActivityCall(name, args[0] if len(args) == 1 else args, self._now, error=str(error))
        )
        self._append(
            run,
            EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED,
            activity_task_failed_event_attributes=ActivityTaskFailedEventAttributes(
                failure=failure,
                scheduled_event_id=scheduled_id,
                started_event_id=started_id,
                retry_state=RetryState.RETRY_STATE_NON_RETRYABLE_FAILURE,
            ),
        )

    def _resolve_child(self, run: _Run, initiated_id: int, wft: int, command: Any) -> None:
        defn = self._defs.get(command.workflow_type)
        arg: Any = None
        if defn is not None and command.input:
            arg = self._converter.payload_converter.from_payloads(
                list(command.input), defn.arg_types
            )[0]
        exists = command.workflow_id in self._running
        self.children.append(
            ChildStart(command.workflow_id, command.workflow_type, arg, self._now, exists)
        )
        if exists:
            self._append(
                run,
                EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_FAILED,
                start_child_workflow_execution_failed_event_attributes=(
                    StartChildWorkflowExecutionFailedEventAttributes(
                        namespace=self._namespace,
                        workflow_id=command.workflow_id,
                        workflow_type=WorkflowType(name=command.workflow_type),
                        cause=StartChildWorkflowExecutionFailedCause.START_CHILD_WORKFLOW_EXECUTION_FAILED_CAUSE_WORKFLOW_ALREADY_EXISTS,
                        initiated_event_id=initiated_id,
                        workflow_task_completed_event_id=wft,
                    )
                ),
            )
            return
        self._running.add(command.workflow_id)
        self._append(
            run,
            EventType.EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_STARTED,
            child_workflow_execution_started_event_attributes=ChildWorkflowExecutionStartedEventAttributes(
                namespace=self._namespace,
                initiated_event_id=initiated_id,
                workflow_execution=WorkflowExecution(
                    workflow_id=command.workflow_id, run_id=f"sim-child-{len(self.children)}"
                ),
                workflow_type=WorkflowType(name=command.workflow_type),
            ),
        )


# ---------- scriptable services


class ScriptedServices:
    """In-memory double implementing every service Protocol; records each call.

    Defaults describe a quiet world: search finds nothing, documents verify
    AMBER, suppliers never reply, everything is authorized. Tests change the
    public attributes to script a scenario.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        # evidence / documents / matching
        self.search_results: list[SearchOutcome] = []
        self.verifications: dict[str, VerificationOutcome] = {}
        self.matches: dict[str, MatchOutcome] = {}
        # policy
        self.authorized: dict[GatedAction, bool] = {a: True for a in GatedAction}
        # supplier mailbox
        self.supplier_contact = True
        self.thread_replies: list[ThreadCheckResult] = []
        self.sent_messages: list[SupplierRequest] = []
        # owner inbox
        self.owner_items: list[NeedsYouItem] = []
        self.resolved_items: list[OwnerItemResolution] = []
        # approvals
        self.approval_requests: list[ApprovalRequest] = []
        self.approvers: dict[str, ApproverCheck] = {}
        self.approval_events: list[ApprovalAuditEvent] = []
        # ledger
        self.outcomes: list[ItemOutcome] = []
        # month close
        self.audits: list[CompletenessReport] = []
        self.package = PackageRef(
            package_id="pkg-1", evidence_id="ev-package", complete_items=0, missing_items=0
        )
        self.delivery = DeliveryReceipt(delivered=True, delivery_evidence_id="ev-delivery")
        self.delivery_confirmations: list[DeliveryConfirmation] = []
        self.query_resolutions: dict[str, QueryResolution] = {}
        self.verdicts: list[ClosureVerdict] = []

    def services(self) -> WorkflowServices:
        s = self
        return WorkflowServices(
            evidence=s, documents=s, reconciler=s, policy=s, suppliers=s,
            owner=s, approvals=s, ledger=s, month_close=s,
        )  # fmt: skip

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def _log(self, name: str, arg: Any) -> None:
        self.calls.append((name, arg))

    @staticmethod
    def _next(queue: list[Any], default: Any) -> Any:
        return queue.pop(0) if queue else default

    # EvidenceFinder / DocumentPipeline / Reconciler
    async def search_for_evidence(self, request: SearchRequest) -> SearchOutcome:
        self._log("search_for_evidence", request)
        return self._next(self.search_results, SearchOutcome(sources_checked=("email",)))

    async def ingest_and_verify(self, request: VerifyRequest) -> VerificationOutcome:
        self._log("ingest_and_verify", request)
        for evidence_id in request.evidence_ids:
            if evidence_id in self.verifications:
                return self.verifications[evidence_id]
        return VerificationOutcome(quality=Quality.AMBER, evidence_ids=request.evidence_ids)

    async def reconcile_transaction(self, request: ReconcileRequest) -> MatchOutcome:
        self._log("reconcile_transaction", request)
        return self.matches.get(
            request.document_id, MatchOutcome(matched=False, quality=Quality.AMBER)
        )

    # PolicyGate
    async def is_action_authorized(self, request: AuthorizationRequest) -> AuthorizationDecision:
        self._log("is_action_authorized", request)
        return AuthorizationDecision(authorized=self.authorized.get(request.action, False))

    # SupplierMailbox
    async def send_supplier_request(self, request: SupplierRequest) -> SupplierMessageReceipt:
        self._log("send_supplier_request", request)
        if not self.supplier_contact:
            return SupplierMessageReceipt(sent=False, reason="no contact")
        self.sent_messages.append(request)
        return SupplierMessageReceipt(
            sent=True,
            thread_id=request.thread_id or "thread-1",
            message_evidence_id=f"ev-mail-{len(self.sent_messages)}",
        )

    async def check_thread_for_reply(self, request: ThreadCheckRequest) -> ThreadCheckResult:
        self._log("check_thread_for_reply", request)
        return self._next(self.thread_replies, ThreadCheckResult(replied=False))

    # OwnerInbox
    async def notify_owner(self, item: NeedsYouItem) -> OwnerItemRef:
        self._log("notify_owner", item)
        self.owner_items.append(item)
        return OwnerItemRef(item_id=f"card-{len(self.owner_items)}")

    async def resolve_owner_item(self, resolution: OwnerItemResolution) -> None:
        self._log("resolve_owner_item", resolution)
        self.resolved_items.append(resolution)

    # ApprovalDesk
    async def request_approval(self, request: ApprovalRequest) -> OwnerItemRef:
        self._log("request_approval", request)
        self.approval_requests.append(request)
        return OwnerItemRef(item_id=request.owner_item_id or "approval-card-1")

    async def verify_approver(self, request: ApproverCheckRequest) -> ApproverCheck:
        self._log("verify_approver", request)
        return self.approvers.get(
            request.assertion_id, ApproverCheck(verified=False, reason="unknown sign-in")
        )

    async def record_approval_event(self, event: ApprovalAuditEvent) -> None:
        self._log("record_approval_event", event)
        self.approval_events.append(event)

    # ItemLedger
    async def record_item_outcome(self, outcome: ItemOutcome) -> None:
        self._log("record_item_outcome", outcome)
        self.outcomes.append(outcome)

    # MonthCloseDesk
    async def run_completeness_audit(self, request: CompletenessRequest) -> CompletenessReport:
        """Pops ``audits`` in order; the last report repeats."""
        self._log("run_completeness_audit", request)
        if len(self.audits) > 1:
            return self.audits.pop(0)
        return (
            self.audits[0]
            if self.audits
            else CompletenessReport(transactions_checked=0, documents_collected=0)
        )

    async def prepare_accountant_package(self, request: PackageRequest) -> PackageRef:
        self._log("prepare_accountant_package", request)
        return self.package

    async def deliver_accountant_package(self, request: DeliveryRequest) -> DeliveryReceipt:
        self._log("deliver_accountant_package", request)
        return self.delivery

    async def confirm_delivery(self, request: DeliveryCheckRequest) -> DeliveryConfirmation:
        self._log("confirm_delivery", request)
        return self._next(self.delivery_confirmations, DeliveryConfirmation(confirmed=False))

    async def handle_accountant_query(self, request: QueryHandlingRequest) -> QueryResolution:
        self._log("handle_accountant_query", request)
        qid = request.query.query_id
        return self.query_resolutions.get(
            qid, QueryResolution(query_id=qid, resolved=True, evidence_ids=(f"ev-answer-{qid}",))
        )

    async def evaluate_closure(self, request: ClosureRequest) -> ClosureVerdict:
        self._log("evaluate_closure", request)
        return self._next(
            self.verdicts,
            ClosureVerdict(
                closed=True,
                open_items=0,
                summary=MonthSummary(transactions_checked=1),
                evidence_ids=("ev-closure",),
            ),
        )
