"""Question planning service."""

from __future__ import annotations

from typing import assert_never

from pydantic import ValidationError

from app.agent.planning.audit import (
    PlannerAttemptFailureEvent,
    PlannerAuditRecorder,
    PlannerDraftReceivedEvent,
    PlannerFailureAttributes,
    PlannerFinalEvent,
    RequestRetryDisposition,
    classify_planner_failure,
)
from app.agent.planning.contract import (
    ExternalSearchPlan,
    InternalAndExternalPlan,
    InternalRetrievalPlan,
    NoRetrievalPlan,
    PlanningRequest,
    QuestionPlan,
    QuestionPlanDraft,
    QuestionPlanDraftGenerator,
    QuestionPlannerResponseInvalidError,
    plan_from_draft,
    safe_fallback_plan,
)
from app.agent.planning.metrics import record_question_planner_outcome
from app.analysis.ai_provider_errors import AIProviderError

_PLANNER_AUDITED_ERRORS = (
    AIProviderError,
    QuestionPlannerResponseInvalidError,
    ValidationError,
)
_MAX_ATTEMPTS = 2


class QuestionPlanningService:
    """Create completed question plans from LLM drafts."""

    def __init__(
        self,
        *,
        planner: QuestionPlanDraftGenerator,
        audit_recorder: PlannerAuditRecorder | None = None,
    ) -> None:
        self._planner = planner
        self._audit_recorder = audit_recorder

    async def plan(self, request: PlanningRequest) -> QuestionPlan:
        """Return a completed plan, retrying only response-shape failures."""

        ai_model = _planner_attr(self._planner, "model_name")
        prompt_version = _planner_attr(self._planner, "prompt_version")
        previous_error: str | None = None

        for attempt_number in range(1, _MAX_ATTEMPTS + 1):
            try:
                draft = await self._planner.plan(
                    request,
                    previous_error=previous_error,
                )
            except _PLANNER_AUDITED_ERRORS as exc:
                failure = classify_planner_failure(exc)
                await _record_attempt_failure(
                    audit_recorder=self._audit_recorder,
                    attempt_number=attempt_number,
                    failure=failure,
                    ai_model=ai_model,
                    prompt_version=prompt_version,
                )
                retriable = (
                    failure.request_retry_disposition
                    is RequestRetryDisposition.RETRY_IN_REQUEST
                    and attempt_number < _MAX_ATTEMPTS
                )
                if retriable:
                    previous_error = str(exc)
                    continue
                return await _fallback_with_audit(
                    request=request,
                    audit_recorder=self._audit_recorder,
                    attempt_count=attempt_number,
                    retry_used=attempt_number > 1,
                    failure=failure,
                    ai_model=ai_model,
                    prompt_version=prompt_version,
                )

            await _record_draft_received(
                audit_recorder=self._audit_recorder,
                draft=draft,
                attempt_number=attempt_number,
                ai_model=ai_model,
                prompt_version=prompt_version,
            )
            plan = plan_from_draft(
                draft,
                fallback_query=request.context.standalone_question,
            )
            await _record_plan_created(
                audit_recorder=self._audit_recorder,
                plan=plan,
                attempt_count=attempt_number,
                retry_used=attempt_number > 1,
                ai_model=ai_model,
                prompt_version=prompt_version,
            )
            record_question_planner_outcome(
                result="planned",
                retry_used=attempt_number > 1,
                planned_retrieval_mode=plan.retrieval_mode,
            )
            return plan

        raise AssertionError("unreachable: planning loop must return or raise")


def _planner_attr(planner: QuestionPlanDraftGenerator, name: str) -> str | None:
    value = getattr(planner, name, None)
    return value if isinstance(value, str) else None


async def _record_draft_received(
    *,
    audit_recorder: PlannerAuditRecorder | None,
    draft: QuestionPlanDraft,
    attempt_number: int,
    ai_model: str | None,
    prompt_version: str | None,
) -> None:
    if audit_recorder is None:
        return
    event = PlannerDraftReceivedEvent(
        attempt_number=attempt_number,
        retrieval_mode=draft.retrieval_mode,
        draft_internal_query_count=len(draft.internal_queries),
        draft_external_query_count=len(draft.external_collection_goals),
        ai_model=ai_model,
        prompt_version=prompt_version,
    )
    try:
        await audit_recorder.record_draft_received(event)
    except Exception:
        return


async def _record_attempt_failure(
    *,
    audit_recorder: PlannerAuditRecorder | None,
    attempt_number: int,
    failure: PlannerFailureAttributes,
    ai_model: str | None,
    prompt_version: str | None,
) -> None:
    if audit_recorder is None:
        return
    event = PlannerAttemptFailureEvent.from_failure(
        attempt_number=attempt_number,
        failure=failure,
        ai_model=ai_model,
        prompt_version=prompt_version,
    )
    try:
        await audit_recorder.record_attempt_failure(event)
    except Exception:
        return


async def _record_plan_created(
    *,
    audit_recorder: PlannerAuditRecorder | None,
    plan: QuestionPlan,
    attempt_count: int,
    retry_used: bool,
    ai_model: str | None,
    prompt_version: str | None,
) -> None:
    if audit_recorder is None:
        return
    internal_query_count, external_query_count = _plan_query_counts(plan)
    event = PlannerFinalEvent.plan_created(
        attempt_count=attempt_count,
        retry_used=retry_used,
        retrieval_mode=plan.retrieval_mode,
        internal_query_count=internal_query_count,
        external_query_count=external_query_count,
        ai_model=ai_model,
        prompt_version=prompt_version,
    )
    await _record_final_event(audit_recorder, event)


async def _record_final_event(
    audit_recorder: PlannerAuditRecorder | None,
    event: PlannerFinalEvent,
) -> None:
    if audit_recorder is None:
        return
    try:
        await audit_recorder.record_final_event(event)
    except Exception:
        return


async def _fallback_with_audit(
    *,
    request: PlanningRequest,
    audit_recorder: PlannerAuditRecorder | None,
    attempt_count: int,
    retry_used: bool,
    failure: PlannerFailureAttributes,
    ai_model: str | None,
    prompt_version: str | None,
) -> QuestionPlan:
    fallback = safe_fallback_plan(fallback_query=request.context.standalone_question)
    if audit_recorder is not None:
        internal_query_count, external_query_count = _plan_query_counts(fallback)
        event = PlannerFinalEvent.fallback(
            attempt_count=attempt_count,
            retry_used=retry_used,
            retrieval_mode=fallback.retrieval_mode,
            internal_query_count=internal_query_count,
            external_query_count=external_query_count,
            failure=failure,
            ai_model=ai_model,
            prompt_version=prompt_version,
        )
        await _record_final_event(audit_recorder, event)
    record_question_planner_outcome(
        result="fallback",
        retry_used=retry_used,
        planned_retrieval_mode=fallback.retrieval_mode,
    )
    return fallback


def _plan_query_counts(plan: QuestionPlan) -> tuple[int, int]:
    match plan:
        case NoRetrievalPlan():
            return 0, 0
        case InternalRetrievalPlan(internal_queries=internal_queries):
            return len(internal_queries), 0
        case ExternalSearchPlan(external_research_tasks=external_research_tasks):
            return 0, len(external_research_tasks)
        case InternalAndExternalPlan(
            internal_queries=internal_queries,
            external_research_tasks=external_research_tasks,
        ):
            return len(internal_queries), len(external_research_tasks)
        case _ as unreachable:
            assert_never(unreachable)
