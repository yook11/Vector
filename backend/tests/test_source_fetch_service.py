"""SourceFetchService のテスト。

Service は fetch 失敗を例外として伝播する (retry 判断は呼び出し側の Task)。
永続化オーケストレーション (重複排除 / 上限制御 / session.add) は本 Service の責務。
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.ingestion.candidate import ArticleCandidate
from app.collection.ingestion.service import SourceFetchService
from app.domain.safe_url import SafeUrl
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource


@pytest.mark.asyncio
async def test_execute_returns_not_found_for_missing_source(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """存在しない source_id の場合は status='not_found' を返す。"""
    svc = SourceFetchService(session_factory)

    result = await svc.execute(source_id=9999)

    assert result.status == "not_found"
    assert result.new_discovered == []


@pytest.mark.asyncio
async def test_execute_returns_skipped_quota_when_exceeded(
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """DAILY_REQUEST_LIMIT を持つ fetcher でクォータ超過なら status='skipped_quota'。"""
    mock_fetcher = AsyncMock()
    mock_fetcher.DAILY_REQUEST_LIMIT = 25

    with (
        patch(
            "app.collection.ingestion.service.get_fetcher",
            return_value=mock_fetcher,
        ),
        patch(
            "app.collection.ingestion.service.check_daily_quota",
            return_value=False,
        ),
    ):
        svc = SourceFetchService(session_factory)
        result = await svc.execute(source_id=sample_source.id)

    assert result.status == "skipped_quota"
    mock_fetcher.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_execute_skips_quota_check_for_fetcher_without_limit(
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """DAILY_REQUEST_LIMIT を持たない fetcher ではクォータチェックをスキップ。"""
    mock_fetcher = AsyncMock()
    # DAILY_REQUEST_LIMIT 属性を持たせない
    del mock_fetcher.DAILY_REQUEST_LIMIT
    mock_fetcher.fetch = AsyncMock(return_value={})

    with (
        patch(
            "app.collection.ingestion.service.get_fetcher",
            return_value=mock_fetcher,
        ),
        patch(
            "app.collection.ingestion.service.check_daily_quota",
        ) as mock_quota,
    ):
        svc = SourceFetchService(session_factory)
        result = await svc.execute(source_id=sample_source.id)

    mock_quota.assert_not_called()
    assert result.status == "fetched"


@pytest.mark.asyncio
async def test_execute_persists_new_candidates(
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """fetcher から返った候補 dict が DB に永続化される。"""
    url_1 = SafeUrl("https://example.com/1")
    url_2 = SafeUrl("https://example.com/2")
    candidates = {
        url_1: ArticleCandidate(url=url_1, title="Article 1"),
        url_2: ArticleCandidate(url=url_2, title="Article 2"),
    }

    mock_fetcher = AsyncMock()
    del mock_fetcher.DAILY_REQUEST_LIMIT
    mock_fetcher.fetch = AsyncMock(return_value=candidates)

    with patch(
        "app.collection.ingestion.service.get_fetcher",
        return_value=mock_fetcher,
    ):
        svc = SourceFetchService(session_factory)
        result = await svc.execute(source_id=sample_source.id)

    assert result.status == "fetched"
    assert len(result.new_discovered) == 2
    assert all(a.news_source_id == sample_source.id for a in result.new_discovered)

    async with session_factory() as verify:
        rows = (await verify.execute(select(DiscoveredArticle))).scalars().all()
        assert len(rows) == 2


@pytest.mark.asyncio
async def test_execute_skips_duplicate_urls(
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """既に DB に存在する URL は重複排除される。"""
    async with session_factory() as seed:
        seed.add(
            DiscoveredArticle(
                original_title="Existing",
                original_url="https://example.com/existing",
                news_source_id=sample_source.id,
            )
        )
        await seed.commit()

    url_existing = SafeUrl("https://example.com/existing")
    url_new = SafeUrl("https://example.com/new")
    candidates = {
        url_existing: ArticleCandidate(url=url_existing, title="Existing"),
        url_new: ArticleCandidate(url=url_new, title="New One"),
    }

    mock_fetcher = AsyncMock()
    del mock_fetcher.DAILY_REQUEST_LIMIT
    mock_fetcher.fetch = AsyncMock(return_value=candidates)

    with patch(
        "app.collection.ingestion.service.get_fetcher",
        return_value=mock_fetcher,
    ):
        svc = SourceFetchService(session_factory)
        result = await svc.execute(source_id=sample_source.id)

    assert len(result.new_discovered) == 1
    assert result.new_discovered[0].original_url == url_new


@pytest.mark.asyncio
async def test_execute_respects_max_articles_limit(
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """max_articles_per_fetch の上限を超えない。"""
    candidates: dict[SafeUrl, ArticleCandidate] = {}
    for i in range(60):
        url = SafeUrl(f"https://example.com/{i}")
        candidates[url] = ArticleCandidate(url=url, title=f"Article {i}")

    mock_fetcher = AsyncMock()
    del mock_fetcher.DAILY_REQUEST_LIMIT
    mock_fetcher.fetch = AsyncMock(return_value=candidates)

    with (
        patch(
            "app.collection.ingestion.service.get_fetcher",
            return_value=mock_fetcher,
        ),
        patch("app.collection.ingestion.service.settings") as mock_settings,
    ):
        mock_settings.max_articles_per_fetch = 50
        svc = SourceFetchService(session_factory)
        result = await svc.execute(source_id=sample_source.id)

    assert len(result.new_discovered) == 50


@pytest.mark.asyncio
async def test_execute_with_empty_candidates(
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """fetcher が空 dict を返した場合、新規記事は 0 件。"""
    mock_fetcher = AsyncMock()
    del mock_fetcher.DAILY_REQUEST_LIMIT
    mock_fetcher.fetch = AsyncMock(return_value={})

    with patch(
        "app.collection.ingestion.service.get_fetcher",
        return_value=mock_fetcher,
    ):
        svc = SourceFetchService(session_factory)
        result = await svc.execute(source_id=sample_source.id)

    assert result.status == "fetched"
    assert result.new_discovered == []


@pytest.mark.asyncio
async def test_execute_propagates_permanent_error(
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """PermanentFetchError は Task 層に伝播する。"""
    mock_fetcher = AsyncMock()
    del mock_fetcher.DAILY_REQUEST_LIMIT
    mock_fetcher.fetch = AsyncMock(side_effect=PermanentFetchError("HTTP 404: sample"))

    with patch(
        "app.collection.ingestion.service.get_fetcher",
        return_value=mock_fetcher,
    ):
        svc = SourceFetchService(session_factory)
        with pytest.raises(PermanentFetchError):
            await svc.execute(source_id=sample_source.id)


@pytest.mark.asyncio
async def test_execute_propagates_temporary_error(
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """TemporaryFetchError は Task 層に伝播する (retry 判断は Task の責務)。"""
    mock_fetcher = AsyncMock()
    del mock_fetcher.DAILY_REQUEST_LIMIT
    mock_fetcher.fetch = AsyncMock(side_effect=TemporaryFetchError("HTTP 500: sample"))

    with patch(
        "app.collection.ingestion.service.get_fetcher",
        return_value=mock_fetcher,
    ):
        svc = SourceFetchService(session_factory)
        with pytest.raises(TemporaryFetchError):
            await svc.execute(source_id=sample_source.id)
