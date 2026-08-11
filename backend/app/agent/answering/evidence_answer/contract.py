"""Evidence answer contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.contract import NonBlankText
from app.agent.planning.contract import TargetTimeWindow

__all__ = [
    "EvidenceAnswerDraft",
    "EvidenceAnswerDraftInvalidError",
    "EvidenceAnswerInput",
    "EvidenceAnswerOutcome",
    "EvidenceAnswerer",
    "EvidenceAnswerUnavailable",
]


@dataclass(frozen=True, slots=True)
class EvidenceAnswerInput:
    """Evidence Answer Agentの1 attempt input。"""

    request: AnsweringRequest
    evidence: tuple[AnswerEvidenceItem, ...]
    target_time_window: TargetTimeWindow | None
    # 前attemptが失敗した場合のみ、どこで何が失敗したかが入る(初回attemptはNone)。
    repair_context: str | None = None
    previous_output_truncated: bool = False
    review_missing: tuple[str, ...] = ()


class EvidenceAnswerDraft(BaseModel):
    """Evidence 回答工程 (LLM) の出力 draft。本文と算出済みcited_refsだけを持つ。"""

    model_config = ConfigDict(frozen=True)

    answer: NonBlankText
    cited_refs: list[str] = Field(default_factory=list)


class EvidenceAnswerUnavailable(BaseModel):
    """生成が尽きた結果。回答draftとは別の型で表す。

    ユーザーへ見せる定型本文とmissing_aspectsの1行はresult_assemblyが所有する。
    """

    model_config = ConfigDict(frozen=True)

    failure_code: NonBlankText


EvidenceAnswerOutcome = EvidenceAnswerDraft | EvidenceAnswerUnavailable


class EvidenceAnswerer(Protocol):
    """本文とcited refsが整合するdraft、または生成不能を返す。"""

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[AnswerEvidenceItem],
        target_time_window: TargetTimeWindow | None,
        review_missing: tuple[str, ...],
    ) -> EvidenceAnswerOutcome: ...


class EvidenceAnswerDraftInvalidError(Exception):
    """draft が evidence への接地契約を破ったことを表す typed error。"""
