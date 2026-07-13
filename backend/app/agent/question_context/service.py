"""Thread-scoped question context preparation with a safe fallback."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import structlog
from pydantic import ValidationError

from app.agent.question_context.contract import (
    QuestionContext,
    QuestionContextDraft,
    QuestionContextGenerator,
    QuestionContextPreparationResult,
    QuestionContextResponseInvalidError,
    QuestionContextTelemetry,
    question_context_from_draft,
)
from app.agent.question_context.metrics import record_question_context_outcome
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.analysis.ai_provider_errors import AIProviderError

HISTORY_MESSAGE_LIMIT = 6
HISTORY_MESSAGE_CHAR_CAP = 2000
MISSING_ASPECT_CHAR_CAP = 300
MISSING_ASPECT_LIMIT = 8

logger = structlog.get_logger(__name__)
_CONTEXT_FAILURES = (
    AIProviderError,
    QuestionContextResponseInvalidError,
    ValidationError,
)


class QuestionContextService:
    """Prepare question context while preserving existing behavior on failure."""

    def __init__(self, *, generator: QuestionContextGenerator | None) -> None:
        self._generator = generator

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
        if self._generator is None:
            return _fallback_result(
                question=question,
                run_id=run_id,
                failure_type="generator_unavailable",
                previous_answer_had_missing_aspects=previous_answer_had_missing_aspects,
            )

        try:
            draft = await self._generator.generate(
                question=question,
                history=_history_for_prompt(history),
                as_of=as_of,
            )
            context = question_context_from_draft(draft)
        except _CONTEXT_FAILURES as exc:
            return _fallback_result(
                question=question,
                run_id=run_id,
                failure_type=exc.__class__.__name__,
                previous_answer_had_missing_aspects=previous_answer_had_missing_aspects,
            )

        if not history:
            context = QuestionContext(
                standalone_question=question,
                content_requirements=context.content_requirements,
                response_requirements=context.response_requirements,
                relevant_prior_coverage="",
                active_goal=context.active_goal,
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
        )
        return QuestionContextPreparationResult(context=context, telemetry=telemetry)


def _fallback_result(
    *,
    question: str,
    run_id: UUID,
    failure_type: str,
    previous_answer_had_missing_aspects: bool,
) -> QuestionContextPreparationResult:
    logger.warning(
        "question_context_preparation_failed",
        run_id=str(run_id),
        failure_type=failure_type,
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
