"""Internal search boundary errors."""

from __future__ import annotations

from typing import Literal

__all__ = ["InternalSearchError", "InternalSearchFailurePhase"]

type InternalSearchFailurePhase = Literal["query_embedding", "article_search"]


class InternalSearchError(Exception):
    """回答継続可能と分類された内部検索の運用失敗。"""

    def __init__(self, *, phase: InternalSearchFailurePhase) -> None:
        super().__init__()
        self.phase = phase
