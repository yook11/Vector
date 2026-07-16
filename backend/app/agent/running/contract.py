"""1回の回答実行における phase contract。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.agent.contract import AnswerQuestionResult
from app.agent.question_context.contract import (
    QuestionContext,
    QuestionContextPreparationResult,
)
from app.agent.threads.contracts import ThreadMessageSnapshot

__all__ = [
    "AnsweringRunContext",
    "QuestionContextPreparer",
    "RunContext",
    "RunHooks",
    "RunInput",
    "RunResult",
]


@dataclass(frozen=True, slots=True)
class RunInput:
    question: str
    history: tuple[ThreadMessageSnapshot, ...]


@dataclass(frozen=True, slots=True)
class RunContext:
    run_id: UUID
    as_of: datetime


@dataclass(frozen=True, slots=True)
class AnsweringRunContext:
    run_context: RunContext
    question_context: QuestionContext
    previous_answer: str


@dataclass(frozen=True, slots=True)
class RunResult:
    final_output: AnswerQuestionResult
    context: AnsweringRunContext


class QuestionContextPreparer(Protocol):
    async def prepare(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
        run_id: UUID,
    ) -> QuestionContextPreparationResult: ...


class RunHooks(Protocol):
    async def on_answering_context_prepared(
        self,
        *,
        original_question: str,
        has_history: bool,
        question_context: QuestionContext,
    ) -> None: ...
