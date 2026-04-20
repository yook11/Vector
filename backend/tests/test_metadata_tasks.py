"""メタデータタスク (dispatch_sources / fetch_source_metadata) のテスト。

Task 層は SourceFetchService を呼ぶだけ — ビジネス判断は Service に閉じているため、
ここでは Service を mock し、Task の分岐 (status ディスパッチ + retry) を検証する。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.ingestion.service import SourceFetchResult
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource


def _mock_session_context(mock_session: AsyncMock) -> MagicMock:
    """mock_session を返す async context manager モックを作成する。"""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _make_ctx(
    session_factory: MagicMock | None = None,
    retry_count: int = 0,
    max_retries: int = 0,
) -> MagicMock:
    """state.session_factory と labels を持つ taskiq Context のモックを作成する。"""
    ctx = MagicMock()
    ctx.state.session_factory = session_factory or MagicMock()
    ctx.message.labels = {
        "retry_count": retry_count,
        "max_retries": max_retries,
    }
    return ctx


def _patch_session_factory(ctx: MagicMock, mock_session: AsyncMock) -> None:
    """ctx.state.session_factory() が async cm 経由で mock_session を返すようにする。"""
    ctx.state.session_factory.return_value = _mock_session_context(mock_session)


def _patch_service(result_or_exc) -> object:  # noqa: ANN001
    """SourceFetchService.execute を mock する context manager を返す。"""
    if isinstance(result_or_exc, BaseException):
        execute = AsyncMock(side_effect=result_or_exc)
    else:
        execute = AsyncMock(return_value=result_or_exc)
    svc = MagicMock()
    svc.execute = execute
    return patch(
        "app.collection.tasks.SourceFetchService",
        return_value=svc,
    )


# ---------------------------------------------------------------------------
# dispatch_sources
# ---------------------------------------------------------------------------


class TestDispatchSources:
    @pytest.mark.asyncio
    async def test_dispatches_all_active_sources(self) -> None:
        """全アクティブソースに対して fetch_source_metadata を dispatch する。"""
        from app.collection.tasks import dispatch_sources

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()
        _patch_session_factory(mock_ctx, mock_session)

        source_a = MagicMock(spec=NewsSource)
        source_a.id = 1
        source_b = MagicMock(spec=NewsSource)
        source_b.id = 2
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [source_a, source_b]
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch("app.collection.tasks.fetch_source_metadata") as mock_fsm:
            mock_fsm.kiq = AsyncMock()
            result = await dispatch_sources(ctx=mock_ctx)

        assert result["dispatched_count"] == 2
        assert mock_fsm.kiq.call_count == 2
        mock_fsm.kiq.assert_any_call(1)
        mock_fsm.kiq.assert_any_call(2)

    @pytest.mark.asyncio
    async def test_skips_when_no_sources(self) -> None:
        """アクティブソースが無い場合は dispatch しない。"""
        from app.collection.tasks import dispatch_sources

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()
        _patch_session_factory(mock_ctx, mock_session)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await dispatch_sources(ctx=mock_ctx)

        assert result["dispatched_count"] == 0


# ---------------------------------------------------------------------------
# fetch_source_metadata
# ---------------------------------------------------------------------------


class TestFetchSourceMetadata:
    @pytest.mark.asyncio
    async def test_fetches_and_dispatches_content(self) -> None:
        """新規記事を全件 fetch_content に dispatch する。"""
        from app.collection.tasks import fetch_source_metadata

        mock_ctx = _make_ctx()
        discovered_a = MagicMock(spec=DiscoveredArticle)
        discovered_a.id = 10
        discovered_b = MagicMock(spec=DiscoveredArticle)
        discovered_b.id = 11

        fetch_result = SourceFetchResult(
            status="fetched",
            new_discovered=[discovered_a, discovered_b],
        )

        with (
            _patch_service(fetch_result),
            patch("app.collection.tasks._record_fetch_log", new_callable=AsyncMock),
            patch("app.collection.tasks.fetch_content") as mock_fc,
        ):
            mock_fc.kiq = AsyncMock()
            result = await fetch_source_metadata(source_id=1, ctx=mock_ctx)

        assert result["new_count"] == 2
        assert result["status"] == "success"
        assert mock_fc.kiq.call_count == 2
        mock_fc.kiq.assert_any_call(10)
        mock_fc.kiq.assert_any_call(11)

    @pytest.mark.asyncio
    async def test_skipped_quota_returns_early(self) -> None:
        """Service が status='skipped_quota' を返したら下流 dispatch しない。"""
        from app.collection.tasks import fetch_source_metadata

        mock_ctx = _make_ctx()
        fetch_result = SourceFetchResult(status="skipped_quota")

        with (
            _patch_service(fetch_result),
            patch(
                "app.collection.tasks._record_fetch_log",
                new_callable=AsyncMock,
            ) as mock_log,
            patch("app.collection.tasks.fetch_content") as mock_fc,
        ):
            mock_fc.kiq = AsyncMock()
            result = await fetch_source_metadata(source_id=1, ctx=mock_ctx)

        assert result["status"] == "skipped"
        assert result["reason"] == "daily_quota"
        mock_fc.kiq.assert_not_called()
        mock_log.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_found_returns_early(self) -> None:
        """Service が status='not_found' を返したら not_found を返す。"""
        from app.collection.tasks import fetch_source_metadata

        mock_ctx = _make_ctx()
        fetch_result = SourceFetchResult(status="not_found")

        with (
            _patch_service(fetch_result),
            patch(
                "app.collection.tasks._record_fetch_log",
                new_callable=AsyncMock,
            ) as mock_log,
            patch("app.collection.tasks.fetch_content") as mock_fc,
        ):
            mock_fc.kiq = AsyncMock()
            result = await fetch_source_metadata(source_id=999, ctx=mock_ctx)

        assert result["status"] == "not_found"
        mock_fc.kiq.assert_not_called()
        mock_log.assert_not_called()

    @pytest.mark.asyncio
    async def test_permanent_error_records_error_status(self) -> None:
        """PermanentFetchError を捕捉して status=error を返す (raise しない)。"""
        from app.collection.tasks import fetch_source_metadata

        mock_ctx = _make_ctx()

        with (
            _patch_service(PermanentFetchError("HTTP 404: Broken Source")),
            patch(
                "app.collection.tasks._record_fetch_log",
                new_callable=AsyncMock,
            ) as mock_log,
            patch("app.collection.tasks.fetch_content") as mock_fc,
        ):
            mock_fc.kiq = AsyncMock()
            result = await fetch_source_metadata(source_id=1, ctx=mock_ctx)

        assert result["status"] == "error"
        assert "404" in result["reason"]
        mock_log.assert_awaited_once()
        mock_fc.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_temporary_error_raises_for_retry(self) -> None:
        """TemporaryFetchError は retry 可能なので raise する。"""
        from app.collection.tasks import fetch_source_metadata

        mock_ctx = _make_ctx(retry_count=0, max_retries=2)

        with (
            _patch_service(TemporaryFetchError("HTTP 500: Flaky Source")),
            patch("app.collection.tasks._record_fetch_log", new_callable=AsyncMock),
        ):
            with pytest.raises(TemporaryFetchError):
                await fetch_source_metadata(source_id=1, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_temporary_error_last_attempt_returns(self) -> None:
        """TemporaryFetchError でも最終試行では飲み込んで status=error を返す。"""
        from app.collection.tasks import fetch_source_metadata

        mock_ctx = _make_ctx(retry_count=2, max_retries=2)

        with (
            _patch_service(TemporaryFetchError("HTTP 500: Flaky Source")),
            patch("app.collection.tasks._record_fetch_log", new_callable=AsyncMock),
        ):
            result = await fetch_source_metadata(source_id=1, ctx=mock_ctx)

        assert result["status"] == "error"
