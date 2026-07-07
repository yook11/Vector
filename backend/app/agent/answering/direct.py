"""Direct answer port and draft contract."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.agent.contract import NonBlankText

__all__ = ["DirectAnswerDraft", "DirectAnswerer"]


class DirectAnswerDraft(BaseModel):
    """Direct 回答工程 (LLM) の出力 draft。"""

    model_config = ConfigDict(frozen=True)

    answer: NonBlankText


class DirectAnswerer(Protocol):
    """検索なしで自然に回答する工程。evidence を受け取らない。"""

    async def answer(
        self,
        *,
        question: str,
        as_of: datetime,
    ) -> DirectAnswerDraft: ...
