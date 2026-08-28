"""質問計画を完成させる工程。"""

from __future__ import annotations

from app.agent.agent import Agent
from app.agent.planning.contract import (
    PlanningInput,
    QuestionPlan,
    QuestionPlanDraft,
    plan_from_draft,
)
from app.agent.planning.failure import PlanningError, planning_error_from
from app.agent.recording.planning import (
    PlanningFailed,
    PlanningRecorder,
    PlanningSucceeded,
    logfire_planning_recorder,
)
from app.agent.runtime.contract import (
    AgentResponseInvalidError,
    AgentRuntimeScopeFactory,
)
from app.analysis.ai_provider_errors import (
    AIProviderContentError,
    AIProviderStateError,
)

_PLANNING_SOURCE_ERRORS = (
    AIProviderStateError,
    AIProviderContentError,
    AgentResponseInvalidError,
)
_MAX_ATTEMPTS = 2


class QuestionPlanningService:
    """LLM draft から完成した質問計画を作る。"""

    def __init__(
        self,
        *,
        agent: Agent[PlanningInput, QuestionPlanDraft],
        runtime_scope_factory: AgentRuntimeScopeFactory,
        recorder: PlanningRecorder = logfire_planning_recorder,
    ) -> None:
        self._agent = agent
        self._runtime_scope_factory = runtime_scope_factory
        self._recorder = recorder

    async def plan(self, input: PlanningInput) -> QuestionPlan:
        """完成した計画を返す。応答形状の失敗だけ再試行する。"""

        async with self._recorder.record(agent_name=self._agent.name) as recording:
            try:
                async with self._runtime_scope_factory() as runtime:
                    for attempt_number in range(1, _MAX_ATTEMPTS + 1):
                        try:
                            draft = await runtime.call(
                                self._agent,
                                input,
                                attempt_number=attempt_number,
                            )
                            plan = plan_from_draft(draft)
                        except _PLANNING_SOURCE_ERRORS as cause:
                            if (
                                isinstance(cause, AgentResponseInvalidError)
                                and attempt_number < _MAX_ATTEMPTS
                            ):
                                continue
                            raise planning_error_from(cause) from cause
                        break
                    else:
                        raise AssertionError(
                            "unreachable: planning loop must return or raise"
                        )
            except PlanningError as error:
                recording.set_outcome(
                    PlanningFailed(
                        failure_code=error.code,
                        attempt_count=attempt_number,
                    )
                )
                raise

            recording.set_outcome(
                PlanningSucceeded(
                    plan_type=plan.plan_type,
                    attempt_count=attempt_number,
                )
            )
            return plan
