"""Thread-scoped question context preparation with a safe fallback."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID

import logfire
import structlog
from pydantic import ValidationError

from app.agent.agent import Agent
from app.agent.question_context.contract import (
    QuestionContext,
    QuestionContextDraft,
    QuestionContextGenerationInput,
    QuestionContextPreparationResult,
    QuestionContextTelemetry,
    question_context_from_draft,
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
_PHASE_SPAN_NAME = "agent_phase"
_GENERATOR_UNAVAILABLE = "generator_unavailable"
_CONTEXT_FINALIZE_INVALID = "context_finalize_invalid"
_PROVIDER_ERROR = "provider_error"


class QuestionContextService:
    """Prepare question context while preserving existing behavior on failure."""

    def __init__(
        self,
        *,
        agent: Agent[QuestionContextGenerationInput, QuestionContextDraft],
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
    ) -> QuestionContextPreparationResult:
        previous_answer_had_missing_aspects = _latest_assistant_has_missing_aspects(
            history
        )
        if self._runtime_scope_factory is None:
            return _fallback_result(
                question=question,
                run_id=run_id,
                failure_code=_GENERATOR_UNAVAILABLE,
                previous_answer_had_missing_aspects=previous_answer_had_missing_aspects,
                prompt_version=self._agent.prompt.version,
                ai_model=self._agent.model.name,
            )

        with _question_context_phase(self._agent.name):
            try:
                async with self._runtime_scope_factory() as runtime:
                    draft = await runtime.invoke(
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
                    previous_answer_had_missing_aspects=(
                        previous_answer_had_missing_aspects
                    ),
                    prompt_version=self._agent.prompt.version,
                    ai_model=self._agent.model.name,
                )

            try:
                context = question_context_from_draft(draft)
                if not history:
                    context = QuestionContext(
                        standalone_question=question,
                        content_requirements=context.content_requirements,
                        response_requirements=context.response_requirements,
                        relevant_prior_coverage="",
                        active_goal=context.active_goal,
                    )
            except ValidationError:
                return _fallback_result(
                    question=question,
                    run_id=run_id,
                    failure_code=_CONTEXT_FINALIZE_INVALID,
                    previous_answer_had_missing_aspects=(
                        previous_answer_had_missing_aspects
                    ),
                    prompt_version=self._agent.prompt.version,
                    ai_model=self._agent.model.name,
                )
            telemetry = QuestionContextTelemetry(
                explicit_feedback_detected=(
                    draft.explicit_feedback_detected if history else False
                ),
                previous_answer_had_missing_aspects=(
                    previous_answer_had_missing_aspects if history else False
                ),
            )
            record_question_context_outcome(
                result="prepared",
                explicit_feedback_detected=telemetry.explicit_feedback_detected,
                previous_answer_had_missing_aspects=(
                    telemetry.previous_answer_had_missing_aspects
                ),
                prompt_version=self._agent.prompt.version,
                ai_model=self._agent.model.name,
            )
            return QuestionContextPreparationResult(
                context=context,
                telemetry=telemetry,
            )


def _fallback_result(
    *,
    question: str,
    run_id: UUID,
    failure_code: str,
    previous_answer_had_missing_aspects: bool,
    prompt_version: str,
    ai_model: str,
) -> QuestionContextPreparationResult:
    logger.warning(
        "question_context_preparation_failed",
        run_id=str(run_id),
        failure_type=failure_code,
    )
    telemetry = QuestionContextTelemetry(
        previous_answer_had_missing_aspects=previous_answer_had_missing_aspects
    )
    record_question_context_outcome(
        result="failed",
        explicit_feedback_detected=telemetry.explicit_feedback_detected,
        previous_answer_had_missing_aspects=(
            telemetry.previous_answer_had_missing_aspects
        ),
        prompt_version=prompt_version,
        ai_model=ai_model,
        failure_code=failure_code,
    )
    return QuestionContextPreparationResult(
        context=question_context_from_draft(
            QuestionContextDraft(
                standalone_question=question,
                content_requirements=[question],
            )
        ),
        telemetry=telemetry,
    )


def _failure_code(error: Exception) -> str:
    if isinstance(error, AgentResponseInvalidError):
        return error.defect.value
    code = getattr(error, "CODE", None)
    if isinstance(code, str) and code:
        return code
    return _PROVIDER_ERROR


@contextmanager
def _question_context_phase(agent_name: str) -> Iterator[None]:
    with logfire.span(
        _PHASE_SPAN_NAME,
        phase="question_context",
        agent_name=agent_name,
    ):
        yield


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


def _latest_assistant_has_missing_aspects(
    history: list[ThreadMessageSnapshot],
) -> bool:
    for message in reversed(history):
        if message.role == "assistant":
            return bool(message.missing_aspects)
    return False
