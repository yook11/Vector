"""``ArticleCompletionFailureHandler`` の integration test。

検証する性質 (failure 後処理 = ``pending_html_articles`` 状態遷移):

- acquisition ``Terminal`` → pending を ``closed`` (leased_until=None)
- acquisition ``Retryable`` 非 exhausted → ``open`` + 未来 ready_at (policy schedule)
- acquisition ``Retryable`` exhausted (attempt_count >= max_attempts) → ``closed``
- acquisition ``Retryable`` + server 指示 retry_after_seconds → その秒数で ready_at
- completion ``CompletionRejection`` → pending を ``closed`` (retry なし)

handler は 2 入口: ``handle_acquisition_failure`` (分類済 ``AcquisitionDecision``)
と ``handle_completion_rejected`` (``CompletionRejection``)。いずれも自前 session
で状態遷移 + log を完結させる (service の主線とは別ファイル / 別責務)。handler は
DB を再読込せず ``ready.attempt_count`` を exhausted 判定に使うため、exhausted
ケースは attempt_count を先に UPDATE してから Ready を構築する。handler は別
session で commit するので、検証前に ``db_session`` を rollback して fresh
transaction で読む (cross-session read)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.collection.article_completion.acquisition_failure import Retryable, Terminal
from app.collection.article_completion.completion_failure import CompletionRejection
from app.collection.article_completion.failure_handling import (
    ArticleCompletionFailureHandler,
)
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.repository import ArticleCompletionRepository
from app.collection.article_completion.retry_policy import (
    BLIP_POLICY,
    RETRY_AFTER_POLICY,
)
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.source_fetch.pending_enqueue import PendingHtmlEnqueue
from app.models.news_source import NewsSource, SourceType
from app.models.pending_html_article import PendingHtmlArticle
from app.shared.value_objects.source_name import SourceName


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


def _observed(source: NewsSource, url: str) -> ObservedArticle:
    return ObservedArticle(
        source_name=SourceName(str(source.name)),
        source_url=CanonicalArticleUrl(url),
        title=ObservedField(value="TC Title", origin=ObservedOrigin.feed),
        published_at=ObservedField(
            value=PublishedAt(datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)),
            origin=ObservedOrigin.feed,
        ),
    )


async def _make_ready(
    db_session: AsyncSession,
    source: NewsSource,
    url: str,
) -> ReadyForArticleCompletion:
    """``pending_html_articles`` 行を 1 件作って claim 済 Ready を返す。

    claim 後 ``status='running'`` / ``attempt_count=1``。返す Ready は
    Task 層が ``try_advance_from`` で構築するのと同じ厚い precondition 型。
    """
    pending_id = await PendingHtmlEnqueue(db_session).enqueue(
        _observed(source, url),
        source_id=source.id,
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert pending_id is not None
    await db_session.commit()
    now = datetime.now(UTC)
    repository = ArticleCompletionRepository(db_session)
    ids = await repository.claim_ready_batch(
        limit=10,
        now=now,
        leased_until=now + timedelta(minutes=5),
    )
    await db_session.commit()
    assert pending_id in ids
    ready = await repository.try_load_for_completion(pending_id)
    assert ready is not None
    return ready


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
async def test_acquisition_terminal_closes_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """acquisition ``Terminal`` → pending status='closed' / leased_until=None。"""
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/term")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_acquisition_failure(
        ready, Terminal(reason_code="test_terminal")
    )

    pending = await _reload_pending(db_session, ready.pending_id)
    assert pending.status == "closed"
    assert pending.leased_until is None


@pytest.mark.asyncio
async def test_acquisition_retryable_non_exhausted_reopens_with_future_ready_at(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """acquisition ``Retryable`` (BLIP, attempt=1 < max) → open + 未来 ready_at。

    BLIP_POLICY.schedule[0] = 0.5 分 = 30 秒。claim 直後 attempt_count=1 <
    max_attempts(8) なので exhausted ではなく retry scheduling。
    """
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/blip")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_acquisition_failure(
        ready, Retryable(reason_code="blip", policy=BLIP_POLICY)
    )

    pending = await _reload_pending(db_session, ready.pending_id)
    assert pending.status == "open"
    assert pending.leased_until is None
    assert pending.ready_at is not None
    delta = pending.ready_at - datetime.now(UTC)
    assert timedelta(seconds=20) < delta < timedelta(seconds=40)


@pytest.mark.asyncio
async def test_acquisition_retryable_exhausted_closes_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """acquisition ``Retryable`` で attempt_count >= max_attempts → ``closed``。

    handler は DB 再読込せず ``ready.attempt_count`` を見るため、
    attempt_count を max まで UPDATE → commit → その後 Ready を構築して
    exhausted 判定に反映させる (Ready 構築後の UPDATE は反映されない)。
    """
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/exhaust")
    await db_session.execute(
        update(PendingHtmlArticle)
        .where(PendingHtmlArticle.id == ready.pending_id)
        .values(attempt_count=BLIP_POLICY.max_attempts)
    )
    await db_session.commit()
    exhausted_ready = await ArticleCompletionRepository(
        db_session
    ).try_load_for_completion(ready.pending_id)
    assert exhausted_ready is not None
    assert exhausted_ready.attempt_count == BLIP_POLICY.max_attempts
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_acquisition_failure(
        exhausted_ready, Retryable(reason_code="blip", policy=BLIP_POLICY)
    )

    pending = await _reload_pending(db_session, ready.pending_id)
    assert pending.status == "closed"
    assert pending.leased_until is None


@pytest.mark.asyncio
async def test_acquisition_retryable_uses_server_retry_after_seconds(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """acquisition ``Retryable`` + retry_after_seconds=120 → ready_at が約 120 秒後。

    server 指示 (RETRY_AFTER policy + override 秒) は policy schedule より優先。
    """
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/ra")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_acquisition_failure(
        ready,
        Retryable(
            reason_code="server_retry_after",
            policy=RETRY_AFTER_POLICY,
            retry_after_seconds=120.0,
        ),
    )

    pending = await _reload_pending(db_session, ready.pending_id)
    assert pending.status == "open"
    assert pending.ready_at is not None
    delta = pending.ready_at - datetime.now(UTC)
    assert timedelta(seconds=100) < delta < timedelta(seconds=140)


@pytest.mark.asyncio
async def test_completion_rejected_closes_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """completion ``CompletionRejection`` → pending status='closed'。

    Stage 2 拒絶は Accept 軸で retry を持たず、acquisition Terminal と同様に
    pending を閉じる (別入口 / 別 log event だが状態遷移は同じ closed)。
    leased_until も None に戻る。
    """
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/reject")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_completion_rejected(
        ready, CompletionRejection(reason_code="completion_published_at_missing")
    )

    pending = await _reload_pending(db_session, ready.pending_id)
    assert pending.status == "closed"
    assert pending.leased_until is None
