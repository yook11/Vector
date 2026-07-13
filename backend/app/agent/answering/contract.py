"""Shared answering contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.agent.question_context.contract import QuestionContext

__all__ = ["AnsweringRequest"]


class AnsweringRequest(BaseModel):
    """Answererへ渡す質問コンテキストと実行時点。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: QuestionContext
    as_of: datetime
