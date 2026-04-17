"""ContentFetchService のテスト (DB 統合テスト)。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.extraction.extractor import (
    ArticleHtmlExtractor,
    HtmlExtractionResult,
    PermanentFetchError,
    TemporaryFetchError,
)
from app.collection.extraction.service import ContentFetchService, mark_article_skipped
from app.models.news_article import NewsArticle
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


async def _make_article(
    db_session: AsyncSession,
    source: NewsSource,
    url: str,
    original_content: str | None = None,
    published_at: datetime | None = None,
) -> NewsArticle:
    article = NewsArticle(
        original_title="Test Article",
        original_url=url,
        news_source_id=source.id,
        published_at=published_at,
        original_content=original_content,
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


async def test_fetched_persists_content(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """本文取得成功時は original_content を保存し 'fetched' を返す。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/fetched"
    )
    article_id = article.id

    extracted_date = datetime(2026, 3, 15, 10, 30, 0, tzinfo=UTC)
    extractor = _mock_html_extractor(
        body="Full article body text.", published_at=extracted_date
    )
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(article_id)

    assert result.status == "fetched"
    extractor.fetch.assert_called_once_with("https://example.com/fetched")

    db_session.expire_all()
    refreshed = await db_session.get(NewsArticle, article_id)
    assert refreshed is not None
    assert refreshed.original_content == "Full article body text."
    assert refreshed.published_at == extracted_date
    assert refreshed.skip_content_fetch is False


async def test_already_exists_skips_fetch(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """本文と公開日時を共に持つ記事は fetch せず 'already_exists' を返す。"""
    article = await _make_article(
        db_session,
        sample_source,
        "https://example.com/existing",
        original_content="Already here.",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    article_id = article.id

    extractor = _mock_html_extractor()
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(article_id)

    assert result.status == "already_exists"
    extractor.fetch.assert_not_called()


async def test_content_exists_but_no_date_triggers_fetch(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """本文ありだが公開日時なしの記事は HTML 抽出を実行する。"""
    article = await _make_article(
        db_session,
        sample_source,
        "https://example.com/content-no-date",
        original_content="Existing content.",
        published_at=None,
    )
    article_id = article.id

    extracted_date = datetime(2026, 3, 15, tzinfo=UTC)
    extractor = _mock_html_extractor(body="New body text.", published_at=extracted_date)
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(article_id)

    assert result.status == "fetched"

    db_session.expire_all()
    refreshed = await db_session.get(NewsArticle, article_id)
    assert refreshed is not None
    # 既存の本文は上書きしない
    assert refreshed.original_content == "Existing content."
    # 日付は新たに保存される
    assert refreshed.published_at == extracted_date


async def test_body_fails_but_date_succeeds(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """本文抽出失敗でも日付だけ取れた場合は日付を保存し 'fetched' を返す。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/paywall-date"
    )
    article_id = article.id

    extracted_date = datetime(2026, 4, 10, 14, 0, 0, tzinfo=UTC)
    extractor = _mock_html_extractor(body=None, published_at=extracted_date)
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(article_id)

    assert result.status == "fetched"

    db_session.expire_all()
    refreshed = await db_session.get(NewsArticle, article_id)
    assert refreshed is not None
    assert refreshed.original_content is None
    assert refreshed.published_at == extracted_date
    assert refreshed.skip_content_fetch is False


async def test_permanent_error_marks_skip(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """PermanentFetchError 時は記事を skipped とマークし 'skipped' を返す。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/forbidden"
    )
    article_id = article.id

    extractor = _mock_html_extractor(side_effect=PermanentFetchError("HTTP 403"))
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(article_id)

    assert result.status == "skipped"

    db_session.expire_all()
    refreshed = await db_session.get(NewsArticle, article_id)
    assert refreshed is not None
    assert refreshed.skip_content_fetch is True
    assert refreshed.original_content is None


async def test_quality_gate_marks_skip(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """body も published_at も取れなかった場合は skipped とマークする。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/minimal"
    )
    article_id = article.id

    extractor = _mock_html_extractor(body=None, published_at=None)
    svc = ContentFetchService(session_factory, extractor)
    result = await svc.execute(article_id)

    assert result.status == "skipped"

    db_session.expire_all()
    refreshed = await db_session.get(NewsArticle, article_id)
    assert refreshed is not None
    assert refreshed.skip_content_fetch is True


async def test_article_not_found_returns_skipped(
    db_session: AsyncSession,
    session_factory,
) -> None:
    """記事が見つからない場合は fetcher を呼ばず 'skipped' を返す。"""
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
    article = await _make_article(
        db_session, sample_source, "https://example.com/temp-error"
    )
    article_id = article.id

    extractor = _mock_html_extractor(side_effect=TemporaryFetchError("HTTP 500"))
    svc = ContentFetchService(session_factory, extractor)

    with pytest.raises(TemporaryFetchError):
        await svc.execute(article_id)

    # 一時エラーでは記事を skipped にマークしないこと
    db_session.expire_all()
    refreshed = await db_session.get(NewsArticle, article_id)
    assert refreshed is not None
    assert refreshed.skip_content_fetch is False


async def test_mark_article_skipped_utility(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """ユーティリティは指定記事の skip_content_fetch=True を設定する。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/mark-skip"
    )
    article_id = article.id

    await mark_article_skipped(session_factory, article_id)

    db_session.expire_all()
    refreshed = await db_session.get(NewsArticle, article_id)
    assert refreshed is not None
    assert refreshed.skip_content_fetch is True


async def test_mark_article_skipped_missing_article(session_factory) -> None:
    """存在しない記事に対してユーティリティを呼んでも例外を送出しない。"""
    await mark_article_skipped(session_factory, 999999)
