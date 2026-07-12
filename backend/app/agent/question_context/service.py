"""Thread-scoped question context preparation with a safe fallback."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import structlog
from pydantic import ValidationError

from app.agent.question_context.contract import (
    QuestionContext,
    QuestionContextGenerator,
    QuestionContextResponseInvalidError,
    question_context_from_draft,
)
from app.agent.question_context.metrics import record_question_context_outcome
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.analysis.ai_provider_errors import AIProviderError

HISTORY_MESSAGE_LIMIT = 6
HISTORY_MESSAGE_CHAR_CAP = 2000

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
    ) -> QuestionContext:
        if not history:
            record_question_context_outcome(result="skipped")
            return _passthrough(question)
        if self._generator is None:
            raise RuntimeError("generator is required when history is present")

        try:
            draft = await self._generator.generate(
                question=question,
                history=_history_for_prompt(history),
                as_of=as_of,
            )
            context = question_context_from_draft(draft)
        except _CONTEXT_FAILURES as exc:
            logger.warning(
                "question_resolution_failed",
                run_id=str(run_id),
                failure_type=exc.__class__.__name__,
            )
            record_question_context_outcome(result="failed")
            return _passthrough(question)

        record_question_context_outcome(result="resolved")
        return context


def _passthrough(question: str) -> QuestionContext:
    return QuestionContext(standalone_question=question)


def _history_for_prompt(
    history: list[ThreadMessageSnapshot],
) -> list[ThreadMessageSnapshot]:
    return [
        ThreadMessageSnapshot(
            role=message.role,
            content=message.content[:HISTORY_MESSAGE_CHAR_CAP],
        )
        for message in history
    ]
