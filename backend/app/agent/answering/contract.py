"""Shared answering contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.agent.contract import NonBlankText
from app.agent.threads.contracts import ThreadMessageSnapshot

__all__ = ["AnsweringRequest"]


class AnsweringRequest(BaseModel):
    """Answererへ渡す質問と会話履歴、実行時点。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: NonBlankText
    # 現在の質問より前のthreadメッセージ(古い順)。
    history: tuple[ThreadMessageSnapshot, ...] = ()
    as_of: datetime
