"""fetch_source_metadata での FetchLog 記録のテスト。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.errors import PermanentFetchError
from app.collection.ingestion.persister import SourceFetchResult
from app.models.discovered_article import DiscoveredArticle
from app.models.fetch_log import FetchLog, FetchStatus
from app.models.news_source import NewsSource


def _mock_session_context(mock_session: AsyncSession) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _make_ctx(session_factory: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.state.session_factory = session_factory
    ctx.message.labels = {"retry_count": 0, "max_retries": 0}
    return ctx


@pytest.mark.asyncio
async def test_fetch_log_recorded_on_success(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """フェッチ成功時に status='success' の FetchLog が記録される。"""
    from app.collection.tasks import fetch_source_metadata

    discovered = MagicMock(spec=DiscoveredArticle)
    discovered.id = 1

    fetch_result = SourceFetchResult(new_discovered=[discovered])

    mock_fetcher = AsyncMock()
    mock_fetcher.fetch = AsyncMock(return_value=fetch_result)

    # session_factory として db_session を返す factory を使う
    session_factory = MagicMock()
    session_factory.return_value = _mock_session_context(db_session)
    mock_ctx = _make_ctx(session_factory)

    with (
        patch(
            "app.collection.tasks.get_fetcher",
            return_value=mock_fetcher,
        ),
        patch("app.collection.tasks.fetch_content") as mock_fc,
    ):
        mock_fc.kiq = AsyncMock()
        await fetch_source_metadata(source_id=sample_source.id, ctx=mock_ctx)

    stmt = select(FetchLog).where(FetchLog.source_id == sample_source.id)
    result = await db_session.execute(stmt)
    log = result.scalar_one()

    assert log.status == FetchStatus.SUCCESS
    assert log.articles_count == 1
    assert log.error_message is None
    assert log.duration_ms is not None
    assert log.duration_ms >= 0


@pytest.mark.asyncio
async def test_fetch_log_recorded_on_permanent_error(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """PermanentFetchError を捕捉して status='error' の FetchLog が記録される。"""
    from app.collection.tasks import fetch_source_metadata

    mock_fetcher = AsyncMock()
    mock_fetcher.fetch = AsyncMock(
        side_effect=PermanentFetchError("HTTP 404: Test Source")
    )

    session_factory = MagicMock()
    session_factory.return_value = _mock_session_context(db_session)
    mock_ctx = _make_ctx(session_factory)

    with patch(
        "app.collection.tasks.get_fetcher",
        return_value=mock_fetcher,
    ):
        await fetch_source_metadata(source_id=sample_source.id, ctx=mock_ctx)

    stmt = select(FetchLog).where(FetchLog.source_id == sample_source.id)
    result = await db_session.execute(stmt)
    log = result.scalar_one()

    assert log.status == FetchStatus.ERROR
    assert log.articles_count == 0
    assert log.error_message == "HTTP 404: Test Source"
    assert log.duration_ms is not None
