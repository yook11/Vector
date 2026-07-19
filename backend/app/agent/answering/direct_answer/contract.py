"""Direct answer contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.agent.answering.contract import AnsweringRequest
from app.agent.contract import AnswerGenerationStopped, NonBlankText

__all__ = [
    "AnswerGenerationStopped",
    "DirectAnswerInput",
    "DirectAnswerDraft",
    "DirectAnswerer",
    "DirectAnswerInvalidError",
]

_DIRECT_ANSWER_BLANK_RESPONSE = "direct_answer_blank_response"


class DirectAnswerInvalidError(ValueError):
    """Direct answer response が answer draft として消化できない。"""

    def __init__(self, code: str = _DIRECT_ANSWER_BLANK_RESPONSE) -> None:
        self.code = code
        super().__init__(code)


class DirectAnswerDraft(BaseModel):
    """Direct 回答工程 (LLM) の出力 draft。"""

    model_config = ConfigDict(frozen=True)

    answer: NonBlankText


@dataclass(frozen=True, slots=True)
class DirectAnswerInput:
    """Direct Answer Agentへ渡す1 attempt分の入力。"""

    request: AnsweringRequest
    previous_answer: str
    previous_error: str | None = None


class DirectAnswerer(Protocol):
    """検索なしで自然に回答する工程。

    provider・validation失敗またはroutine stop signalを伝播する。
    """

    async def answer(
        self,
        *,
        request: AnsweringRequest,
        previous_answer: str = "",
    ) -> DirectAnswerDraft: ...
