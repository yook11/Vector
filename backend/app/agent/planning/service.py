"""質問計画を完成させる工程。"""

from __future__ import annotations

import asyncio

from app.agent.agent import Agent
from app.agent.output import AgentOutputInvalidError, validate_output
from app.agent.planning.contract import (
    PlanningInput,
    QuestionPlan,
    QuestionPlanDraft,
    plan_from_draft,
)
from app.agent.planning.failure import PlanningError
from app.agent.recording.planning import (
    PlanningFailed,
    PlanningRecorder,
    PlanningSucceeded,
    logfire_planning_recorder,
)
from app.agent.retry_prospect import is_resolvable_by_retry
from app.agent.runtime.contract import AgentRuntimeScopeFactory
from app.ai_providers.errors import (
    CLASSIFIED_AI_PROVIDER_ERRORS,
)

_PLANNING_SOURCE_ERRORS = (
    *CLASSIFIED_AI_PROVIDER_ERRORS,
    AgentOutputInvalidError,
)
_MAX_ATTEMPTS = 2
_PLANNING_TIMEOUT_SECONDS = 15


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
        """完成した計画を返す。呼び直せば解消しうる失敗だけ再試行する。"""

        async with self._recorder.record(agent_name=self._agent.name) as recording:
            attempt_number = 0
            timeout = asyncio.timeout(_PLANNING_TIMEOUT_SECONDS)
            try:
                async with timeout:
                    async with self._runtime_scope_factory() as runtime:
                        for attempt_number in range(1, _MAX_ATTEMPTS + 1):
                            try:
                                payload = await runtime.call(
                                    self._agent,
                                    input,
                                    attempt_number=attempt_number,
                                )
                                draft = validate_output(self._agent, payload)
                                plan = plan_from_draft(draft)
                            except _PLANNING_SOURCE_ERRORS as cause:
                                if (
                                    is_resolvable_by_retry(cause)
                                    and attempt_number < _MAX_ATTEMPTS
                                ):
                                    continue
                                raise PlanningError(cause=cause) from cause
                            break
                        else:
                            raise AssertionError(
                                "unreachable: planning loop must return or raise"
                            )
            except TimeoutError as cause:
                if not timeout.expired():
                    raise
                error = PlanningError(cause=cause)
                recording.report_outcome(
                    PlanningFailed(
                        failure_code=error.code,
                        attempt_count=attempt_number,
                    )
                )
                raise error from cause
            except PlanningError as error:
                recording.report_outcome(
                    PlanningFailed(
                        failure_code=error.code,
                        attempt_count=attempt_number,
                    )
                )
                raise

            recording.report_outcome(
                PlanningSucceeded(
                    plan_type=plan.plan_type,
                    attempt_count=attempt_number,
                )
            )
            return plan
