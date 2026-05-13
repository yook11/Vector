"""extraction リポジトリ (``ArticleRepository``) の統合テスト。

PR-E 仕様: ``source_url`` (canonicalize 済み) を SSoT とする経路を検証する。
``save`` / ``find_by_source_url`` と並行レース対応 (``ON CONFLICT DO NOTHING``)
を検証する。``exists_by_source_url`` は ingestion BC に移管済
(``tests/collection/ingestion/test_article_seen_repository.py``)。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.collection.extraction.domain import Article, PublishedAt
from app.collection.extraction.domain.article import ArticleDraft
from app.collection.extraction.repository import (
    ArticleRepository,
    PersistedArticleId,
)
from app.models.article import Article as ArticleORM
from app.models.news_source import NewsSource
from app.shared.value_objects.safe_url import SafeUrl


def _draft(
    body: str = "x" * 60, published_at: PublishedAt | None = None
) -> ArticleDraft:
    return ArticleDraft(title="Title", body=body, published_at=published_at)


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_returns_persisted_id(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    repo = ArticleRepository(db_session)
    persisted = await repo.save(
        _draft(published_at=PublishedAt(datetime(2026, 3, 1, tzinfo=UTC))),
        source_id=sample_source.id,
        source_url=SafeUrl("https://example.com/article/save"),
    )

    assert isinstance(persisted, PersistedArticleId)
    assert persisted.id > 0
    assert persisted.created_at.tzinfo is not None


@pytest.mark.asyncio
async def test_save_persists_payload(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """draft の title / body / published_at が ORM 行に正しく書き込まれる。"""
    body = "y" * 80
    draft = ArticleDraft(
        title="Payload Title",
        body=body,
        published_at=PublishedAt(datetime(2026, 3, 1, tzinfo=UTC)),
    )
    canonical = SafeUrl("https://example.com/article/payload")

    repo = ArticleRepository(db_session)
    persisted = await repo.save(draft, source_id=sample_source.id, source_url=canonical)
    assert persisted is not None
    await db_session.commit()

    orm = await db_session.get(ArticleORM, persisted.id)
    assert orm is not None
    assert orm.original_title == "Payload Title"
    assert orm.original_content == body
    assert orm.published_at == datetime(2026, 3, 1, tzinfo=UTC)
    assert str(orm.source_url) == str(canonical)


@pytest.mark.asyncio
async def test_save_accepts_none_published_at(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    repo = ArticleRepository(db_session)
    persisted = await repo.save(
        _draft(),
        source_id=sample_source.id,
        source_url=SafeUrl("https://example.com/article/no-date"),
    )
    assert persisted is not None
    await db_session.commit()

    orm = await db_session.get(ArticleORM, persisted.id)
    assert orm is not None
    assert orm.published_at is None


@pytest.mark.asyncio
async def test_save_returns_none_on_duplicate_source_url(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """同一 ``source_url`` への 2 度目の save は ``None`` (UNIQUE 違反吸収)。"""
    canonical = SafeUrl("https://example.com/article/dup")
    repo = ArticleRepository(db_session)

    first = await repo.save(_draft(), source_id=sample_source.id, source_url=canonical)
    await db_session.commit()
    assert first is not None

    second = await repo.save(_draft(), source_id=sample_source.id, source_url=canonical)
    assert second is None


@pytest.mark.asyncio
async def test_save_does_not_commit(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """save は INSERT 発行のみ。commit は呼び出し側の責務。"""
    repo = ArticleRepository(db_session)
    persisted = await repo.save(
        _draft(),
        source_id=sample_source.id,
        source_url=SafeUrl("https://example.com/article/no-commit"),
    )
    assert persisted is not None

    await db_session.rollback()
    rows = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# find_by_source_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_by_source_url_returns_entity(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    canonical = SafeUrl("https://example.com/article/find")
    repo = ArticleRepository(db_session)
    persisted = await repo.save(
        _draft(), source_id=sample_source.id, source_url=canonical
    )
    await db_session.commit()
    assert persisted is not None

    result = await repo.find_by_source_url(canonical)
    assert isinstance(result, Article)
    assert result.id == persisted.id


@pytest.mark.asyncio
async def test_find_by_source_url_returns_none_for_missing(
    db_session: AsyncSession,
) -> None:
    repo = ArticleRepository(db_session)
    assert await repo.find_by_source_url(SafeUrl("https://example.com/never")) is None


# ---------------------------------------------------------------------------
# 並行レース統合テスト
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_save_returns_one_persisted_one_none(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """同一 ``source_url`` への並行 save は片方が ``None`` になる。

    ``ON CONFLICT DO NOTHING`` の構造的並行制御を検証する。
    """
    canonical = SafeUrl("https://example.com/article/race")

    async def _save_in_new_session() -> PersistedArticleId | None:
        async with session_factory() as session:
            repo = ArticleRepository(session)
            persisted = await repo.save(
                _draft(), source_id=sample_source.id, source_url=canonical
            )
            await session.commit()
            return persisted

    results = await asyncio.gather(
        _save_in_new_session(),
        _save_in_new_session(),
    )

    assert sum(1 for r in results if r is not None) == 1
    assert sum(1 for r in results if r is None) == 1
