"""``ArticleCompletionAuditRepository`` の integration test。

handler 経由で焼かれる経路 (scrape outcome / completion rejected / stale) は
``test_article_completion_failure_handler.py`` が所有する。本ファイルは repository を
直接呼び、handler 経由では届きにくい契約を検証する:

- ``append_persist_outcome`` の 3 outcome (成功 / superseded / url_conflict)
- 2 軸原則: ``ParseCrashed`` = failed + error_class、``ContentQualityTooLow`` =
  rejected + error_class None / 構造化列
- ``append_persist_crashed`` (経路 9) の error_class / error_chain / redact
- repository は ``session.commit()`` を呼ばない (caller tx 境界保持)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.audit.stages.completion import ArticleCompletionAuditRepository
from app.collection.article_acquisition.repository import IncompleteArticleRepository
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.repository import (
    ArticleCompletionRepository,
    CompletionSucceeded,
    CompletionSuperseded,
    CompletionUrlConflict,
)
from app.collection.article_completion.scrape_failure import (
    ContentQualityTooLow,
    ParseCrashed,
)
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.persistence.article_store import ArticleStore
from app.collection.sources.source_name import SourceName
from app.models.news_source import NewsSource, SourceType
from app.models.pipeline_event import PipelineEvent

_URL = "https://techcrunch.com/audit"


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
    db_session: AsyncSession, source: NewsSource, url: str
) -> ReadyForArticleCompletion:
    """claim 済 Ready (status='running' / attempt_count=1) を返す。"""
    pending_id = await IncompleteArticleRepository(db_session).save(
        _observed(source, url),
        source_id=source.id,
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert pending_id is not None
    await db_session.commit()
    now = datetime.now(UTC)
    repository = ArticleCompletionRepository(db_session)
    await repository.claim_ready_batch(
        limit=10, now=now, leased_until=now + timedelta(minutes=5)
    )
    await db_session.commit()
    ready = await repository.try_load_for_completion(pending_id)
    assert ready is not None
    return ready


def _analyzable(source: NewsSource, url: str) -> AnalyzableArticle:
    return AnalyzableArticle(
        title="A Real Title",
        body="x" * 120,
        published_at=PublishedAt(datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)),
        source_id=source.id,
        source_url=CanonicalArticleUrl(url),
    )


async def _fetch_one(db_session: AsyncSession, source_id: int) -> PipelineEvent:
    await db_session.rollback()
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(
                    PipelineEvent.stage == "completion",
                    PipelineEvent.source_id == source_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    return rows[0]


@pytest.mark.asyncio
async def test_append_persist_outcome_success(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """成功 → succeeded / article_completed / body_length。body_head は焼かない。"""
    ready = await _make_ready(db_session, tc_source, _URL)
    advanced = _analyzable(tc_source, _URL)
    article_id = await ArticleStore(db_session).save(advanced)
    await db_session.commit()
    assert article_id is not None

    async with session_factory() as session:
        await ArticleCompletionAuditRepository(session).append_persist_outcome(
            ready=ready,
            outcome=CompletionSucceeded(article_id=article_id),
            advanced=advanced,
        )
        await session.commit()

    ev = await _fetch_one(db_session, tc_source.id)
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "article_completed"
    assert ev.article_id == article_id
    assert ev.category is None
    assert ev.payload["canonical_url"] == _URL
    assert ev.payload["body_length"] == len(advanced.body)  # 入力由来
    assert ev.payload["body_head"] is None  # 成功は焼かない (articles 重複)
    assert ev.payload["scraper_class"] is None  # 定数列は書かない


@pytest.mark.asyncio
async def test_append_persist_outcome_superseded(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """superseded (経路 6) → skipped / persist_superseded / article_id None。"""
    ready = await _make_ready(db_session, tc_source, _URL)
    advanced = _analyzable(tc_source, _URL)

    async with session_factory() as session:
        await ArticleCompletionAuditRepository(session).append_persist_outcome(
            ready=ready, outcome=CompletionSuperseded(), advanced=advanced
        )
        await session.commit()

    ev = await _fetch_one(db_session, tc_source.id)
    assert ev.event_type == "skipped"
    assert ev.outcome_code == "persist_superseded"
    assert ev.article_id is None
    assert ev.payload["body_length"] is None  # 完成 body は破棄、焼かない


@pytest.mark.asyncio
async def test_append_persist_outcome_url_conflict(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """url_conflict (経路 7) → skipped / persist_url_conflict / article_id None。"""
    ready = await _make_ready(db_session, tc_source, _URL)
    advanced = _analyzable(tc_source, _URL)

    async with session_factory() as session:
        await ArticleCompletionAuditRepository(session).append_persist_outcome(
            ready=ready, outcome=CompletionUrlConflict(), advanced=advanced
        )
        await session.commit()

    ev = await _fetch_one(db_session, tc_source.id)
    assert ev.event_type == "skipped"
    assert ev.outcome_code == "persist_url_conflict"
    assert ev.article_id is None


@pytest.mark.asyncio
async def test_append_scrape_outcome_parse_crashed_is_failed_with_error_class(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """ParseCrashed は parser 例外を伴う技術故障 → failed + error_class 記録。"""
    ready = await _make_ready(db_session, tc_source, _URL)

    async with session_factory() as session:
        await ArticleCompletionAuditRepository(session).append_scrape_outcome(
            ready=ready,
            failure=ParseCrashed(error_class="LxmlError", error_message="boom"),
        )
        await session.commit()

    ev = await _fetch_one(db_session, tc_source.id)
    assert ev.event_type == "failed"
    assert ev.outcome_code == "scrape_parse_crashed"
    assert ev.error_class == "LxmlError"
    assert ev.payload["error_message"] == "boom"


@pytest.mark.asyncio
async def test_append_scrape_outcome_content_quality_is_rejected_with_metric(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """ContentQualityTooLow は内容判定 → rejected + error_class None + 構造化列。"""
    ready = await _make_ready(db_session, tc_source, _URL)

    async with session_factory() as session:
        await ArticleCompletionAuditRepository(session).append_scrape_outcome(
            ready=ready,
            failure=ContentQualityTooLow(
                body_length=12, title_present=False, body_sample="too short"
            ),
        )
        await session.commit()

    ev = await _fetch_one(db_session, tc_source.id)
    assert ev.event_type == "rejected"
    assert ev.outcome_code == "scrape_content_quality_too_low"
    assert ev.error_class is None  # 値判定なので mechanism なし
    assert ev.payload["body_length"] == 12
    assert ev.payload["quality_gate_metric"] == {"title_present": False}
    assert ev.payload["body_head"] == "too short"  # 失敗経路は唯一の witness


@pytest.mark.asyncio
async def test_append_persist_crashed_records_failed_with_chain_and_redaction(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """経路 9: failed / persist_crashed / error_class FQN / error_chain / redact。"""
    ready = await _make_ready(db_session, tc_source, _URL)
    try:
        try:
            raise ValueError("inner cause")
        except ValueError as inner:
            raise RuntimeError(
                "Authorization: Bearer "
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.SflKxwRJSMeKKF2QT4secret db down"
            ) from inner
    except RuntimeError as exc:
        captured = exc

    async with session_factory() as session:
        await ArticleCompletionAuditRepository(session).append_persist_crashed(
            ready=ready, exc=captured
        )
        await session.commit()

    ev = await _fetch_one(db_session, tc_source.id)
    assert ev.event_type == "failed"
    assert ev.outcome_code == "persist_crashed"
    assert ev.error_class is not None
    assert ev.error_class.endswith(".RuntimeError")
    # cause chain が FQN list で残る (RuntimeError → ValueError)
    assert ev.payload["error_chain"][0].endswith(".RuntimeError")
    assert any(c.endswith(".ValueError") for c in ev.payload["error_chain"])
    # secret は redact 済
    assert ev.payload["error_message"] is not None
    assert "SflKxwRJSMeKKF2QT4secret" not in ev.payload["error_message"]
    assert "***" in ev.payload["error_message"]


@pytest.mark.asyncio
async def test_repository_does_not_commit(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """repository は caller の commit を奪わない (未 commit は永続化されない)。"""
    source_id = tc_source.id  # rollback 前に確保 (rollback は ORM 属性を expire させる)
    ready = await _make_ready(db_session, tc_source, _URL)

    async with session_factory() as session:
        await ArticleCompletionAuditRepository(session).append_stale_attempt(
            ready=ready
        )
        # 意図的に commit しない

    await db_session.rollback()
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(
                    PipelineEvent.stage == "completion",
                    PipelineEvent.source_id == source_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 0
