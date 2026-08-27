"""直接回答工程 answer() 1回の記録。"""

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

from app.agent.contract import AnswerGenerationStopped
from app.agent.phase_span import agent_phase
from app.agent.recording.types import PhaseStatus

__all__ = [
    "DirectAnswerFailed",
    "DirectAnswerOutcome",
    "DirectAnswerRecorder",
    "DirectAnswerRecording",
    "DirectAnswerSucceeded",
    "LogfireDirectAnswerRecorder",
    "logfire_direct_answer_recorder",
]

_DURATION_METRIC = "vector.agent.direct_answer.duration"
_MISSING_OUTCOME = "none"

_duration_histogram = logfire.metric_histogram(
    _DURATION_METRIC,
    unit="s",
    description="Direct answer process duration",
)


@dataclass(frozen=True, slots=True)
class DirectAnswerSucceeded:
    """完成した直接回答工程の結論。"""

    attempt_count: int

    def __post_init__(self) -> None:
        if self.attempt_count < 1:
            raise ValueError("attempt_count must be positive")


@dataclass(frozen=True, slots=True)
class DirectAnswerFailed:
    """分類済み失敗で終了した直接回答工程の結論。"""

    failure_code: str
    attempt_count: int

    def __post_init__(self) -> None:
        if not self.failure_code:
            raise ValueError("failure_code must not be empty")
        if self.attempt_count < 1:
            raise ValueError("attempt_count must be positive")


type DirectAnswerOutcome = DirectAnswerSucceeded | DirectAnswerFailed


class DirectAnswerRecording(Protocol):
    """answer() 1回に結論を関連付ける記録ハンドル。"""

    def set_outcome(self, outcome: DirectAnswerOutcome) -> None: ...


class DirectAnswerRecorder(Protocol):
    """answer() 1回のspan・duration・分類済みoutcomeを完結させる。"""

    def record(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[DirectAnswerRecording]: ...


@dataclass(slots=True)
class _DirectAnswerRecording:
    outcome: DirectAnswerOutcome | None = None

    def set_outcome(self, outcome: DirectAnswerOutcome) -> None:
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class _DirectAnswerExit:
    status: PhaseStatus
    outcome: DirectAnswerOutcome | None
    error: BaseException | None

    @classmethod
    def resolve(
        cls,
        *,
        outcome: DirectAnswerOutcome | None,
        error: BaseException | None,
    ) -> _DirectAnswerExit:
        if isinstance(
            error,
            asyncio.CancelledError | GeneratorExit | AnswerGenerationStopped,
        ):
            return cls(
                status=PhaseStatus.STOPPED,
                outcome=None,
                error=error,
            )
        if isinstance(outcome, DirectAnswerSucceeded) and error is None:
            return cls(
                status=PhaseStatus.COMPLETED,
                outcome=outcome,
                error=None,
            )
        if isinstance(outcome, DirectAnswerFailed):
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


class LogfireDirectAnswerRecorder:
    def record(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[DirectAnswerRecording]:
        return self._record(agent_name=agent_name)

    @asynccontextmanager
    async def _record(
        self,
        *,
        agent_name: str,
    ) -> AsyncIterator[DirectAnswerRecording]:
        recording = _DirectAnswerRecording()
        started_at = _started_at()
        span = _try_open_direct_answer_span(agent_name=agent_name)
        error: BaseException | None = None
        try:
            yield recording
        except BaseException as exc:
            error = exc
            raise
        finally:
            direct_answer_exit = _DirectAnswerExit.resolve(
                outcome=recording.outcome,
                error=error,
            )
            _try_close_direct_answer_span(span, error=direct_answer_exit.error)
            _record_duration(
                started_at=started_at,
                direct_answer_exit=direct_answer_exit,
            )
            _record_outcome(direct_answer_exit)


def _try_open_direct_answer_span(
    *,
    agent_name: str,
) -> AbstractContextManager[None] | None:
    try:
        span = agent_phase(phase="answering", agent_name=agent_name)
        span.__enter__()
        return span
    except Exception:
        return None


def _try_close_direct_answer_span(
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
    direct_answer_exit: _DirectAnswerExit,
) -> None:
    if started_at is None:
        return
    try:
        _duration_histogram.record(
            perf_counter() - started_at,
            attributes={
                "status": direct_answer_exit.status.value,
                "outcome": _outcome_label(direct_answer_exit.outcome),
            },
        )
    except Exception:
        return


def _record_outcome(direct_answer_exit: _DirectAnswerExit) -> None:
    outcome = direct_answer_exit.outcome
    if outcome is None:
        return
    try:
        from app.agent.answering.metrics import record_direct_answer_outcome

        if isinstance(outcome, DirectAnswerSucceeded):
            record_direct_answer_outcome(
                result="succeeded",
                attempt_count=outcome.attempt_count,
            )
            return
        record_direct_answer_outcome(
            result="failed",
            attempt_count=outcome.attempt_count,
            failure_code=outcome.failure_code,
        )
    except Exception:
        return


def _outcome_label(outcome: DirectAnswerOutcome | None) -> str:
    if isinstance(outcome, DirectAnswerSucceeded):
        return "succeeded"
    if isinstance(outcome, DirectAnswerFailed):
        return "failed"
    return _MISSING_OUTCOME


logfire_direct_answer_recorder = LogfireDirectAnswerRecorder()
