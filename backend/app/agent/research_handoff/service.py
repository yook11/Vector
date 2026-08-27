"""申し送りの整理工程。

回答は既にストリームし終えているため、この工程の失敗でRunを失敗させない。
分類済みの失敗では受け取ったhandoffをそのまま返し、整理は前回の値が残る。
"""

from __future__ import annotations

from app.agent.agent import Agent
from app.agent.contract import ORGANIZED_TEXT_MAX_CHARS, ResearchHandoff
from app.agent.recording.research_handoff import (
    ResearchHandoffFailed,
    ResearchHandoffRecorder,
    ResearchHandoffSucceeded,
    logfire_research_handoff_recorder,
)
from app.agent.research_handoff.contract import (
    HandoffMaterial,
    HandoffOrganizerInput,
    ResearchHandoffDraft,
)
from app.agent.runtime.contract import (
    AgentResponseInvalidError,
    AgentRuntimeScopeFactory,
)
from app.analysis.ai_provider_errors import AIProviderError

__all__ = ["ResearchHandoffService"]

_CLASSIFIED_FAILURES = (AIProviderError, AgentResponseInvalidError)
_UNCLASSIFIED_PROVIDER_FAILURE = "provider_error"


class ResearchHandoffService:
    """台帳を積み終えたhandoffの整理3本を書き直す。"""

    def __init__(
        self,
        *,
        agent: Agent[HandoffOrganizerInput, ResearchHandoffDraft],
        runtime_scope_factory: AgentRuntimeScopeFactory,
        recorder: ResearchHandoffRecorder = logfire_research_handoff_recorder,
    ) -> None:
        self._agent = agent
        self._runtime_scope_factory = runtime_scope_factory
        self._recorder = recorder

    async def organize(
        self,
        *,
        handoff: ResearchHandoff,
        material: HandoffMaterial,
    ) -> ResearchHandoff:
        """整理を書き直したhandoffを返す。失敗時は受け取ったものをそのまま返す。"""
        async with self._recorder.record(agent_name=self._agent.name) as recording:
            try:
                async with self._runtime_scope_factory() as runtime:
                    draft = await runtime.call(
                        self._agent,
                        HandoffOrganizerInput(handoff=handoff, material=material),
                        attempt_number=1,
                    )
            except _CLASSIFIED_FAILURES as cause:
                recording.set_outcome(
                    ResearchHandoffFailed(failure_code=_failure_code(cause))
                )
                return handoff

            organized = _organized_from_draft(handoff=handoff, draft=draft)
            recording.set_outcome(ResearchHandoffSucceeded())
            return organized


def _organized_from_draft(
    *,
    handoff: ResearchHandoff,
    draft: ResearchHandoffDraft,
) -> ResearchHandoff:
    """整理3本だけを差し替える。台帳はdraftに含まれないため書き換わらない。"""
    return ResearchHandoff(
        updated_at=handoff.updated_at,
        runs=handoff.runs,
        collected_overview=_clean(draft.collected_overview),
        unresolved_points=_clean(draft.unresolved_points),
        next_search_guidance=_clean(draft.next_search_guidance),
    )


def _clean(value: str) -> str:
    return value.strip()[:ORGANIZED_TEXT_MAX_CHARS].strip()


def _failure_code(cause: Exception) -> str:
    """記録の欠損でこの工程を失敗させないため、code不在でも文字列を返す。"""
    if isinstance(cause, AgentResponseInvalidError):
        return cause.defect.value
    code = getattr(cause, "CODE", None)
    return code if isinstance(code, str) and code else _UNCLASSIFIED_PROVIDER_FAILURE
