"""回答準備後のrun lifecycle hook。"""

from __future__ import annotations

from app.agent.contract import AnswerEventReporter, QuestionResolvedEvent
from app.agent.question_context.contract import QuestionContext

__all__ = ["QuestionResolvedRunHooks"]


class QuestionResolvedRunHooks:
    def __init__(self, *, events: AnswerEventReporter) -> None:
        self._events = events

    async def on_answering_context_prepared(
        self,
        *,
        original_question: str,
        has_history: bool,
        question_context: QuestionContext,
    ) -> None:
        if not has_history:
            return
        if question_context.standalone_question.strip() == original_question.strip():
            return
        await self._events.event_occurred(
            QuestionResolvedEvent(
                standalone_question=question_context.standalone_question,
            )
        )
