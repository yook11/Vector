"""Input Safety Agentのwire contractとapplication contract。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

INPUT_SAFETY_TEXT_CHAR_CAP = 1000


class InputSafetyResult(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"


class InputSafetyBlockReason(StrEnum):
    DANGEROUS_OR_ILLEGAL_INSTRUCTIONS = "dangerous_or_illegal_instructions"
    CREDENTIAL_OR_PRIVACY_ABUSE = "credential_or_privacy_abuse"
    TARGETED_HATE_OR_HARASSMENT = "targeted_hate_or_harassment"
    SEXUAL_EXPLOITATION = "sexual_exploitation"
    SELF_HARM_INSTRUCTIONS = "self_harm_instructions"
    PROVIDER_SAFETY_FILTER = "provider_safety_filter"


INPUT_SAFETY_POLICY_BLOCK_REASONS = (
    InputSafetyBlockReason.DANGEROUS_OR_ILLEGAL_INSTRUCTIONS,
    InputSafetyBlockReason.CREDENTIAL_OR_PRIVACY_ABUSE,
    InputSafetyBlockReason.TARGETED_HATE_OR_HARASSMENT,
    InputSafetyBlockReason.SEXUAL_EXPLOITATION,
    InputSafetyBlockReason.SELF_HARM_INSTRUCTIONS,
)


class InputSafetyAgentOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_safety_result: InputSafetyResult
    block_reason: InputSafetyBlockReason | None = None

    @model_validator(mode="after")
    def validate_block_reason(self) -> Self:
        if self.input_safety_result is InputSafetyResult.BLOCK:
            if self.block_reason is None:
                raise ValueError("block result must include block reason")
            if self.block_reason not in INPUT_SAFETY_POLICY_BLOCK_REASONS:
                raise ValueError("agent output must include policy block reason")
        elif self.block_reason is not None:
            raise ValueError("allow result cannot include block reason")
        return self


class InputSafetyCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_safety_result: InputSafetyResult
    block_reason: InputSafetyBlockReason | None = None

    @property
    def is_blocked(self) -> bool:
        return self.input_safety_result is InputSafetyResult.BLOCK

    @model_validator(mode="after")
    def validate_block_reason(self) -> Self:
        if self.is_blocked:
            if self.block_reason is None:
                raise ValueError("block result must include block reason")
        elif self.block_reason is not None:
            raise ValueError("allow result cannot include block reason")
        return self


class InputSafetyPreviousTurn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    user_question: str = Field(
        min_length=1,
        max_length=INPUT_SAFETY_TEXT_CHAR_CAP,
    )
    assistant_answer: str | None = Field(
        default=None,
        min_length=1,
        max_length=INPUT_SAFETY_TEXT_CHAR_CAP,
    )


@dataclass(frozen=True, slots=True)
class InputSafetyAgentInput:
    question: str
    previous_turn: InputSafetyPreviousTurn | None


class InputSafetyChecker(Protocol):
    async def check(
        self,
        *,
        question: str,
        previous_turn: InputSafetyPreviousTurn | None,
        run_id: UUID,
    ) -> InputSafetyCheckResult: ...


class InputSafetyBlocked(Exception):
    def __init__(self, *, block_reason: InputSafetyBlockReason) -> None:
        self.block_reason = block_reason
        super().__init__("input safety blocked")
