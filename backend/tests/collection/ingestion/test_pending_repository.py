"""``PendingHtmlArticleRepository`` の統合テスト (実 Postgres)。

``create`` / ``find_by_id`` / ``claim_batch`` (FOR UPDATE SKIP LOCKED) /
``sweep_expired`` / ``mark_*`` / ``delete_one`` の振る舞いを ``CHECK`` 制約と
合わせて検証する。

PR-E (article_urls 廃止プラン) で ``create`` から ``article_url_id`` 引数が
消え、``url`` (canonicalize 済み SafeUrl) が SSoT になった。``find_by_id``
は JOIN を撤去して ``url`` のみ返す。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.ingestion.pending_repository import (
    PendingHtmlArticleRepository,
    PendingHtmlContext,
)
from app.collection.ingestion.staged_attributes import StagedArticleAttributes
from app.models.news_source import NewsSource
from app.shared.value_objects.safe_url import SafeUrl


def _attrs(title: str = "Sample") -> StagedArticleAttributes:
    return StagedArticleAttributes(
        title=title,
        published_at_hint=PublishedAt(datetime(2026, 5, 1, tzinfo=UTC)),
        prefer_html_title=False,
    )


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_returns_pending_id(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    repo = PendingHtmlArticleRepository(db_session)
    pending_id = await repo.create(
        url=SafeUrl("https://example.com/p/create"),
        source_id=sample_source.id,
        staged_attributes=_attrs(),
        ready_at=datetime.now(UTC),
    )
    assert isinstance(pending_id, int)
    assert pending_id > 0


@pytest.mark.asyncio
async def test_create_returns_none_on_duplicate_url(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """``UNIQUE(url)`` 違反 (同 tick race) は ``None`` で吸収される。"""
    url = SafeUrl("https://example.com/p/dup")
    repo = PendingHtmlArticleRepository(db_session)
    first = await repo.create(
        url=url,
        source_id=sample_source.id,
        staged_attributes=_attrs(),
        ready_at=datetime.now(UTC),
    )
    await db_session.commit()
    assert first is not None

    second = await repo.create(
        url=url,
        source_id=sample_source.id,
        staged_attributes=_attrs(),
        ready_at=datetime.now(UTC),
    )
    assert second is None


@pytest.mark.asyncio
async def test_create_persists_url_with_null_article_url_id(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """PR-E: 新規 pending 行は ``url`` のみ保持し ``article_url_id`` は NULL。"""
    url = SafeUrl("https://example.com/p/url-only")
    repo = PendingHtmlArticleRepository(db_session)
    pending_id = await repo.create(
        url=url,
        source_id=sample_source.id,
        staged_attributes=_attrs(),
        ready_at=datetime.now(UTC),
    )
    await db_session.commit()
    assert pending_id is not None

    row = (
        await db_session.execute(
            text(
                "SELECT url, article_url_id FROM pending_html_articles WHERE id = :id"
            ),
            {"id": pending_id},
        )
    ).first()
    assert row is not None
    assert row.url == str(url)
    assert row.article_url_id is None


# ---------------------------------------------------------------------------
# find_by_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_by_id_returns_context_with_url(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """``find_by_id`` は ``url`` を直接保持する row 値で返す (JOIN 撤去後)。"""
    url = SafeUrl("https://example.com/p/find")
    repo = PendingHtmlArticleRepository(db_session)
    pending_id = await repo.create(
        url=url,
        source_id=sample_source.id,
        staged_attributes=_attrs(title="Find Me"),
        ready_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    await db_session.commit()
    assert pending_id is not None

    ctx = await repo.find_by_id(pending_id)
    assert isinstance(ctx, PendingHtmlContext)
    assert ctx.id == pending_id
    assert ctx.source_id == sample_source.id
    assert ctx.status == "open"
    assert ctx.staged_attributes.title == "Find Me"
    assert ctx.url == url
    assert ctx.article_url_id is None  # PR-E ingestion で NULL
    assert ctx.attempt_count == 0


@pytest.mark.asyncio
async def test_find_by_id_returns_none_for_missing(
    db_session: AsyncSession,
) -> None:
    repo = PendingHtmlArticleRepository(db_session)
    assert await repo.find_by_id(999999) is None


# ---------------------------------------------------------------------------
# claim_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_batch_picks_only_open_ready(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """``status='open' AND ready_at <= NOW()`` のみ claim 対象。"""
    repo = PendingHtmlArticleRepository(db_session)
    now = datetime.now(UTC)

    ready_id = await repo.create(
        url=SafeUrl("https://example.com/p/ready"),
        source_id=sample_source.id,
        staged_attributes=_attrs(),
        ready_at=now - timedelta(minutes=1),
    )
    await repo.create(
        url=SafeUrl("https://example.com/p/future"),
        source_id=sample_source.id,
        staged_attributes=_attrs(),
        ready_at=now + timedelta(minutes=10),
    )
    await db_session.commit()

    ids = await repo.claim_batch(limit=10, lease_minutes=5)
    await db_session.commit()
    assert ids == [ready_id]


@pytest.mark.asyncio
async def test_claim_batch_advances_state_atomically(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """claim で running 化 + leased_until 設定 + attempt_count++ が一括適用される."""
    repo = PendingHtmlArticleRepository(db_session)
    pending_id = await repo.create(
        url=SafeUrl("https://example.com/p/claim-state"),
        source_id=sample_source.id,
        staged_attributes=_attrs(),
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()

    ids = await repo.claim_batch(limit=10, lease_minutes=5)
    await db_session.commit()
    assert ids == [pending_id]

    ctx = await repo.find_by_id(pending_id)
    assert ctx is not None
    assert ctx.status == "running"
    assert ctx.leased_until is not None
    delta = ctx.leased_until - datetime.now(UTC)
    assert timedelta(minutes=4) <= delta <= timedelta(minutes=6)
    assert ctx.attempt_count == 1


@pytest.mark.asyncio
async def test_claim_batch_respects_limit(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    repo = PendingHtmlArticleRepository(db_session)
    now = datetime.now(UTC)
    for i in range(5):
        await repo.create(
            url=SafeUrl(f"https://example.com/p/limit-{i}"),
            source_id=sample_source.id,
            staged_attributes=_attrs(),
            ready_at=now - timedelta(seconds=1),
        )
    await db_session.commit()

    ids = await repo.claim_batch(limit=2, lease_minutes=5)
    assert len(ids) == 2


@pytest.mark.asyncio
async def test_concurrent_claim_batch_skips_locked(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """並行 claim_batch は FOR UPDATE SKIP LOCKED で同じ行を二重 claim しない."""
    repo = PendingHtmlArticleRepository(db_session)
    now = datetime.now(UTC)
    created_ids: list[int] = []
    for i in range(4):
        pid = await repo.create(
            url=SafeUrl(f"https://example.com/p/race-{i}"),
            source_id=sample_source.id,
            staged_attributes=_attrs(),
            ready_at=now - timedelta(seconds=1),
        )
        assert pid is not None
        created_ids.append(pid)
    await db_session.commit()

    async def _claim_in_new_session() -> list[int]:
        async with session_factory() as session:
            repo2 = PendingHtmlArticleRepository(session)
            ids = await repo2.claim_batch(limit=10, lease_minutes=5)
            await session.commit()
            return ids

    results = await asyncio.gather(
        _claim_in_new_session(),
        _claim_in_new_session(),
    )
    flat = [pid for chunk in results for pid in chunk]
    assert sorted(flat) == sorted(created_ids)
    assert len(flat) == len(set(flat))


# ---------------------------------------------------------------------------
# sweep_expired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_expired_reopens_dead_lease(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """死んだ lease (running + leased_until <= NOW) は ``open`` に戻される。"""
    repo = PendingHtmlArticleRepository(db_session)
    pending_id = await repo.create(
        url=SafeUrl("https://example.com/p/sweep"),
        source_id=sample_source.id,
        staged_attributes=_attrs(),
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert pending_id is not None
    await db_session.execute(
        text(
            "UPDATE pending_html_articles "
            "SET status='running', leased_until=NOW() - INTERVAL '1 minute' "
            "WHERE id = :id"
        ),
        {"id": pending_id},
    )
    await db_session.commit()

    swept = await repo.sweep_expired()
    await db_session.commit()
    assert swept == 1

    ctx = await repo.find_by_id(pending_id)
    assert ctx is not None
    assert ctx.status == "open"
    assert ctx.leased_until is None


@pytest.mark.asyncio
async def test_sweep_expired_leaves_live_lease(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """生きている lease (leased_until > NOW) は触らない。"""
    repo = PendingHtmlArticleRepository(db_session)
    pending_id = await repo.create(
        url=SafeUrl("https://example.com/p/sweep-live"),
        source_id=sample_source.id,
        staged_attributes=_attrs(),
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert pending_id is not None
    await repo.claim_batch(limit=10, lease_minutes=5)
    await db_session.commit()

    swept = await repo.sweep_expired()
    assert swept == 0


# ---------------------------------------------------------------------------
# mark_terminal / mark_exhausted / mark_will_retry / delete_one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_terminal_closes_pending(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    repo = PendingHtmlArticleRepository(db_session)
    pending_id = await repo.create(
        url=SafeUrl("https://example.com/p/terminal"),
        source_id=sample_source.id,
        staged_attributes=_attrs(),
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert pending_id is not None
    await repo.claim_batch(limit=10, lease_minutes=5)
    await db_session.commit()

    await repo.mark_terminal(pending_id)
    await db_session.commit()

    ctx = await repo.find_by_id(pending_id)
    assert ctx is not None
    assert ctx.status == "closed"
    assert ctx.leased_until is None


@pytest.mark.asyncio
async def test_mark_exhausted_closes_pending(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """``mark_exhausted`` は DB 上 ``mark_terminal`` と同じ状態に閉じる."""
    repo = PendingHtmlArticleRepository(db_session)
    pending_id = await repo.create(
        url=SafeUrl("https://example.com/p/exhausted"),
        source_id=sample_source.id,
        staged_attributes=_attrs(),
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert pending_id is not None
    await repo.claim_batch(limit=10, lease_minutes=5)
    await db_session.commit()

    await repo.mark_exhausted(pending_id)
    await db_session.commit()

    ctx = await repo.find_by_id(pending_id)
    assert ctx is not None
    assert ctx.status == "closed"
    assert ctx.leased_until is None


@pytest.mark.asyncio
async def test_mark_will_retry_reopens_with_future_ready_at(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """一時失敗で ``open`` + 未来 ``ready_at`` + ``leased_until=NULL`` に戻る。"""
    repo = PendingHtmlArticleRepository(db_session)
    pending_id = await repo.create(
        url=SafeUrl("https://example.com/p/retry"),
        source_id=sample_source.id,
        staged_attributes=_attrs(),
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert pending_id is not None
    await repo.claim_batch(limit=10, lease_minutes=5)
    await db_session.commit()

    next_at = datetime.now(UTC) + timedelta(minutes=15)
    await repo.mark_will_retry(pending_id, ready_at=next_at)
    await db_session.commit()

    ctx = await repo.find_by_id(pending_id)
    assert ctx is not None
    assert ctx.status == "open"
    assert ctx.leased_until is None
    assert ctx.ready_at is not None
    assert abs((ctx.ready_at - next_at).total_seconds()) < 1


@pytest.mark.asyncio
async def test_delete_one_removes_row(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """成功時の片付け: ``articles`` INSERT と同 tx で pending を消す想定。"""
    repo = PendingHtmlArticleRepository(db_session)
    pending_id = await repo.create(
        url=SafeUrl("https://example.com/p/delete"),
        source_id=sample_source.id,
        staged_attributes=_attrs(),
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert pending_id is not None
    await db_session.commit()

    await repo.delete_one(pending_id)
    await db_session.commit()

    assert await repo.find_by_id(pending_id) is None
