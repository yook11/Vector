"""申し送りの整理工程。

回答は既にストリームし終えているため、この工程の失敗でRunを失敗させない。
分類済みの失敗では受け取ったhandoffをそのまま返し、整理は前回の値が残る。
"""

from __future__ import annotations

from typing import Protocol

from app.agent.agent import Agent
from app.agent.llm_call.caller import LlmCallerScopeFactory, call_agent
from app.agent.output import AgentOutputInvalidError
from app.agent.recording.research_handoff import (
    ResearchHandoffFailed,
    ResearchHandoffRecorder,
    ResearchHandoffSucceeded,
    logfire_research_handoff_recorder,
)
from app.agent.research_handoff.failure import ResearchHandoffError
from app.agent.research_handoff.handoff import ResearchHandoff, ResearchHandoffDraft
from app.agent.research_handoff.handoff_input import ResearchHandoffInput
from app.ai_providers.errors import AIProviderError

__all__ = ["ResearchHandoffOrganizer", "ResearchHandoffService"]

_CLASSIFIED_FAILURES = (AIProviderError, AgentOutputInvalidError)


class ResearchHandoffOrganizer(Protocol):
    """台帳を積み終えたhandoffの整理3本を書き直す。"""

    async def organize(self, input: ResearchHandoffInput) -> ResearchHandoff: ...


class ResearchHandoffService:
    def __init__(
        self,
        *,
        agent: Agent[ResearchHandoffInput, ResearchHandoffDraft],
        llm_caller_scope_factory: LlmCallerScopeFactory,
        recorder: ResearchHandoffRecorder = logfire_research_handoff_recorder,
    ) -> None:
        self._agent = agent
        self._llm_caller_scope_factory = llm_caller_scope_factory
        self._recorder = recorder

    async def organize(self, input: ResearchHandoffInput) -> ResearchHandoff:
        """整理を書き直したhandoffを返す。失敗時は受け取ったものをそのまま返す。"""
        async with self._recorder.record(agent_name=self._agent.name) as recording:
            try:
                async with self._llm_caller_scope_factory() as caller:
                    draft = await call_agent(
                        caller, self._agent, input, attempt_number=1
                    )
            except _CLASSIFIED_FAILURES as cause:
                error = ResearchHandoffError(cause=cause)
                recording.report_outcome(ResearchHandoffFailed(failure_code=error.code))
                return input.handoff

            recording.report_outcome(ResearchHandoffSucceeded())
            return input.handoff.from_draft(draft)
