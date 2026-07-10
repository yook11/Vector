"""Direct answer contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.agent.contract import NonBlankText

__all__ = [
    "DirectAnswerDraft",
    "DirectAnswerer",
    "DirectAnswerGenerator",
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


class DirectAnswerGenerator(Protocol):
    """LLM adapter boundary that returns unvalidated direct answer text."""

    async def generate(
        self,
        *,
        question: str,
        as_of: datetime,
        user_intent: str = "",
        user_activity_context: str = "",
        previous_answer: str = "",
        previous_error: str | None = None,
    ) -> str: ...


class DirectAnswerer(Protocol):
    """検索なしで自然に回答する工程。

    失敗時は AIProviderError | DirectAnswerInvalidError を伝播する。
    """

    async def answer(
        self,
        *,
        question: str,
        as_of: datetime,
        user_intent: str = "",
        user_activity_context: str = "",
        previous_answer: str = "",
    ) -> DirectAnswerDraft: ...
