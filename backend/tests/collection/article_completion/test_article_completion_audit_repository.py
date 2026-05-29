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
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.stages.completion import ArticleCompletionAuditRepository
from app.collection.article_acquisition.repository import IncompleteArticleRepository
from app.collection.article_completion.ready import (
    ArticleCompletionReadyBuildFacts,
    ArticleCompletionReadyBuildPendingMissingError,
    ArticleCompletionReadyBuildPendingNotRunningError,
    ArticleCompletionReadyBuildUrlInvalidError,
    ReadyForArticleCompletion,
)
from app.collection.article_completion.repository import (
    ArticleCompletionRepository,
    CompletionSucceeded,
    CompletionSuperseded,
    CompletionUrlConflict,
)
from app.collection.article_completion.scrape_failure import (
    ContentQualityTooLow,
    FetchFailed,
    ParseCrashed,
)
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedArticleInvalidError,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchGatewayError,
)
from app.collection.persistence.article_store import ArticleStore
from app.collection.sources.errors import SourceNotRegisteredError
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
    return await ReadyForArticleCompletion.try_advance_from(
        pending_id=pending_id,
        repo=repository,
    )


def _ready_build_facts(
    source: NewsSource,
    *,
    pending_id: int = 42,
    status: str = "running",
    url: str = "https://techcrunch.com/bad",
    attempt_count: int = 1,
) -> ArticleCompletionReadyBuildFacts:
    return ArticleCompletionReadyBuildFacts(
        pending_id=pending_id,
        source_id=source.id,
        source_name=SourceName(str(source.name)),
        status=status,
        staged_attributes={},
        source_url=url,
        attempt_count=attempt_count,
    )


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


async def _fetch_by_outcome(
    db_session: AsyncSession, outcome_code: str
) -> PipelineEvent:
    await db_session.rollback()
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(
                    PipelineEvent.stage == "completion",
                    PipelineEvent.outcome_code == outcome_code,
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
    assert ev.retryability is None
    assert ev.payload["canonical_url"] == _URL
    assert ev.payload["attempt_count"] == ready.attempt_count
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
    assert ev.retryability is None
    assert ev.payload["attempt_count"] == ready.attempt_count
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
    assert ev.retryability is None
    assert ev.payload["attempt_count"] == ready.attempt_count


@pytest.mark.asyncio
async def test_append_scrape_outcome_retryable_fetch_failed_projection(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """retryable な transport 失敗は failed + retryability / failure_kind を焼く。"""
    ready = await _make_ready(db_session, tc_source, _URL)

    async with session_factory() as session:
        await ArticleCompletionAuditRepository(session).append_scrape_outcome(
            ready=ready,
            failure=FetchFailed(error=FetchGatewayError(status_code=502)),
        )
        await session.commit()

    ev = await _fetch_one(db_session, tc_source.id)
    assert ev.event_type == "failed"
    assert ev.outcome_code == "fetch_gateway_failure"
    assert ev.retryability == "retryable"
    assert ev.payload["attempt_count"] == ready.attempt_count
    assert ev.payload["failure_kind"] == "external_fetch"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_append_scrape_outcome_terminal_fetch_failed_projection(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """terminal な transport 失敗は non_retryable として projection する。"""
    ready = await _make_ready(db_session, tc_source, _URL)

    async with session_factory() as session:
        await ArticleCompletionAuditRepository(session).append_scrape_outcome(
            ready=ready,
            failure=FetchFailed(
                error=FetchAccessDeniedError(status_code=403, reason="forbidden")
            ),
        )
        await session.commit()

    ev = await _fetch_one(db_session, tc_source.id)
    assert ev.event_type == "failed"
    assert ev.outcome_code == "fetch_access_denied"
    assert ev.retryability == "non_retryable"
    assert ev.payload["attempt_count"] == ready.attempt_count
    assert ev.payload["failure_kind"] == "external_fetch"
    assert ev.payload["failure_action"] is None


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
    assert ev.retryability == "non_retryable"
    assert ev.error_class == "LxmlError"
    assert ev.payload["attempt_count"] == ready.attempt_count
    assert ev.payload["error_message"] == "boom"
    assert ev.payload["failure_kind"] == "scrape_parse_crashed"
    assert ev.payload["failure_action"] is None


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
    assert ev.retryability is None
    assert ev.error_class is None  # 値判定なので mechanism なし
    assert ev.payload["attempt_count"] == ready.attempt_count
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
    assert ev.retryability == "unknown"
    assert ev.error_class is not None
    assert ev.error_class.endswith(".RuntimeError")
    assert ev.payload["attempt_count"] == ready.attempt_count
    assert ev.payload["failure_kind"] == "persist_crashed"
    assert ev.payload["failure_action"] is None
    # cause chain が FQN list で残る (RuntimeError → ValueError)
    assert ev.payload["error_chain"][0].endswith(".RuntimeError")
    assert any(c.endswith(".ValueError") for c in ev.payload["error_chain"])
    # secret は redact 済
    assert ev.payload["error_message"] is not None
    assert "SflKxwRJSMeKKF2QT4secret" not in ev.payload["error_message"]
    assert "***" in ev.payload["error_message"]


@pytest.mark.asyncio
async def test_append_persist_crashed_db_error_uses_db_projection(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """persist DB 例外は outcome 互換を保ちつつ DB failure_kind を焼く。"""
    ready = await _make_ready(db_session, tc_source, _URL)
    exc = OperationalError("SELECT 1", {}, Exception("connection dropped"))

    async with session_factory() as session:
        await ArticleCompletionAuditRepository(session).append_persist_crashed(
            ready=ready, exc=exc
        )
        await session.commit()

    ev = await _fetch_one(db_session, tc_source.id)
    assert ev.event_type == "failed"
    assert ev.outcome_code == "persist_crashed"
    assert ev.retryability == "retryable"
    assert ev.payload["attempt_count"] == ready.attempt_count
    assert ev.payload["failure_kind"] == "db_runtime"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_append_ready_build_error_records_pending_missing_skipped(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    exc = ArticleCompletionReadyBuildPendingMissingError()

    async with session_factory() as session:
        await ArticleCompletionAuditRepository(session).append_ready_build_error(
            pending_id=999,
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_by_outcome(
        db_session, "completion_ready_build_blocked_pending_missing"
    )
    assert ev.event_type == "skipped"
    assert ev.source_id is None
    assert ev.retryability is None
    assert ev.error_class is None
    assert ev.payload["pending_id"] == 999
    assert ev.payload["pending_status"] is None
    assert ev.payload["failure_kind"] is None


@pytest.mark.asyncio
async def test_append_ready_build_error_records_pending_not_running_skipped(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    exc = ArticleCompletionReadyBuildPendingNotRunningError()
    facts = _ready_build_facts(
        tc_source,
        pending_id=100,
        status="open",
        url="https://techcrunch.com/open",
        attempt_count=0,
    )

    async with session_factory() as session:
        await ArticleCompletionAuditRepository(session).append_ready_build_error(
            pending_id=100,
            exc=exc,
            facts=facts,
        )
        await session.commit()

    ev = await _fetch_one(db_session, tc_source.id)
    assert ev.event_type == "skipped"
    assert ev.outcome_code == "completion_ready_build_blocked_pending_not_running"
    assert ev.retryability is None
    assert ev.error_class is None
    assert ev.payload["pending_id"] == 100
    assert ev.payload["pending_status"] == "open"
    assert ev.payload["source_name"] == "TechCrunch"
    assert ev.payload["canonical_url"] == "https://techcrunch.com/open"
    assert ev.payload["attempt_count"] == 0
    assert ev.payload["failure_kind"] is None


@pytest.mark.parametrize(
    ("error_cls", "canonical_url", "outcome_code", "failure_kind"),
    [
        (
            ObservedArticleInvalidError,
            "https://techcrunch.com/bad",
            "completion_ready_build_failed_observed_article_invalid",
            "observed_article_invalid",
        ),
        (
            SourceNotRegisteredError,
            "https://techcrunch.com/bad",
            "completion_ready_build_failed_source_not_registered",
            "source_not_registered",
        ),
        (
            ArticleCompletionReadyBuildUrlInvalidError,
            "ftp://techcrunch.com/bad",
            "completion_ready_build_failed_url_invalid",
            "url_invalid",
        ),
    ],
)
@pytest.mark.asyncio
async def test_append_ready_build_error_uses_domain_error_code(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    error_cls: type[Exception],
    canonical_url: str,
    outcome_code: str,
    failure_kind: str,
) -> None:
    exc = error_cls()
    facts = _ready_build_facts(tc_source, url=canonical_url)

    async with session_factory() as session:
        await ArticleCompletionAuditRepository(session).append_ready_build_error(
            pending_id=42,
            exc=exc,
            facts=facts,
        )
        await session.commit()

    ev = await _fetch_by_outcome(db_session, outcome_code)
    assert ev.event_type == "failed"
    assert ev.retryability == "unknown"
    assert ev.error_class is not None
    assert ev.payload["failure_kind"] == failure_kind
    assert ev.payload["pending_id"] == 42
    assert ev.payload["pending_status"] == "running"
    assert ev.payload["source_name"] == "TechCrunch"
    assert ev.payload["attempt_count"] == 1


@pytest.mark.asyncio
async def test_append_ready_build_error_records_db_error(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    exc = OperationalError("SELECT 1", {}, Exception("connection dropped"))

    async with session_factory() as session:
        await ArticleCompletionAuditRepository(session).append_ready_build_error(
            pending_id=42,
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_by_outcome(db_session, "completion_ready_build_failed_db_error")
    assert ev.event_type == "failed"
    assert ev.retryability == "unknown"
    assert ev.payload["failure_kind"] == "db_error"
    assert ev.payload["pending_id"] == 42


@pytest.mark.asyncio
async def test_append_ready_build_error_records_unexpected_error(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    exc = RuntimeError("boom")

    async with session_factory() as session:
        await ArticleCompletionAuditRepository(session).append_ready_build_error(
            pending_id=42,
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_by_outcome(
        db_session, "completion_ready_build_failed_unexpected_error"
    )
    assert ev.event_type == "failed"
    assert ev.retryability == "unknown"
    assert ev.payload["failure_kind"] == "unexpected_error"
    assert ev.payload["pending_id"] == 42


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
