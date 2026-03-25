"""Tests for duplicate article detection service (Phase 4)."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analysis import ArticleAnalysis, ImpactLevel
from app.models.news import NewsArticle
from app.models.news_source import NewsSource
from app.services.dedup import DedupResult, detect_duplicates


def _make_embedding(seed: int = 0, dim: int = 768) -> list[float]:
    """Generate a deterministic embedding vector from a seed."""
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(dim)
    # Normalize to unit vector (cosine distance is meaningful for unit vectors)
    vec = vec / np.linalg.norm(vec)
    return vec.tolist()


def _make_similar_embedding(base: list[float], noise: float = 0.05) -> list[float]:
    """Create an embedding similar to base by adding small noise."""
    rng = np.random.default_rng(42)
    arr = np.array(base)
    perturbation = rng.standard_normal(len(base)) * noise
    result = arr + perturbation
    result = result / np.linalg.norm(result)
    return result.tolist()


async def _create_article_with_analysis(
    session: AsyncSession,
    source: NewsSource,
    *,
    title: str,
    url: str,
    embedding: list[float] | None = None,
    published_at: datetime | None = None,
) -> tuple[NewsArticle, ArticleAnalysis | None]:
    """Helper to create a news article and its analysis with embedding."""
    article = NewsArticle(
        original_title=title,
        original_url=url,
        news_source_id=source.id,
        published_at=published_at or datetime.now(UTC),
        # Legacy columns (NOT NULL)
        title_original=title,
        url=url,
        source=source.name,
    )
    session.add(article)
    await session.flush()

    analysis = None
    if embedding is not None:
        analysis = ArticleAnalysis(
            news_article_id=article.id,
            translated_title=f"Translated: {title}",
            summary="Test summary",
            impact_level=ImpactLevel.MEDIUM,
            reasoning="Test reasoning",
            ai_model="gemini-2.0-flash",
            embedding=embedding,
            embedding_model="text-embedding-004",
        )
        session.add(analysis)
        await session.flush()

    await session.commit()
    await session.refresh(article)
    if analysis:
        await session.refresh(analysis)
    return article, analysis


@pytest.mark.asyncio
async def test_detect_duplicates_no_articles(db_session: AsyncSession) -> None:
    """Empty article_ids returns zero results."""
    result = await detect_duplicates(db_session, [])
    assert result == DedupResult(processed=0, duplicates_found=0)


@pytest.mark.asyncio
async def test_detect_duplicates_no_match(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """Dissimilar articles are not matched."""
    now = datetime.now(UTC)
    emb_a = _make_embedding(seed=1)
    emb_b = _make_embedding(seed=999)  # very different

    a, _ = await _create_article_with_analysis(
        db_session,
        sample_source,
        title="Article A",
        url="https://a.com/1",
        embedding=emb_a,
        published_at=now,
    )
    await _create_article_with_analysis(
        db_session,
        sample_source,
        title="Article B",
        url="https://b.com/2",
        embedding=emb_b,
        published_at=now,
    )

    result = await detect_duplicates(db_session, [a.id], threshold=0.15)
    assert result.duplicates_found == 0


@pytest.mark.asyncio
async def test_detect_duplicates_finds_similar(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """Two similar articles are detected as duplicates."""
    now = datetime.now(UTC)
    emb_base = _make_embedding(seed=10)
    emb_similar = _make_similar_embedding(emb_base, noise=0.01)

    a, _ = await _create_article_with_analysis(
        db_session,
        sample_source,
        title="OpenAI Launches GPT-5",
        url="https://a.com/gpt5",
        embedding=emb_base,
        published_at=now,
    )
    b, _ = await _create_article_with_analysis(
        db_session,
        sample_source,
        title="OpenAI Unveils GPT-5 Model",
        url="https://b.com/gpt5",
        embedding=emb_similar,
        published_at=now + timedelta(hours=1),
    )

    # b is the new article to check
    result = await detect_duplicates(db_session, [b.id], threshold=0.5)
    assert result.processed == 1
    assert result.duplicates_found == 1


@pytest.mark.asyncio
async def test_detect_duplicates_time_window(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """Articles outside the time window are not matched."""
    now = datetime.now(UTC)
    emb_base = _make_embedding(seed=40)
    emb_similar = _make_similar_embedding(emb_base, noise=0.01)

    await _create_article_with_analysis(
        db_session,
        sample_source,
        title="Old Article",
        url="https://a.com/old",
        embedding=emb_base,
        published_at=now - timedelta(days=10),
    )
    new, _ = await _create_article_with_analysis(
        db_session,
        sample_source,
        title="New Similar Article",
        url="https://b.com/new",
        embedding=emb_similar,
        published_at=now,
    )

    result = await detect_duplicates(
        db_session, [new.id], threshold=0.5, time_window_days=3
    )
    assert result.duplicates_found == 0


@pytest.mark.asyncio
async def test_detect_duplicates_no_embedding_skipped(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """Articles without analysis embedding are not processed."""
    now = datetime.now(UTC)

    # Create article WITHOUT analysis (no embedding)
    article = NewsArticle(
        original_title="No Analysis",
        original_url="https://a.com/no-analysis",
        news_source_id=sample_source.id,
        published_at=now,
        title_original="No Analysis",
        url="https://a.com/no-analysis",
        source=sample_source.name,
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    result = await detect_duplicates(db_session, [article.id], threshold=0.5)
    assert result.processed == 0
    assert result.duplicates_found == 0
