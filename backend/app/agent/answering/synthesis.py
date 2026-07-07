"""Answer synthesis port and draft contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.answering.evidence import AnswerEvidenceItem

__all__ = [
    "AnswerDraft",
    "AnswerDraftInvalidError",
    "AnswerSynthesizer",
]


class AnswerDraft(BaseModel):
    """Synthesizer output before deterministic assembly."""

    model_config = ConfigDict(frozen=True)

    sufficiency: Literal["answered", "insufficient"]
    answer: str = Field(min_length=1)
    cited_refs: list[str] = Field(default_factory=list)
    missing_aspects: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_answered_has_no_missing_aspects(self) -> Self:
        if self.sufficiency == "answered" and self.missing_aspects:
            raise ValueError("answered draft cannot include missing aspects")
        return self


class AnswerSynthesizer(Protocol):
    async def synthesize(
        self,
        *,
        question: str,
        evidence: list[AnswerEvidenceItem],
        as_of: datetime,
        target_time_window: str | None,
    ) -> AnswerDraft: ...


class AnswerDraftInvalidError(Exception):
    """Raised when a draft violates evidence-grounding constraints."""
