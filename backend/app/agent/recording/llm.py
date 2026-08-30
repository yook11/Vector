"""LLM attempt の start/end 記録。"""

from __future__ import annotations

from time import perf_counter
from typing import Protocol

import logfire

from app.agent.recording.types import LlmCall, LlmCallResult, PhaseStatus, Usage

__all__ = [
    "LlmCallRecorder",
    "LogfireLlmCallRecorder",
    "logfire_llm_call_recorder",
    "outcome_from_span_result",
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
    """start は必ず LlmCall を返し、記録の例外は本処理へ出さない。"""

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
        result: LlmCallResult | None = None,
        usage: Usage | None = None,
        stopped: bool = False,
    ) -> None: ...


def outcome_from_span_result(span_result: str) -> LlmCallResult:
    """span の result 文字列を LLM 結論へ写す。"""

    return LlmCallResult(span_result)


def _status_from_result(
    *,
    stopped: bool,
    result: LlmCallResult | None,
) -> PhaseStatus:
    """attempt の終わり方を記録の status に写す。"""

    if stopped:
        return PhaseStatus.STOPPED
    if result is LlmCallResult.SUCCEEDED:
        return PhaseStatus.COMPLETED
    return PhaseStatus.FAILED


class LogfireLlmCallRecorder:
    def start(
        self,
        *,
        agent_name: str,
        provider: str,
        model: str,
        attempt_number: int,
    ) -> LlmCall:
        try:
            return LlmCall(
                agent_name=agent_name,
                provider=provider,
                model=model,
                attempt_number=attempt_number,
                started_at=perf_counter(),
            )
        except Exception:
            return LlmCall(
                agent_name=agent_name,
                provider=provider,
                model=model,
                attempt_number=attempt_number,
                started_at=0.0,
            )

    def end(
        self,
        call: LlmCall,
        *,
        result: LlmCallResult | None = None,
        usage: Usage | None = None,
        stopped: bool = False,
    ) -> None:
        try:
            status = _status_from_result(stopped=stopped, result=result)
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


logfire_llm_call_recorder = LogfireLlmCallRecorder()
