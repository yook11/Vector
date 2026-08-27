"""Question planning service."""

from __future__ import annotations

from app.agent.agent import Agent
from app.agent.planning.contract import (
    PlanningAttemptInput,
    PlanningRequest,
    QuestionPlan,
    QuestionPlanDraft,
    plan_from_draft,
)
from app.agent.planning.failure import (
    RequestRetryDisposition,
    classify_planner_failure,
)
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

_PLANNER_CLASSIFIED_ERRORS = (
    AIProviderStateError,
    AIProviderContentError,
    AgentResponseInvalidError,
)
_MAX_ATTEMPTS = 2


class QuestionPlanningService:
    """Create completed question plans from LLM drafts."""

    def __init__(
        self,
        *,
        agent: Agent[PlanningAttemptInput, QuestionPlanDraft],
        runtime_scope_factory: AgentRuntimeScopeFactory,
        recorder: PlanningRecorder = logfire_planning_recorder,
    ) -> None:
        self._agent = agent
        self._runtime_scope_factory = runtime_scope_factory
        self._recorder = recorder

    async def plan(self, request: PlanningRequest) -> QuestionPlan:
        """Return a completed plan, retrying only response-shape failures."""

        repair_context: str | None = None
        completed_plan: QuestionPlan | None = None
        terminal_error: (
            AIProviderStateError
            | AIProviderContentError
            | AgentResponseInvalidError
            | None
        ) = None
        terminal_outcome: PlanningFailed | None = None
        attempt_count = 0

        async with self._recorder.record(agent_name=self._agent.name) as recording:
            try:
                async with self._runtime_scope_factory() as runtime:
                    for attempt_number in range(1, _MAX_ATTEMPTS + 1):
                        attempt_count = attempt_number
                        try:
                            draft = await runtime.call(
                                self._agent,
                                PlanningAttemptInput(
                                    request=request,
                                    repair_context=repair_context,
                                ),
                                attempt_number=attempt_number,
                            )
                            completed_plan = plan_from_draft(draft)
                        except _PLANNER_CLASSIFIED_ERRORS as exc:
                            failure = classify_planner_failure(exc)
                            retriable = (
                                failure.request_retry_disposition
                                is RequestRetryDisposition.RETRY_IN_REQUEST
                                and attempt_number < _MAX_ATTEMPTS
                            )
                            if retriable:
                                repair_context = str(exc)
                                continue
                            terminal_error = exc
                            terminal_outcome = PlanningFailed(
                                failure_code=failure.code,
                                attempt_count=attempt_count,
                            )
                            raise
                        break
            except _PLANNER_CLASSIFIED_ERRORS as exc:
                if exc is terminal_error and terminal_outcome is not None:
                    recording.set_outcome(terminal_outcome)
                raise

            if completed_plan is not None:
                recording.set_outcome(
                    PlanningSucceeded(
                        plan_type=completed_plan.plan_type,
                        attempt_count=attempt_count,
                    )
                )
                return completed_plan

            raise AssertionError("unreachable: planning loop must return or raise")
