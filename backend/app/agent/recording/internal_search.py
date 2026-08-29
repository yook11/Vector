"""内部検索 search() 1回のspan・duration・outcome記録。"""

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
from typing import TYPE_CHECKING, Literal, Protocol

import logfire

from app.agent.recording.types import PhaseStatus

if TYPE_CHECKING:
    from app.agent.evidence_collection.internal_search.contract import (
        InternalSearchFailureCode,
    )

__all__ = [
    "InternalSearchFailed",
    "InternalSearchRecorder",
    "InternalSearchRecording",
    "InternalSearchRecordingOutcome",
    "InternalSearchSucceeded",
    "LogfireInternalSearchRecorder",
    "logfire_internal_search_recorder",
]

_DURATION_METRIC = "vector.agent.internal_search.duration"
_MISSING_OUTCOME = "none"
_SPAN_NAME = "internal_search"

_duration_histogram = logfire.metric_histogram(
    _DURATION_METRIC,
    unit="s",
    description="Internal search process duration",
)


@dataclass(frozen=True, slots=True)
class InternalSearchSucceeded:
    """0件を含め、正常に完了した内部検索の結論。"""

    hit_count: int

    def __post_init__(self) -> None:
        if self.hit_count < 0:
            raise ValueError("hit_count must be non-negative")


@dataclass(frozen=True, slots=True)
class InternalSearchFailed:
    """分類済み運用失敗で終了した内部検索の結論。"""

    failure_code: InternalSearchFailureCode


type InternalSearchRecordingOutcome = InternalSearchSucceeded | InternalSearchFailed


class InternalSearchRecording(Protocol):
    """工程が確定したsearch()の結論をRecorderへ伝える実行中の記録ハンドル。"""

    def report_outcome(self, outcome: InternalSearchRecordingOutcome) -> None: ...


class InternalSearchRecorder(Protocol):
    """search() 1回のspan・duration・分類済みoutcomeを完結させる。"""

    def record(
        self,
        *,
        query_count: int,
    ) -> AbstractAsyncContextManager[InternalSearchRecording]: ...


@dataclass(slots=True)
class _InternalSearchRecording:
    outcome: InternalSearchRecordingOutcome | None = None

    def report_outcome(self, outcome: InternalSearchRecordingOutcome) -> None:
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class _InternalSearchExit:
    status: PhaseStatus
    outcome: InternalSearchRecordingOutcome | None
    error: BaseException | None

    @classmethod
    def resolve(
        cls,
        *,
        outcome: InternalSearchRecordingOutcome | None,
        error: BaseException | None,
    ) -> _InternalSearchExit:
        if isinstance(error, asyncio.CancelledError | GeneratorExit):
            return cls(status=PhaseStatus.STOPPED, outcome=None, error=error)
        if isinstance(outcome, InternalSearchSucceeded):
            if error is None:
                return cls(
                    status=PhaseStatus.COMPLETED,
                    outcome=outcome,
                    error=None,
                )
        if isinstance(outcome, InternalSearchFailed):
            return cls(status=PhaseStatus.FAILED, outcome=outcome, error=error)
        return cls(status=PhaseStatus.FAILED, outcome=None, error=error)


class LogfireInternalSearchRecorder:
    def record(
        self,
        *,
        query_count: int,
    ) -> AbstractAsyncContextManager[InternalSearchRecording]:
        if query_count < 0:
            raise ValueError("query_count must be non-negative")
        return self._record(query_count=query_count)

    @asynccontextmanager
    async def _record(
        self,
        *,
        query_count: int,
    ) -> AsyncIterator[InternalSearchRecording]:
        recording = _InternalSearchRecording()
        started_at = _started_at()
        span = _try_open_span()
        error: BaseException | None = None
        try:
            yield recording
        except BaseException as exc:
            error = exc
            raise
        finally:
            search_exit = _InternalSearchExit.resolve(
                outcome=recording.outcome,
                error=error,
            )
            _try_close_span(span, error=_span_error(search_exit))
            _record_duration(started_at=started_at, search_exit=search_exit)
            _record_outcome(query_count=query_count, search_exit=search_exit)


def _try_open_span() -> AbstractContextManager[object] | None:
    try:
        span = logfire.span(_SPAN_NAME)
        span.__enter__()
        return span
    except Exception:
        return None


def _try_close_span(
    span: AbstractContextManager[object] | None,
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


def _span_error(search_exit: _InternalSearchExit) -> BaseException | None:
    if search_exit.status is PhaseStatus.STOPPED:
        return None
    return search_exit.error


def _started_at() -> float | None:
    try:
        return perf_counter()
    except Exception:
        return None


def _record_duration(
    *,
    started_at: float | None,
    search_exit: _InternalSearchExit,
) -> None:
    if started_at is None:
        return
    try:
        _duration_histogram.record(
            perf_counter() - started_at,
            attributes={
                "status": search_exit.status.value,
                "outcome": _outcome_label(search_exit.outcome),
            },
        )
    except Exception:
        return


def _record_outcome(
    *,
    query_count: int,
    search_exit: _InternalSearchExit,
) -> None:
    outcome = search_exit.outcome
    if outcome is None:
        return
    try:
        from app.agent.evidence_collection.internal_search.metrics import (
            record_internal_retrieval_outcome,
        )

        if isinstance(outcome, InternalSearchSucceeded):
            record_internal_retrieval_outcome(
                result=_success_outcome_label(outcome),
                query_count=query_count,
            )
            return
        record_internal_retrieval_outcome(
            result="failed",
            query_count=query_count,
            failure_code=outcome.failure_code,
        )
    except Exception:
        return


def _outcome_label(outcome: InternalSearchRecordingOutcome | None) -> str:
    if isinstance(outcome, InternalSearchSucceeded):
        return _success_outcome_label(outcome)
    if isinstance(outcome, InternalSearchFailed):
        return "failed"
    return _MISSING_OUTCOME


def _success_outcome_label(
    outcome: InternalSearchSucceeded,
) -> Literal["succeeded", "empty"]:
    return "empty" if outcome.hit_count == 0 else "succeeded"


logfire_internal_search_recorder = LogfireInternalSearchRecorder()
