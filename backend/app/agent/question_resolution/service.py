"""Thread-scoped question resolution with a safe passthrough fallback."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import structlog
from pydantic import ValidationError

from app.agent.question_resolution.contract import (
    QuestionResolutionResponseInvalidError,
    QuestionResolver,
    ResolvedQuestion,
    resolved_question_from_draft,
)
from app.agent.question_resolution.metrics import record_question_resolution_outcome
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.analysis.ai_provider_errors import AIProviderError

HISTORY_MESSAGE_LIMIT = 6
HISTORY_MESSAGE_CHAR_CAP = 2000

logger = structlog.get_logger(__name__)
_RESOLUTION_FAILURES = (
    AIProviderError,
    QuestionResolutionResponseInvalidError,
    ValidationError,
)


class QuestionResolutionService:
    """Resolve follow-up questions while preserving existing behavior on failure."""

    def __init__(self, *, resolver: QuestionResolver | None) -> None:
        self._resolver = resolver

    async def resolve(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
        run_id: UUID,
    ) -> ResolvedQuestion:
        if not history:
            record_question_resolution_outcome(result="skipped")
            return _passthrough(question)
        if self._resolver is None:
            raise RuntimeError("resolver is required when history is present")

        try:
            draft = await self._resolver.resolve(
                question=question,
                history=_history_for_prompt(history),
                as_of=as_of,
            )
            resolved = resolved_question_from_draft(draft)
        except _RESOLUTION_FAILURES as exc:
            logger.warning(
                "question_resolution_failed",
                run_id=str(run_id),
                failure_type=exc.__class__.__name__,
            )
            record_question_resolution_outcome(result="failed")
            return _passthrough(question)

        record_question_resolution_outcome(result="resolved")
        return resolved


def _passthrough(question: str) -> ResolvedQuestion:
    return ResolvedQuestion(standalone_question=question)


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
