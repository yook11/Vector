"""Answer synthesis port and draft contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.answering.evidence import AnswerEvidenceItem
from app.agent.contract import NonBlankText

__all__ = [
    "AnswerDraft",
    "AnswerDraftInvalidError",
    "EvidenceAnswerSynthesizer",
]


class AnswerDraft(BaseModel):
    """Evidence 回答工程 (LLM) の出力 draft。"""

    model_config = ConfigDict(frozen=True)

    sufficiency: Literal["answered", "insufficient"]
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


class EvidenceAnswerSynthesizer(Protocol):
    """evidence に接地し引用付きで回答する工程。"""

    async def synthesize(
        self,
        *,
        question: str,
        evidence: list[AnswerEvidenceItem],
        as_of: datetime,
        target_time_window: str | None,
    ) -> AnswerDraft: ...


class AnswerDraftInvalidError(Exception):
    """draft が evidence への接地契約を破ったことを表す typed error。"""
