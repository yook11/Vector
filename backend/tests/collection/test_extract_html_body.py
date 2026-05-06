"""``extract_html_body`` task の振る舞い不変条件テスト (PR2.5-B 仕様)。

PR2.5-B cutover で task は ``ContentFetchService`` への薄ラッパーになった:

- 入力: ``pending_id: int`` (cron poller ``dispatch_html_fetch_jobs`` から投入)
- ``max_retries=0 + retry_on_error=False`` で taskiq retry を完全に殺す
- 戻り値 Outcome dispatch のみ:
  - ``ContentFetched`` → ``extract_content.kiq`` を発火 + dict 返却
  - ``ConflictLost`` / ``TerminallyDropped`` / ``TransientlyDropped`` / ``None`` →
    何もしない (audit / DB 状態は Service 内で完結)

Service 単体の振る舞い (Outcome variant / payload 観測 / DB 状態遷移) は
``tests/collection/extraction/test_content_fetch_service.py`` を参照。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.collection.errors import PermanentFetchError, ServerErrorBlip
from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.extraction.extractor import ExtractedContent, ExtractionEmpty
from app.collection.ingestion.pending_repository import (
    PendingHtmlArticleRepository,
)
from app.collection.ingestion.staged_attributes import StagedArticleAttributes
from app.collection.ingestion.url_repository import ArticleUrlRepository
from app.collection.tasks import extract_html_body
from app.models.article import Article as ArticleORM
from app.models.news_source import NewsSource, SourceType
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


async def _make_pending_running(
    db_session: AsyncSession, source: NewsSource, url: str
) -> int:
    """``pending_html_articles`` 1 件を作って claim 状態にする。"""
    url_repo = ArticleUrlRepository(db_session)
    article_url_id = await url_repo.upsert_returning(
        normalized_url=SafeUrl(url),
        original_url=SafeUrl(url),
        first_seen_source_id=source.id,
    )
    assert article_url_id is not None
    pending_repo = PendingHtmlArticleRepository(db_session)
    pending_id = await pending_repo.create(
        article_url_id=article_url_id,
        source_id=source.id,
        staged_attributes=StagedArticleAttributes(
            title="TC Title",
            published_at_hint=PublishedAt(datetime(2026, 4, 30, tzinfo=UTC)),
            prefer_html_title=False,
        ),
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert pending_id is not None
    await db_session.commit()
    await pending_repo.claim_batch(limit=10, lease_minutes=5)
    await db_session.commit()
    return pending_id


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> MagicMock:
    """taskiq Context の最小 mock。task は session_factory のみ参照する。"""
    ctx = MagicMock()
    ctx.state.session_factory = session_factory
    return ctx


@pytest.mark.asyncio
async def test_success_chains_extract_content(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功時 ``extract_content.kiq`` が発火し dict が返る。"""
    pending_id = await _make_pending_running(
        db_session, tc_source, "https://techcrunch.com/ok/"
    )
    monkeypatch.setattr(
        "app.collection.extraction.content_fetch_service.ArticleHtmlExtractor.fetch",
        AsyncMock(
            return_value=ExtractedContent(
                title="HTML Title",
                body="x" * 200,
                published_at=PublishedAt(datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )
    extract_content_kiq = AsyncMock()
    monkeypatch.setattr("app.analysis.tasks.extract_content.kiq", extract_content_kiq)

    result = await extract_html_body(pending_id, ctx=_ctx(session_factory))

    assert result is not None
    assert result["status"] == "success"
    assert result["pending_id"] == pending_id
    extract_content_kiq.assert_awaited_once()
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(articles) == 1


@pytest.mark.asyncio
async def test_permanent_error_returns_none_and_skips_chain(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PermanentFetchError → None 返却、``extract_content.kiq`` は呼ばれない。"""
    pending_id = await _make_pending_running(
        db_session, tc_source, "https://techcrunch.com/dead/"
    )
    monkeypatch.setattr(
        "app.collection.extraction.content_fetch_service.ArticleHtmlExtractor.fetch",
        AsyncMock(side_effect=PermanentFetchError("HTTP 404")),
    )
    extract_content_kiq = AsyncMock()
    monkeypatch.setattr("app.analysis.tasks.extract_content.kiq", extract_content_kiq)

    result = await extract_html_body(pending_id, ctx=_ctx(session_factory))

    assert result is None
    extract_content_kiq.assert_not_awaited()


@pytest.mark.asyncio
async def test_temporary_error_returns_none_without_raising(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TemporaryFetchError は Service 内で吸収され、task は raise せず None を返す."""
    pending_id = await _make_pending_running(
        db_session, tc_source, "https://techcrunch.com/blip/"
    )
    monkeypatch.setattr(
        "app.collection.extraction.content_fetch_service.ArticleHtmlExtractor.fetch",
        AsyncMock(side_effect=ServerErrorBlip("HTTP 502")),
    )
    extract_content_kiq = AsyncMock()
    monkeypatch.setattr("app.analysis.tasks.extract_content.kiq", extract_content_kiq)

    result = await extract_html_body(pending_id, ctx=_ctx(session_factory))

    assert result is None
    extract_content_kiq.assert_not_awaited()


@pytest.mark.asyncio
async def test_extraction_empty_returns_none_and_skips_chain(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ExtractionEmpty → None 返却、``extract_content.kiq`` は呼ばれない。"""
    pending_id = await _make_pending_running(
        db_session, tc_source, "https://techcrunch.com/empty/"
    )
    monkeypatch.setattr(
        "app.collection.extraction.content_fetch_service.ArticleHtmlExtractor.fetch",
        AsyncMock(return_value=ExtractionEmpty(reason="parse_error")),
    )
    extract_content_kiq = AsyncMock()
    monkeypatch.setattr("app.analysis.tasks.extract_content.kiq", extract_content_kiq)

    result = await extract_html_body(pending_id, ctx=_ctx(session_factory))

    assert result is None
    extract_content_kiq.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_pending_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重複配送 (DELETE 済 ID) は None で静かに exit。``fetch`` も呼ばれない。"""
    fetch_mock = AsyncMock()
    monkeypatch.setattr(
        "app.collection.extraction.content_fetch_service.ArticleHtmlExtractor.fetch",
        fetch_mock,
    )
    extract_content_kiq = AsyncMock()
    monkeypatch.setattr("app.analysis.tasks.extract_content.kiq", extract_content_kiq)

    result = await extract_html_body(999_999, ctx=_ctx(session_factory))

    assert result is None
    fetch_mock.assert_not_awaited()
    extract_content_kiq.assert_not_awaited()
