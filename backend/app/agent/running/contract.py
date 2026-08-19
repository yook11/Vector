"""1回の回答実行における phase contract。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.agent.answering.direct_answer.contract import DirectAnswerer
from app.agent.answering.evidence_answer.contract import EvidenceAnswerer
from app.agent.contract import AnswerQuestionResult
from app.agent.evidence_collection import NewsCollector
from app.agent.evidence_collection.external_search import (
    ExternalResearchRuntimeFactory,
)
from app.agent.evidence_review import EvidenceReviewer
from app.agent.planning.contract import QuestionPlanner
from app.agent.question_context.contract import AnswerBrief
from app.agent.research_checkpoint import ResearchCheckpoint
from app.agent.threads.contracts import ThreadMessageSnapshot

__all__ = [
    "AnsweringPhases",
    "AnsweringPhasesFactory",
    "AnswerBriefPreparer",
    "RunHooks",
    "RunIdentity",
    "RunInput",
    "RunResult",
]


@dataclass(frozen=True, slots=True)
class AnsweringPhases:
    planner: QuestionPlanner
    collector: NewsCollector
    external_runtime_factory: ExternalResearchRuntimeFactory
    direct_answerer: DirectAnswerer
    evidence_answerer: EvidenceAnswerer
    reviewer: EvidenceReviewer


class AnsweringPhasesFactory(Protocol):
    def __call__(self) -> AnsweringPhases: ...


@dataclass(frozen=True, slots=True)
class RunInput:
    question: str
    history: tuple[ThreadMessageSnapshot, ...]
    # 同threadの直近checkpoint(新しい順)。読出し・検証失敗時は空。
    prior_research: tuple[ResearchCheckpoint, ...] = ()


@dataclass(frozen=True, slots=True)
class RunIdentity:
    user_id: UUID
    run_id: UUID
    thread_id: UUID
    as_of: datetime


@dataclass(frozen=True, slots=True)
class RunResult:
    final_output: AnswerQuestionResult
    answer_brief: AnswerBrief
    # 外部検索を実行しなかったRun(direct_answer含む)ではNone。
    research_checkpoint: ResearchCheckpoint | None = None


class AnswerBriefPreparer(Protocol):
    async def prepare(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
        run_id: UUID,
    ) -> AnswerBrief: ...


class RunHooks(Protocol):
    async def on_answer_brief_prepared(
        self,
        *,
        original_question: str,
        has_history: bool,
        answer_brief: AnswerBrief,
    ) -> None: ...
