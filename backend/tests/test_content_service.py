"""ContentFetchService のテスト (DB 統合テスト)。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.extraction.candidate import DiscoveredNotFound, PublishedAt
from app.collection.extraction.extractor import (
    ArticleHtmlExtractor,
    ExtractedContent,
    ExtractionEmpty,
    ExtractionEmptyReason,
    HtmlExtractionResult,
)
from app.collection.extraction.service import (
    ArticleReady,
    ContentFetchService,
    ExtractionFailed,
)
from app.domain.safe_url import SafeUrl
from app.models.article import Article
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource


def _mock_html_extractor(
    *,
    return_value: HtmlExtractionResult | None = None,
    side_effect: Exception | None = None,
) -> MagicMock:
    """モックの ArticleHtmlExtractor を作成する。"""
    extractor = MagicMock(spec=ArticleHtmlExtractor)
    if side_effect is not None:
        extractor.fetch = AsyncMock(side_effect=side_effect)
    else:
        extractor.fetch = AsyncMock(return_value=return_value)
    return extractor


def _extracted(
    title: str = "Title",
    body: str = "x" * 60,
    published_at: datetime | None = None,
) -> ExtractedContent:
    return ExtractedContent(
        title=title,
        body=body,
        published_at=PublishedAt(published_at) if published_at else None,
    )


def _empty(reason: ExtractionEmptyReason = "quality_gate") -> ExtractionEmpty:
    return ExtractionEmpty(reason=reason)


async def _make_discovered(
    db_session: AsyncSession,
    source: NewsSource,
    url: str,
) -> DiscoveredArticle:
    """テスト用 DiscoveredArticle を作成する。"""
    discovered = DiscoveredArticle(
        original_title="Test Article",
        original_url=url,
        news_source_id=source.id,
    )
    db_session.add(discovered)
    await db_session.commit()
    await db_session.refresh(discovered)
    return discovered


async def test_fetched_creates_article(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """本文取得成功時は Article 行を作成し ArticleReady を返す。"""
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/fetched"
    )

    extracted_date = datetime(2026, 3, 15, 10, 30, 0, tzinfo=UTC)
    body = "Full article body text used for extraction tests, long enough."
    extractor = _mock_html_extractor(
        return_value=_extracted(
            title="Extracted Title",
            body=body,
            published_at=extracted_date,
        )
    )
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(discovered.id)

    assert isinstance(result, ArticleReady)
    extractor.fetch.assert_called_once_with(SafeUrl("https://example.com/fetched"))

    # Service は独自セッションで commit するため、テスト用セッションで再読込する
    db_session.expire_all()
    article = await db_session.get(Article, result.article_id)
    assert article is not None
    assert article.original_title == "Extracted Title"
    assert article.original_content == body
    assert article.published_at == extracted_date


async def test_already_exists_skips_fetch(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """既に Article が存在する場合は fetch せず ArticleReady を返す。"""
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/existing"
    )
    # Article を先に作成
    article = Article(
        discovered_article_id=discovered.id,
        original_title="Already here",
        original_content="Existing content.",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    extractor = _mock_html_extractor(return_value=_empty())
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(discovered.id)

    assert isinstance(result, ArticleReady)
    assert result.article_id == article.id
    extractor.fetch.assert_not_called()


async def test_permanent_error_returns_extraction_failed(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """PermanentFetchError 時は Article を作成せず ExtractionFailed を返す。"""
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/forbidden"
    )

    extractor = _mock_html_extractor(side_effect=PermanentFetchError("HTTP 403"))
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(discovered.id)

    assert isinstance(result, ExtractionFailed)
    assert result.reason == "permanent_fetch_error"

    # Article が作成されていないことを確認
    articles = (await db_session.execute(select(Article))).scalars().all()
    assert len(articles) == 0


async def test_quality_gate_returns_extraction_failed(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """quality_gate 失敗時は ExtractionFailed(reason="quality_gate") を返す。"""
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/quality-gate"
    )

    extractor = _mock_html_extractor(return_value=_empty(reason="quality_gate"))
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(discovered.id)

    assert isinstance(result, ExtractionFailed)
    assert result.reason == "quality_gate"


async def test_not_html_returns_extraction_failed(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """Content-Type 不一致 (not_html) は ExtractionFailed(reason="not_html") を返す。"""
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/not-html"
    )

    extractor = _mock_html_extractor(return_value=_empty(reason="not_html"))
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(discovered.id)

    assert isinstance(result, ExtractionFailed)
    assert result.reason == "not_html"


async def test_discovered_not_found_returns_not_found(
    db_session: AsyncSession,
    session_factory,
) -> None:
    """DiscoveredArticle 不在時は fetcher を呼ばず DiscoveredNotFound を返す。"""
    extractor = _mock_html_extractor(return_value=_empty())
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(999999)

    assert isinstance(result, DiscoveredNotFound)
    extractor.fetch.assert_not_called()


async def test_temporary_error_propagates(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """TemporaryFetchError は伝播させる (リトライ判断は Task の責務)。"""
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/temp-error"
    )

    extractor = _mock_html_extractor(side_effect=TemporaryFetchError("HTTP 500"))
    svc = ContentFetchService(session_factory, extractor)

    with pytest.raises(TemporaryFetchError):
        await svc.execute(discovered.id)

    # Article が作成されていないことを確認
    articles = (await db_session.execute(select(Article))).scalars().all()
    assert len(articles) == 0
