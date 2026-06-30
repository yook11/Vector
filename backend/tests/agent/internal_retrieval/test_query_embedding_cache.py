"""QueryEmbeddingCacheRepository の DB 結合テスト。"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.internal_retrieval.ai.gemini_spec import (
    GEMINI_QUERY_EMBEDDING_SPEC,
    embedder_identity_of,
)
from app.agent.internal_retrieval.query_embedding_cache import (
    QueryEmbeddingCacheRepository,
)
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)
from app.models.query_embedding_cache import QueryEmbeddingCache


def _vector(value: float = 0.1) -> EmbeddingVector:
    return EmbeddingVector(root=tuple([value] * EMBEDDING_DIMENSION))


@pytest.fixture
def identity() -> str:
    return embedder_identity_of(GEMINI_QUERY_EMBEDDING_SPEC)


@pytest.fixture
def repo(db_session: AsyncSession) -> QueryEmbeddingCacheRepository:
    return QueryEmbeddingCacheRepository(db_session)


class TestStoreThenFetchCached:
    async def test_stored_query_is_returned_by_fetch(
        self,
        repo: QueryEmbeddingCacheRepository,
        db_session: AsyncSession,
        identity: str,
    ) -> None:
        """store→fetch_cached で同一 (query, identity) がヒットする。"""
        query = "NVIDIA earnings report"
        vec = _vector(0.1)

        await repo.store(embedder_identity=identity, query_text=query, vector=vec)
        await db_session.commit()

        result = await repo.fetch_cached(embedder_identity=identity, queries=[query])

        assert query in result
        assert len(result) == 1

    async def test_unstored_query_absent_from_result(
        self,
        repo: QueryEmbeddingCacheRepository,
        db_session: AsyncSession,
        identity: str,
    ) -> None:
        """未保存 query は fetch_cached の結果 dict に現れない。"""
        await repo.store(
            embedder_identity=identity,
            query_text="stored query",
            vector=_vector(0.1),
        )
        await db_session.commit()

        result = await repo.fetch_cached(
            embedder_identity=identity,
            queries=["not stored query"],
        )

        assert "not stored query" not in result
        assert len(result) == 0


class TestEmbedderIdentityIsolation:
    async def test_different_identity_returns_separate_vector(
        self,
        repo: QueryEmbeddingCacheRepository,
        db_session: AsyncSession,
        identity: str,
    ) -> None:
        """同一 query + 別 identity = 別エントリ (stale hit 防止の中核 oracle)。"""
        query = "AI chip demand"
        other_identity = "other-provider:other-model:RETRIEVAL_QUERY:768:768"
        vec_a = _vector(0.1)
        vec_b = _vector(0.9)

        await repo.store(embedder_identity=identity, query_text=query, vector=vec_a)
        await repo.store(
            embedder_identity=other_identity, query_text=query, vector=vec_b
        )
        await db_session.commit()

        result_a = await repo.fetch_cached(embedder_identity=identity, queries=[query])
        result_b = await repo.fetch_cached(
            embedder_identity=other_identity, queries=[query]
        )

        # 各 identity で別ベクトルが返る (stale hit が起きていない)。
        assert result_a[query].to_list()[0] == pytest.approx(0.1, abs=1e-2)
        assert result_b[query].to_list()[0] == pytest.approx(0.9, abs=1e-2)

    async def test_same_identity_different_query_returns_separate_entries(
        self,
        repo: QueryEmbeddingCacheRepository,
        db_session: AsyncSession,
        identity: str,
    ) -> None:
        """同一 identity + 別 query = 別エントリ。"""
        query_a = "AI chip demand"
        query_b = "GPU shortage 2025"
        vec_a = _vector(0.2)
        vec_b = _vector(0.8)

        await repo.store(embedder_identity=identity, query_text=query_a, vector=vec_a)
        await repo.store(embedder_identity=identity, query_text=query_b, vector=vec_b)
        await db_session.commit()

        result = await repo.fetch_cached(
            embedder_identity=identity, queries=[query_a, query_b]
        )

        assert result[query_a].to_list()[0] == pytest.approx(0.2, abs=1e-2)
        assert result[query_b].to_list()[0] == pytest.approx(0.8, abs=1e-2)


class TestOnConflictDoNothing:
    async def test_duplicate_store_raises_no_exception(
        self,
        repo: QueryEmbeddingCacheRepository,
        db_session: AsyncSession,
        identity: str,
    ) -> None:
        """同一 (query, identity) を 2 回 store しても例外なし。"""
        query = "semiconductor supply chain"
        await repo.store(
            embedder_identity=identity, query_text=query, vector=_vector(0.3)
        )
        await db_session.commit()

        # 2 回目は別ベクトル—例外が起きないことを確認する。
        await repo.store(
            embedder_identity=identity, query_text=query, vector=_vector(0.7)
        )
        await db_session.commit()

    async def test_duplicate_store_keeps_exactly_one_row(
        self,
        repo: QueryEmbeddingCacheRepository,
        db_session: AsyncSession,
        identity: str,
    ) -> None:
        """2 回 store しても行は 1 件だけ残る。"""
        query = "semiconductor supply chain"
        await repo.store(
            embedder_identity=identity, query_text=query, vector=_vector(0.3)
        )
        await db_session.commit()
        await repo.store(
            embedder_identity=identity, query_text=query, vector=_vector(0.7)
        )
        await db_session.commit()

        count = await db_session.scalar(
            select(func.count()).select_from(QueryEmbeddingCache)
        )

        assert count == 1

    async def test_duplicate_store_preserves_first_vector(
        self,
        repo: QueryEmbeddingCacheRepository,
        db_session: AsyncSession,
        identity: str,
    ) -> None:
        """on_conflict_do_nothing により 1 回目のベクトルが保持される（先勝ち）。"""
        query = "semiconductor supply chain"
        first_vec = _vector(0.3)
        second_vec = _vector(0.7)

        await repo.store(embedder_identity=identity, query_text=query, vector=first_vec)
        await db_session.commit()
        await repo.store(
            embedder_identity=identity, query_text=query, vector=second_vec
        )
        await db_session.commit()

        result = await repo.fetch_cached(embedder_identity=identity, queries=[query])

        # 1 回目 (0.3) が保持され、2 回目 (0.7) は無視される。
        assert result[query].to_list()[0] == pytest.approx(0.3, abs=1e-2)


class TestVectorRoundTrip:
    async def test_vector_round_trip_within_halfvec_tolerance(
        self,
        repo: QueryEmbeddingCacheRepository,
        db_session: AsyncSession,
        identity: str,
    ) -> None:
        """既知ベクトルを store→fetch し、HALFVEC (float16) 丸め許容内で一致する。"""
        query = "vector round-trip test"
        # 0 以外の具体的な値を使い round-trip を非空虚にする。
        known_value = 0.123
        original = _vector(known_value)

        await repo.store(embedder_identity=identity, query_text=query, vector=original)
        await db_session.commit()

        result = await repo.fetch_cached(embedder_identity=identity, queries=[query])
        fetched = result[query]

        assert len(fetched) == EMBEDDING_DIMENSION  # 次元数が保たれる
        for element in fetched.to_list():
            assert element == pytest.approx(known_value, abs=1e-2)


class TestBatchFetch:
    async def test_partial_hit_returns_only_stored_queries(
        self,
        repo: QueryEmbeddingCacheRepository,
        db_session: AsyncSession,
        identity: str,
    ) -> None:
        """3 query 中 2 件 store → fetch で hit 2 件だけ返る。"""
        q_hit_1 = "stored query alpha"
        q_hit_2 = "stored query beta"
        q_miss = "not stored query gamma"

        await repo.store(
            embedder_identity=identity, query_text=q_hit_1, vector=_vector(0.2)
        )
        await repo.store(
            embedder_identity=identity, query_text=q_hit_2, vector=_vector(0.4)
        )
        await db_session.commit()

        result = await repo.fetch_cached(
            embedder_identity=identity,
            queries=[q_hit_1, q_hit_2, q_miss],
        )

        assert q_hit_1 in result
        assert q_hit_2 in result
        assert q_miss not in result
        assert len(result) == 2

    async def test_empty_queries_returns_empty_dict(
        self,
        repo: QueryEmbeddingCacheRepository,
        db_session: AsyncSession,
        identity: str,
    ) -> None:
        """空 queries は DB に触れず {} を返す。"""
        result = await repo.fetch_cached(embedder_identity=identity, queries=[])

        assert result == {}


class TestQueryTextConsistency:
    async def test_store_and_fetch_with_same_text_hit(
        self,
        repo: QueryEmbeddingCacheRepository,
        db_session: AsyncSession,
        identity: str,
    ) -> None:
        """store と fetch_cached に同一文字列を使うとヒットする。

        API がテキストから一貫して hash を導出する（生 hash を外から渡せない）ことを
        end-to-end で検証する。
        """
        query_text = "consistent hash derivation"

        await repo.store(
            embedder_identity=identity,
            query_text=query_text,
            vector=_vector(0.5),
        )
        await db_session.commit()

        result = await repo.fetch_cached(
            embedder_identity=identity,
            queries=[query_text],
        )

        assert query_text in result


class TestCheckConstraint:
    async def test_short_query_hash_raises_integrity_error(
        self,
        db_session: AsyncSession,
        identity: str,
    ) -> None:
        """query_hash が 64 字未満の行を直接 add+flush すると IntegrityError。"""
        invalid_row = QueryEmbeddingCache(
            query_hash="short",  # char_length != 64 → CheckConstraint 違反
            embedder_identity=identity,
            query_vector=[0.0] * EMBEDDING_DIMENSION,
        )
        db_session.add(invalid_row)

        with pytest.raises(IntegrityError):
            await db_session.flush()
