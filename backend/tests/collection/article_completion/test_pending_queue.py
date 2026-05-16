"""``PendingHtmlQueue`` の統合テスト (実 Postgres)。

Stage 2 (article_completion) の ``find_by_id`` / ``claim_batch``
(FOR UPDATE SKIP LOCKED) / ``sweep_expired`` / ``mark_*`` / ``delete_one`` の
振る舞いを ``CHECK`` 制約と合わせて検証する。pending 行の投入は Stage 1 の
``PendingHtmlEnqueue`` を使う (1 テーブルを 2 工程から操作する分割の検証も兼ねる)。

``url`` (``CanonicalArticleUrl`` 型で canonical 性を構造保証) が SSoT。
``find_by_id`` は ``url`` を直接 SELECT して返す。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.article_completion.pending_queue import (
    PendingHtmlContext,
    PendingHtmlQueue,
)
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


async def _enqueue(
    db_session: AsyncSession,
    *,
    source_id: int,
    url: str,
    title: str = "Sample",
    ready_at: datetime,
) -> int:
    """Stage 1 投入で ``status='open'`` の pending を 1 件作る。"""
    pending_id = await PendingHtmlEnqueue(db_session).enqueue(
        _incomplete(source_id=source_id, url=url, title=title),
        ready_at=ready_at,
    )
    assert pending_id is not None
    return pending_id


# ---------------------------------------------------------------------------
# find_by_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_by_id_returns_context_with_url(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """``find_by_id`` は ``url`` を直接保持する row 値で返す (JOIN 撤去後)。"""
    url = CanonicalArticleUrl("https://example.com/p/find")
    pending_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        url=str(url),
        title="Find Me",
        ready_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    await db_session.commit()

    queue = PendingHtmlQueue(db_session)
    ctx = await queue.find_by_id(pending_id)
    assert isinstance(ctx, PendingHtmlContext)
    assert ctx.row_meta.id == pending_id
    assert ctx.row_meta.source_id == sample_source.id
    assert ctx.row_meta.status == "open"
    assert ctx.incomplete_article.title == "Find Me"
    assert ctx.incomplete_article.source_url == url
    assert ctx.row_meta.attempt_count == 0


@pytest.mark.asyncio
async def test_find_by_id_returns_none_for_missing(
    db_session: AsyncSession,
) -> None:
    queue = PendingHtmlQueue(db_session)
    assert await queue.find_by_id(999999) is None


# ---------------------------------------------------------------------------
# claim_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_batch_picks_only_open_ready(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """``status='open' AND ready_at <= NOW()`` のみ claim 対象。"""
    now = datetime.now(UTC)
    ready_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/ready",
        ready_at=now - timedelta(minutes=1),
    )
    await _enqueue(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/future",
        ready_at=now + timedelta(minutes=10),
    )
    await db_session.commit()

    queue = PendingHtmlQueue(db_session)
    ids = await queue.claim_batch(limit=10, lease_minutes=5)
    await db_session.commit()
    assert ids == [ready_id]


@pytest.mark.asyncio
async def test_claim_batch_advances_state_atomically(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """claim で running 化 + leased_until 設定 + attempt_count++ が一括適用される."""
    pending_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/claim-state",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()

    queue = PendingHtmlQueue(db_session)
    ids = await queue.claim_batch(limit=10, lease_minutes=5)
    await db_session.commit()
    assert ids == [pending_id]

    ctx = await queue.find_by_id(pending_id)
    assert ctx is not None
    assert ctx.row_meta.status == "running"
    assert ctx.row_meta.leased_until is not None
    delta = ctx.row_meta.leased_until - datetime.now(UTC)
    assert timedelta(minutes=4) <= delta <= timedelta(minutes=6)
    assert ctx.row_meta.attempt_count == 1


@pytest.mark.asyncio
async def test_claim_batch_respects_limit(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    now = datetime.now(UTC)
    for i in range(5):
        await _enqueue(
            db_session,
            source_id=sample_source.id,
            url=f"https://example.com/p/limit-{i}",
            ready_at=now - timedelta(seconds=1),
        )
    await db_session.commit()

    queue = PendingHtmlQueue(db_session)
    ids = await queue.claim_batch(limit=2, lease_minutes=5)
    assert len(ids) == 2


@pytest.mark.asyncio
async def test_concurrent_claim_batch_skips_locked(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """並行 claim_batch は FOR UPDATE SKIP LOCKED で同じ行を二重 claim しない."""
    now = datetime.now(UTC)
    created_ids: list[int] = []
    for i in range(4):
        pid = await _enqueue(
            db_session,
            source_id=sample_source.id,
            url=f"https://example.com/p/race-{i}",
            ready_at=now - timedelta(seconds=1),
        )
        created_ids.append(pid)
    await db_session.commit()

    async def _claim_in_new_session() -> list[int]:
        async with session_factory() as session:
            queue = PendingHtmlQueue(session)
            ids = await queue.claim_batch(limit=10, lease_minutes=5)
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
    pending_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/sweep",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.execute(
        text(
            "UPDATE pending_html_articles "
            "SET status='running', leased_until=NOW() - INTERVAL '1 minute' "
            "WHERE id = :id"
        ),
        {"id": pending_id},
    )
    await db_session.commit()

    queue = PendingHtmlQueue(db_session)
    swept = await queue.sweep_expired()
    await db_session.commit()
    assert swept == 1

    ctx = await queue.find_by_id(pending_id)
    assert ctx is not None
    assert ctx.row_meta.status == "open"
    assert ctx.row_meta.leased_until is None


@pytest.mark.asyncio
async def test_sweep_expired_leaves_live_lease(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """生きている lease (leased_until > NOW) は触らない。"""
    await _enqueue(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/sweep-live",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    queue = PendingHtmlQueue(db_session)
    await queue.claim_batch(limit=10, lease_minutes=5)
    await db_session.commit()

    swept = await queue.sweep_expired()
    assert swept == 0


# ---------------------------------------------------------------------------
# mark_terminal / mark_exhausted / mark_will_retry / delete_one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_terminal_closes_pending(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    pending_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/terminal",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    queue = PendingHtmlQueue(db_session)
    await queue.claim_batch(limit=10, lease_minutes=5)
    await db_session.commit()

    await queue.mark_terminal(pending_id)
    await db_session.commit()

    ctx = await queue.find_by_id(pending_id)
    assert ctx is not None
    assert ctx.row_meta.status == "closed"
    assert ctx.row_meta.leased_until is None


@pytest.mark.asyncio
async def test_mark_exhausted_closes_pending(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """``mark_exhausted`` は DB 上 ``mark_terminal`` と同じ状態に閉じる."""
    pending_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/exhausted",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    queue = PendingHtmlQueue(db_session)
    await queue.claim_batch(limit=10, lease_minutes=5)
    await db_session.commit()

    await queue.mark_exhausted(pending_id)
    await db_session.commit()

    ctx = await queue.find_by_id(pending_id)
    assert ctx is not None
    assert ctx.row_meta.status == "closed"
    assert ctx.row_meta.leased_until is None


@pytest.mark.asyncio
async def test_mark_will_retry_reopens_with_future_ready_at(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """一時失敗で ``open`` + 未来 ``ready_at`` + ``leased_until=NULL`` に戻る。"""
    pending_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/retry",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    queue = PendingHtmlQueue(db_session)
    await queue.claim_batch(limit=10, lease_minutes=5)
    await db_session.commit()

    next_at = datetime.now(UTC) + timedelta(minutes=15)
    await queue.mark_will_retry(pending_id, ready_at=next_at)
    await db_session.commit()

    ctx = await queue.find_by_id(pending_id)
    assert ctx is not None
    assert ctx.row_meta.status == "open"
    assert ctx.row_meta.leased_until is None
    assert ctx.row_meta.ready_at is not None
    assert abs((ctx.row_meta.ready_at - next_at).total_seconds()) < 1


@pytest.mark.asyncio
async def test_delete_one_removes_row(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """成功時の片付け: ``articles`` INSERT と同 tx で pending を消す想定。"""
    pending_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/delete",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()

    queue = PendingHtmlQueue(db_session)
    await queue.delete_one(pending_id)
    await db_session.commit()

    assert await queue.find_by_id(pending_id) is None
