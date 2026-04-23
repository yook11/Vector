"""DiscoveredArticleRepository の統合テスト。"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.ingestion.repository import DiscoveredArticleRepository
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource
from app.shared.value_objects.safe_url import SafeUrl


@pytest.mark.asyncio
async def test_fetch_existing_urls_returns_empty_for_no_urls(
    db_session: AsyncSession,
) -> None:
    repo = DiscoveredArticleRepository(db_session)

    existing = await repo.fetch_existing_urls([])

    assert existing == set()


@pytest.mark.asyncio
async def test_fetch_existing_urls_returns_only_persisted_urls(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """DB に存在する URL のみが返り、未登録 URL は含まれない。"""
    persisted = DiscoveredArticle(
        original_title="Persisted",
        original_url="https://example.com/persisted",
        news_source_id=sample_source.id,
    )
    db_session.add(persisted)
    await db_session.commit()

    repo = DiscoveredArticleRepository(db_session)
    url_existing = SafeUrl("https://example.com/persisted")
    url_new = SafeUrl("https://example.com/new")

    existing = await repo.fetch_existing_urls([url_existing, url_new])

    assert existing == {url_existing}


@pytest.mark.asyncio
async def test_add_registers_discovered_article_in_session(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """add は session 経由で永続化対象に登録する。"""
    repo = DiscoveredArticleRepository(db_session)
    discovered = DiscoveredArticle(
        original_title="New",
        original_url=SafeUrl("https://example.com/new"),
        news_source_id=sample_source.id,
    )

    repo.add(discovered)
    await db_session.flush()

    assert discovered.id is not None
