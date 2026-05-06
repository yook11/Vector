"""``ArticleUrlRepository.upsert_returning`` の統合テスト。

新規 URL は id を返し、既知 URL は ``None`` を返す race-safe upsert の
振る舞いを実 Postgres に対して検証する。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.ingestion.url_repository import ArticleUrlRepository
from app.models.news_source import NewsSource
from app.shared.value_objects.safe_url import SafeUrl


@pytest.mark.asyncio
async def test_upsert_returning_returns_id_for_new_url(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    repo = ArticleUrlRepository(db_session)
    url_id = await repo.upsert_returning(
        normalized_url=SafeUrl("https://example.com/au/new"),
        original_url=SafeUrl("https://example.com/au/new"),
        first_seen_source_id=sample_source.id,
    )
    assert isinstance(url_id, int)
    assert url_id > 0


@pytest.mark.asyncio
async def test_upsert_returning_returns_none_for_known_url(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """同一 ``normalized_url`` への 2 度目の呼び出しは ``None``。"""
    repo = ArticleUrlRepository(db_session)

    first = await repo.upsert_returning(
        normalized_url=SafeUrl("https://example.com/au/dup"),
        original_url=SafeUrl("https://example.com/au/dup"),
        first_seen_source_id=sample_source.id,
    )
    await db_session.commit()
    assert first is not None

    second = await repo.upsert_returning(
        normalized_url=SafeUrl("https://example.com/au/dup"),
        original_url=SafeUrl("https://example.com/au/dup"),
        first_seen_source_id=sample_source.id,
    )
    assert second is None


@pytest.mark.asyncio
async def test_concurrent_upsert_returning_one_id_one_none(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """並行 ``upsert_returning`` は片方が id、もう片方が ``None``。"""

    async def _upsert_in_new_session() -> int | None:
        async with session_factory() as session:
            repo = ArticleUrlRepository(session)
            url_id = await repo.upsert_returning(
                normalized_url=SafeUrl("https://example.com/au/race"),
                original_url=SafeUrl("https://example.com/au/race"),
                first_seen_source_id=sample_source.id,
            )
            await session.commit()
            return url_id

    results = await asyncio.gather(
        _upsert_in_new_session(),
        _upsert_in_new_session(),
    )

    assert sum(1 for r in results if r is not None) == 1
    assert sum(1 for r in results if r is None) == 1
