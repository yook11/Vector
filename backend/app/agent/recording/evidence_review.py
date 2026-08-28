"""精査工程 review() 1回の記録。"""

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

from app.agent.phase_span import agent_phase
from app.agent.recording.types import PhaseStatus

__all__ = [
    "EvidenceReviewFailed",
    "EvidenceReviewOutcome",
    "EvidenceReviewRecorder",
    "EvidenceReviewRecording",
    "EvidenceReviewSucceeded",
    "LogfireEvidenceReviewRecorder",
    "logfire_evidence_review_recorder",
]

_DURATION_METRIC = "vector.agent.evidence_review.duration"
_MISSING_OUTCOME = "none"

_duration_histogram = logfire.metric_histogram(
    _DURATION_METRIC,
    unit="s",
    description="Evidence review process duration",
)


@dataclass(frozen=True, slots=True)
class EvidenceReviewSucceeded:
    """完成した精査工程の結論。"""

    attempt_count: int

    def __post_init__(self) -> None:
        if self.attempt_count < 1:
            raise ValueError("attempt_count must be positive")


@dataclass(frozen=True, slots=True)
class EvidenceReviewFailed:
    """分類済み失敗を返した精査工程の結論。"""

    failure_code: str
    attempt_count: int

    def __post_init__(self) -> None:
        if not self.failure_code:
            raise ValueError("failure_code must not be empty")
        if self.attempt_count < 1:
            raise ValueError("attempt_count must be positive")


type EvidenceReviewOutcome = EvidenceReviewSucceeded | EvidenceReviewFailed


class EvidenceReviewRecording(Protocol):
    """review() 1回に結論を関連付ける記録ハンドル。"""

    def set_outcome(self, outcome: EvidenceReviewOutcome) -> None: ...


class EvidenceReviewRecorder(Protocol):
    """review() 1回のspan・duration・分類済みoutcomeを完結させる。"""

    def record(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[EvidenceReviewRecording]: ...


@dataclass(slots=True)
class _EvidenceReviewRecording:
    outcome: EvidenceReviewOutcome | None = None

    def set_outcome(self, outcome: EvidenceReviewOutcome) -> None:
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class _EvidenceReviewExit:
    status: PhaseStatus
    outcome: EvidenceReviewOutcome | None
    error: BaseException | None

    @classmethod
    def resolve(
        cls,
        *,
        outcome: EvidenceReviewOutcome | None,
        error: BaseException | None,
    ) -> _EvidenceReviewExit:
        if isinstance(error, asyncio.CancelledError | GeneratorExit):
            return cls(status=PhaseStatus.STOPPED, outcome=None, error=error)
        if error is None and outcome is not None:
            return cls(status=PhaseStatus.COMPLETED, outcome=outcome, error=None)
        return cls(status=PhaseStatus.FAILED, outcome=None, error=error)


class LogfireEvidenceReviewRecorder:
    def record(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[EvidenceReviewRecording]:
        return self._record(agent_name=agent_name)

    @asynccontextmanager
    async def _record(
        self,
        *,
        agent_name: str,
    ) -> AsyncIterator[EvidenceReviewRecording]:
        recording = _EvidenceReviewRecording()
        started_at = _started_at()
        span = _try_open_evidence_review_span(agent_name=agent_name)
        error: BaseException | None = None
        try:
            yield recording
        except BaseException as exc:
            error = exc
            raise
        finally:
            evidence_review_exit = _EvidenceReviewExit.resolve(
                outcome=recording.outcome,
                error=error,
            )
            _try_close_evidence_review_span(span, error=evidence_review_exit.error)
            _record_duration(
                started_at=started_at,
                evidence_review_exit=evidence_review_exit,
            )
            _record_outcome(evidence_review_exit)


def _try_open_evidence_review_span(
    *,
    agent_name: str,
) -> AbstractContextManager[None] | None:
    try:
        span = agent_phase(phase="evidence_review", agent_name=agent_name)
        span.__enter__()
        return span
    except Exception:
        return None


def _try_close_evidence_review_span(
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
    evidence_review_exit: _EvidenceReviewExit,
) -> None:
    if started_at is None:
        return
    try:
        _duration_histogram.record(
            perf_counter() - started_at,
            attributes={
                "status": evidence_review_exit.status.value,
                "outcome": _outcome_label(evidence_review_exit.outcome),
            },
        )
    except Exception:
        return


def _record_outcome(evidence_review_exit: _EvidenceReviewExit) -> None:
    outcome = evidence_review_exit.outcome
    if outcome is None:
        return
    try:
        from app.agent.evidence_review.metrics import record_evidence_review_outcome

        if isinstance(outcome, EvidenceReviewSucceeded):
            record_evidence_review_outcome(
                result="succeeded",
                attempt_count=outcome.attempt_count,
            )
            return
        record_evidence_review_outcome(
            result="failed",
            attempt_count=outcome.attempt_count,
            failure_code=outcome.failure_code,
        )
    except Exception:
        return


def _outcome_label(outcome: EvidenceReviewOutcome | None) -> str:
    if isinstance(outcome, EvidenceReviewSucceeded):
        return "succeeded"
    if isinstance(outcome, EvidenceReviewFailed):
        return "failed"
    return _MISSING_OUTCOME


logfire_evidence_review_recorder = LogfireEvidenceReviewRecorder()
