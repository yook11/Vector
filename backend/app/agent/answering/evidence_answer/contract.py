"""Evidence answer contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.evidence import AnswerInputEvidence
from app.agent.contract import NonBlankText
from app.agent.planning.contract import TargetTimeWindow

__all__ = [
    "EvidenceAnswerDraft",
    "EvidenceAnswerDraftInvalidError",
    "EvidenceAnswerInput",
    "EvidenceAnswerer",
]


@dataclass(frozen=True, slots=True)
class EvidenceAnswerInput:
    """この工程が受け取り、Agent に渡す入力。"""

    request: AnsweringRequest
    evidence: tuple[AnswerInputEvidence, ...]
    target_time_window: TargetTimeWindow | None
    review_missing: tuple[str, ...]
    previous_output_truncated: bool = False


class EvidenceAnswerDraft(BaseModel):
    """Evidence 回答工程 (LLM) の出力 draft。本文と算出済みcited_refsだけを持つ。"""

    model_config = ConfigDict(frozen=True)

    answer: NonBlankText
    cited_refs: list[str] = Field(default_factory=list)


class EvidenceAnswerer(Protocol):
    """本文とcited refsが整合するdraftを返し、生成不能は例外で通知する。"""

    async def answer(self, input: EvidenceAnswerInput) -> EvidenceAnswerDraft: ...


class EvidenceAnswerDraftInvalidError(Exception):
    """draft が evidence への接地契約を破ったことを表す typed error。"""
