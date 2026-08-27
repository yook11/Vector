"""申し送り整理工程 organize() 1回の記録。"""

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
    "LogfireResearchHandoffRecorder",
    "ResearchHandoffFailed",
    "ResearchHandoffOutcome",
    "ResearchHandoffRecorder",
    "ResearchHandoffRecording",
    "ResearchHandoffSucceeded",
    "logfire_research_handoff_recorder",
]

_DURATION_METRIC = "vector.agent.research_handoff.duration"
_MISSING_OUTCOME = "none"

_duration_histogram = logfire.metric_histogram(
    _DURATION_METRIC,
    unit="s",
    description="Research handoff organize duration",
)


@dataclass(frozen=True, slots=True)
class ResearchHandoffSucceeded:
    """整理を書き直せた工程の結論。"""


@dataclass(frozen=True, slots=True)
class ResearchHandoffFailed:
    """分類済み失敗で終了した工程の結論。整理は前回の値が残る。"""

    failure_code: str

    def __post_init__(self) -> None:
        if not self.failure_code:
            raise ValueError("failure_code must not be empty")


type ResearchHandoffOutcome = ResearchHandoffSucceeded | ResearchHandoffFailed


class ResearchHandoffRecording(Protocol):
    """organize() 1回に結論を関連付ける記録ハンドル。"""

    def set_outcome(self, outcome: ResearchHandoffOutcome) -> None: ...


class ResearchHandoffRecorder(Protocol):
    """organize() 1回のspan・duration・分類済みoutcomeを完結させる。"""

    def record(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[ResearchHandoffRecording]: ...


@dataclass(slots=True)
class _ResearchHandoffRecording:
    outcome: ResearchHandoffOutcome | None = None

    def set_outcome(self, outcome: ResearchHandoffOutcome) -> None:
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class _ResearchHandoffExit:
    status: PhaseStatus
    outcome: ResearchHandoffOutcome | None
    error: BaseException | None

    @classmethod
    def resolve(
        cls,
        *,
        outcome: ResearchHandoffOutcome | None,
        error: BaseException | None,
    ) -> _ResearchHandoffExit:
        # 回答の停止とcancelでこの工程も畳まれる。失敗ではなく停止として残す。
        if isinstance(
            error,
            asyncio.CancelledError | GeneratorExit | AnswerGenerationStopped,
        ):
            return cls(status=PhaseStatus.STOPPED, outcome=None, error=error)
        if isinstance(outcome, ResearchHandoffSucceeded) and error is None:
            return cls(status=PhaseStatus.COMPLETED, outcome=outcome, error=None)
        if isinstance(outcome, ResearchHandoffFailed):
            return cls(status=PhaseStatus.FAILED, outcome=outcome, error=error)
        return cls(status=PhaseStatus.FAILED, outcome=None, error=error)


class LogfireResearchHandoffRecorder:
    def record(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[ResearchHandoffRecording]:
        return self._record(agent_name=agent_name)

    @asynccontextmanager
    async def _record(
        self,
        *,
        agent_name: str,
    ) -> AsyncIterator[ResearchHandoffRecording]:
        recording = _ResearchHandoffRecording()
        started_at = _started_at()
        span = _try_open_span(agent_name=agent_name)
        error: BaseException | None = None
        try:
            yield recording
        except BaseException as exc:
            error = exc
            raise
        finally:
            handoff_exit = _ResearchHandoffExit.resolve(
                outcome=recording.outcome,
                error=error,
            )
            _try_close_span(span, error=handoff_exit.error)
            _record_duration(started_at=started_at, handoff_exit=handoff_exit)
            _record_outcome(handoff_exit)


def _try_open_span(*, agent_name: str) -> AbstractContextManager[None] | None:
    try:
        span = agent_phase(phase="research_handoff", agent_name=agent_name)
        span.__enter__()
        return span
    except Exception:
        return None


def _try_close_span(
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
    handoff_exit: _ResearchHandoffExit,
) -> None:
    if started_at is None:
        return
    try:
        _duration_histogram.record(
            perf_counter() - started_at,
            attributes={
                "status": handoff_exit.status.value,
                "outcome": _outcome_label(handoff_exit.outcome),
            },
        )
    except Exception:
        return


def _record_outcome(handoff_exit: _ResearchHandoffExit) -> None:
    outcome = handoff_exit.outcome
    if outcome is None:
        return
    try:
        from app.agent.research_handoff.metrics import (
            record_research_handoff_outcome,
        )

        if isinstance(outcome, ResearchHandoffSucceeded):
            record_research_handoff_outcome(result="organized")
            return
        record_research_handoff_outcome(
            result="failed",
            failure_code=outcome.failure_code,
        )
    except Exception:
        return


def _outcome_label(outcome: ResearchHandoffOutcome | None) -> str:
    if isinstance(outcome, ResearchHandoffSucceeded):
        return "organized"
    if isinstance(outcome, ResearchHandoffFailed):
        return "failed"
    return _MISSING_OUTCOME


logfire_research_handoff_recorder = LogfireResearchHandoffRecorder()
