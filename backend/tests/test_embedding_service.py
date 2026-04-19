"""EmbeddingService のテスト (DB 統合テスト)。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.embedder.base import BaseEmbedder
from app.analysis.embedding_service import EmbeddingService
from app.models.article_analysis import ArticleAnalysis, ImpactLevel
from app.models.category import Category
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource
from app.models.topic import Topic


def _mock_embedder(vector: list[float] | None = None) -> MagicMock:
    """固定ベクトルを返すモック embedder を作成する。"""
    embedder = MagicMock(spec=BaseEmbedder)
    embedder.MODEL = "cl-nagoya/ruri-v3-310m"
    embedder.model_name = "cl-nagoya/ruri-v3-310m"
    embedder.embed_document = AsyncMock(
        return_value=vector or [0.1] * 768,
    )
    return embedder


async def test_embedding_creates_vector(
    db_session: AsyncSession,
    session_factory,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> None:
    """EmbeddingService は既存分析に対して embedding を永続化する。"""
    topic = Topic(name="embedding test", category_id=sample_categories[0].id)
    db_session.add(topic)
    await db_session.flush()

    article = NewsArticle(
        original_title="Test Article",
        original_url="https://example.com/embed-test",
        news_source_id=sample_source.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()

    analysis = ArticleAnalysis(
        news_article_id=article.id,
        translated_title="テスト記事",
        summary="テスト要約",
        impact_level=ImpactLevel.MEDIUM,
        reasoning="テスト理由",
        ai_model="gemini-2.5-flash-lite",
        topic_id=topic.id,
    )
    db_session.add(analysis)
    await db_session.commit()

    article_id = article.id
    embedder = _mock_embedder()
    svc = EmbeddingService(session_factory)
    result = await svc.execute(article_id, embedder)

    assert result.status == "created"
    embedder.embed_document.assert_called_once_with("テスト記事\nテスト要約")

    # 永続化された embedding を確認 (Service は独自セッションで commit 済み)
    db_session.expire_all()
    stmt = select(ArticleAnalysis).where(
        ArticleAnalysis.news_article_id == article_id,
    )
    refreshed = (await db_session.execute(stmt)).scalar_one()
    assert refreshed.embedding is not None
    assert len(refreshed.embedding.to_list()) == 768
    assert refreshed.embedding_model == "cl-nagoya/ruri-v3-310m"


async def test_embedding_idempotency(
    db_session: AsyncSession,
    session_factory,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> None:
    """embedding 済み分析は API を呼ばずに already_exists を返す。"""
    topic = Topic(name="idempotent test", category_id=sample_categories[0].id)
    db_session.add(topic)
    await db_session.flush()

    article = NewsArticle(
        original_title="Already Embedded",
        original_url="https://example.com/idempotent",
        news_source_id=sample_source.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()

    analysis = ArticleAnalysis(
        news_article_id=article.id,
        translated_title="既存タイトル",
        summary="既存要約",
        impact_level=ImpactLevel.LOW,
        reasoning="既存理由",
        ai_model="gemini-2.5-flash-lite",
        embedding=[0.2] * 768,
        embedding_model="cl-nagoya/ruri-v3-310m",
        topic_id=topic.id,
    )
    db_session.add(analysis)
    await db_session.commit()

    embedder = _mock_embedder()
    svc = EmbeddingService(session_factory)
    result = await svc.execute(article.id, embedder)

    assert result.status == "already_exists"
    embedder.embed_document.assert_not_called()


async def test_embedding_no_analysis_raises(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """分析が存在しない場合は ValueError を送出する。"""
    article = NewsArticle(
        original_title="No Analysis",
        original_url="https://example.com/no-analysis",
        news_source_id=sample_source.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    embedder = _mock_embedder()
    svc = EmbeddingService(session_factory)

    with pytest.raises(ValueError, match="No analysis found"):
        await svc.execute(article.id, embedder)
