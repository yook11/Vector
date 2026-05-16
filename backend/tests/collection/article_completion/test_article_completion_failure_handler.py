"""``ArticleCompletionFailureHandler`` の integration test。

検証する性質 (failure 後処理 = ``pending_html_articles`` 状態遷移):

- ``Terminal`` → pending を ``closed`` (leased_until=None)
- ``Retryable`` 非 exhausted → pending ``open`` + 未来 ready_at (policy schedule)
- ``Retryable`` exhausted (attempt_count >= policy.max_attempts) → ``closed``
- ``Retryable`` + server 指示 retry_after_seconds → その秒数で ready_at

handler は分類済 ``CompletionDisposition`` を受け、自前 session で状態遷移 +
log を完結させる (service の主線とは別ファイル / 別責務)。handler は DB を
再読込せず ``pending.row_meta.attempt_count`` を exhausted 判定に使うため、
exhausted ケースは attempt_count を先に UPDATE してから context を構築する。
handler は別 session で commit するので、検証前に ``db_session`` を rollback
して fresh transaction で読む (cross-session read)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.collection.article_completion.disposition import Retryable, Terminal
from app.collection.article_completion.failure_handling import (
    ArticleCompletionFailureHandler,
)
from app.collection.article_completion.pending_queue import (
    PendingHtmlContext,
    PendingHtmlQueue,
)
from app.collection.article_completion.retry_policy import (
    BLIP_POLICY,
    RETRY_AFTER_POLICY,
)
from app.collection.domain.incomplete_article import (
    IncompleteArticle,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.source_fetch.pending_enqueue import PendingHtmlEnqueue
from app.models.news_source import NewsSource, SourceType
from app.models.pending_html_article import PendingHtmlArticle
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


@pytest.fixture
async def tc_source(db_session: AsyncSession) -> NewsSource:
    source = NewsSource(
        name="TechCrunch",
        source_type=SourceType.RSS,
        site_url="https://techcrunch.com",
        endpoint_url="https://techcrunch.com/feed/",
        is_active=True,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


def _incomplete(source: NewsSource, url: str) -> IncompleteArticle:
    return IncompleteArticle(
        title="TC Title",
        source_id=source.id,
        source_url=CanonicalArticleUrl(url),
        published_at_hint=PublishedAt(datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)),
        prefer_html_title=False,
    )


async def _make_context(
    db_session: AsyncSession,
    source: NewsSource,
    url: str,
) -> PendingHtmlContext:
    """``pending_html_articles`` 行を 1 件作って claim 済 context を返す。

    claim 後 ``status='running'`` / ``attempt_count=1``。返す context は
    handler 入力用の合成 view (``find_by_id`` 経由)。
    """
    pending_id = await PendingHtmlEnqueue(db_session).enqueue(
        _incomplete(source, url),
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert pending_id is not None
    await db_session.commit()
    pending_queue = PendingHtmlQueue(db_session)
    ids = await pending_queue.claim_batch(limit=10, lease_minutes=5)
    await db_session.commit()
    assert pending_id in ids
    ctx = await pending_queue.find_by_id(pending_id)
    assert ctx is not None
    return ctx


async def _reload_pending(
    db_session: AsyncSession, pending_id: int
) -> PendingHtmlArticle:
    """handler の別 session commit を見るため fresh tx で pending を読み直す。"""
    await db_session.rollback()
    return (
        await db_session.execute(
            select(PendingHtmlArticle).where(PendingHtmlArticle.id == pending_id)
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_terminal_closes_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """``Terminal`` → pending status='closed' / leased_until=None。"""
    ctx = await _make_context(db_session, tc_source, "https://techcrunch.com/term")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle(ctx, Terminal(reason_code="test_terminal"))

    pending = await _reload_pending(db_session, ctx.row_meta.id)
    assert pending.status == "closed"
    assert pending.leased_until is None


@pytest.mark.asyncio
async def test_retryable_non_exhausted_reopens_with_future_ready_at(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """``Retryable`` (BLIP, attempt=1 < max) → open + 未来 ready_at (約 30 秒後)。

    BLIP_POLICY.schedule[0] = 0.5 分 = 30 秒。claim 直後 attempt_count=1 <
    max_attempts(8) なので exhausted ではなく mark_will_retry。
    """
    ctx = await _make_context(db_session, tc_source, "https://techcrunch.com/blip")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle(ctx, Retryable(reason_code="blip", policy=BLIP_POLICY))

    pending = await _reload_pending(db_session, ctx.row_meta.id)
    assert pending.status == "open"
    assert pending.leased_until is None
    assert pending.ready_at is not None
    delta = pending.ready_at - datetime.now(UTC)
    assert timedelta(seconds=20) < delta < timedelta(seconds=40)


@pytest.mark.asyncio
async def test_retryable_exhausted_closes_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """attempt_count >= policy.max_attempts → ``closed``。

    handler は DB 再読込せず ``pending.row_meta.attempt_count`` を見るため、
    attempt_count を max まで UPDATE → commit → その後 context を構築して
    exhausted 判定に反映させる (context 作成後の UPDATE は反映されない)。
    """
    ctx = await _make_context(db_session, tc_source, "https://techcrunch.com/exhaust")
    await db_session.execute(
        text("UPDATE pending_html_articles SET attempt_count = :n WHERE id = :id"),
        {"n": BLIP_POLICY.max_attempts, "id": ctx.row_meta.id},
    )
    await db_session.commit()
    exhausted_ctx = await PendingHtmlQueue(db_session).find_by_id(ctx.row_meta.id)
    assert exhausted_ctx is not None
    assert exhausted_ctx.row_meta.attempt_count == BLIP_POLICY.max_attempts
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle(
        exhausted_ctx, Retryable(reason_code="blip", policy=BLIP_POLICY)
    )

    pending = await _reload_pending(db_session, ctx.row_meta.id)
    assert pending.status == "closed"
    assert pending.leased_until is None


@pytest.mark.asyncio
async def test_retryable_uses_server_retry_after_seconds(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """``Retryable`` + retry_after_seconds=120 → ready_at が約 120 秒後。

    server 指示 (RETRY_AFTER policy + override 秒) は policy schedule より優先。
    """
    ctx = await _make_context(db_session, tc_source, "https://techcrunch.com/ra")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle(
        ctx,
        Retryable(
            reason_code="server_retry_after",
            policy=RETRY_AFTER_POLICY,
            retry_after_seconds=120.0,
        ),
    )

    pending = await _reload_pending(db_session, ctx.row_meta.id)
    assert pending.status == "open"
    assert pending.ready_at is not None
    delta = pending.ready_at - datetime.now(UTC)
    assert timedelta(seconds=100) < delta < timedelta(seconds=140)
