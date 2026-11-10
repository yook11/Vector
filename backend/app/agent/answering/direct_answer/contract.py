"""Direct answer contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from app.agent.answering.contract import AnsweringRequest
from app.agent.contract import NonBlankText
from app.shared.errors import ApplicationError

if TYPE_CHECKING:
    from app.agent.running.state import CannotContinue

__all__ = [
    "DirectAnswerInput",
    "DirectAnswerDraft",
    "DirectAnswerer",
    "DirectAnswerInvalidError",
]


class DirectAnswerInvalidError(ApplicationError):
    """Direct answer response が answer draft として消化できない。"""

    CODE: ClassVar[str] = "direct_answer_blank_response"

    def __init__(self) -> None:
        super().__init__("直接回答の応答が空でした")


class DirectAnswerDraft(BaseModel):
    """Direct 回答工程 (LLM) の出力 draft。"""

    model_config = ConfigDict(frozen=True)

    answer: NonBlankText


@dataclass(frozen=True, slots=True)
class DirectAnswerInput:
    """この工程が受け取り、Agent に渡す入力。"""

    request: AnsweringRequest
    previous_answer: str


class DirectAnswerer(Protocol):
    """検索なしで自然に回答する工程。

    分類済み工程失敗はDirectAnswerErrorで伝播し、Runが止まったらその理由を返す。
    """

    async def answer(
        self, input: DirectAnswerInput
    ) -> DirectAnswerDraft | CannotContinue: ...
