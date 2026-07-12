"""Internal search query embedding boundary."""

from __future__ import annotations

import hashlib
from typing import Any, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent.planning.contract import MAX_INTERNAL_QUERIES
from app.analysis.embedding.domain.value_objects import EmbeddingVector

__all__ = [
    "MAX_INTERNAL_QUERIES",
    "InternalQueryEmbedder",
    "InternalQueryEmbedding",
    "InternalSearchQueries",
    "query_hash_of",
]


class InternalSearchQueries(BaseModel):
    """Normalized query texts for internal retrieval."""

    model_config = ConfigDict(frozen=True)

    queries: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("queries", mode="before")
    @classmethod
    def _strip_queries(cls, value: Any) -> Any:
        if value is None:
            return ()
        return tuple(
            query.strip() if isinstance(query, str) else query for query in value
        )

    @model_validator(mode="after")
    def _validate_queries(self) -> Self:
        if len(self.queries) > MAX_INTERNAL_QUERIES:
            raise ValueError(
                "internal search queries must be capped before construction"
            )
        if any(not query for query in self.queries):
            raise ValueError("internal search queries cannot include blank queries")
        return self


def query_hash_of(text: str) -> str:
    """embed 対象テキストの sha256 hex (キャッシュキー)。

    不変条件「hash する文字列 = embed する文字列」を保つため、呼び出し側が実際に
    embed する文字列をそのまま渡す。ここで再正規化はしない。
    """

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class InternalQueryEmbedding(BaseModel):
    """Internal search query paired with an embedding vector."""

    model_config = ConfigDict(frozen=True)

    query: str = Field(min_length=1)
    # This first slice reuses the document vector VO so query/doc dimensions match.
    vector: EmbeddingVector

    @field_validator("query", mode="before")
    @classmethod
    def _strip_query(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value


class InternalQueryEmbedder(Protocol):
    """Embeds each internal query once, preserving input order."""

    async def embed_queries(
        self,
        queries: InternalSearchQueries,
    ) -> list[InternalQueryEmbedding]: ...
