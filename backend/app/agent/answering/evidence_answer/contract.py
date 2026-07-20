"""Evidence answer contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.contract import NonBlankText
from app.agent.planning.contract import TargetTimeWindow

__all__ = [
    "EvidenceAnswerDraft",
    "EvidenceAnswerDraftGenerationInvalidError",
    "EvidenceAnswerDraftInvalidError",
    "EvidenceAnswerInput",
    "EvidenceAnswerer",
    "EvidenceAnswerSufficiency",
    "RawEvidenceAnswerDraft",
]

EvidenceAnswerSufficiency = Literal["answered", "insufficient"]


@dataclass(frozen=True, slots=True)
class EvidenceAnswerInput:
    """Evidence Answer Agentの1 attempt input。"""

    request: AnsweringRequest
    evidence: tuple[AnswerEvidenceItem, ...]
    target_time_window: TargetTimeWindow | None
    previous_error: str | None = None


class EvidenceAnswerDraftGenerationInvalidError(ValueError):
    """LLM response envelope が raw draft として消化できない。"""

    def __init__(self, defect_code: str) -> None:
        self.defect_code = defect_code
        super().__init__(defect_code)


class EvidenceAnswerDraft(BaseModel):
    """Evidence 回答工程 (LLM) の出力 draft。"""

    model_config = ConfigDict(frozen=True)

    sufficiency: EvidenceAnswerSufficiency
    answer: NonBlankText
    cited_refs: list[str] = Field(default_factory=list)
    missing_aspects: list[NonBlankText] = Field(default_factory=list)
    unfulfilled_requirement_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_sufficiency_contract(self) -> Self:
        if self.sufficiency == "answered":
            if self.missing_aspects:
                raise ValueError("answered draft cannot include missing aspects")
            if not self.cited_refs:
                raise ValueError("answered draft requires at least one citation")
        if self.sufficiency == "insufficient" and not self.missing_aspects:
            raise ValueError("insufficient draft must include missing aspects")
        return self


class RawEvidenceAnswerDraft(BaseModel):
    """LLM adapter boundary の lenient evidence answer draft。"""

    model_config = ConfigDict(frozen=True)

    sufficiency: object | None = None
    answer: object | None = None
    cited_refs: list[object] = Field(default_factory=list)
    missing_aspects: list[object] = Field(default_factory=list)
    unfulfilled_requirement_ids: list[object] = Field(default_factory=list)


class EvidenceAnswerer(Protocol):
    """markerとcited refsが整合し、unfulfilled_requirement_idsが
    request contextの入力requirement IDの部分集合であるdraftを返す。
    """

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[AnswerEvidenceItem],
        target_time_window: TargetTimeWindow | None,
    ) -> EvidenceAnswerDraft: ...


class EvidenceAnswerDraftInvalidError(Exception):
    """draft が evidence への接地契約を破ったことを表す typed error。"""
