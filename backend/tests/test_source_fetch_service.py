"""SourceFetchService のテスト。

Service は fetch 失敗を例外として伝播する (呼び出し側の Task が retry 判断をする)。
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.ingestion.persister import PersistResult
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
    mock_fetcher.fetch = AsyncMock(return_value=PersistResult(new_discovered=[]))

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
async def test_execute_returns_fetched_with_new_discovered(
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """fetch 成功時は status='fetched' と新規記事リストを返す。"""

    async def fake_fetch(client, session, source):  # noqa: ANN001
        discovered = DiscoveredArticle(
            original_title="New Story",
            original_url=SafeUrl("https://example.com/new"),
            news_source_id=source.id,
        )
        session.add(discovered)
        return PersistResult(new_discovered=[discovered])

    mock_fetcher = AsyncMock()
    del mock_fetcher.DAILY_REQUEST_LIMIT
    mock_fetcher.fetch = AsyncMock(side_effect=fake_fetch)

    with patch(
        "app.collection.ingestion.service.get_fetcher",
        return_value=mock_fetcher,
    ):
        svc = SourceFetchService(session_factory)
        result = await svc.execute(source_id=sample_source.id)

    assert result.status == "fetched"
    assert len(result.new_discovered) == 1
    assert result.new_discovered[0].id is not None


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
