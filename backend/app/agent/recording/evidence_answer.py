"""根拠回答工程 answer() 1回の記録。"""

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
    "EvidenceAnswerFailed",
    "EvidenceAnswerRecorder",
    "EvidenceAnswerRecording",
    "EvidenceAnswerRecordingOutcome",
    "EvidenceAnswerSucceeded",
    "LogfireEvidenceAnswerRecorder",
    "logfire_evidence_answer_recorder",
]

_DURATION_METRIC = "vector.agent.evidence_answer.duration"
_MISSING_OUTCOME = "none"

_duration_histogram = logfire.metric_histogram(
    _DURATION_METRIC,
    unit="s",
    description="Evidence answer process duration",
)


@dataclass(frozen=True, slots=True)
class EvidenceAnswerSucceeded:
    """完成した根拠回答工程の結論。"""

    attempt_count: int

    def __post_init__(self) -> None:
        if self.attempt_count < 1:
            raise ValueError("attempt_count must be positive")


@dataclass(frozen=True, slots=True)
class EvidenceAnswerFailed:
    """分類済み生成失敗を返した根拠回答工程の結論。"""

    failure_code: str
    attempt_count: int

    def __post_init__(self) -> None:
        if not self.failure_code:
            raise ValueError("failure_code must not be empty")
        if self.attempt_count < 1:
            raise ValueError("attempt_count must be positive")


type EvidenceAnswerRecordingOutcome = EvidenceAnswerSucceeded | EvidenceAnswerFailed


class EvidenceAnswerRecording(Protocol):
    """answer() 1回に結論を関連付ける記録ハンドル。"""

    def set_outcome(self, outcome: EvidenceAnswerRecordingOutcome) -> None: ...


class EvidenceAnswerRecorder(Protocol):
    """answer() 1回のspan・duration・分類済みoutcomeを完結させる。"""

    def record(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[EvidenceAnswerRecording]: ...


@dataclass(slots=True)
class _EvidenceAnswerRecording:
    outcome: EvidenceAnswerRecordingOutcome | None = None

    def set_outcome(self, outcome: EvidenceAnswerRecordingOutcome) -> None:
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class _EvidenceAnswerExit:
    status: PhaseStatus
    outcome: EvidenceAnswerRecordingOutcome | None
    error: BaseException | None

    @classmethod
    def resolve(
        cls,
        *,
        outcome: EvidenceAnswerRecordingOutcome | None,
        error: BaseException | None,
    ) -> _EvidenceAnswerExit:
        if isinstance(
            error,
            asyncio.CancelledError | GeneratorExit | AnswerGenerationStopped,
        ):
            return cls(status=PhaseStatus.STOPPED, outcome=None, error=error)
        if error is None and outcome is not None:
            return cls(status=PhaseStatus.COMPLETED, outcome=outcome, error=None)
        return cls(status=PhaseStatus.FAILED, outcome=None, error=error)


class LogfireEvidenceAnswerRecorder:
    def record(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[EvidenceAnswerRecording]:
        return self._record(agent_name=agent_name)

    @asynccontextmanager
    async def _record(
        self,
        *,
        agent_name: str,
    ) -> AsyncIterator[EvidenceAnswerRecording]:
        recording = _EvidenceAnswerRecording()
        started_at = _started_at()
        span = _try_open_evidence_answer_span(agent_name=agent_name)
        error: BaseException | None = None
        try:
            yield recording
        except BaseException as exc:
            error = exc
            raise
        finally:
            evidence_answer_exit = _EvidenceAnswerExit.resolve(
                outcome=recording.outcome,
                error=error,
            )
            _try_close_evidence_answer_span(span, error=evidence_answer_exit.error)
            _record_duration(
                started_at=started_at,
                evidence_answer_exit=evidence_answer_exit,
            )
            _record_outcome(evidence_answer_exit)


def _try_open_evidence_answer_span(
    *,
    agent_name: str,
) -> AbstractContextManager[None] | None:
    try:
        span = agent_phase(phase="answering", agent_name=agent_name)
        span.__enter__()
        return span
    except Exception:
        return None


def _try_close_evidence_answer_span(
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
    evidence_answer_exit: _EvidenceAnswerExit,
) -> None:
    if started_at is None:
        return
    try:
        _duration_histogram.record(
            perf_counter() - started_at,
            attributes={
                "status": evidence_answer_exit.status.value,
                "outcome": _outcome_label(evidence_answer_exit.outcome),
            },
        )
    except Exception:
        return


def _record_outcome(evidence_answer_exit: _EvidenceAnswerExit) -> None:
    outcome = evidence_answer_exit.outcome
    if outcome is None:
        return
    try:
        from app.agent.answering.metrics import record_evidence_answer_outcome

        if isinstance(outcome, EvidenceAnswerSucceeded):
            record_evidence_answer_outcome(
                result="succeeded",
                attempt_count=outcome.attempt_count,
            )
            return
        record_evidence_answer_outcome(
            result="failed",
            attempt_count=outcome.attempt_count,
            failure_code=outcome.failure_code,
        )
    except Exception:
        return


def _outcome_label(outcome: EvidenceAnswerRecordingOutcome | None) -> str:
    if isinstance(outcome, EvidenceAnswerSucceeded):
        return "succeeded"
    if isinstance(outcome, EvidenceAnswerFailed):
        return "failed"
    return _MISSING_OUTCOME


logfire_evidence_answer_recorder = LogfireEvidenceAnswerRecorder()
