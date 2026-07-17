"""1回の回答処理を進行する AnsweringRunner。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import logfire

from app.agent.contract import (
    AnswerGenerationStopped,
    AnswerQuestionInput,
    QuestionAnsweringAgent,
)
from app.agent.running.contract import (
    AnsweringRunContext,
    QuestionContextPreparer,
    RunContext,
    RunHooks,
    RunInput,
    RunResult,
)
from app.agent.threads.contracts import ThreadMessageSnapshot

__all__ = ["AnsweringRunner"]

_SPAN_NAME = "agent_answering_run"


class AnsweringRunner:
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
        with _answering_run_span(run_id=run_context.run_id):
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


@contextmanager
def _answering_run_span(*, run_id: UUID) -> Iterator[None]:
    """正常な停止制御を error にせず、同じ例外を span 終了後に再送出する。"""
    stopped: AnswerGenerationStopped | None = None
    with logfire.span(_SPAN_NAME, run_id=str(run_id)):
        try:
            yield
        except AnswerGenerationStopped as exc:
            stopped = exc
    if stopped is not None:
        raise stopped


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
