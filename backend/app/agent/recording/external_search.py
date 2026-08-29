"""外部検索 search() 1回のspan・duration・outcome記録。"""

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
from typing import TYPE_CHECKING, Protocol

import logfire

from app.agent.phase_span import agent_phase
from app.agent.recording.types import PhaseStatus

if TYPE_CHECKING:
    from app.agent.evidence_collection.external_search.contract import (
        ExternalSearchFailureCode,
    )

__all__ = [
    "ExternalSearchFailed",
    "ExternalSearchOutcome",
    "ExternalSearchRecorder",
    "ExternalSearchRecording",
    "ExternalSearchSucceeded",
    "LogfireExternalSearchRecorder",
    "logfire_external_search_recorder",
]

_OUTCOME_METRIC = "vector.agent.external_search.outcome"
_DURATION_METRIC = "vector.agent.external_search.duration"
_MISSING_OUTCOME = "none"
_SPAN_NAME = "external_search"

_outcome_counter = logfire.metric_counter(
    _OUTCOME_METRIC,
    unit="1",
    description="External search process outcome",
)
_duration_histogram = logfire.metric_histogram(
    _DURATION_METRIC,
    unit="s",
    description="External search process duration",
)


@dataclass(frozen=True, slots=True)
class ExternalSearchSucceeded:
    """外部検索を実行して結果を返した結論。"""


@dataclass(frozen=True, slots=True)
class ExternalSearchFailed:
    """分類済みの縮退で戻り値を返した結論。"""

    failure_code: ExternalSearchFailureCode


type ExternalSearchOutcome = ExternalSearchSucceeded | ExternalSearchFailed


class ExternalSearchRecording(Protocol):
    """工程の結論をRecorderへ伝え、query生成scopeを提供する記録ハンドル。"""

    def report_outcome(self, outcome: ExternalSearchOutcome) -> None: ...

    def record_query_generation(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[None]: ...


class ExternalSearchRecorder(Protocol):
    """search() 1回のspan・duration・分類済みoutcomeを完結させる。"""

    def record(self) -> AbstractAsyncContextManager[ExternalSearchRecording]: ...


@dataclass(slots=True)
class _ExternalSearchRecording:
    outcome: ExternalSearchOutcome | None = None

    def report_outcome(self, outcome: ExternalSearchOutcome) -> None:
        self.outcome = outcome

    def record_query_generation(
        self,
        *,
        agent_name: str,
    ) -> AbstractAsyncContextManager[None]:
        return _record_query_generation(agent_name=agent_name)


@dataclass(frozen=True, slots=True)
class _ExternalSearchExit:
    status: PhaseStatus
    outcome: ExternalSearchOutcome | None
    error: BaseException | None

    @classmethod
    def resolve(
        cls,
        *,
        outcome: ExternalSearchOutcome | None,
        error: BaseException | None,
    ) -> _ExternalSearchExit:
        if isinstance(error, asyncio.CancelledError | GeneratorExit):
            return cls(status=PhaseStatus.STOPPED, outcome=None, error=error)
        if outcome is not None and error is None:
            return cls(status=PhaseStatus.COMPLETED, outcome=outcome, error=None)
        return cls(status=PhaseStatus.FAILED, outcome=None, error=error)


class LogfireExternalSearchRecorder:
    def record(self) -> AbstractAsyncContextManager[ExternalSearchRecording]:
        return self._record()

    @asynccontextmanager
    async def _record(self) -> AsyncIterator[ExternalSearchRecording]:
        recording = _ExternalSearchRecording()
        started_at = _started_at()
        span = _try_open_search_span()
        error: BaseException | None = None
        try:
            yield recording
        except BaseException as exc:
            error = exc
            raise
        finally:
            search_exit = _ExternalSearchExit.resolve(
                outcome=recording.outcome,
                error=error,
            )
            _try_close_span(span, error=_span_error(search_exit))
            _record_duration(started_at=started_at, search_exit=search_exit)
            _record_outcome(search_exit)


@asynccontextmanager
async def _record_query_generation(*, agent_name: str) -> AsyncIterator[None]:
    span = _try_open_query_generation_span(agent_name=agent_name)
    error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        error = exc
        raise
    finally:
        _try_close_span(span, error=_span_error_from_error(error))


def _try_open_search_span() -> AbstractContextManager[object] | None:
    try:
        span = logfire.span(_SPAN_NAME)
        span.__enter__()
        return span
    except Exception:
        return None


def _try_open_query_generation_span(
    *,
    agent_name: str,
) -> AbstractContextManager[None] | None:
    try:
        span = agent_phase(
            phase="evidence_collection",
            agent_name=agent_name,
        )
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


def _span_error(search_exit: _ExternalSearchExit) -> BaseException | None:
    if search_exit.status is PhaseStatus.STOPPED:
        return None
    return search_exit.error


def _span_error_from_error(error: BaseException | None) -> BaseException | None:
    if isinstance(error, asyncio.CancelledError | GeneratorExit):
        return None
    return error


def _started_at() -> float | None:
    try:
        return perf_counter()
    except Exception:
        return None


def _record_duration(
    *,
    started_at: float | None,
    search_exit: _ExternalSearchExit,
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


def _record_outcome(search_exit: _ExternalSearchExit) -> None:
    outcome = search_exit.outcome
    if outcome is None:
        return
    try:
        _outcome_counter.add(
            1,
            attributes={"result": _outcome_label(outcome)},
        )
    except Exception:
        return


def _outcome_label(outcome: ExternalSearchOutcome | None) -> str:
    if isinstance(outcome, ExternalSearchSucceeded):
        return "succeeded"
    if isinstance(outcome, ExternalSearchFailed):
        return outcome.failure_code.value
    return _MISSING_OUTCOME


logfire_external_search_recorder = LogfireExternalSearchRecorder()
