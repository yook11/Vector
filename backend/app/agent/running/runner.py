"""1回の回答処理を進行する pure application Runner。"""

from __future__ import annotations

from app.agent.contract import AnswerQuestionInput, QuestionAnsweringAgent
from app.agent.running.contract import (
    AnsweringRunContext,
    QuestionContextPreparer,
    RunContext,
    RunHooks,
    RunInput,
    RunResult,
)
from app.agent.threads.contracts import ThreadMessageSnapshot

__all__ = ["Runner"]


class Runner:
    def __init__(
        self,
        *,
        context_preparer: QuestionContextPreparer,
    ) -> None:
        self._context_preparer = context_preparer

    async def run(
        self,
        starting_agent: QuestionAnsweringAgent,
        input: RunInput,
        *,
        run_context: RunContext,
        hooks: RunHooks | None = None,
    ) -> RunResult:
        preparation = await self._context_preparer.prepare(
            question=input.question,
            history=list(input.history),
            as_of=run_context.as_of,
            run_id=run_context.run_id,
        )
        answering_context = AnsweringRunContext(
            run_context=run_context,
            question_context=preparation.context,
            previous_answer=_latest_assistant_answer(input.history),
        )
        if hooks is not None:
            await hooks.on_answering_context_prepared(
                original_question=input.question,
                has_history=bool(input.history),
                question_context=answering_context.question_context,
            )
        final_output = await starting_agent.answer(
            AnswerQuestionInput(
                context=answering_context.question_context,
                as_of=answering_context.run_context.as_of,
                previous_answer=answering_context.previous_answer,
            )
        )
        return RunResult(
            final_output=final_output,
            context=answering_context,
        )


def _latest_assistant_answer(
    history: tuple[ThreadMessageSnapshot, ...],
) -> str:
    return next(
        (
            message.content
            for message in reversed(history)
            if message.role == "assistant"
        ),
        "",
    )
