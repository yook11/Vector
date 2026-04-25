"""EmbeddingService (app.analysis.embedding.service) の DB 統合テスト。

Outcome tagged union (``EmbeddedOutcome`` / ``AlreadyEmbeddedOutcome`` /
``SkippedOutcome``) と Service フローの全経路を検証する:

- 正常系 (新規生成 → 永続化)
- 冪等ヒット (既存埋め込み)
- skipped: extraction_not_found / analysis_pending / analysis_rejected /
  invalid_input
- エラー伝搬: RateLimit / Provider / Network は Task 層に伝搬
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.embedder.base import BaseEmbedder
from app.analysis.embedding.domain.embedding import Embedding
from app.analysis.embedding.domain.value_objects import EMBEDDING_DIMENSION
from app.analysis.embedding.service import (
    AlreadyEmbeddedOutcome,
    EmbeddedOutcome,
    EmbeddingService,
    SkippedOutcome,
)
from app.analysis.errors import (
    InvalidInputError,
    NetworkError,
    ProviderError,
    RateLimitError,
)
from app.models.article import Article
from app.models.article_analysis import ArticleAnalysis
from app.models.article_extraction import ArticleExtraction
from app.models.article_rejection import ArticleRejection
from app.models.category import Category
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_embedder(
    *,
    vector: list[float] | None = None,
    raises: Exception | None = None,
) -> MagicMock:
    """固定ベクトルを返す (または例外を投げる) モック embedder。"""
    embedder = MagicMock(spec=BaseEmbedder)
    embedder.MODEL = "cl-nagoya/ruri-v3-310m"
    embedder.model_name = "cl-nagoya/ruri-v3-310m"
    if raises is not None:
        embedder.embed_document = AsyncMock(side_effect=raises)
    else:
        embedder.embed_document = AsyncMock(
            return_value=vector or [0.1] * EMBEDDING_DIMENSION,
        )
    return embedder


async def _build_article(
    db_session: AsyncSession,
    source: NewsSource,
    *,
    url: str,
    title: str = "Article",
) -> Article:
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
        original_content="content body content body",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()
    return article


async def _build_extraction(
    db_session: AsyncSession,
    article: Article,
    *,
    translated_title: str = "テスト抽出タイトル",
    summary: str = "テスト抽出要約",
) -> ArticleExtraction:
    extraction = ArticleExtraction(
        article_id=article.id,
        translated_title=translated_title,
        summary=summary,
        ai_model="gemini-2.5-flash-lite",
    )
    db_session.add(extraction)
    await db_session.flush()
    return extraction


async def _build_analysis(
    db_session: AsyncSession,
    extraction: ArticleExtraction,
    category_id: int,
    *,
    translated_title: str = "分析タイトル",
    summary: str = "分析要約",
    embedding: list[float] | None = None,
) -> ArticleAnalysis:
    analysis = ArticleAnalysis(
        extraction_id=extraction.id,
        translated_title=translated_title,
        summary=summary,
        investor_take="投資家視点",
        ai_model="gemini-2.5-flash-lite",
        topic="embedding service",
        category_id=category_id,
        embedding=embedding,
        embedding_model=("cl-nagoya/ruri-v3-310m" if embedding is not None else None),
    )
    db_session.add(analysis)
    await db_session.flush()
    return analysis


# ---------------------------------------------------------------------------
# Happy path: EmbeddedOutcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_returns_embedded_outcome_and_persists(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """正常系: 埋め込み生成 → 永続化 → EmbeddedOutcome 返却。"""
    article = await _build_article(
        db_session, sample_source, url="https://example.com/embed-ok"
    )
    extraction = await _build_extraction(db_session, article)
    analysis = await _build_analysis(db_session, extraction, sample_categories[0].id)
    await db_session.commit()
    article_id = article.id
    analysis_id = analysis.id

    embedder = _mock_embedder()
    svc = EmbeddingService(session_factory)
    result = await svc.execute(article_id, embedder)

    assert isinstance(result, EmbeddedOutcome)
    assert isinstance(result.embedding, Embedding)
    assert result.embedding.analysis_id == analysis_id
    assert result.embedding.model_name == "cl-nagoya/ruri-v3-310m"
    embedder.embed_document.assert_called_once_with("分析タイトル\n分析要約")

    db_session.expire_all()
    refetched = await db_session.get(ArticleAnalysis, analysis_id)
    assert refetched is not None
    assert refetched.embedding is not None
    assert refetched.embedding_model == "cl-nagoya/ruri-v3-310m"


# ---------------------------------------------------------------------------
# Idempotency: AlreadyEmbeddedOutcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_returns_already_embedded_when_existing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """既存埋め込み → embedder 呼ばずに AlreadyEmbeddedOutcome。"""
    article = await _build_article(
        db_session, sample_source, url="https://example.com/already-embedded"
    )
    extraction = await _build_extraction(db_session, article)
    await _build_analysis(
        db_session,
        extraction,
        sample_categories[0].id,
        embedding=[0.4] * EMBEDDING_DIMENSION,
    )
    await db_session.commit()
    article_id = article.id

    embedder = _mock_embedder()
    svc = EmbeddingService(session_factory)
    result = await svc.execute(article_id, embedder)

    assert isinstance(result, AlreadyEmbeddedOutcome)
    assert result.embedding.model_name == "cl-nagoya/ruri-v3-310m"
    embedder.embed_document.assert_not_called()


# ---------------------------------------------------------------------------
# Skipped: extraction_not_found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_skipped_when_extraction_missing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Stage 1 未完了 → SkippedOutcome(extraction_not_found)。"""
    article = await _build_article(
        db_session, sample_source, url="https://example.com/no-extraction"
    )
    await db_session.commit()
    article_id = article.id

    embedder = _mock_embedder()
    svc = EmbeddingService(session_factory)
    result = await svc.execute(article_id, embedder)

    assert isinstance(result, SkippedOutcome)
    assert result.reason == "extraction_not_found"
    embedder.embed_document.assert_not_called()


# ---------------------------------------------------------------------------
# Skipped: analysis_pending (extraction あり、analysis/rejection 共に無し)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_skipped_when_analysis_pending(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Stage 2 未完了 → SkippedOutcome(analysis_pending)。"""
    article = await _build_article(
        db_session, sample_source, url="https://example.com/pending"
    )
    await _build_extraction(db_session, article)
    await db_session.commit()
    article_id = article.id

    embedder = _mock_embedder()
    svc = EmbeddingService(session_factory)
    result = await svc.execute(article_id, embedder)

    assert isinstance(result, SkippedOutcome)
    assert result.reason == "analysis_pending"
    embedder.embed_document.assert_not_called()


# ---------------------------------------------------------------------------
# Skipped: analysis_rejected (extraction あり、rejection あり、analysis 無し)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_skipped_when_analysis_rejected(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Stage 2 で OutOfScope → SkippedOutcome(analysis_rejected)。"""
    article = await _build_article(
        db_session, sample_source, url="https://example.com/rejected"
    )
    extraction = await _build_extraction(db_session, article)
    rejection = ArticleRejection(
        extraction_id=extraction.id,
        investor_take="対象外と判定された理由",
        ai_model="gemini-2.5-flash-lite",
    )
    db_session.add(rejection)
    await db_session.commit()
    article_id = article.id

    embedder = _mock_embedder()
    svc = EmbeddingService(session_factory)
    result = await svc.execute(article_id, embedder)

    assert isinstance(result, SkippedOutcome)
    assert result.reason == "analysis_rejected"
    embedder.embed_document.assert_not_called()


# ---------------------------------------------------------------------------
# Skipped: invalid_input (embedder が InvalidInputError を投げた)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_skipped_when_embedder_raises_invalid_input(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """embedder が InvalidInputError → SkippedOutcome(invalid_input)。

    DB は変更されないこと (commit が呼ばれない) も検証する。
    """
    article = await _build_article(
        db_session, sample_source, url="https://example.com/invalid"
    )
    extraction = await _build_extraction(db_session, article)
    analysis = await _build_analysis(db_session, extraction, sample_categories[0].id)
    await db_session.commit()
    article_id = article.id
    analysis_id = analysis.id

    embedder = _mock_embedder(raises=InvalidInputError("input rejected"))
    svc = EmbeddingService(session_factory)
    result = await svc.execute(article_id, embedder)

    assert isinstance(result, SkippedOutcome)
    assert result.reason == "invalid_input"

    db_session.expire_all()
    refetched = await db_session.get(ArticleAnalysis, analysis_id)
    assert refetched is not None
    assert refetched.embedding is None
    assert refetched.embedding_model is None


# ---------------------------------------------------------------------------
# Error propagation: RateLimit / Provider / Network は Task 層へ伝搬
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        RateLimitError("rate limited"),
        ProviderError("provider down"),
        NetworkError("timeout"),
    ],
)
async def test_execute_propagates_retryable_errors(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
    exc: Exception,
) -> None:
    """RateLimit/Provider/Network は Service で握らず Task 層に伝搬する。"""
    article = await _build_article(
        db_session,
        sample_source,
        url=f"https://example.com/error-{type(exc).__name__}",
    )
    extraction = await _build_extraction(db_session, article)
    await _build_analysis(db_session, extraction, sample_categories[0].id)
    await db_session.commit()
    article_id = article.id

    embedder = _mock_embedder(raises=exc)
    svc = EmbeddingService(session_factory)
    with pytest.raises(type(exc)):
        await svc.execute(article_id, embedder)


# ---------------------------------------------------------------------------
# _build_text: 結合フォーマット契約
# ---------------------------------------------------------------------------


def test_build_text_joins_title_and_summary_with_newline() -> None:
    """旧 ``build_embed_text`` と同一フォーマット契約を維持する。"""
    from datetime import datetime as _dt

    from app.analysis.classification.domain.analysis import Analysis
    from app.analysis.domain.value_objects.topic import TopicName

    analysis = Analysis(
        id=1,
        extraction_id=1,
        translated_title="タイトルです",
        summary="要約です",
        topic=TopicName(root="topic"),
        category_id=1,
        investor_take="視点",
        ai_model="m",
        analyzed_at=_dt(2026, 4, 25, tzinfo=UTC),
    )
    assert EmbeddingService._build_text(analysis) == "タイトルです\n要約です"
