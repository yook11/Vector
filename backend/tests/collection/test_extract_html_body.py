"""``extract_html_body`` task の retry policy / merge / 永続化テスト。

PR-1b' (collection-acquisition-redesign Phase 1)。``ArticleHtmlExtractor`` は
mock に差し替え、エラー種別ごとの drop / retry 動作と Article 永続化 +
extract_content chain を確認する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.extraction.extractor import ExtractedContent, ExtractionEmpty
from app.collection.ingestion.domain import (
    ArticleCandidate,
    DiscoveredArticleDraft,
)
from app.collection.ingestion.domain.fetched_article import (
    PendingHtmlFetch,
)
from app.collection.ingestion.repository import DiscoveredArticleRepository
from app.collection.ingestion.staged import StagedArticle
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


@pytest.fixture
async def staged_article(
    db_session: AsyncSession, tc_source: NewsSource
) -> StagedArticle:
    """discovered 行を 1 件先に作って StagedArticle を組む。"""
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


def _ctx(
    session_factory: async_sessionmaker[AsyncSession], retry_count: int = 0
) -> MagicMock:
    """taskiq Context の最小 mock。``retry_count`` は last_attempt 検査用。"""
    ctx = MagicMock()
    ctx.state.session_factory = session_factory
    # is_last_attempt(ctx) は内部で labels 経由で retry_count を見る。
    # 詳細は実装次第なので「retry_count に意味のある値が入っている mock」
    # として最低限提供する。
    ctx.message = MagicMock()
    ctx.message.labels = {"retry_on_error": "True", "max_retries": "3"}
    ctx.kwargs = {}
    ctx.task_name = "extract_html_body"
    ctx.retry_count = retry_count
    return ctx


@pytest.mark.asyncio
async def test_permanent_error_drops_without_retry(
    session_factory: async_sessionmaker[AsyncSession],
    staged_article: StagedArticle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PermanentFetchError は catch して drop, retry しない。"""
    fetch_mock = AsyncMock(side_effect=PermanentFetchError("HTTP 404"))
    monkeypatch.setattr(
        "app.collection.extraction.extractor.ArticleHtmlExtractor.fetch", fetch_mock
    )

    result = await extract_html_body(staged_article, ctx=_ctx(session_factory))

    assert result is None
    fetch_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_temporary_error_raises_to_taskiq(
    session_factory: async_sessionmaker[AsyncSession],
    staged_article: StagedArticle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TemporaryFetchError は raise (taskiq retry 委譲)。last_attempt=False。"""
    fetch_mock = AsyncMock(side_effect=TemporaryFetchError("HTTP 503"))
    monkeypatch.setattr(
        "app.collection.extraction.extractor.ArticleHtmlExtractor.fetch", fetch_mock
    )
    # is_last_attempt を強制 False
    monkeypatch.setattr("app.collection.tasks.is_last_attempt", lambda _ctx: False)

    with pytest.raises(TemporaryFetchError):
        await extract_html_body(staged_article, ctx=_ctx(session_factory))


@pytest.mark.asyncio
async def test_temporary_error_drops_on_last_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    staged_article: StagedArticle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """is_last_attempt=True の TemporaryFetchError は drop (再 raise しない)。"""
    fetch_mock = AsyncMock(side_effect=TemporaryFetchError("HTTP 503"))
    monkeypatch.setattr(
        "app.collection.extraction.extractor.ArticleHtmlExtractor.fetch", fetch_mock
    )
    monkeypatch.setattr("app.collection.tasks.is_last_attempt", lambda _ctx: True)

    result = await extract_html_body(staged_article, ctx=_ctx(session_factory))

    assert result is None


@pytest.mark.asyncio
async def test_extraction_empty_drops_without_retry(
    session_factory: async_sessionmaker[AsyncSession],
    staged_article: StagedArticle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ExtractionEmpty (parse_error 等) は drop, retry しない。"""
    fetch_mock = AsyncMock(return_value=ExtractionEmpty(reason="parse_error"))
    monkeypatch.setattr(
        "app.collection.extraction.extractor.ArticleHtmlExtractor.fetch", fetch_mock
    )

    result = await extract_html_body(staged_article, ctx=_ctx(session_factory))

    assert result is None


@pytest.mark.asyncio
async def test_success_persists_article_and_chains_extract_content(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    staged_article: StagedArticle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTML 抽出成功 → Article 永続化 + extract_content.kiq 発火。"""
    extracted = ExtractedContent(
        title="HTML Title (ignored, RSS preferred)",
        body="x" * 200,
        published_at=PublishedAt(value=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)),
    )
    fetch_mock = AsyncMock(return_value=extracted)
    monkeypatch.setattr(
        "app.collection.extraction.extractor.ArticleHtmlExtractor.fetch", fetch_mock
    )

    extract_content_kiq = AsyncMock()
    monkeypatch.setattr("app.analysis.tasks.extract_content.kiq", extract_content_kiq)

    result = await extract_html_body(staged_article, ctx=_ctx(session_factory))

    assert result is not None
    assert result["status"] == "success"
    assert result["discovered_id"] == staged_article.discovered_id

    # Article 行が作成されている
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(articles) == 1
    assert articles[0].original_title == "TC Title"  # RSS title 採用
    # 公開日時は RSS hint (2026-04-30) が HTML (2026-05-01) より優先される
    assert articles[0].published_at is not None
    assert articles[0].published_at.day == 30

    # extract_content.kiq が発火された
    extract_content_kiq.assert_awaited_once()


@pytest.mark.asyncio
async def test_html_published_at_used_when_rss_hint_missing(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RSS hint が None のとき HTML 由来 published_at が採用される。"""
    repo = DiscoveredArticleRepository(db_session)
    candidate = ArticleCandidate(
        url=SafeUrl("https://techcrunch.com/no-rss-pub/"), title="No PubDate"
    )
    draft = DiscoveredArticleDraft.from_candidate(
        candidate, news_source_id=tc_source.id
    )
    [discovered] = await repo.save_many([draft])
    await db_session.commit()

    pending = PendingHtmlFetch(
        title="No PubDate",
        source_id=tc_source.id,
        source_url=SafeUrl("https://techcrunch.com/no-rss-pub/"),
        published_at_hint=None,  # RSS pubDate 欠落
    )
    staged = StagedArticle(discovered_id=discovered.id, pending=pending)

    extracted = ExtractedContent(
        title="HTML Title",
        body="y" * 200,
        published_at=PublishedAt(value=datetime(2026, 5, 1, 8, 30, 0, tzinfo=UTC)),
    )
    monkeypatch.setattr(
        "app.collection.extraction.extractor.ArticleHtmlExtractor.fetch",
        AsyncMock(return_value=extracted),
    )
    monkeypatch.setattr("app.analysis.tasks.extract_content.kiq", AsyncMock())

    result = await extract_html_body(staged, ctx=_ctx(session_factory))

    assert result is not None
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(articles) == 1
    assert articles[0].published_at is not None
    assert articles[0].published_at.day == 1  # HTML 由来
    assert articles[0].published_at.hour == 8


@pytest.mark.asyncio
async def test_both_published_at_missing_drops_as_failed(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RSS hint と HTML 両方 published_at が None なら drop (Failed 降格)。"""
    repo = DiscoveredArticleRepository(db_session)
    candidate = ArticleCandidate(
        url=SafeUrl("https://techcrunch.com/both-missing/"), title="Both Missing"
    )
    draft = DiscoveredArticleDraft.from_candidate(
        candidate, news_source_id=tc_source.id
    )
    [discovered] = await repo.save_many([draft])
    await db_session.commit()

    pending = PendingHtmlFetch(
        title="Both Missing",
        source_id=tc_source.id,
        source_url=SafeUrl("https://techcrunch.com/both-missing/"),
        published_at_hint=None,
    )
    staged = StagedArticle(discovered_id=discovered.id, pending=pending)

    extracted = ExtractedContent(
        title="HTML Title",
        body="z" * 200,
        published_at=None,  # HTML も published_at なし
    )
    monkeypatch.setattr(
        "app.collection.extraction.extractor.ArticleHtmlExtractor.fetch",
        AsyncMock(return_value=extracted),
    )
    extract_content_kiq = AsyncMock()
    monkeypatch.setattr("app.analysis.tasks.extract_content.kiq", extract_content_kiq)

    result = await extract_html_body(staged, ctx=_ctx(session_factory))

    assert result is None
    # Article は永続化されない
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(articles) == 0
    extract_content_kiq.assert_not_awaited()
