"""EmbeddingService (app.analysis.embedding.service) の DB 統合テスト。

Pattern A' (typed-pipeline-preconditions.md §1.1) に従い Outcome は
``EmbeddedOutcome | InvalidInputOutcome`` の 2 variants に縮退している。
precondition 分岐 (extraction_not_found / analysis_pending / analysis_rejected /
既存 embedding) は ``ReadyForEmbedding.try_advance_from`` 側責務に移管したため
本ファイルでは扱わない (test_ready_for_embedding.py 参照)。

検証する経路:
- 正常系 (新規生成 → 永続化 → EmbeddedOutcome)
- 並行 race 敗北 → 読戻し → EmbeddedOutcome 合流
- InvalidInput (embedder が InvalidInputError) → InvalidInputOutcome
- エラー伝搬: RateLimit / Provider / Network は Task 層に伝搬
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.embedder.base import BaseEmbedder
from app.analysis.embedding.domain.embedding import Embedding
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.domain.value_objects import EMBEDDING_DIMENSION
from app.analysis.embedding.service import (
    EmbeddedOutcome,
    EmbeddingService,
    InvalidInputOutcome,
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


def _make_ready(
    analysis_id: int, *, text: str = "分析タイトル\n分析要約"
) -> ReadyForEmbedding:
    return ReadyForEmbedding(analysis_id=analysis_id, text_for_embedding=text)


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
    analysis_id = analysis.id

    embedder = _mock_embedder()
    svc = EmbeddingService(session_factory)
    ready = _make_ready(analysis_id)
    result = await svc.execute(ready, embedder)

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
# Race 敗北: 既に埋め込まれた行に対する save → None → 読戻し → EmbeddedOutcome 合流
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_reads_back_winner_when_save_loses_race(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """Ready 構築後に他ワーカーが先に書き込んだ場合、save は None を返し
    Service が ``find_by_analysis_id`` で勝者を読戻して EmbeddedOutcome に合流する。
    """
    article = await _build_article(
        db_session, sample_source, url="https://example.com/race"
    )
    extraction = await _build_extraction(db_session, article)
    analysis = await _build_analysis(
        db_session,
        extraction,
        sample_categories[0].id,
        embedding=[0.4] * EMBEDDING_DIMENSION,
    )
    await db_session.commit()
    analysis_id = analysis.id

    embedder = _mock_embedder()
    svc = EmbeddingService(session_factory)
    # Ready 構築自体は precondition (is_embedded_for) 済みの前提だが、本テストでは
    # race 状況をシミュレートするため Ready を直接構築して execute に渡す。
    ready = _make_ready(analysis_id)
    result = await svc.execute(ready, embedder)

    assert isinstance(result, EmbeddedOutcome)
    # 勝者 (先に書き込まれた値) を読戻している
    assert result.embedding.model_name == "cl-nagoya/ruri-v3-310m"


# ---------------------------------------------------------------------------
# InvalidInput: embedder が InvalidInputError を投げた
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_returns_invalid_input_outcome_when_embedder_rejects(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """embedder が InvalidInputError → InvalidInputOutcome。

    DB は変更されないこと (save が呼ばれない) も検証する。
    """
    article = await _build_article(
        db_session, sample_source, url="https://example.com/invalid"
    )
    extraction = await _build_extraction(db_session, article)
    analysis = await _build_analysis(db_session, extraction, sample_categories[0].id)
    await db_session.commit()
    analysis_id = analysis.id

    embedder = _mock_embedder(raises=InvalidInputError("input rejected"))
    svc = EmbeddingService(session_factory)
    ready = _make_ready(analysis_id)
    result = await svc.execute(ready, embedder)

    assert isinstance(result, InvalidInputOutcome)

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
    analysis = await _build_analysis(db_session, extraction, sample_categories[0].id)
    await db_session.commit()
    analysis_id = analysis.id

    embedder = _mock_embedder(raises=exc)
    svc = EmbeddingService(session_factory)
    ready = _make_ready(analysis_id)
    with pytest.raises(type(exc)):
        await svc.execute(ready, embedder)
