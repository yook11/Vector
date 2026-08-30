"""LLM provider attempt 1回の記録。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import Literal, Protocol

import logfire
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.semconv._incubating.attributes.gen_ai_attributes import (
    GEN_AI_OPERATION_NAME,
    GEN_AI_PROVIDER_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
)
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.trace import SpanKind, StatusCode

from app.agent.recording.types import PhaseStatus, Usage
from app.agent.runtime.llm_failure import LlmAttemptFailed

__all__ = [
    "LlmCallMode",
    "LlmCallRecorder",
    "LlmCallRecording",
    "LogfireLlmCallRecorder",
    "logfire_llm_call_recorder",
]

_SPAN_NAME = "agent_provider_call"
_OUTCOME_METRIC = "vector.agent.llm_call.outcome"
_DURATION_METRIC = "vector.agent.llm_call.duration"
_TOKENS_METRIC = "vector.agent.llm_call.tokens"
_MISSING_RESULT = "none"
_FAILED_RESULT = "failed"
_SUCCEEDED_RESULT = "succeeded"
_GEN_AI_REASONING_OUTPUT_TOKENS = "gen_ai.usage.reasoning.output_tokens"
_SPAN_FAILURE_RESULTS = frozenset({"blocked", "invalid_response", "provider_error"})
_TRACER = trace.get_tracer(__name__)

_outcome_counter = logfire.metric_counter(
    _OUTCOME_METRIC,
    unit="1",
    description="Agent LLM provider attempt outcome",
)
_duration_histogram = logfire.metric_histogram(
    _DURATION_METRIC,
    unit="s",
    description="Agent LLM provider attempt duration",
)
_tokens_counter = logfire.metric_counter(
    _TOKENS_METRIC,
    unit="1",
    description="Agent LLM provider attempt tokens",
)

type LlmCallMode = Literal["call", "stream"]


class LlmCallRecording(Protocol):
    """実行中の provider attempt へ usage と分類済み失敗を伝える。"""

    def report_usage(self, usage: Usage) -> None: ...

    def report_outcome(
        self,
        failure: LlmAttemptFailed,
        *,
        span_result: str,
    ) -> None: ...


class LlmCallRecorder(Protocol):
    """provider attempt 1回の span・duration・分類済み outcome を完結させる。"""

    def record(
        self,
        *,
        agent_name: str,
        provider: str,
        model: str,
        attempt_number: int,
        prompt_version: str,
        operation_name: str,
        gen_ai_provider: str,
        mode: LlmCallMode,
        parent_context: otel_context.Context | None = None,
    ) -> AbstractAsyncContextManager[LlmCallRecording]: ...


@dataclass(slots=True)
class _LlmCallRecording:
    span: _SpanHandle | None
    usage: Usage | None = None
    failure: LlmAttemptFailed | None = None
    span_result: str | None = None

    def report_usage(self, usage: Usage) -> None:
        self.usage = usage
        _try_write_usage(self.span, usage)

    def report_outcome(
        self,
        failure: LlmAttemptFailed,
        *,
        span_result: str,
    ) -> None:
        if span_result not in _SPAN_FAILURE_RESULTS:
            raise ValueError("span_result must be a provider-attempt failure bucket")
        self.failure = failure
        self.span_result = span_result


@dataclass(frozen=True, slots=True)
class _LlmCallExit:
    status: PhaseStatus
    result: str
    failure: LlmAttemptFailed | None
    span_result: str | None
    error: BaseException | None

    @classmethod
    def resolve(
        cls,
        *,
        failure: LlmAttemptFailed | None,
        span_result: str | None,
        error: BaseException | None,
    ) -> _LlmCallExit:
        if isinstance(error, asyncio.CancelledError | GeneratorExit):
            return cls(
                status=PhaseStatus.STOPPED,
                result=_MISSING_RESULT,
                failure=None,
                span_result=None,
                error=error,
            )
        if failure is not None:
            return cls(
                status=PhaseStatus.FAILED,
                result=_FAILED_RESULT,
                failure=failure,
                span_result=span_result,
                error=error,
            )
        if error is None:
            return cls(
                status=PhaseStatus.COMPLETED,
                result=_SUCCEEDED_RESULT,
                failure=None,
                span_result=None,
                error=None,
            )
        return cls(
            status=PhaseStatus.FAILED,
            result=_MISSING_RESULT,
            failure=None,
            span_result=None,
            error=error,
        )


@dataclass(frozen=True, slots=True)
class _SpanHandle:
    mode: LlmCallMode
    writer: object
    closer: object | None = None


class LogfireLlmCallRecorder:
    def record(
        self,
        *,
        agent_name: str,
        provider: str,
        model: str,
        attempt_number: int,
        prompt_version: str,
        operation_name: str,
        gen_ai_provider: str,
        mode: LlmCallMode,
        parent_context: otel_context.Context | None = None,
    ) -> AbstractAsyncContextManager[LlmCallRecording]:
        return self._record(
            agent_name=agent_name,
            provider=provider,
            model=model,
            attempt_number=attempt_number,
            prompt_version=prompt_version,
            operation_name=operation_name,
            gen_ai_provider=gen_ai_provider,
            mode=mode,
            parent_context=parent_context,
        )

    @asynccontextmanager
    async def _record(
        self,
        *,
        agent_name: str,
        provider: str,
        model: str,
        attempt_number: int,
        prompt_version: str,
        operation_name: str,
        gen_ai_provider: str,
        mode: LlmCallMode,
        parent_context: otel_context.Context | None,
    ) -> AsyncIterator[LlmCallRecording]:
        started_at = _started_at()
        span = _try_open_span(
            mode=mode,
            parent_context=parent_context,
            attributes=_span_attributes(
                agent_name=agent_name,
                attempt_number=attempt_number,
                prompt_version=prompt_version,
                operation_name=operation_name,
                gen_ai_provider=gen_ai_provider,
                model=model,
            ),
        )
        recording = _LlmCallRecording(span=span)
        error: BaseException | None = None
        try:
            yield recording
        except BaseException as exc:
            error = exc
            raise
        finally:
            llm_exit = _LlmCallExit.resolve(
                failure=recording.failure,
                span_result=recording.span_result,
                error=error,
            )
            _try_close_span(span, llm_exit=llm_exit)
            identity = {
                "agent_name": agent_name,
                "provider": provider,
                "model": model,
                "attempt_number": attempt_number,
            }
            _record_outcome(identity=identity, llm_exit=llm_exit)
            _record_duration(
                started_at=started_at,
                identity=identity,
                llm_exit=llm_exit,
            )
            _record_tokens(
                usage=recording.usage,
                identity=identity,
                llm_exit=llm_exit,
            )


def _span_attributes(
    *,
    agent_name: str,
    attempt_number: int,
    prompt_version: str,
    operation_name: str,
    gen_ai_provider: str,
    model: str,
) -> dict[str, object]:
    return {
        "agent_name": agent_name,
        "attempt_number": attempt_number,
        "prompt_version": prompt_version,
        GEN_AI_OPERATION_NAME: operation_name,
        GEN_AI_PROVIDER_NAME: gen_ai_provider,
        GEN_AI_REQUEST_MODEL: model,
    }


def _try_open_span(
    *,
    mode: LlmCallMode,
    parent_context: otel_context.Context | None,
    attributes: dict[str, object],
) -> _SpanHandle | None:
    try:
        if mode == "stream":
            writer = _TRACER.start_span(
                _SPAN_NAME,
                context=parent_context,
                kind=SpanKind.CLIENT,
                attributes=attributes,
            )
            return _SpanHandle(mode="stream", writer=writer)
        closer = logfire.span(
            _SPAN_NAME,
            _span_kind=SpanKind.CLIENT,
            **attributes,
        )
        writer = closer.__enter__()
        return _SpanHandle(mode="call", writer=writer, closer=closer)
    except Exception:
        return None


def _try_close_span(span: _SpanHandle | None, *, llm_exit: _LlmCallExit) -> None:
    if span is None:
        return
    _try_apply_span_conclusion(span.writer, llm_exit=llm_exit)
    try:
        if span.mode == "stream":
            if (
                llm_exit.status is not PhaseStatus.STOPPED
                and llm_exit.failure is None
                and llm_exit.error is not None
            ):
                span.writer.record_exception(llm_exit.error)
                span.writer.set_status(StatusCode.ERROR, str(llm_exit.error))
            span.writer.end()
            return
        closer = span.closer
        if closer is None:
            return
        if (
            llm_exit.status is PhaseStatus.STOPPED
            or llm_exit.failure is not None
            or llm_exit.error is None
        ):
            closer.__exit__(None, None, None)
            return
        error = llm_exit.error
        closer.__exit__(type(error), error, error.__traceback__)
    except BaseException:
        return


def _try_apply_span_conclusion(writer: object, *, llm_exit: _LlmCallExit) -> None:
    try:
        if llm_exit.status is PhaseStatus.STOPPED:
            return
        if llm_exit.failure is not None:
            if llm_exit.span_result is not None:
                writer.set_attribute("result", llm_exit.span_result)
            writer.set_attribute(ERROR_TYPE, llm_exit.failure.failure_code)
            writer.set_status(StatusCode.ERROR)
            return
        if llm_exit.error is None:
            writer.set_attribute("result", _SUCCEEDED_RESULT)
    except Exception:
        return


def _try_write_usage(span: _SpanHandle | None, usage: Usage) -> None:
    if span is None:
        return
    writer = span.writer
    try:
        if usage.input_tokens is not None:
            writer.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, usage.input_tokens)
        if usage.output_tokens is not None:
            writer.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, usage.output_tokens)
        if usage.cache_read_input_tokens is not None:
            writer.set_attribute(
                GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
                usage.cache_read_input_tokens,
            )
        if usage.reasoning_output_tokens is not None:
            writer.set_attribute(
                _GEN_AI_REASONING_OUTPUT_TOKENS,
                usage.reasoning_output_tokens,
            )
    except Exception:
        return


def _started_at() -> float | None:
    try:
        return perf_counter()
    except Exception:
        return None


def _metric_attributes(
    *,
    identity: dict[str, object],
    llm_exit: _LlmCallExit,
) -> dict[str, object]:
    return {
        **identity,
        "status": llm_exit.status.value,
        "result": llm_exit.result,
    }


def _record_outcome(
    *,
    identity: dict[str, object],
    llm_exit: _LlmCallExit,
) -> None:
    if llm_exit.result == _MISSING_RESULT:
        return
    try:
        attributes = _metric_attributes(identity=identity, llm_exit=llm_exit)
        if llm_exit.failure is not None:
            attributes = {
                **attributes,
                "failure_code": llm_exit.failure.failure_code,
            }
        _outcome_counter.add(1, attributes=attributes)
    except Exception:
        return


def _record_duration(
    *,
    started_at: float | None,
    identity: dict[str, object],
    llm_exit: _LlmCallExit,
) -> None:
    if started_at is None:
        return
    try:
        _duration_histogram.record(
            perf_counter() - started_at,
            attributes=_metric_attributes(identity=identity, llm_exit=llm_exit),
        )
    except Exception:
        return


def _record_tokens(
    *,
    usage: Usage | None,
    identity: dict[str, object],
    llm_exit: _LlmCallExit,
) -> None:
    if usage is None:
        return
    try:
        attributes = _metric_attributes(identity=identity, llm_exit=llm_exit)
        for direction, value in (
            ("input", usage.input_tokens),
            ("output", usage.output_tokens),
            ("cache_read_input", usage.cache_read_input_tokens),
            ("reasoning_output", usage.reasoning_output_tokens),
        ):
            if value is not None:
                _tokens_counter.add(
                    value,
                    attributes={**attributes, "direction": direction},
                )
    except Exception:
        return


logfire_llm_call_recorder = LogfireLlmCallRecorder()
