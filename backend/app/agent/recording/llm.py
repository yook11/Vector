"""LLM attempt の start/end 記録。"""

from __future__ import annotations

from time import perf_counter
from typing import Protocol

import logfire

from app.agent.recording.types import LlmCall, LlmCallResult, PhaseStatus, Usage

__all__ = [
    "LlmCallRecorder",
    "LogfireLlmCallRecorder",
    "close_llm_call",
    "logfire_llm_call_recorder",
    "outcome_from_span_result",
    "usage_from_deepseek_usage",
    "usage_from_gemini_metadata",
]

_OUTCOME_METRIC = "vector.agent.llm_call.outcome"
_DURATION_METRIC = "vector.agent.llm_call.duration"
_TOKENS_METRIC = "vector.agent.llm_call.tokens"
_MISSING_RESULT = "none"

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


class LlmCallRecorder(Protocol):
    def start(
        self,
        *,
        agent_name: str,
        provider: str,
        model: str,
        attempt_number: int,
    ) -> LlmCall: ...

    def end(
        self,
        call: LlmCall,
        *,
        status: PhaseStatus,
        result: LlmCallResult | None,
        usage: Usage | None,
    ) -> None: ...


def outcome_from_span_result(
    span_result: str,
) -> tuple[PhaseStatus, LlmCallResult]:
    """span の result 文字列を status と LLM 結論へ写像する。"""

    result = LlmCallResult(span_result)
    if result is LlmCallResult.SUCCEEDED:
        return PhaseStatus.COMPLETED, result
    return PhaseStatus.FAILED, result


def usage_from_gemini_metadata(usage: object | None) -> Usage | None:
    return _usage_if_present(
        input_tokens=_optional_token(getattr(usage, "prompt_token_count", None)),
        output_tokens=_optional_token(getattr(usage, "candidates_token_count", None)),
        cache_read_input_tokens=_optional_token(
            getattr(usage, "cached_content_token_count", None)
        ),
        reasoning_output_tokens=_optional_token(
            getattr(usage, "thoughts_token_count", None)
        ),
    )


def usage_from_deepseek_usage(usage: object | None) -> Usage | None:
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    return _usage_if_present(
        input_tokens=_optional_token(getattr(usage, "prompt_tokens", None)),
        output_tokens=_optional_token(getattr(usage, "completion_tokens", None)),
        cache_read_input_tokens=_optional_token(
            getattr(prompt_details, "cached_tokens", None)
        ),
        reasoning_output_tokens=_optional_token(
            getattr(completion_details, "reasoning_tokens", None)
        ),
    )


def close_llm_call(
    recorder: LlmCallRecorder,
    call: LlmCall,
    *,
    status: PhaseStatus,
    result: LlmCallResult | None,
    usage: Usage | None,
) -> None:
    """recorder.end の失敗で本処理を落とさない。"""

    try:
        recorder.end(call, status=status, result=result, usage=usage)
    except Exception:
        return


class LogfireLlmCallRecorder:
    def start(
        self,
        *,
        agent_name: str,
        provider: str,
        model: str,
        attempt_number: int,
    ) -> LlmCall:
        return LlmCall(
            agent_name=agent_name,
            provider=provider,
            model=model,
            attempt_number=attempt_number,
            started_at=perf_counter(),
        )

    def end(
        self,
        call: LlmCall,
        *,
        status: PhaseStatus,
        result: LlmCallResult | None,
        usage: Usage | None,
    ) -> None:
        try:
            attributes = {
                "agent_name": call.agent_name,
                "provider": call.provider,
                "model": call.model,
                "status": status.value,
                "result": result.value if result is not None else _MISSING_RESULT,
            }
            _outcome_counter.add(1, attributes=attributes)
            _duration_histogram.record(
                perf_counter() - call.started_at,
                attributes=attributes,
            )
            if usage is None:
                return
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


def _optional_token(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _usage_if_present(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_input_tokens: int | None,
    reasoning_output_tokens: int | None,
) -> Usage | None:
    if (
        input_tokens is None
        and output_tokens is None
        and cache_read_input_tokens is None
        and reasoning_output_tokens is None
    ):
        return None
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
    )


logfire_llm_call_recorder = LogfireLlmCallRecorder()
