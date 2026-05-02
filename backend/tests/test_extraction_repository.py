"""extraction リポジトリ (``ArticleRepository``) の統合テスト。

Entity / VO ベースの API (``ArticleRepository.save``,
``find_by_discovered_article_id``) と並行レース対応
(``ON CONFLICT DO NOTHING``) を検証する。
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
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource


async def _make_discovered(
    db_session: AsyncSession, source: NewsSource, url: str
) -> DiscoveredArticle:
    discovered = DiscoveredArticle(
        original_title="seed",
        original_url=url,
        news_source_id=source.id,
    )
    db_session.add(discovered)
    await db_session.commit()
    await db_session.refresh(discovered)
    return discovered


def _draft(
    body: str = "x" * 60, published_at: PublishedAt | None = None
) -> ArticleDraft:
    return ArticleDraft(title="Title", body=body, published_at=published_at)


async def _save(
    repo: ArticleRepository,
    draft: ArticleDraft,
    discovered: DiscoveredArticle,
) -> PersistedArticleId | None:
    """テスト用 ``ArticleRepository.save`` ラッパ。``DiscoveredArticle`` から
    ``source_id`` / ``source_url`` を派生させる。

    Phase 0b で ``save`` の引数に ``source_id`` / ``source_url`` が必須化された
    ため、テスト側はここで束ねて取り回す。
    """
    return await repo.save(
        draft,
        discovered_article_id=discovered.id,
        source_id=discovered.news_source_id,
        source_url=discovered.original_url,
    )


# ---------------------------------------------------------------------------
# ArticleRepository.save
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_returns_persisted_id(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/save"
    )
    draft = _draft(
        published_at=PublishedAt(datetime(2026, 3, 1, tzinfo=UTC)),
    )

    repo = ArticleRepository(db_session)
    persisted = await _save(repo, draft, discovered)

    assert isinstance(persisted, PersistedArticleId)
    assert persisted.id > 0
    assert persisted.created_at.tzinfo is not None


@pytest.mark.asyncio
async def test_save_persists_draft_payload(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/payload"
    )
    body = "y" * 80
    draft = ArticleDraft(
        title="Payload Title",
        body=body,
        published_at=PublishedAt(datetime(2026, 3, 1, tzinfo=UTC)),
    )

    repo = ArticleRepository(db_session)
    persisted = await _save(repo, draft, discovered)
    assert persisted is not None
    await db_session.commit()

    orm = await db_session.get(ArticleORM, persisted.id)
    assert orm is not None
    assert orm.original_title == "Payload Title"
    assert orm.original_content == body
    assert orm.published_at == datetime(2026, 3, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_save_accepts_none_published_at(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/no-date"
    )

    repo = ArticleRepository(db_session)
    persisted = await _save(repo, _draft(), discovered)
    assert persisted is not None
    await db_session.commit()

    orm = await db_session.get(ArticleORM, persisted.id)
    assert orm is not None
    assert orm.published_at is None


@pytest.mark.asyncio
async def test_save_returns_none_on_duplicate_in_same_session(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """同一 ``discovered_article_id`` への 2 度目の save は ``None`` を返す。

    並行レース敗北の挙動を単一セッションで再現する単体テスト。
    """
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/dup"
    )
    repo = ArticleRepository(db_session)

    first = await _save(repo, _draft(), discovered)
    await db_session.commit()
    assert first is not None

    second = await _save(repo, _draft(), discovered)
    assert second is None


@pytest.mark.asyncio
async def test_save_does_not_commit(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """save は INSERT 発行のみ。commit は呼び出し側の責務。"""
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/no-commit"
    )

    repo = ArticleRepository(db_session)
    persisted = await _save(repo, _draft(), discovered)
    assert persisted is not None

    # ロールバックで消える = コミットされていない
    await db_session.rollback()
    rows = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# ArticleRepository.find_by_discovered_article_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_by_discovered_article_id_returns_entity(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/find"
    )
    repo = ArticleRepository(db_session)
    persisted = await _save(repo, _draft(), discovered)
    await db_session.commit()
    assert persisted is not None

    result = await repo.find_by_discovered_article_id(discovered.id)
    assert isinstance(result, Article)
    assert result.id == persisted.id
    assert result.discovered_article_id == discovered.id


@pytest.mark.asyncio
async def test_find_by_discovered_article_id_returns_none_for_missing(
    db_session: AsyncSession,
) -> None:
    repo = ArticleRepository(db_session)
    assert await repo.find_by_discovered_article_id(999999) is None


# ---------------------------------------------------------------------------
# 並行レース統合テスト (PR 2a の主目的)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_save_returns_one_persisted_one_none(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """同一 ``discovered_article_id`` への並行 save は片方が ``None`` になる。

    ``ON CONFLICT DO NOTHING`` の構造的並行制御を検証する。2 つの独立した
    セッションで同時に save を発行し、片方が ``PersistedArticleId``、
    もう片方が ``None`` (並行レース敗北) を返すことを確認する。
    """
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/race"
    )

    async def _save_in_new_session() -> PersistedArticleId | None:
        async with session_factory() as session:
            repo = ArticleRepository(session)
            persisted = await _save(repo, _draft(), discovered)
            await session.commit()
            return persisted

    results = await asyncio.gather(
        _save_in_new_session(),
        _save_in_new_session(),
    )

    assert sum(1 for r in results if r is not None) == 1
    assert sum(1 for r in results if r is None) == 1
