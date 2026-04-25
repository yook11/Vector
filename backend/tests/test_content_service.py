"""ContentFetchService のテスト (DB 統合テスト)。

PR 2b: Outcome union (``ContentFetchedOutcome`` / ``AlreadyFetchedOutcome`` /
``ContentFetchSkippedOutcome``) を検証する。``DiscoveredArticleMissing``
例外は廃止され、``discovered_not_found`` は Skipped に縮退する。
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.errors import (
    PermanentFetchError,
    TemporaryFetchError,
)
from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.extraction.extractor import (
    ArticleHtmlExtractor,
    ExtractedContent,
    ExtractionEmpty,
    ExtractionEmptyReason,
    HtmlExtractionResult,
)
from app.collection.extraction.service import (
    AlreadyFetchedOutcome,
    ContentFetchedOutcome,
    ContentFetchService,
    ContentFetchSkippedOutcome,
)
from app.models.article import Article
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource
from app.shared.value_objects.safe_url import SafeUrl


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


# ---------------------------------------------------------------------------
# ContentFetchedOutcome (新規抽出成功)
# ---------------------------------------------------------------------------


async def test_fetched_creates_article(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """本文取得成功時は Article 行を作成し ContentFetchedOutcome を返す。"""
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

    assert isinstance(result, ContentFetchedOutcome)
    assert result.article.id > 0
    assert result.article.discovered_article_id == discovered.id
    assert result.article.title == "Extracted Title"
    assert result.article.body == body
    assert result.article.published_at == PublishedAt(extracted_date)
    extractor.fetch.assert_called_once_with(SafeUrl("https://example.com/fetched"))

    # Service は独自セッションで commit するため、テスト用セッションで再読込する
    db_session.expire_all()
    article = await db_session.get(Article, result.article.id)
    assert article is not None
    assert article.original_title == "Extracted Title"


# ---------------------------------------------------------------------------
# AlreadyFetchedOutcome (冪等ヒット)
# ---------------------------------------------------------------------------


async def test_already_exists_returns_already_fetched(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """既に Article が存在する場合は fetch せず AlreadyFetchedOutcome を返す。"""
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/existing"
    )
    article = Article(
        discovered_article_id=discovered.id,
        original_title="Already here",
        original_content="Existing content body of sufficient length.",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    extractor = _mock_html_extractor(return_value=_empty())
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(discovered.id)

    assert isinstance(result, AlreadyFetchedOutcome)
    assert result.article.id == article.id
    assert result.article.title == "Already here"
    extractor.fetch.assert_not_called()


# ---------------------------------------------------------------------------
# ContentFetchSkippedOutcome (各 reason)
# ---------------------------------------------------------------------------


async def test_discovered_not_found_returns_skipped(
    db_session: AsyncSession,
    session_factory,
) -> None:
    """DB に DiscoveredArticle がない場合は Skipped(discovered_not_found) を返す。

    PR 2b で DiscoveredArticleMissing 例外を廃止したことの回帰検証。
    """
    extractor = _mock_html_extractor(return_value=_empty())
    svc = ContentFetchService(session_factory, extractor)

    result = await svc.execute(999999)

    assert isinstance(result, ContentFetchSkippedOutcome)
    assert result.reason == "discovered_not_found"
    assert result.discovered_article_id == 999999
    extractor.fetch.assert_not_called()


async def test_permanent_error_returns_skipped(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """PermanentFetchError 時は Article を作成せず Skipped(permanent_fetch_error)。"""
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/forbidden"
    )

    extractor = _mock_html_extractor(side_effect=PermanentFetchError("HTTP 403"))
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(discovered.id)

    assert isinstance(result, ContentFetchSkippedOutcome)
    assert result.reason == "permanent_fetch_error"
    assert result.discovered_article_id == discovered.id

    articles = (await db_session.execute(select(Article))).scalars().all()
    assert len(articles) == 0


async def test_quality_gate_returns_skipped(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """quality_gate 失敗時は Skipped(reason="quality_gate") を返す。"""
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/quality-gate"
    )

    extractor = _mock_html_extractor(return_value=_empty(reason="quality_gate"))
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(discovered.id)

    assert isinstance(result, ContentFetchSkippedOutcome)
    assert result.reason == "quality_gate"
    assert result.discovered_article_id == discovered.id


async def test_not_html_returns_skipped(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """Content-Type 不一致 (not_html) は Skipped(reason="not_html") を返す。"""
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/not-html"
    )

    extractor = _mock_html_extractor(return_value=_empty(reason="not_html"))
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(discovered.id)

    assert isinstance(result, ContentFetchSkippedOutcome)
    assert result.reason == "not_html"


# ---------------------------------------------------------------------------
# Exception propagation
# ---------------------------------------------------------------------------


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

    articles = (await db_session.execute(select(Article))).scalars().all()
    assert len(articles) == 0
