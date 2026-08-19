"""Thread-scoped question context preparation with a safe fallback."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import structlog
from pydantic import ValidationError

from app.agent.agent import Agent
from app.agent.phase_span import agent_phase
from app.agent.question_context.contract import (
    AnswerBrief,
    AnswerBriefDraft,
    QuestionContextGenerationInput,
    answer_brief_from_draft,
)
from app.agent.question_context.metrics import record_question_context_outcome
from app.agent.runtime.contract import (
    AgentResponseInvalidError,
    AgentRuntimeScopeFactory,
)
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.analysis.ai_provider_errors import AIProviderError

HISTORY_MESSAGE_LIMIT = 6
HISTORY_MESSAGE_CHAR_CAP = 2000
MISSING_ASPECT_CHAR_CAP = 300
MISSING_ASPECT_LIMIT = 8

logger = structlog.get_logger(__name__)
_RUNTIME_FAILURES = (
    AIProviderError,
    AgentResponseInvalidError,
)
_GENERATOR_UNAVAILABLE = "generator_unavailable"
_CONTEXT_FINALIZE_INVALID = "context_finalize_invalid"
_PROVIDER_ERROR = "provider_error"


class QuestionContextService:
    """Prepare question context while preserving existing behavior on failure."""

    def __init__(
        self,
        *,
        agent: Agent[QuestionContextGenerationInput, AnswerBriefDraft],
        runtime_scope_factory: AgentRuntimeScopeFactory | None,
    ) -> None:
        self._agent = agent
        self._runtime_scope_factory = runtime_scope_factory

    async def prepare(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
        run_id: UUID,
    ) -> AnswerBrief:
        if self._runtime_scope_factory is None:
            return _fallback_result(
                question=question,
                run_id=run_id,
                failure_code=_GENERATOR_UNAVAILABLE,
                prompt_version=self._agent.prompt.version,
                ai_model=self._agent.model.name,
            )

        with agent_phase(phase="context_resolution", agent_name=self._agent.name):
            try:
                async with self._runtime_scope_factory() as runtime:
                    draft = await runtime.call(
                        self._agent,
                        QuestionContextGenerationInput(
                            question=question,
                            history=tuple(_history_for_prompt(history)),
                            as_of=as_of,
                        ),
                        attempt_number=1,
                    )
            except _RUNTIME_FAILURES as exc:
                return _fallback_result(
                    question=question,
                    run_id=run_id,
                    failure_code=_failure_code(exc),
                    prompt_version=self._agent.prompt.version,
                    ai_model=self._agent.model.name,
                )

            try:
                answer_brief = answer_brief_from_draft(draft)
                if not history:
                    answer_brief = AnswerBrief(
                        standalone_question=question,
                        answer_requirements=answer_brief.answer_requirements,
                        relevant_prior_coverage="",
                        active_goal=answer_brief.active_goal,
                    )
            except ValidationError:
                return _fallback_result(
                    question=question,
                    run_id=run_id,
                    failure_code=_CONTEXT_FINALIZE_INVALID,
                    prompt_version=self._agent.prompt.version,
                    ai_model=self._agent.model.name,
                )
            record_question_context_outcome(
                result="prepared",
                prompt_version=self._agent.prompt.version,
                ai_model=self._agent.model.name,
            )
            return answer_brief


def _fallback_result(
    *,
    question: str,
    run_id: UUID,
    failure_code: str,
    prompt_version: str,
    ai_model: str,
) -> AnswerBrief:
    logger.warning(
        "question_context_preparation_failed",
        run_id=str(run_id),
        failure_type=failure_code,
    )
    record_question_context_outcome(
        result="failed",
        prompt_version=prompt_version,
        ai_model=ai_model,
        failure_code=failure_code,
    )
    return answer_brief_from_draft(AnswerBriefDraft(standalone_question=question))


def _failure_code(error: Exception) -> str:
    if isinstance(error, AgentResponseInvalidError):
        return error.defect.value
    code = getattr(error, "CODE", None)
    if isinstance(code, str) and code:
        return code
    return _PROVIDER_ERROR


def _history_for_prompt(
    history: list[ThreadMessageSnapshot],
) -> list[ThreadMessageSnapshot]:
    seen_missing_aspects: set[str] = set()
    prompt_history: list[ThreadMessageSnapshot] = []
    for message in history:
        missing_aspects: list[str] = []
        if message.role == "assistant":
            for missing_aspect in message.missing_aspects:
                normalized = _normalize_missing_aspect(missing_aspect)
                if (
                    normalized
                    and normalized not in seen_missing_aspects
                    and len(seen_missing_aspects) < MISSING_ASPECT_LIMIT
                ):
                    seen_missing_aspects.add(normalized)
                    missing_aspects.append(normalized)
        prompt_history.append(
            ThreadMessageSnapshot(
                role=message.role,
                content=message.content[:HISTORY_MESSAGE_CHAR_CAP],
                missing_aspects=tuple(missing_aspects),
            )
        )
    return prompt_history


def _normalize_missing_aspect(value: str) -> str:
    return value.strip()[:MISSING_ASPECT_CHAR_CAP].strip()
