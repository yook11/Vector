"""収集工程 collect() 1回とtask fan-outのspan記録。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    asynccontextmanager,
)
from dataclasses import dataclass
from typing import Protocol

import logfire

from app.agent.phase_span import agent_phase

__all__ = [
    "EvidenceCollectionRecorder",
    "EvidenceCollectionRecording",
    "LogfireEvidenceCollectionRecorder",
    "logfire_evidence_collection_recorder",
]

_TASK_SPAN_NAME = "evidence_collection_task"


class EvidenceCollectionRecording(Protocol):
    """collect()内のresearch taskを親工程へ関連付ける記録ハンドル。"""

    def record_task(
        self,
        *,
        task_index: int,
    ) -> AbstractAsyncContextManager[None]: ...


class EvidenceCollectionRecorder(Protocol):
    """collect() 1回とtask fan-outのspanを完結させる。"""

    def record(self) -> AbstractAsyncContextManager[EvidenceCollectionRecording]: ...


@dataclass(frozen=True, slots=True)
class _EvidenceCollectionRecording:
    def record_task(
        self,
        *,
        task_index: int,
    ) -> AbstractAsyncContextManager[None]:
        if task_index < 0:
            raise ValueError("task_index must be non-negative")
        return _record_task(task_index=task_index)


class LogfireEvidenceCollectionRecorder:
    def record(self) -> AbstractAsyncContextManager[EvidenceCollectionRecording]:
        return self._record()

    @asynccontextmanager
    async def _record(self) -> AsyncIterator[EvidenceCollectionRecording]:
        span = _try_open_collection_span()
        error: BaseException | None = None
        try:
            yield _EvidenceCollectionRecording()
        except BaseException as exc:
            error = exc
            raise
        finally:
            _try_close_span(span, error=_span_error(error))


@asynccontextmanager
async def _record_task(*, task_index: int) -> AsyncIterator[None]:
    span = _try_open_task_span(task_index=task_index)
    error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        error = exc
        raise
    finally:
        _try_close_span(span, error=_span_error(error))


def _try_open_collection_span() -> AbstractContextManager[None] | None:
    try:
        span = agent_phase(phase="evidence_collection")
        span.__enter__()
        return span
    except Exception:
        return None


def _try_open_task_span(*, task_index: int) -> AbstractContextManager[object] | None:
    try:
        span = logfire.span(_TASK_SPAN_NAME, task_index=task_index)
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


def _span_error(error: BaseException | None) -> BaseException | None:
    if isinstance(error, asyncio.CancelledError | GeneratorExit):
        return None
    return error


logfire_evidence_collection_recorder = LogfireEvidenceCollectionRecorder()
