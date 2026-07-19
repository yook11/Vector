"""1回の回答実行における phase contract。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.agent.answering.direct_answer.contract import DirectAnswerer
from app.agent.answering.evidence_answer.contract import EvidenceAnswerer
from app.agent.contract import AnswerQuestionResult
from app.agent.evidence_collection.contract import EvidenceCollector
from app.agent.planning.contract import QuestionPlanner
from app.agent.question_context.contract import (
    QuestionContext,
    QuestionContextPreparationResult,
)
from app.agent.threads.contracts import ThreadMessageSnapshot

__all__ = [
    "AnsweringPhases",
    "AnsweringPhasesFactory",
    "AnsweringRunContext",
    "QuestionContextPreparer",
    "RunContext",
    "RunHooks",
    "RunInput",
    "RunResult",
]


@dataclass(frozen=True, slots=True)
class AnsweringPhases:
    planner: QuestionPlanner
    evidence_collector: EvidenceCollector
    direct_answerer: DirectAnswerer
    evidence_answerer: EvidenceAnswerer


class AnsweringPhasesFactory(Protocol):
    def __call__(self) -> AnsweringPhases: ...


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
