"""EmbeddingService (app.analysis.embedding.service) の DB 統合テスト。

Stage 5 は pipeline 終端のため execute は副作用のみ (永続化) を行い ``None`` を
返す。Outcome / Entity 復元 / 読み戻しは廃止済み (2026-05-12)。precondition 分岐
+ embedder 入力 text 取得は ``ReadyForEmbedding`` 構造保証に移管したため本
ファイルでは扱わない (test_ready_for_embedding.py 参照)。

検証する経路:
- 正常系 (新規生成 → 永続化 → None)
- 並行 update で先に書かれていた → log + None で短絡 (DB は先行値のまま)
- InvalidInput (embedder が InvalidInputError) → log + None で短絡、DB 未変更
- エラー伝搬: RateLimit / Provider / Network は Task 層に伝搬
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.domain.value_objects import EMBEDDING_DIMENSION
from app.analysis.embedding.service import EmbeddingService
from app.analysis.errors import (
    InvalidInputError,
    NetworkError,
    ProviderError,
    RateLimitError,
)
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment
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
    article = Article(
        source_id=source.id,
        source_url=url,
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
) -> InScopeAssessment:
    analysis = InScopeAssessment(
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
    analysis_id: int,
    *,
    text: str = "分析タイトル\n分析要約",
) -> ReadyForEmbedding:
    return ReadyForEmbedding(analysis_id=analysis_id, text_for_embedding=text)


# ---------------------------------------------------------------------------
# Happy path: 永続化されて None が返る
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_persists_embedding_on_success(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """正常系: 埋め込み生成 → 永続化 → None 返却。"""
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

    assert result is None
    # 厚い Ready 経由で text を直接渡す
    embedder.embed_document.assert_called_once_with("分析タイトル\n分析要約")

    db_session.expire_all()
    refetched = await db_session.get(InScopeAssessment, analysis_id)
    assert refetched is not None
    assert refetched.embedding is not None
    assert refetched.embedding_model == "cl-nagoya/ruri-v3-310m"


# ---------------------------------------------------------------------------
# 並行 update で先に書かれていた: save が False → log + None で短絡
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_shortcircuits_when_already_persisted(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """Ready 構築後に他ワーカーが先に書き込んだ場合、save は False を返し
    Service が log + None で短絡する。DB は先行値のまま上書きされない。
    """
    preexisting_vector = [0.4] * EMBEDDING_DIMENSION
    article = await _build_article(
        db_session, sample_source, url="https://example.com/concurrent"
    )
    extraction = await _build_extraction(db_session, article)
    analysis = await _build_analysis(
        db_session,
        extraction,
        sample_categories[0].id,
        embedding=preexisting_vector,
    )
    await db_session.commit()
    analysis_id = analysis.id

    embedder = _mock_embedder(vector=[0.9] * EMBEDDING_DIMENSION)
    svc = EmbeddingService(session_factory)
    # Ready 構築時には未 embedded だったが、その直後に他ワーカーが先に書き込んだ
    # 並行状況を再現するため Ready を直接構築して execute に渡す。
    ready = _make_ready(analysis_id)
    result = await svc.execute(ready, embedder)

    assert result is None
    # 先行する write の値のまま、後続の save で上書きされていない
    db_session.expire_all()
    refetched = await db_session.get(InScopeAssessment, analysis_id)
    assert refetched is not None
    raw = refetched.embedding
    values = raw.to_list() if hasattr(raw, "to_list") else list(raw)
    assert values[0] == pytest.approx(0.4, abs=1e-3)


# ---------------------------------------------------------------------------
# InvalidInput: embedder が InvalidInputError を投げた
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_returns_none_when_embedder_rejects(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """embedder が InvalidInputError → log + None で短絡。

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

    assert result is None

    db_session.expire_all()
    refetched = await db_session.get(InScopeAssessment, analysis_id)
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
