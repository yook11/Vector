"""``ArticleStore`` の統合テスト (実 Postgres)。

``source_url`` (canonicalize 済み) を SSoT とする経路を検証する。
``save`` / ``exists_by_source_url`` と並行レース対応
(``ON CONFLICT DO NOTHING``) を検証する。Pattern R 即時獲得 / Pattern H
補完獲得の両工程が共有する書込側 Store。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.value_objects import PublishedAt
from app.collection.persistence.article_store import ArticleStore
from app.models.article import Article as ArticleORM
from app.models.news_source import NewsSource
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


def _ready(
    source_id: int,
    url: str,
    body: str = "x" * 60,
) -> AnalyzableArticle:
    return AnalyzableArticle(
        title="Ready Title",
        body=body,
        published_at=PublishedAt(datetime(2026, 3, 1, tzinfo=UTC)),
        source_id=source_id,
        source_url=CanonicalArticleUrl(url),
    )


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_persists_ready_article(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """``AnalyzableArticle`` を直 INSERT し、新規採番された ``article_id`` を返す。"""
    store = ArticleStore(db_session)
    ready = _ready(sample_source.id, "https://example.com/article/save-ready")

    article_id = await store.save(ready)
    assert isinstance(article_id, int)
    assert article_id > 0
    await db_session.commit()

    orm = await db_session.get(ArticleORM, article_id)
    assert orm is not None
    assert orm.original_title == "Ready Title"
    assert orm.original_content == "x" * 60
    assert orm.published_at == datetime(2026, 3, 1, tzinfo=UTC)
    assert str(orm.source_url) == "https://example.com/article/save-ready"


@pytest.mark.asyncio
async def test_save_returns_none_on_duplicate_source_url(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """同一 ``source_url`` への 2 度目の save は ``None`` (ON CONFLICT 吸収)。"""
    store = ArticleStore(db_session)
    url = "https://example.com/article/save-ready-dup"

    first = await store.save(_ready(sample_source.id, url))
    await db_session.commit()
    assert first is not None

    second = await store.save(_ready(sample_source.id, url))
    assert second is None


@pytest.mark.asyncio
async def test_save_does_not_commit(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """save は INSERT 発行のみ。commit は呼び出し側の責務。"""
    store = ArticleStore(db_session)
    article_id = await store.save(
        _ready(sample_source.id, "https://example.com/article/no-commit")
    )
    assert article_id is not None

    await db_session.rollback()
    assert await db_session.get(ArticleORM, article_id) is None


# ---------------------------------------------------------------------------
# exists_by_source_url (PR 3 で ArticleSeenRepository から統合)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exists_by_source_url_returns_true_when_present(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    url = "https://example.com/article/seen-true"
    store = ArticleStore(db_session)
    await store.save(_ready(sample_source.id, url))
    await db_session.commit()

    assert await store.exists_by_source_url(CanonicalArticleUrl(url)) is True


@pytest.mark.asyncio
async def test_exists_by_source_url_returns_false_when_absent(
    db_session: AsyncSession,
) -> None:
    assert (
        await ArticleStore(db_session).exists_by_source_url(
            CanonicalArticleUrl("https://example.com/article/seen-false")
        )
        is False
    )
