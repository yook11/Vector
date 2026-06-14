"""EmbeddingRepository の DB 境界テスト。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)
from app.analysis.embedding.repository import EmbeddingRepository
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.news_source import NewsSource


async def _build_analysis(
    db_session: AsyncSession,
    source: NewsSource,
    category_id: int,
    *,
    url: str,
    embedding: list[float] | None = None,
) -> AnalyzedArticleRecord:
    """Stage 2 完了済みの分析行を 1 件作成する。"""
    article = AnalyzableArticleRecord(
        source_id=source.id,
        source_url=url,
        original_title="seed",
        original_content="content body content body content body content body",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()
    extraction = ArticleCuration(
        analyzable_article_id=article.id,
        translated_title="抽出タイトル",
        summary="抽出要約",
    )
    db_session.add(extraction)
    await db_session.flush()
    analysis = AnalyzedArticleRecord(
        curation_id=extraction.id,
        translated_title="分析タイトル",
        summary="分析要約",
        investor_take="投資家視点",
        category_id=category_id,
        embedding=embedding,
    )
    db_session.add(analysis)
    await db_session.commit()
    await db_session.refresh(analysis)
    return analysis


def _zero_vector() -> list[float]:
    return [0.0] * EMBEDDING_DIMENSION


def _vector(value: float = 0.1) -> EmbeddingVector:
    return EmbeddingVector(root=tuple([value] * EMBEDDING_DIMENSION))


@pytest.mark.asyncio
async def test_load_ready_build_facts_returns_values_when_unembedded(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    analysis = await _build_analysis(
        db_session,
        sample_source,
        sample_categories[0].id,
        url="https://example.com/load-ready-facts",
    )
    repo = EmbeddingRepository(db_session)
    curation = await db_session.get(ArticleCuration, analysis.curation_id)
    assert curation is not None

    facts = await repo.load_ready_build_facts(analysis.id)

    assert facts is not None
    assert facts.analyzable_article_id == curation.analyzable_article_id
    assert facts.has_embedding is False
    assert facts.translated_title == "分析タイトル"
    assert facts.summary == "分析要約"


@pytest.mark.asyncio
async def test_load_ready_build_facts_marks_existing_embedding(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    analysis = await _build_analysis(
        db_session,
        sample_source,
        sample_categories[0].id,
        url="https://example.com/load-ready-facts-embedded",
        embedding=_zero_vector(),
    )
    repo = EmbeddingRepository(db_session)

    facts = await repo.load_ready_build_facts(analysis.id)

    assert facts is not None
    assert facts.has_embedding is True


@pytest.mark.asyncio
async def test_load_ready_build_facts_returns_none_when_missing(
    db_session: AsyncSession,
) -> None:
    repo = EmbeddingRepository(db_session)
    assert await repo.load_ready_build_facts(999_999) is None


# save (条件付き UPDATE + RETURNING → bool)


@pytest.mark.asyncio
async def test_save_writes_embedding_and_returns_true(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    analysis = await _build_analysis(
        db_session,
        sample_source,
        sample_categories[0].id,
        url="https://example.com/save",
    )
    analyzed_article_id = analysis.id

    repo = EmbeddingRepository(db_session)
    saved = await repo.save(
        _vector(0.3),
        analyzed_article_id=analyzed_article_id,
    )
    await db_session.commit()

    assert saved is True
    db_session.expire_all()
    refetched = await db_session.get(AnalyzedArticleRecord, analyzed_article_id)
    assert refetched is not None
    assert refetched.embedding is not None


@pytest.mark.asyncio
async def test_save_returns_false_on_concurrent_write(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """既に他ワーカーが書いた行への 2 度目の save は False を返す。"""
    analysis = await _build_analysis(
        db_session,
        sample_source,
        sample_categories[0].id,
        url="https://example.com/already-saved",
        embedding=_zero_vector(),
    )
    analyzed_article_id = analysis.id

    repo = EmbeddingRepository(db_session)
    saved = await repo.save(
        _vector(0.7),
        analyzed_article_id=analyzed_article_id,
    )

    assert saved is False
    db_session.expire_all()
    refetched = await db_session.get(AnalyzedArticleRecord, analyzed_article_id)
    assert refetched is not None
    # 既存の embedding を上書きしないこと
    assert refetched.embedding is not None
    raw = refetched.embedding
    values = raw.to_list() if hasattr(raw, "to_list") else list(raw)
    assert values[0] == pytest.approx(0.0, abs=1e-3)


@pytest.mark.asyncio
async def test_save_returns_false_for_unknown_analyzed_article_id(
    db_session: AsyncSession,
) -> None:
    repo = EmbeddingRepository(db_session)
    saved = await repo.save(
        _vector(),
        analyzed_article_id=999_999,
    )
    assert saved is False
