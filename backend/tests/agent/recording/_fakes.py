"""LlmCallRecorder の記録を蓄積する test double。"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter

from app.agent.recording.types import LlmCall, LlmCallResult, PhaseStatus, Usage

__all__ = ["RecordingLlmCallRecorder"]


@dataclass(frozen=True, slots=True)
class RecordedLlmCallEnd:
    call: LlmCall
    status: PhaseStatus
    result: LlmCallResult | None
    usage: Usage | None


@dataclass(slots=True)
class RecordingLlmCallRecorder:
    starts: list[LlmCall] = field(default_factory=list)
    ends: list[RecordedLlmCallEnd] = field(default_factory=list)

    def start(
        self,
        *,
        agent_name: str,
        provider: str,
        model: str,
        attempt_number: int,
    ) -> LlmCall:
        call = LlmCall(
            agent_name=agent_name,
            provider=provider,
            model=model,
            attempt_number=attempt_number,
            started_at=perf_counter(),
        )
        self.starts.append(call)
        return call

    def end(
        self,
        call: LlmCall,
        *,
        status: PhaseStatus,
        result: LlmCallResult | None,
        usage: Usage | None,
    ) -> None:
        self.ends.append(
            RecordedLlmCallEnd(
                call=call,
                status=status,
                result=result,
                usage=usage,
            )
        )
