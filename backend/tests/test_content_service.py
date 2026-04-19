"""ContentFetchService のテスト (DB 統合テスト)。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.extraction.extractor import (
    ArticleHtmlExtractor,
    HtmlExtractionResult,
    PermanentFetchError,
    TemporaryFetchError,
)
from app.collection.extraction.service import ContentFetchService
from app.models.article import Article
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource


def _mock_html_extractor(
    body: str | None = None,
    published_at: datetime | None = None,
    side_effect: Exception | None = None,
) -> MagicMock:
    """モックの ArticleHtmlExtractor を作成する。"""
    extractor = MagicMock(spec=ArticleHtmlExtractor)
    if side_effect is not None:
        extractor.fetch = AsyncMock(side_effect=side_effect)
    else:
        extractor.fetch = AsyncMock(
            return_value=HtmlExtractionResult(body=body, published_at=published_at)
        )
    return extractor


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
    """本文取得成功時は Article 行を作成し 'fetched' を返す。"""
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/fetched"
    )

    extracted_date = datetime(2026, 3, 15, 10, 30, 0, tzinfo=UTC)
    extractor = _mock_html_extractor(
        body="Full article body text.", published_at=extracted_date
    )
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(discovered.id)

    assert result.status == "fetched"
    assert result.article_id is not None
    extractor.fetch.assert_called_once_with("https://example.com/fetched")

    # Service は独自セッションで commit するため、テスト用セッションで再読込する
    db_session.expire_all()
    article = await db_session.get(Article, result.article_id)
    assert article is not None
    assert article.original_content == "Full article body text."
    assert article.published_at == extracted_date


async def test_already_exists_skips_fetch(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """既に Article が存在する場合は fetch せず 'already_exists' を返す。"""
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

    extractor = _mock_html_extractor()
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(discovered.id)

    assert result.status == "already_exists"
    assert result.article_id == article.id
    extractor.fetch.assert_not_called()


async def test_permanent_error_returns_skipped(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """PermanentFetchError 時は Article を作成せず 'skipped' を返す。"""
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/forbidden"
    )

    extractor = _mock_html_extractor(side_effect=PermanentFetchError("HTTP 403"))
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(discovered.id)

    assert result.status == "skipped"
    assert result.article_id is None

    # Article が作成されていないことを確認
    articles = (await db_session.execute(select(Article))).scalars().all()
    assert len(articles) == 0


async def test_quality_gate_returns_skipped(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """body が取れなかった場合は Article を作成せず skipped を返す。"""
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/minimal"
    )

    extractor = _mock_html_extractor(body=None, published_at=None)
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(discovered.id)

    assert result.status == "skipped"
    assert result.article_id is None


async def test_discovered_not_found_returns_skipped(
    db_session: AsyncSession,
    session_factory,
) -> None:
    """DiscoveredArticle が見つからない場合は fetcher を呼ばず 'skipped' を返す。"""
    extractor = _mock_html_extractor()
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(999999)

    assert result.status == "skipped"
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
