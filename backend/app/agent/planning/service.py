"""Question planning service."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import logfire
from opentelemetry.trace import StatusCode

from app.agent.agent import Agent
from app.agent.planning.contract import (
    PlanningAttemptInput,
    PlanningRequest,
    QuestionPlan,
    QuestionPlanDraft,
    plan_from_draft,
    safe_fallback_plan,
)
from app.agent.planning.failure import (
    RequestRetryDisposition,
    classify_planner_failure,
)
from app.agent.planning.metrics import record_question_planner_outcome
from app.agent.runtime.contract import (
    AgentResponseInvalidError,
    AgentRuntimeScopeFactory,
)
from app.analysis.ai_provider_errors import AIProviderError

_PLANNER_CLASSIFIED_ERRORS = (
    AIProviderError,
    AgentResponseInvalidError,
)
_MAX_ATTEMPTS = 2
_PHASE_SPAN_NAME = "agent_phase"


class QuestionPlanningService:
    """Create completed question plans from LLM drafts."""

    def __init__(
        self,
        *,
        agent: Agent[PlanningAttemptInput, QuestionPlanDraft],
        runtime_scope_factory: AgentRuntimeScopeFactory,
    ) -> None:
        self._agent = agent
        self._runtime_scope_factory = runtime_scope_factory

    async def plan(self, request: PlanningRequest) -> QuestionPlan:
        """Return a completed plan, retrying only response-shape failures."""

        previous_error: str | None = None

        with _planning_phase(self._agent.name):
            async with self._runtime_scope_factory() as runtime:
                for attempt_number in range(1, _MAX_ATTEMPTS + 1):
                    try:
                        draft = await runtime.invoke(
                            self._agent,
                            PlanningAttemptInput(
                                request=request,
                                previous_error=previous_error,
                            ),
                            attempt_number=attempt_number,
                        )
                    except _PLANNER_CLASSIFIED_ERRORS as exc:
                        failure = classify_planner_failure(exc)
                        retriable = (
                            failure.request_retry_disposition
                            is RequestRetryDisposition.RETRY_IN_REQUEST
                            and attempt_number < _MAX_ATTEMPTS
                        )
                        if retriable:
                            previous_error = str(exc)
                            continue
                        fallback = safe_fallback_plan(
                            fallback_query=request.context.standalone_question
                        )
                        record_question_planner_outcome(
                            result="fallback",
                            retry_used=attempt_number > 1,
                            planned_retrieval_mode=fallback.retrieval_mode,
                            failure_code=failure.code,
                        )
                        return fallback

                    plan = plan_from_draft(
                        draft,
                        fallback_query=request.context.standalone_question,
                    )
                    record_question_planner_outcome(
                        result="planned",
                        retry_used=attempt_number > 1,
                        planned_retrieval_mode=plan.retrieval_mode,
                    )
                    return plan

                raise AssertionError("unreachable: planning loop must return or raise")


@contextmanager
def _planning_phase(agent_name: str) -> Iterator[None]:
    """Planner policy spanへ分類不能な終了だけをerrorとして残す。"""
    with logfire.span(
        _PHASE_SPAN_NAME,
        phase="question_planning",
        agent_name=agent_name,
    ) as span:
        try:
            yield
        except BaseException:
            _record_unclassified_phase_error(span)
            raise


def _record_unclassified_phase_error(span: Any) -> None:
    """LogfireSpanはOTel委譲(__getattr__)を型から隠すためAnyで受ける。"""
    span.set_status(StatusCode.ERROR, "unclassified agent phase error")
