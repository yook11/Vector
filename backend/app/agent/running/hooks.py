"""回答準備後のrun lifecycle hook。"""

from __future__ import annotations

from app.agent.contract import AnswerEventReporter, QuestionResolvedEvent
from app.agent.question_context.contract import AnswerBrief

__all__ = ["QuestionResolvedRunHooks"]


class QuestionResolvedRunHooks:
    def __init__(self, *, events: AnswerEventReporter) -> None:
        self._events = events

    async def on_answer_brief_prepared(
        self,
        *,
        original_question: str,
        has_history: bool,
        answer_brief: AnswerBrief,
    ) -> None:
        if not has_history:
            return
        if answer_brief.standalone_question.strip() == original_question.strip():
            return
        await self._events.event_occurred(
            QuestionResolvedEvent(
                standalone_question=answer_brief.standalone_question,
            )
        )
