"""Question planning service."""

from __future__ import annotations

import asyncio
from typing import Literal

from app.agent.agent import Agent
from app.agent.contract import PlanType
from app.agent.phase_span import agent_phase
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
from app.agent.planning.metrics import PlannerOutcomeResult
from app.agent.recording.planning import (
    PlanningRecorder,
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
        terminal_failure_code: str | None = None
        retry_used = False
        outcome: PlannerOutcomeResult | None = None
        plan_type: PlanType | Literal["not_created"] | None = None
        failure_code: str | None = None
        stopped = False
        call = await self._recorder.start()

        try:
            with agent_phase(phase="planning", agent_name=self._agent.name):
                try:
                    async with self._runtime_scope_factory() as runtime:
                        for attempt_number in range(1, _MAX_ATTEMPTS + 1):
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
                                    retry_used = True
                                    continue
                                terminal_error = exc
                                terminal_failure_code = failure.code
                                raise
                            retry_used = attempt_number > 1
                            break
                except _PLANNER_CLASSIFIED_ERRORS as exc:
                    if exc is terminal_error:
                        outcome = "failed"
                        plan_type = "not_created"
                        failure_code = terminal_failure_code
                    raise

                if completed_plan is not None:
                    outcome = "planned"
                    plan_type = completed_plan.plan_type
                    return completed_plan

                raise AssertionError("unreachable: planning loop must return or raise")
        except (asyncio.CancelledError, GeneratorExit):
            stopped = True
            outcome = None
            plan_type = None
            failure_code = None
            raise
        finally:
            await self._recorder.end(
                call,
                outcome=outcome,
                retry_used=retry_used,
                plan_type=plan_type,
                failure_code=failure_code,
                stopped=stopped,
            )
