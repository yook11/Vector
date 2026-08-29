"""計画工程 plan() 1回の記録。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    asynccontextmanager,
)
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

import logfire

from app.agent.contract import PlanType
from app.agent.phase_span import agent_phase
from app.agent.recording.types import PhaseStatus

__all__ = [
    "LogfirePlanningRecorder",
    "PlanningFailed",
    "PlanningOutcome",
    "PlanningRecorder",
    "PlanningRecording",
    "PlanningSucceeded",
    "logfire_planning_recorder",
]

_DURATION_METRIC = "vector.agent.planner.duration"
_MISSING_OUTCOME = "none"

_duration_histogram = logfire.metric_histogram(
    _DURATION_METRIC,
    unit="s",
    description="Question planner process duration",
)


@dataclass(frozen=True, slots=True)
class PlanningSucceeded:
    """完成した計画工程の結論。"""

    plan_type: PlanType
    attempt_count: int

    def __post_init__(self) -> None:
        if self.attempt_count < 1:
            raise ValueError("attempt_count must be positive")


@dataclass(frozen=True, slots=True)
class PlanningFailed:
    """分類済み失敗で終了した計画工程の結論。"""

    failure_code: str
    attempt_count: int

    def __post_init__(self) -> None:
        if not self.failure_code:
            raise ValueError("failure_code must not be empty")
        if self.attempt_count < 1:
            raise ValueError("attempt_count must be positive")


type PlanningOutcome = PlanningSucceeded | PlanningFailed


class PlanningRecording(Protocol):
    """工程が確定したplan()の結論をRecorderへ伝える実行中の記録ハンドル。"""

    def report_outcome(self, outcome: PlanningOutcome) -> None: ...


class PlanningRecorder(Protocol):
    """plan() 1回のspan・duration・分類済みoutcomeを完結させる。"""

    def record(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[PlanningRecording]: ...


@dataclass(slots=True)
class _PlanningRecording:
    outcome: PlanningOutcome | None = None

    def report_outcome(self, outcome: PlanningOutcome) -> None:
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class _PlanningExit:
    status: PhaseStatus
    outcome: PlanningOutcome | None
    error: BaseException | None

    @classmethod
    def resolve(
        cls,
        *,
        outcome: PlanningOutcome | None,
        error: BaseException | None,
    ) -> _PlanningExit:
        if isinstance(error, asyncio.CancelledError | GeneratorExit):
            return cls(
                status=PhaseStatus.STOPPED,
                outcome=None,
                error=error,
            )
        if isinstance(outcome, PlanningSucceeded) and error is None:
            return cls(
                status=PhaseStatus.COMPLETED,
                outcome=outcome,
                error=None,
            )
        if isinstance(outcome, PlanningFailed):
            return cls(
                status=PhaseStatus.FAILED,
                outcome=outcome,
                error=error,
            )
        return cls(
            status=PhaseStatus.FAILED,
            outcome=None,
            error=error,
        )


class LogfirePlanningRecorder:
    def record(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[PlanningRecording]:
        return self._record(agent_name=agent_name)

    @asynccontextmanager
    async def _record(self, *, agent_name: str) -> AsyncIterator[PlanningRecording]:
        recording = _PlanningRecording()
        started_at = _started_at()
        span = _try_open_planning_span(agent_name=agent_name)
        error: BaseException | None = None
        try:
            yield recording
        except BaseException as exc:
            error = exc
            raise
        finally:
            planning_exit = _PlanningExit.resolve(
                outcome=recording.outcome,
                error=error,
            )
            _try_close_planning_span(span, error=planning_exit.error)
            _record_duration(started_at=started_at, planning_exit=planning_exit)
            _record_outcome(planning_exit)


def _try_open_planning_span(
    *,
    agent_name: str,
) -> AbstractContextManager[None] | None:
    try:
        span = agent_phase(phase="planning", agent_name=agent_name)
        span.__enter__()
        return span
    except Exception:
        return None


def _try_close_planning_span(
    span: AbstractContextManager[None] | None,
    *,
    error: BaseException | None,
) -> None:
    if span is None:
        return
    try:
        if error is None:
            span.__exit__(None, None, None)
            return
        span.__exit__(type(error), error, error.__traceback__)
    except BaseException:
        return


def _started_at() -> float | None:
    try:
        return perf_counter()
    except Exception:
        return None


def _record_duration(
    *,
    started_at: float | None,
    planning_exit: _PlanningExit,
) -> None:
    if started_at is None:
        return
    try:
        _duration_histogram.record(
            perf_counter() - started_at,
            attributes={
                "status": planning_exit.status.value,
                "outcome": _outcome_label(planning_exit.outcome),
            },
        )
    except Exception:
        return


def _record_outcome(planning_exit: _PlanningExit) -> None:
    outcome = planning_exit.outcome
    if outcome is None:
        return
    try:
        from app.agent.planning.metrics import record_question_planner_outcome

        if isinstance(outcome, PlanningSucceeded):
            record_question_planner_outcome(
                result="succeeded",
                attempt_count=outcome.attempt_count,
                plan_type=outcome.plan_type,
            )
            return
        record_question_planner_outcome(
            result="failed",
            attempt_count=outcome.attempt_count,
            plan_type="not_created",
            failure_code=outcome.failure_code,
        )
    except Exception:
        return


def _outcome_label(outcome: PlanningOutcome | None) -> str:
    if isinstance(outcome, PlanningSucceeded):
        return "succeeded"
    if isinstance(outcome, PlanningFailed):
        return "failed"
    return _MISSING_OUTCOME


logfire_planning_recorder = LogfirePlanningRecorder()
