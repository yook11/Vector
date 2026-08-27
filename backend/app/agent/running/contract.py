"""1回の回答実行における phase contract。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.agent.answering.direct_answer.contract import DirectAnswerer
from app.agent.answering.evidence_answer.contract import EvidenceAnswerer
from app.agent.contract import AnswerQuestionResult, ResearchHandoff
from app.agent.evidence_collection import EvidenceCollector
from app.agent.evidence_review import EvidenceReviewer
from app.agent.planning.contract import QuestionPlanner
from app.agent.research_handoff.service import ResearchHandoffOrganizer
from app.agent.threads.contracts import ThreadMessageSnapshot

__all__ = [
    "AnsweringPhases",
    "AnsweringPhasesFactory",
    "RunIdentity",
    "RunInput",
    "RunResult",
]


@dataclass(frozen=True, slots=True)
class AnsweringPhases:
    planner: QuestionPlanner
    collector: EvidenceCollector
    direct_answerer: DirectAnswerer
    evidence_answerer: EvidenceAnswerer
    reviewer: EvidenceReviewer
    organizer: ResearchHandoffOrganizer


class AnsweringPhasesFactory(Protocol):
    def __call__(self) -> AnsweringPhases: ...


@dataclass(frozen=True, slots=True)
class RunInput:
    question: str
    history: tuple[ThreadMessageSnapshot, ...]
    # 同threadの調査の申し送り。読出し・検証失敗時はNone。
    research_handoff: ResearchHandoff | None = None


@dataclass(frozen=True, slots=True)
class RunIdentity:
    user_id: UUID
    run_id: UUID
    thread_id: UUID
    as_of: datetime


@dataclass(frozen=True, slots=True)
class RunResult:
    final_output: AnswerQuestionResult
    # 記録を追加できなかったRun(direct_answer含む)ではNone。書き込み側はNoneを
    # 「触らない」と解釈し、threadの既存handoffをそのまま残す。
    research_handoff: ResearchHandoff | None = None
