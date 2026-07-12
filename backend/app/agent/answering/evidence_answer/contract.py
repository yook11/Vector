"""Evidence answer contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.contract import NonBlankText

__all__ = [
    "EvidenceAnswerDraft",
    "EvidenceAnswerDraftGenerationInvalidError",
    "EvidenceAnswerDraftGenerator",
    "EvidenceAnswerDraftInvalidError",
    "EvidenceAnswerer",
    "EvidenceAnswerSufficiency",
    "RawEvidenceAnswerDraft",
]

EvidenceAnswerSufficiency = Literal["answered", "insufficient"]


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


class EvidenceAnswerDraftGenerator(Protocol):
    """LLM adapter boundary that streams an unvalidated evidence JSON envelope."""

    def stream(
        self,
        *,
        question: str,
        evidence: list[AnswerEvidenceItem],
        as_of: datetime,
        target_time_window: str | None,
        user_intent: str = "",
        prior_coverage: str = "",
        user_activity_context: str = "",
        previous_error: str | None = None,
    ) -> AsyncIterator[str]: ...


class EvidenceAnswerer(Protocol):
    """evidence に接地し、marker と cited refs が整合した draft を返す。"""

    async def answer(
        self,
        *,
        question: str,
        evidence: list[AnswerEvidenceItem],
        as_of: datetime,
        target_time_window: str | None,
        user_intent: str = "",
        prior_coverage: str = "",
        user_activity_context: str = "",
    ) -> EvidenceAnswerDraft: ...


class EvidenceAnswerDraftInvalidError(Exception):
    """draft が evidence への接地契約を破ったことを表す typed error。"""
