"""``PendingHtmlEnqueue`` の統合テスト (実 Postgres)。

Stage 1 (source_fetch) の ``pending_html_articles`` 投入 (``status='open'``
INSERT) の振る舞いを ``UNIQUE(url)`` と合わせて検証する。``url``
(``CanonicalArticleUrl`` 型で canonical 性を構造保証) が SSoT。Stage 2 の
claim/sweep/状態遷移は ``article_completion/test_repository.py`` 側で検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.domain.incomplete_article import (
    IncompleteArticle,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.source_fetch.pending_enqueue import PendingHtmlEnqueue
from app.models.news_source import NewsSource
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


def _incomplete(
    *,
    source_id: int,
    url: str,
    title: str = "Sample",
) -> IncompleteArticle:
    return IncompleteArticle(
        title=title,
        source_id=source_id,
        source_url=CanonicalArticleUrl(url),
        published_at_hint=PublishedAt(datetime(2026, 5, 1, tzinfo=UTC)),
        prefer_html_title=False,
    )


@pytest.mark.asyncio
async def test_enqueue_returns_pending_id(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    enqueue = PendingHtmlEnqueue(db_session)
    pending_id = await enqueue.enqueue(
        _incomplete(source_id=sample_source.id, url="https://example.com/p/save"),
        ready_at=datetime.now(UTC),
    )
    assert isinstance(pending_id, int)
    assert pending_id > 0


@pytest.mark.asyncio
async def test_enqueue_returns_none_on_duplicate_url(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """``UNIQUE(url)`` 違反 (同 tick race) は ``None`` で吸収される。"""
    url = "https://example.com/p/dup"
    enqueue = PendingHtmlEnqueue(db_session)
    first = await enqueue.enqueue(
        _incomplete(source_id=sample_source.id, url=url),
        ready_at=datetime.now(UTC),
    )
    await db_session.commit()
    assert first is not None

    second = await enqueue.enqueue(
        _incomplete(source_id=sample_source.id, url=url),
        ready_at=datetime.now(UTC),
    )
    assert second is None


@pytest.mark.asyncio
async def test_enqueue_persists_url(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """新規 pending 行は ``url`` (canonicalize 済み) のみで投入される。"""
    url = CanonicalArticleUrl("https://example.com/p/url-only")
    enqueue = PendingHtmlEnqueue(db_session)
    pending_id = await enqueue.enqueue(
        _incomplete(source_id=sample_source.id, url=str(url)),
        ready_at=datetime.now(UTC),
    )
    await db_session.commit()
    assert pending_id is not None

    row = (
        await db_session.execute(
            text("SELECT url FROM pending_html_articles WHERE id = :id"),
            {"id": pending_id},
        )
    ).first()
    assert row is not None
    assert row.url == str(url)
