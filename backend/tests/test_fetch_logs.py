"""fetch_source_metadata での FetchLog 記録のテスト (Task 層の責務)。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.collection.errors import PermanentFetchError
from app.collection.ingestion.service import SourceFetchResult
from app.models.discovered_article import DiscoveredArticle
from app.models.fetch_log import FetchLog, FetchStatus
from app.models.news_source import NewsSource


def _make_ctx(session_factory: async_sessionmaker[AsyncSession]) -> MagicMock:
    ctx = MagicMock()
    ctx.state.session_factory = session_factory
    ctx.message.labels = {"retry_count": 0, "max_retries": 0}
    return ctx


def _patch_service_returning(result: SourceFetchResult) -> object:
    svc = MagicMock()
    svc.execute = AsyncMock(return_value=result)
    return patch("app.collection.tasks.SourceFetchService", return_value=svc)


def _patch_service_raising(exc: BaseException) -> object:
    svc = MagicMock()
    svc.execute = AsyncMock(side_effect=exc)
    return patch("app.collection.tasks.SourceFetchService", return_value=svc)


@pytest.mark.asyncio
async def test_fetch_log_recorded_on_success(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """フェッチ成功時に status='success' の FetchLog が記録される。"""
    from app.collection.tasks import fetch_source_metadata

    discovered = MagicMock(spec=DiscoveredArticle)
    discovered.id = 1
    fetch_result = SourceFetchResult(status="fetched", new_discovered=[discovered])

    mock_ctx = _make_ctx(session_factory)

    with (
        _patch_service_returning(fetch_result),
        patch("app.collection.tasks.fetch_content") as mock_fc,
    ):
        mock_fc.kiq = AsyncMock()
        await fetch_source_metadata(source_id=sample_source.id, ctx=mock_ctx)

    stmt = select(FetchLog).where(FetchLog.source_id == sample_source.id)
    log = (await db_session.execute(stmt)).scalar_one()

    assert log.status == FetchStatus.SUCCESS
    assert log.articles_count == 1
    assert log.error_message is None
    assert log.duration_ms is not None
    assert log.duration_ms >= 0


@pytest.mark.asyncio
async def test_fetch_log_recorded_on_permanent_error(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """PermanentFetchError を捕捉して status='error' の FetchLog が記録される。"""
    from app.collection.tasks import fetch_source_metadata

    mock_ctx = _make_ctx(session_factory)

    with _patch_service_raising(PermanentFetchError("HTTP 404: Test Source")):
        await fetch_source_metadata(source_id=sample_source.id, ctx=mock_ctx)

    stmt = select(FetchLog).where(FetchLog.source_id == sample_source.id)
    log = (await db_session.execute(stmt)).scalar_one()

    assert log.status == FetchStatus.ERROR
    assert log.articles_count == 0
    assert log.error_message == "HTTP 404: Test Source"
    assert log.duration_ms is not None


@pytest.mark.asyncio
async def test_no_fetch_log_on_not_found(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """存在しないソース (Service が not_found) では FetchLog を書かない。"""
    from app.collection.tasks import fetch_source_metadata

    mock_ctx = _make_ctx(session_factory)
    fetch_result = SourceFetchResult(status="not_found")

    with _patch_service_returning(fetch_result):
        await fetch_source_metadata(source_id=9999, ctx=mock_ctx)

    stmt = select(FetchLog)
    logs = (await db_session.execute(stmt)).scalars().all()
    assert len(logs) == 0
