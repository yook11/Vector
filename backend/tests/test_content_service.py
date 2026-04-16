"""ContentFetchService のテスト (DB 統合テスト)。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.article_body_fetcher import (
    ArticleBodyFetcher,
    PermanentFetchError,
    TemporaryFetchError,
)
from app.collection.content_service import ContentFetchService, mark_article_skipped
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource


def _mock_body_fetcher(
    return_value: str | None = None,
    side_effect: Exception | None = None,
) -> MagicMock:
    """モックの ArticleBodyFetcher を作成する。"""
    fetcher = MagicMock(spec=ArticleBodyFetcher)
    if side_effect is not None:
        fetcher.fetch = AsyncMock(side_effect=side_effect)
    else:
        fetcher.fetch = AsyncMock(return_value=return_value)
    return fetcher


async def _make_article(
    db_session: AsyncSession,
    source: NewsSource,
    url: str,
    original_content: str | None = None,
) -> NewsArticle:
    article = NewsArticle(
        original_title="Test Article",
        original_url=url,
        news_source_id=source.id,
        published_at=datetime.now(UTC),
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

    body_fetcher = _mock_body_fetcher(return_value="Full article body text.")
    svc = ContentFetchService(session_factory, body_fetcher)
    result = await svc.execute(article_id)

    assert result.status == "fetched"
    body_fetcher.fetch.assert_called_once_with("https://example.com/fetched")

    db_session.expire_all()
    refreshed = await db_session.get(NewsArticle, article_id)
    assert refreshed is not None
    assert refreshed.original_content == "Full article body text."
    assert refreshed.skip_content_fetch is False


async def test_already_exists_skips_fetch(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """本文を持つ記事は fetch せず 'already_exists' を返す。"""
    article = await _make_article(
        db_session,
        sample_source,
        "https://example.com/existing",
        original_content="Already here.",
    )
    article_id = article.id

    body_fetcher = _mock_body_fetcher()
    svc = ContentFetchService(session_factory, body_fetcher)
    result = await svc.execute(article_id)

    assert result.status == "already_exists"
    body_fetcher.fetch.assert_not_called()


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

    body_fetcher = _mock_body_fetcher(side_effect=PermanentFetchError("HTTP 403"))
    svc = ContentFetchService(session_factory, body_fetcher)
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
    """body_fetcher が None を返したら記事を skipped とマークする。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/minimal"
    )
    article_id = article.id

    body_fetcher = _mock_body_fetcher(return_value=None)
    svc = ContentFetchService(session_factory, body_fetcher)
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
    body_fetcher = _mock_body_fetcher()
    svc = ContentFetchService(session_factory, body_fetcher)
    result = await svc.execute(999999)

    assert result.status == "skipped"
    body_fetcher.fetch.assert_not_called()


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

    body_fetcher = _mock_body_fetcher(side_effect=TemporaryFetchError("HTTP 500"))
    svc = ContentFetchService(session_factory, body_fetcher)

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
