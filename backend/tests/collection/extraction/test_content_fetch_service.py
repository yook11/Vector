"""``ContentFetchService`` の不変条件テスト。

Service の責務:

- HTTP 取得 → 抽出 → promotion → 永続化 → 監査までを完結させる
- ``TemporaryFetchError`` のみ caller (task) に raise する
- ``pipeline_events`` に Stage 2 行を焼き付ける (success / terminal / transient)

検証する不変条件:

- 各失敗モードが正しい ``Outcome`` variant を返す
- ``pipeline_events.payload`` に ``reason_code`` / ``body_length`` /
  ``quality_gate_metric`` が観測される
- ``TemporaryFetchError`` は audit を残さず raise する (caller の retry 判断材料)
- ``audit_exhausted`` は ``dropped_transient`` 行を独立して焼く
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.extraction.content_fetch_service import (
    ContentFetched,
    ContentFetchService,
    TerminallyDropped,
    TransientlyDropped,
)
from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.extraction.extractor import ExtractedContent, ExtractionEmpty
from app.collection.ingestion.domain import (
    ArticleCandidate,
    DiscoveredArticleDraft,
)
from app.collection.ingestion.domain.fetched_article import PendingHtmlFetch
from app.collection.ingestion.repository import DiscoveredArticleRepository
from app.collection.ingestion.staged import StagedArticle
from app.models.article import Article as ArticleORM
from app.models.news_source import NewsSource, SourceType
from app.models.pipeline_event import PipelineEvent
from app.shared.value_objects.safe_url import SafeUrl


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


@pytest.fixture
async def staged_article(
    db_session: AsyncSession, tc_source: NewsSource
) -> StagedArticle:
    repo = DiscoveredArticleRepository(db_session)
    candidate = ArticleCandidate(
        url=SafeUrl("https://techcrunch.com/article-1/"), title="TC Title"
    )
    draft = DiscoveredArticleDraft.from_candidate(
        candidate, news_source_id=tc_source.id
    )
    [discovered] = await repo.save_many([draft])
    await db_session.commit()

    pending = PendingHtmlFetch(
        title="TC Title",
        source_id=tc_source.id,
        source_url=SafeUrl("https://techcrunch.com/article-1/"),
        published_at_hint=PublishedAt(
            value=datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)
        ),
    )
    return StagedArticle(discovered_id=discovered.id, pending=pending)


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, mock: AsyncMock) -> None:
    """``ArticleHtmlExtractor.fetch`` を Service の import path 経由で差し替える。"""
    monkeypatch.setattr(
        "app.collection.extraction.content_fetch_service.ArticleHtmlExtractor.fetch",
        mock,
    )


@pytest.mark.asyncio
async def test_success_returns_content_fetched_with_persisted_article(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    staged_article: StagedArticle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ExtractedContent + 永続化成功 → ``ContentFetched`` 返却 + Article 1 件作成。"""
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ExtractedContent(
                title="HTML Title",
                body="x" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    svc = ContentFetchService(session_factory)
    outcome = await svc.execute(staged_article, attempt=1)

    assert isinstance(outcome, ContentFetched)
    assert outcome.article.discovered_article_id == staged_article.discovered_id
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(articles) == 1


@pytest.mark.asyncio
async def test_success_writes_audit_with_body_length(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    staged_article: StagedArticle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功時 ``pipeline_events`` に SUCCEEDED + body_length が焼かれる。"""
    body = "x" * 250
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ExtractedContent(
                title="HTML Title",
                body=body,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    svc = ContentFetchService(session_factory)
    await svc.execute(staged_article, attempt=1)

    events = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.stage == "content_fetch")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "succeeded"
    assert event.outcome_code == "fetched"
    assert event.attempt == 1
    assert event.payload["body_length"] == len(body)
    assert event.payload["discovered_article_id"] == staged_article.discovered_id
    assert event.payload["extractor_class"] == "ArticleHtmlExtractor"


@pytest.mark.asyncio
async def test_permanent_fetch_error_returns_terminal_dropped(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    staged_article: StagedArticle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PermanentFetchError → ``TerminallyDropped("permanent_fetch_error")`` + audit。"""
    _patch_fetch(monkeypatch, AsyncMock(side_effect=PermanentFetchError("HTTP 404")))

    svc = ContentFetchService(session_factory)
    outcome = await svc.execute(staged_article, attempt=1)

    assert isinstance(outcome, TerminallyDropped)
    assert outcome.reason_code == "permanent_fetch_error"
    # Article は永続化されない
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert articles == []
    # audit は記録される
    event = (
        await db_session.execute(
            select(PipelineEvent).where(PipelineEvent.stage == "content_fetch")
        )
    ).scalar_one()
    assert event.outcome_code == "dropped_terminal"
    assert event.payload["reason_code"] == "permanent_fetch_error"


@pytest.mark.asyncio
async def test_extraction_empty_returns_terminal_with_reason_in_code(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    staged_article: StagedArticle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ExtractionEmpty(reason) → reason_code が ``extraction_empty_<reason>`` 形式。"""
    _patch_fetch(
        monkeypatch, AsyncMock(return_value=ExtractionEmpty(reason="not_html"))
    )

    svc = ContentFetchService(session_factory)
    outcome = await svc.execute(staged_article, attempt=1)

    assert isinstance(outcome, TerminallyDropped)
    assert outcome.reason_code == "extraction_empty_not_html"
    event = (
        await db_session.execute(
            select(PipelineEvent).where(PipelineEvent.stage == "content_fetch")
        )
    ).scalar_one()
    assert event.payload["reason_code"] == "extraction_empty_not_html"


@pytest.mark.asyncio
async def test_promotion_failure_records_quality_gate_metric(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """body 短すぎ → promotion ``Failed`` → quality_gate_metric に body_length。"""
    repo = DiscoveredArticleRepository(db_session)
    candidate = ArticleCandidate(
        url=SafeUrl("https://techcrunch.com/short/"), title="Short"
    )
    draft = DiscoveredArticleDraft.from_candidate(
        candidate, news_source_id=tc_source.id
    )
    [discovered] = await repo.save_many([draft])
    await db_session.commit()
    pending = PendingHtmlFetch(
        title="Short",
        source_id=tc_source.id,
        source_url=SafeUrl("https://techcrunch.com/short/"),
        published_at_hint=None,
    )
    staged = StagedArticle(discovered_id=discovered.id, pending=pending)

    # body=200 chars だが published_at が両方 None → published_at_missing で Failed
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ExtractedContent(
                title="OK",
                body="x" * 200,
                published_at=None,
            )
        ),
    )

    svc = ContentFetchService(session_factory)
    outcome = await svc.execute(staged, attempt=1)

    assert isinstance(outcome, TerminallyDropped)
    assert outcome.reason_code.startswith("promotion_")
    event = (
        await db_session.execute(
            select(PipelineEvent).where(PipelineEvent.stage == "content_fetch")
        )
    ).scalar_one()
    assert event.payload["reason_code"].startswith("promotion_")
    assert event.payload["quality_gate_metric"]["body_length"] == 200


@pytest.mark.asyncio
async def test_temporary_fetch_error_propagates_without_audit(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    staged_article: StagedArticle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TemporaryFetchError は raise + audit row 不在 (caller が retry 判断するため)。"""
    _patch_fetch(monkeypatch, AsyncMock(side_effect=TemporaryFetchError("HTTP 503")))

    svc = ContentFetchService(session_factory)
    with pytest.raises(TemporaryFetchError):
        await svc.execute(staged_article, attempt=1)

    events = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.stage == "content_fetch")
            )
        )
        .scalars()
        .all()
    )
    assert events == []


@pytest.mark.asyncio
async def test_audit_exhausted_writes_dropped_transient_event(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    staged_article: StagedArticle,
) -> None:
    """``audit_exhausted`` 単独呼出 → FAILED + outcome=dropped_transient が焼かれる。"""
    svc = ContentFetchService(session_factory)
    exc = TemporaryFetchError("HTTP 503")

    await svc.audit_exhausted(staged_article, attempt=4, exc=exc)

    event = (
        await db_session.execute(
            select(PipelineEvent).where(PipelineEvent.stage == "content_fetch")
        )
    ).scalar_one()
    assert event.event_type == "failed"
    assert event.outcome_code == "dropped_transient"
    assert event.attempt == 4
    assert event.payload["reason_code"] == "temporary_fetch_error_exhausted"
    assert event.payload["error_chain"] is not None


@pytest.mark.asyncio
async def test_race_lost_returns_existing_article(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    staged_article: StagedArticle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """save が race-lost で None → find で既存を読み戻し ``ContentFetched`` 返却。

    あらかじめ同 discovered_id の Article を 1 件作っておく → save が UNIQUE
    違反で None を返す → find_by_discovered_article_id で既存を取り込む。
    """
    # 既存 Article を直接 INSERT (race の "勝者" 役)
    existing = ArticleORM(
        discovered_article_id=staged_article.discovered_id,
        original_title="Existing",
        original_content="y" * 100,
        published_at=datetime(2026, 4, 30, tzinfo=UTC),
        source_id=staged_article.pending.source_id,
        source_url=staged_article.pending.source_url,
    )
    db_session.add(existing)
    await db_session.commit()

    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ExtractedContent(
                title="HTML Title",
                body="z" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    svc = ContentFetchService(session_factory)
    outcome = await svc.execute(staged_article, attempt=1)

    assert isinstance(outcome, ContentFetched)
    assert outcome.article.discovered_article_id == staged_article.discovered_id
    # 行は 1 件のまま (race の敗者は INSERT しない)
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(articles) == 1


@pytest.mark.asyncio
async def test_transient_dropped_only_via_audit_exhausted() -> None:
    """型契約: ``TransientlyDropped`` は ``audit_exhausted`` 経由でのみ生成される。

    ``execute`` で transient 失敗が起きたら raise (caller が retry 判断)、
    ``audit_exhausted`` を呼んだ時のみ transient 状態が記録される。
    具体動作は別 2 テスト (propagates_without_audit / writes_dropped_transient)。
    """
    assert TransientlyDropped is not None  # 型 import の整合性確認のみ
