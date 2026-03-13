"""Tests for duplicate article detection service (3B-1)."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article_group import ArticleGroup
from app.models.news import NewsArticle
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


async def _create_article(
    session: AsyncSession,
    *,
    title: str,
    url: str,
    source: str = "TestSource",
    embedding: list[float] | None = None,
    published_at: datetime | None = None,
    article_group_id: int | None = None,
) -> NewsArticle:
    """Helper to create a news article."""
    article = NewsArticle(
        title_original=title,
        url=url,
        source=source,
        embedding=embedding,
        published_at=published_at or datetime.now(UTC),
        article_group_id=article_group_id,
    )
    session.add(article)
    await session.commit()
    await session.refresh(article)
    return article


@pytest.mark.asyncio
async def test_detect_duplicates_no_articles(db_session: AsyncSession) -> None:
    """Empty article_ids returns zero results."""
    result = await detect_duplicates(db_session, [])
    assert result == DedupResult(processed=0, grouped=0, new_groups=0)


@pytest.mark.asyncio
async def test_detect_duplicates_no_match(db_session: AsyncSession) -> None:
    """Dissimilar articles are not grouped."""
    now = datetime.now(UTC)
    emb_a = _make_embedding(seed=1)
    emb_b = _make_embedding(seed=999)  # very different

    a = await _create_article(
        db_session,
        title="Article A",
        url="https://a.com/1",
        embedding=emb_a,
        published_at=now,
    )
    await _create_article(
        db_session,
        title="Article B",
        url="https://b.com/2",
        embedding=emb_b,
        published_at=now,
    )

    result = await detect_duplicates(db_session, [a.id], threshold=0.15)
    assert result.grouped == 0
    assert result.new_groups == 0


@pytest.mark.asyncio
async def test_detect_duplicates_creates_group(db_session: AsyncSession) -> None:
    """Two similar articles are grouped together."""
    now = datetime.now(UTC)
    emb_base = _make_embedding(seed=10)
    emb_similar = _make_similar_embedding(emb_base, noise=0.01)

    a = await _create_article(
        db_session,
        title="OpenAI Launches GPT-5",
        url="https://a.com/gpt5",
        source="TechCrunch",
        embedding=emb_base,
        published_at=now,
    )
    b = await _create_article(
        db_session,
        title="OpenAI Unveils GPT-5 Model",
        url="https://b.com/gpt5",
        source="Reuters",
        embedding=emb_similar,
        published_at=now + timedelta(hours=1),
    )

    # b is the new article to check
    result = await detect_duplicates(db_session, [b.id], threshold=0.5)
    assert result.grouped == 1
    assert result.new_groups == 1

    # Both articles should be in the same group
    await db_session.refresh(a)
    await db_session.refresh(b)
    assert a.article_group_id is not None
    assert a.article_group_id == b.article_group_id


@pytest.mark.asyncio
async def test_detect_duplicates_joins_existing_group(
    db_session: AsyncSession,
) -> None:
    """A new similar article joins an existing group."""
    now = datetime.now(UTC)
    emb_base = _make_embedding(seed=20)

    # Create group with one article
    group = ArticleGroup(article_count=1)
    db_session.add(group)
    await db_session.flush()

    a = await _create_article(
        db_session,
        title="Quantum Breakthrough",
        url="https://a.com/quantum",
        embedding=emb_base,
        published_at=now,
        article_group_id=group.id,
    )
    group.canonical_id = a.id
    db_session.add(group)
    await db_session.commit()

    # New similar article
    emb_similar = _make_similar_embedding(emb_base, noise=0.01)
    c = await _create_article(
        db_session,
        title="Quantum Computing Breakthrough Announced",
        url="https://c.com/quantum",
        source="TheVerge",
        embedding=emb_similar,
        published_at=now + timedelta(hours=2),
    )

    result = await detect_duplicates(db_session, [c.id], threshold=0.5)
    assert result.grouped == 1
    assert result.new_groups == 0  # joined existing, did not create new

    await db_session.refresh(c)
    assert c.article_group_id == group.id

    await db_session.refresh(group)
    assert group.article_count == 2


@pytest.mark.asyncio
async def test_detect_duplicates_skips_already_grouped(
    db_session: AsyncSession,
) -> None:
    """Articles already in a group are skipped."""
    now = datetime.now(UTC)
    emb = _make_embedding(seed=30)

    group = ArticleGroup(article_count=1)
    db_session.add(group)
    await db_session.flush()

    a = await _create_article(
        db_session,
        title="Already Grouped",
        url="https://a.com/grouped",
        embedding=emb,
        published_at=now,
        article_group_id=group.id,
    )
    group.canonical_id = a.id
    db_session.add(group)
    await db_session.commit()

    result = await detect_duplicates(db_session, [a.id], threshold=0.5)
    assert result.processed == 0  # skipped because already grouped


@pytest.mark.asyncio
async def test_detect_duplicates_time_window(db_session: AsyncSession) -> None:
    """Articles outside the time window are not matched."""
    now = datetime.now(UTC)
    emb_base = _make_embedding(seed=40)
    emb_similar = _make_similar_embedding(emb_base, noise=0.01)

    await _create_article(
        db_session,
        title="Old Article",
        url="https://a.com/old",
        embedding=emb_base,
        published_at=now - timedelta(days=10),
    )
    new = await _create_article(
        db_session,
        title="New Similar Article",
        url="https://b.com/new",
        embedding=emb_similar,
        published_at=now,
    )

    result = await detect_duplicates(
        db_session, [new.id], threshold=0.5, time_window_days=3
    )
    assert result.grouped == 0


@pytest.mark.asyncio
async def test_detect_duplicates_canonical_selection(
    db_session: AsyncSession,
) -> None:
    """Canonical article is the one with the earliest published_at."""
    now = datetime.now(UTC)
    emb_base = _make_embedding(seed=50)
    emb_similar = _make_similar_embedding(emb_base, noise=0.01)

    # Earlier article
    early = await _create_article(
        db_session,
        title="Early Report",
        url="https://a.com/early",
        source="Reuters",
        embedding=emb_base,
        published_at=now - timedelta(hours=5),
    )
    # Later article
    late = await _create_article(
        db_session,
        title="Late Report",
        url="https://b.com/late",
        source="TechCrunch",
        embedding=emb_similar,
        published_at=now,
    )

    await detect_duplicates(db_session, [late.id], threshold=0.5)

    await db_session.refresh(early)
    await db_session.refresh(late)
    assert early.article_group_id is not None

    group = await db_session.get(ArticleGroup, early.article_group_id)
    assert group is not None
    assert group.canonical_id == early.id  # earlier article is canonical
