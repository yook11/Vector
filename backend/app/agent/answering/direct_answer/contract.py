"""Direct answer contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.agent.contract import NonBlankText

__all__ = [
    "AnswerGenerationStopped",
    "DirectAnswerDraft",
    "DirectAnswerer",
    "DirectAnswerGenerator",
    "DirectAnswerInvalidError",
]

_DIRECT_ANSWER_BLANK_RESPONSE = "direct_answer_blank_response"


class AnswerGenerationStopped(Exception):
    """現在のrun attemptが回答生成を継続できなくなった。"""


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
    """LLM adapter boundary that streams unvalidated direct answer text."""

    def stream(
        self,
        *,
        question: str,
        as_of: datetime,
        user_intent: str = "",
        user_activity_context: str = "",
        previous_answer: str = "",
        previous_error: str | None = None,
    ) -> AsyncIterator[str]: ...


class DirectAnswerer(Protocol):
    """検索なしで自然に回答する工程。

    provider・validation失敗またはroutine stop signalを伝播する。
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
