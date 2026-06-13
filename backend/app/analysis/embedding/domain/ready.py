"""Stage 5 embedding を開始できる状態を Domain 側で構築する。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EmbeddingPreconditionProtocol",
    "EmbeddingReadyBuildBlockedCode",
    "EmbeddingReadyBuildBlockedError",
    "EmbeddingReadyBuildFacts",
    "ReadyForEmbedding",
]


class EmbeddingReadyBuildBlockedCode(StrEnum):
    """Stage 5 Ready 構築 blocked の監査 outcome_code。"""

    ANALYZED_ARTICLE_MISSING = "embedding_ready_build_blocked_analyzed_article_missing"
    ALREADY_EMBEDDED = "embedding_ready_build_blocked_already_embedded"


@dataclass(frozen=True, slots=True)
class EmbeddingReadyBuildFacts:
    """Stage 5 Ready 構築に必要な DB 射影。"""

    article_id: int
    has_embedding: bool
    translated_title: str
    summary: str


class EmbeddingReadyBuildBlockedError(Exception):
    """Stage 5 入力として採用できなかった場合に投げる例外。"""

    def __init__(self, code: EmbeddingReadyBuildBlockedCode) -> None:
        self.code = code
        super().__init__(code.value)


class EmbeddingPreconditionProtocol(Protocol):
    """Ready 構築に必要な DB 事実だけを読む repository contract。

    構築可否と blocked 理由は ``ReadyForEmbedding`` が判定する。
    """

    async def load_ready_build_facts(
        self, analyzed_article_id: int
    ) -> EmbeddingReadyBuildFacts | None: ...


class ReadyForEmbedding(BaseModel):
    """embedder 入力と Stage 5 precondition を満たした不変オブジェクト。"""

    model_config = ConfigDict(frozen=True)

    analyzed_article_id: int = Field(gt=0)
    text_for_embedding: str = Field(min_length=1)
    article_id: int = Field(gt=0)

    @classmethod
    async def try_advance_from(
        cls,
        analyzed_article_id: int,
        embedding_repo: EmbeddingPreconditionProtocol,
    ) -> ReadyForEmbedding:
        """DB 事実から Ready を構築し、対象外なら blocked 例外を投げる。"""
        facts = await embedding_repo.load_ready_build_facts(analyzed_article_id)
        if facts is None:
            raise EmbeddingReadyBuildBlockedError(
                EmbeddingReadyBuildBlockedCode.ANALYZED_ARTICLE_MISSING
            )

        if facts.has_embedding:
            raise EmbeddingReadyBuildBlockedError(
                EmbeddingReadyBuildBlockedCode.ALREADY_EMBEDDED
            )

        return cls(
            analyzed_article_id=analyzed_article_id,
            text_for_embedding=f"{facts.translated_title}\n{facts.summary}",
            article_id=facts.article_id,
        )
