"""Internal article vector search repository tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.internal_retrieval.article_search import (
    InternalArticleContent,
    InternalArticleSearchHit,
    PgVectorArticleSearchRepository,
)
from app.agent.internal_retrieval.query_embedding import InternalQueryEmbedding
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.news_source import NewsSource
from app.models.out_of_scope_article_record import OutOfScopeArticleRecord


def _vector(first: float, second: float = 0.0) -> list[float]:
    values = [0.0] * EMBEDDING_DIMENSION
    values[0] = first
    values[1] = second
    return values


def _query_embedding(first: float = 1.0, second: float = 0.0) -> InternalQueryEmbedding:
    return InternalQueryEmbedding(
        query="AI semiconductor demand",
        vector=EmbeddingVector(root=tuple(_vector(first, second))),
    )


async def _create_curation(
    session: AsyncSession,
    source: NewsSource,
    *,
    url: str,
    translated_title: str = "分析タイトル",
    summary: str = "分析要約",
    published_at: datetime | None = None,
) -> ArticleCuration:
    article = AnalyzableArticleRecord(
        source_id=source.id,
        source_url=url,
        original_title=f"Original {translated_title}",
        original_content="original content " * 20,
        published_at=published_at or datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(article)
    await session.flush()
    curation = ArticleCuration(
        analyzable_article_id=article.id,
        translated_title=translated_title,
        summary=summary,
    )
    session.add(curation)
    await session.flush()
    return curation


async def _create_analysis(
    session: AsyncSession,
    source: NewsSource,
    category: Category,
    *,
    url: str,
    translated_title: str,
    summary: str = "分析要約",
    investor_take: str = "投資家視点",
    embedding: list[float] | None,
    key_points: object = None,
    published_at: datetime | None = None,
) -> AnalyzedArticleRecord:
    curation = await _create_curation(
        session,
        source,
        url=url,
        translated_title=translated_title,
        summary=summary,
        published_at=published_at,
    )
    analysis = AnalyzedArticleRecord(
        curation_id=curation.id,
        translated_title=translated_title,
        summary=summary,
        investor_take=investor_take,
        category_id=category.id,
        embedding=embedding,
        key_points=key_points,
    )
    session.add(analysis)
    await session.commit()
    await session.refresh(analysis)
    return analysis


async def _create_out_of_scope(
    session: AsyncSession,
    source: NewsSource,
    *,
    url: str,
    translated_title: str = "対象外タイトル",
) -> OutOfScopeArticleRecord:
    curation = await _create_curation(
        session,
        source,
        url=url,
        translated_title=translated_title,
        summary="対象外要約",
    )
    rejected = OutOfScopeArticleRecord(
        curation_id=curation.id,
        translated_title=translated_title,
        summary="対象外要約",
        investor_take="対象外理由",
        key_points=[],
    )
    session.add(rejected)
    await session.commit()
    await session.refresh(rejected)
    return rejected


class TestPgVectorArticleSearchRepository:
    async def test_search_returns_in_scope_article_projection(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        await _create_analysis(
            db_session,
            sample_source,
            sample_categories[0],
            url="https://example.com/internal-hit",
            translated_title="OpenAI 半導体提携",
            summary="OpenAI が半導体供給網を強化した。",
            investor_take="AI 半導体需要の追い風。",
            embedding=_vector(1.0, 0.0),
            key_points=[
                {
                    "content": "OpenAI が新しい半導体提携を発表した。",
                    "mentions": [
                        {"surface": "OpenAI", "type": "company"},
                        {"surface": "openai", "type": "company"},
                        {"surface": "GPU", "type": "technology"},
                    ],
                }
            ],
            published_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        repo = PgVectorArticleSearchRepository(db_session)

        hits = await repo.search_by_embedding(_query_embedding(), limit=5)

        assert len(hits) == 1
        hit = hits[0]
        assert isinstance(hit, InternalArticleSearchHit)
        assert isinstance(hit.article, InScopeAnalyzedArticle)
        assert isinstance(hit.content, InternalArticleContent)
        assert hit.article.title == "OpenAI 半導体提携"
        assert hit.article.summary == "OpenAI が半導体供給網を強化した。"
        assert hit.content.title == "OpenAI 半導体提携"
        assert hit.content.summary == "OpenAI が半導体供給網を強化した。"
        assert hit.content.key_points == ["OpenAI が新しい半導体提携を発表した。"]
        assert hit.content.mentions == ["OpenAI", "GPU"]
        assert hit.content.published_at == datetime(2026, 1, 2, tzinfo=UTC)
        assert hit.distance == pytest.approx(0.0, abs=1e-3)

    async def test_search_excludes_rows_without_embedding(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        await _create_analysis(
            db_session,
            sample_source,
            sample_categories[0],
            url="https://example.com/no-embedding",
            translated_title="embeddingなし",
            embedding=None,
        )
        await _create_analysis(
            db_session,
            sample_source,
            sample_categories[0],
            url="https://example.com/with-embedding",
            translated_title="embeddingあり",
            embedding=_vector(1.0),
        )
        repo = PgVectorArticleSearchRepository(db_session)

        hits = await repo.search_by_embedding(_query_embedding(), limit=5)

        assert [hit.article.title for hit in hits] == ["embeddingあり"]

    async def test_search_excludes_unassessed_and_out_of_scope_articles(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        await _create_curation(
            db_session,
            sample_source,
            url="https://example.com/unassessed",
            translated_title="未評価",
        )
        await _create_out_of_scope(
            db_session,
            sample_source,
            url="https://example.com/out-of-scope",
            translated_title="対象外",
        )
        await _create_analysis(
            db_session,
            sample_source,
            sample_categories[0],
            url="https://example.com/in-scope",
            translated_title="対象内",
            embedding=_vector(1.0),
        )
        repo = PgVectorArticleSearchRepository(db_session)

        hits = await repo.search_by_embedding(_query_embedding(), limit=5)

        assert [hit.article.title for hit in hits] == ["対象内"]

    async def test_search_orders_by_cosine_distance_and_respects_limit(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        now = datetime(2026, 1, 3, tzinfo=UTC)
        await _create_analysis(
            db_session,
            sample_source,
            sample_categories[0],
            url="https://example.com/far",
            translated_title="遠い記事",
            embedding=_vector(0.0, 1.0),
            published_at=now,
        )
        await _create_analysis(
            db_session,
            sample_source,
            sample_categories[0],
            url="https://example.com/near",
            translated_title="近い記事",
            embedding=_vector(1.0, 0.0),
            published_at=now - timedelta(days=1),
        )
        await _create_analysis(
            db_session,
            sample_source,
            sample_categories[0],
            url="https://example.com/mid",
            translated_title="中間の記事",
            embedding=_vector(1.0, 1.0),
            published_at=now - timedelta(days=2),
        )
        repo = PgVectorArticleSearchRepository(db_session)

        hits = await repo.search_by_embedding(_query_embedding(), limit=2)

        assert [hit.article.title for hit in hits] == ["近い記事", "中間の記事"]
        assert hits[0].distance < hits[1].distance

    async def test_search_orders_same_distance_by_newer_published_at(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        newer = datetime(2026, 1, 3, tzinfo=UTC)
        older = datetime(2026, 1, 1, tzinfo=UTC)
        await _create_analysis(
            db_session,
            sample_source,
            sample_categories[0],
            url="https://example.com/older-same-distance",
            translated_title="古い同距離記事",
            embedding=_vector(1.0, 0.0),
            published_at=older,
        )
        await _create_analysis(
            db_session,
            sample_source,
            sample_categories[0],
            url="https://example.com/newer-same-distance",
            translated_title="新しい同距離記事",
            embedding=_vector(1.0, 0.0),
            published_at=newer,
        )
        repo = PgVectorArticleSearchRepository(db_session)

        hits = await repo.search_by_embedding(_query_embedding(), limit=2)

        assert [hit.article.title for hit in hits] == [
            "新しい同距離記事",
            "古い同距離記事",
        ]

    async def test_search_normalizes_null_key_points_to_empty_content(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        await _create_analysis(
            db_session,
            sample_source,
            sample_categories[0],
            url="https://example.com/null-key-points",
            translated_title="旧行",
            embedding=_vector(1.0),
            key_points=None,
        )
        repo = PgVectorArticleSearchRepository(db_session)

        hits = await repo.search_by_embedding(_query_embedding(), limit=5)

        assert hits[0].content.key_points == []
        assert hits[0].content.mentions == []

    async def test_search_skips_rows_that_cannot_be_reconstructed_as_in_scope(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        await _create_analysis(
            db_session,
            sample_source,
            sample_categories[0],
            url="https://example.com/malformed-key-points",
            translated_title="壊れた行",
            embedding=_vector(1.0),
            key_points="not-a-list",
        )
        await _create_analysis(
            db_session,
            sample_source,
            sample_categories[0],
            url="https://example.com/valid-key-points",
            translated_title="正常な行",
            embedding=_vector(1.0, 0.1),
            key_points=[],
        )
        repo = PgVectorArticleSearchRepository(db_session)

        hits = await repo.search_by_embedding(_query_embedding(), limit=5)

        assert [hit.article.title for hit in hits] == ["正常な行"]

    async def test_hit_does_not_expose_embedding_raw_row_or_raw_key_points(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        await _create_analysis(
            db_session,
            sample_source,
            sample_categories[0],
            url="https://example.com/no-raw-values",
            translated_title="rawなし",
            embedding=_vector(1.0),
            key_points=[
                {
                    "content": "NVIDIA が新GPUを発表した。",
                    "mentions": [{"surface": "NVIDIA", "type": "company"}],
                }
            ],
        )
        repo = PgVectorArticleSearchRepository(db_session)

        hit = (await repo.search_by_embedding(_query_embedding(), limit=5))[0]

        assert not isinstance(hit.article, AnalyzedArticleRecord)
        assert not hasattr(hit, "embedding")
        assert not hasattr(hit, "key_points")
        assert isinstance(hit.content.mentions[0], str)
