"""LLM attempt の start/end 記録。"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

import logfire

from app.agent.recording.types import LlmCall, PhaseStatus, Usage

__all__ = [
    "LlmAttemptFailed",
    "LlmAttemptOutcome",
    "LlmAttemptSucceeded",
    "LlmCallRecorder",
    "LogfireLlmCallRecorder",
    "logfire_llm_call_recorder",
]

_OUTCOME_METRIC = "vector.agent.llm_call.outcome"
_DURATION_METRIC = "vector.agent.llm_call.duration"
_TOKENS_METRIC = "vector.agent.llm_call.tokens"
_MISSING_RESULT = "none"
_FAILED_RESULT = "failed"

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


@dataclass(frozen=True, slots=True)
class LlmAttemptSucceeded:
    """完成した provider attempt の結論。"""


@dataclass(frozen=True, slots=True)
class LlmAttemptFailed:
    """分類済み失敗で終了した provider attempt の結論。"""

    failure_code: str

    def __post_init__(self) -> None:
        if not self.failure_code:
            raise ValueError("failure_code must not be empty")


type LlmAttemptOutcome = LlmAttemptSucceeded | LlmAttemptFailed


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
        outcome: LlmAttemptOutcome | None = None,
        usage: Usage | None = None,
        stopped: bool = False,
    ) -> None: ...


def _status_and_result(
    *,
    stopped: bool,
    outcome: LlmAttemptOutcome | None,
) -> tuple[PhaseStatus, str]:
    """attempt の終わり方を記録の status と result に写す。"""

    if stopped:
        return PhaseStatus.STOPPED, _MISSING_RESULT
    if isinstance(outcome, LlmAttemptSucceeded):
        return PhaseStatus.COMPLETED, "succeeded"
    if isinstance(outcome, LlmAttemptFailed):
        return PhaseStatus.FAILED, _FAILED_RESULT
    return PhaseStatus.FAILED, _MISSING_RESULT


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
        outcome: LlmAttemptOutcome | None = None,
        usage: Usage | None = None,
        stopped: bool = False,
    ) -> None:
        try:
            status, result_label = _status_and_result(stopped=stopped, outcome=outcome)
            attributes = {
                "agent_name": call.agent_name,
                "provider": call.provider,
                "model": call.model,
                "status": status.value,
                "result": result_label,
            }
            outcome_attributes = attributes
            if not stopped and isinstance(outcome, LlmAttemptFailed):
                outcome_attributes = {
                    **attributes,
                    "failure_code": outcome.failure_code,
                }
            _outcome_counter.add(1, attributes=outcome_attributes)
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
