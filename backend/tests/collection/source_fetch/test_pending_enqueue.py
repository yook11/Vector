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

from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.source_fetch.pending_enqueue import PendingHtmlEnqueue
from app.models.news_source import NewsSource
from app.shared.value_objects.source_name import SourceName


def _observed(
    *, url: str, source_name: SourceName, title: str = "Sample"
) -> ObservedArticle:
    return ObservedArticle(
        source_name=source_name,
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
        _observed(url="https://example.com/p/save", source_name=sample_source.name),
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
        _observed(url=url, source_name=sample_source.name),
        source_id=sample_source.id,
        ready_at=datetime.now(UTC),
    )
    await db_session.commit()
    assert first is not None

    second = await enqueue.enqueue(
        _observed(url=url, source_name=sample_source.name),
        source_id=sample_source.id,
        ready_at=datetime.now(UTC),
    )
    assert second is None


@pytest.mark.asyncio
async def test_enqueue_writes_identity_in_columns_not_jsonb(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """identity (``url`` / ``source_name``) を表層列に移し JSONB は snapshot 専用に。

    spec ``Pending source identity refactor.md`` #5 + #6 を pin。
    - #5: ``source_name`` が表層列に書かれる (倒立解消の writer 側証拠)
    - #6: JSONB に identity 系キー (``sourceName`` / ``sourceUrl`` /
      ``source_url``) が含まれない (snapshot 純化の negative invariant)

    Stage 1 writer 1 回呼んだ後の同一行の状態を 1 fixture で語る。
    """
    url = CanonicalArticleUrl("https://example.com/p/url-only")
    enqueue = PendingHtmlEnqueue(db_session)
    pending_id = await enqueue.enqueue(
        _observed(url=str(url), source_name=sample_source.name),
        source_id=sample_source.id,
        ready_at=datetime.now(UTC),
    )
    await db_session.commit()
    assert pending_id is not None

    row = (
        await db_session.execute(
            text(
                "SELECT url, source_name, staged_attributes "
                "FROM pending_html_articles WHERE id = :id"
            ),
            {"id": pending_id},
        )
    ).first()
    assert row is not None
    # #5: identity 列に書かれる
    assert row.url == str(url)
    assert row.source_name == str(sample_source.name)
    # #6: JSONB に identity 系キーが含まれない (snapshot 純化)
    staged = row.staged_attributes
    if isinstance(staged, str):
        staged = json.loads(staged)
    assert "sourceName" not in staged, f"JSONB に sourceName が残っている: {staged}"
    assert "sourceUrl" not in staged
    assert "source_url" not in staged


@pytest.mark.asyncio
async def test_enqueue_row_consistent_with_news_sources_join(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """Stage 1 writer が投入した行で ``(source_id, source_name)`` が
    ``news_sources`` の同一行を指す (spec #7)。

    composite FK ((source_id, source_name) → news_sources(id, name)) は
    INSERT 時に整合を強制するが、本テストは「writer が JOIN 一致する値を
    実際に書いている」ことを動作で語る (#2 は構造、#7 は writer の output)。
    """
    url = "https://example.com/p/join-check"
    enqueue = PendingHtmlEnqueue(db_session)
    pending_id = await enqueue.enqueue(
        _observed(url=url, source_name=sample_source.name),
        source_id=sample_source.id,
        ready_at=datetime.now(UTC),
    )
    await db_session.commit()
    assert pending_id is not None

    join_row = (
        await db_session.execute(
            text(
                "SELECT 1 AS hit FROM pending_html_articles p "
                "JOIN news_sources ns "
                "  ON ns.id = p.source_id AND ns.name = p.source_name "
                "WHERE p.id = :id"
            ),
            {"id": pending_id},
        )
    ).first()
    assert join_row is not None, (
        "pending 行が news_sources と (id, name) で JOIN 一致しない"
    )
