"""Shared answering contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.agent.question_context.contract import AnswerBrief

__all__ = ["AnsweringRequest"]


class AnsweringRequest(BaseModel):
    """Answererへ渡す AnswerBrief と実行時点。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer_brief: AnswerBrief
    as_of: datetime
