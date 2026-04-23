"""EmbeddingService のテスト (DB 統合テスト)。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.embedder.base import BaseEmbedder
from app.analysis.embedding_service import EmbeddingService
from app.models.article import Article
from app.models.article_analysis import ArticleAnalysis, ImpactLevel
from app.models.article_extraction import ArticleExtraction
from app.models.category import Category
from app.models.discovered_article import DiscoveredArticle
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


async def _build_extraction_with_analysis(
    db_session: AsyncSession,
    source: NewsSource,
    topic: Topic,
    *,
    url: str,
    title: str,
    translated_title: str,
    summary: str,
    embedding: list[float] | None = None,
) -> tuple[Article, ArticleExtraction, ArticleAnalysis]:
    """Stage 1+2 完了済みの article / extraction / analysis を作成する。"""
    discovered = DiscoveredArticle(
        original_title=title,
        original_url=url,
        news_source_id=source.id,
    )
    db_session.add(discovered)
    await db_session.flush()
    article = Article(
        discovered_article_id=discovered.id,
        original_title=title,
        original_content="Content.",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()
    extraction = ArticleExtraction(
        article_id=article.id,
        translated_title=translated_title,
        summary=summary,
        ai_model="gemini-2.5-flash-lite",
    )
    db_session.add(extraction)
    await db_session.flush()
    analysis = ArticleAnalysis(
        extraction_id=extraction.id,
        translated_title=translated_title,
        summary=summary,
        impact_level=ImpactLevel.MEDIUM,
        reasoning="テスト理由",
        ai_model="gemini-2.5-flash-lite",
        topic_id=topic.id,
        embedding=embedding,
        embedding_model="cl-nagoya/ruri-v3-310m" if embedding is not None else None,
    )
    db_session.add(analysis)
    return article, extraction, analysis


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

    article, extraction, _ = await _build_extraction_with_analysis(
        db_session,
        sample_source,
        topic,
        url="https://example.com/embed-test",
        title="Test Article",
        translated_title="テスト記事",
        summary="テスト要約",
    )
    await db_session.commit()

    article_id = article.id
    extraction_id = extraction.id
    embedder = _mock_embedder()
    svc = EmbeddingService(session_factory)
    result = await svc.execute(article_id, embedder)

    assert result.status == "created"
    embedder.embed_document.assert_called_once_with("テスト記事\nテスト要約")

    # 永続化された embedding を確認 (Service は独自セッションで commit 済み)
    db_session.expire_all()
    stmt = select(ArticleAnalysis).where(
        ArticleAnalysis.extraction_id == extraction_id,
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

    article, _, _ = await _build_extraction_with_analysis(
        db_session,
        sample_source,
        topic,
        url="https://example.com/idempotent",
        title="Already Embedded",
        translated_title="既存タイトル",
        summary="既存要約",
        embedding=[0.2] * 768,
    )
    await db_session.commit()

    embedder = _mock_embedder()
    svc = EmbeddingService(session_factory)
    result = await svc.execute(article.id, embedder)

    assert result.status == "already_exists"
    embedder.embed_document.assert_not_called()


async def test_embedding_skipped_when_extraction_missing(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """extraction 自体が存在しない（Stage 1 未完了）場合は skipped を返す。"""
    discovered = DiscoveredArticle(
        original_title="No Extraction",
        original_url="https://example.com/no-extraction",
        news_source_id=sample_source.id,
    )
    db_session.add(discovered)
    await db_session.flush()
    article = Article(
        discovered_article_id=discovered.id,
        original_title="No Extraction",
        original_content="content",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    embedder = _mock_embedder()
    svc = EmbeddingService(session_factory)
    result = await svc.execute(article.id, embedder)

    assert result.status == "skipped"
    embedder.embed_document.assert_not_called()


async def test_embedding_skipped_when_analysis_missing(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """analysis のない extraction（rejected 済み / Stage 2 未完了）は skip。"""
    discovered = DiscoveredArticle(
        original_title="Rejected",
        original_url="https://example.com/rejected",
        news_source_id=sample_source.id,
    )
    db_session.add(discovered)
    await db_session.flush()
    article = Article(
        discovered_article_id=discovered.id,
        original_title="Rejected",
        original_content="content",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()
    extraction = ArticleExtraction(
        article_id=article.id,
        translated_title="対象外",
        summary="対象外要約",
        ai_model="gemini-2.5-flash-lite",
    )
    db_session.add(extraction)
    await db_session.commit()

    embedder = _mock_embedder()
    svc = EmbeddingService(session_factory)
    result = await svc.execute(article.id, embedder)

    assert result.status == "skipped"
    embedder.embed_document.assert_not_called()
