"""Query 埋め込みキャッシュの永続化境界。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalQueryEmbedding,
    InternalSearchQueries,
    query_hash_of,
)
from app.analysis.embedding.domain.value_objects import EmbeddingVector
from app.models.query_embedding_cache import QueryEmbeddingCache

__all__ = ["QueryEmbeddingCacheRepository", "TransactionalQueryEmbeddingCache"]


def _as_floats(raw: Any) -> list[float]:
    # pgvector は読み戻しで HalfVector / ndarray を返すため list 化する。
    to_list = getattr(raw, "to_list", None)
    if callable(to_list):
        return list(to_list())
    return [float(value) for value in raw]


class QueryEmbeddingCacheRepository:
    """embed 対象テキストと embedder 同一性で query ベクトルを再利用する。

    生 hash を受け取らず、テキストから ``query_hash_of`` で一度だけ hash を導出して
    「hash する文字列 = embed する文字列」の不変条件を API で保証する。commit は
    呼び出し側が行う。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch_cached(
        self,
        *,
        embedder_identity: str,
        queries: Sequence[str],
    ) -> dict[str, EmbeddingVector]:
        """ヒットした query だけを ``{query: vector}`` で返す。

        1 リクエスト内は単一 embedder_identity を前提とする。
        """

        if not queries:
            return {}

        hash_to_query = {query_hash_of(query): query for query in queries}
        stmt = select(
            QueryEmbeddingCache.query_hash,
            QueryEmbeddingCache.query_vector,
        ).where(
            QueryEmbeddingCache.embedder_identity == embedder_identity,
            QueryEmbeddingCache.query_hash.in_(list(hash_to_query)),
        )
        rows = (await self._session.execute(stmt)).all()
        return {
            hash_to_query[query_hash]: EmbeddingVector(root=tuple(_as_floats(vector)))
            for query_hash, vector in rows
        }

    async def store(
        self,
        *,
        embedder_identity: str,
        query_text: str,
        vector: EmbeddingVector,
    ) -> None:
        """1 件 upsert。``query_text`` は hash 算出用で保存しない。

        同時 miss は ON CONFLICT DO NOTHING で吸収する。
        """

        stmt = (
            pg_insert(QueryEmbeddingCache)
            .values(
                query_hash=query_hash_of(query_text),
                embedder_identity=embedder_identity,
                query_vector=vector.to_list(),
            )
            .on_conflict_do_nothing(
                index_elements=["query_hash", "embedder_identity"],
            )
        )
        await self._session.execute(stmt)


@dataclass(frozen=True, slots=True)
class TransactionalQueryEmbeddingCache:
    """Query embedding cache port using its own session per operation."""

    session_factory: async_sessionmaker[AsyncSession]
    embedder_identity: str

    async def fetch_cached(
        self,
        queries: InternalSearchQueries,
    ) -> dict[str, EmbeddingVector]:
        async with self.session_factory() as session:
            repo = QueryEmbeddingCacheRepository(session)
            result = await repo.fetch_cached(
                embedder_identity=self.embedder_identity,
                queries=queries.queries,
            )
            await session.commit()
            return result

    async def store(self, embedding: InternalQueryEmbedding) -> None:
        async with self.session_factory() as session:
            repo = QueryEmbeddingCacheRepository(session)
            await repo.store(
                embedder_identity=self.embedder_identity,
                query_text=embedding.query,
                vector=embedding.vector,
            )
            await session.commit()
