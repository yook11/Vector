"""``PendingHtmlEnqueue`` の統合テスト (実 Postgres)。

Stage 1 (source_fetch) の ``pending_html_articles`` 投入 (``status='open'``
INSERT) の振る舞いを ``UNIQUE(url)`` と合わせて検証する。``url``
(``CanonicalArticleUrl`` 型で canonical 性を構造保証) が記事 identity の
唯一の authoritative。Stage 2 の claim/sweep/状態遷移は
``article_completion/test_repository.py`` 側で検証する。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.source_fetch.pending_enqueue import PendingHtmlEnqueue
from app.models.news_source import NewsSource
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl
from app.shared.value_objects.source_name import SourceName


def _observed(*, url: str, title: str = "Sample") -> ObservedArticle:
    return ObservedArticle(
        source_name=SourceName("Sample Source"),
        source_url=CanonicalArticleUrl(url),
        title=ObservedField(value=title, origin=ObservedOrigin.feed),
        published_at=ObservedField(
            value=PublishedAt(datetime(2026, 5, 1, tzinfo=UTC)),
            origin=ObservedOrigin.feed,
        ),
    )


@pytest.mark.asyncio
async def test_enqueue_returns_pending_id(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    enqueue = PendingHtmlEnqueue(db_session)
    pending_id = await enqueue.enqueue(
        _observed(url="https://example.com/p/save"),
        source_id=sample_source.id,
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
        _observed(url=url),
        source_id=sample_source.id,
        ready_at=datetime.now(UTC),
    )
    await db_session.commit()
    assert first is not None

    second = await enqueue.enqueue(
        _observed(url=url),
        source_id=sample_source.id,
        ready_at=datetime.now(UTC),
    )
    assert second is None


@pytest.mark.asyncio
async def test_enqueue_persists_url_in_column_not_jsonb(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """``url`` 列が canonical 値の authoritative で、JSONB には ``sourceName``
    が在り ``sourceUrl`` は ``Field(exclude=True)`` で焼かれない (二重管理排除)。
    """
    url = CanonicalArticleUrl("https://example.com/p/url-only")
    enqueue = PendingHtmlEnqueue(db_session)
    pending_id = await enqueue.enqueue(
        _observed(url=str(url)),
        source_id=sample_source.id,
        ready_at=datetime.now(UTC),
    )
    await db_session.commit()
    assert pending_id is not None

    row = (
        await db_session.execute(
            text(
                "SELECT url, staged_attributes FROM pending_html_articles "
                "WHERE id = :id"
            ),
            {"id": pending_id},
        )
    ).first()
    assert row is not None
    assert row.url == str(url)
    staged = row.staged_attributes
    if isinstance(staged, str):
        staged = json.loads(staged)
    assert staged["sourceName"] == "Sample Source"
    assert "sourceUrl" not in staged
    assert "source_url" not in staged
