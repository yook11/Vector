"""メタデータタスク (dispatch_sources / fetch_source_metadata) のテスト。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.collection.ingestion.persister import SourceFetchResult
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

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()
        _patch_session_factory(mock_ctx, mock_session)

        source = MagicMock(spec=NewsSource)
        source.id = 1
        source.name = "Test Source"
        mock_session.get = AsyncMock(return_value=source)

        discovered_a = MagicMock(spec=DiscoveredArticle)
        discovered_a.id = 10

        discovered_b = MagicMock(spec=DiscoveredArticle)
        discovered_b.id = 11

        fetch_result = SourceFetchResult(
            source_id=1,
            success=True,
            new_count=2,
            new_discovered=[discovered_a, discovered_b],
        )

        mock_fetcher = AsyncMock()
        mock_fetcher.fetch = AsyncMock(return_value=fetch_result)

        with (
            patch(
                "app.collection.tasks.get_fetcher",
                return_value=mock_fetcher,
            ),
            patch("app.collection.tasks.fetch_content") as mock_fc,
        ):
            mock_fc.kiq = AsyncMock()
            result = await fetch_source_metadata(source_id=1, ctx=mock_ctx)

        assert result["new_count"] == 2
        assert result["success"] is True
        # 全件 fetch_content へ dispatch
        assert mock_fc.kiq.call_count == 2
        mock_fc.kiq.assert_any_call(10)
        mock_fc.kiq.assert_any_call(11)

    @pytest.mark.asyncio
    async def test_skips_when_daily_quota_exceeded(self) -> None:
        """クォータ超過時は fetcher.fetch を呼ばず skipped を返す。"""
        from app.collection.tasks import fetch_source_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()
        _patch_session_factory(mock_ctx, mock_session)

        source = MagicMock(spec=NewsSource)
        source.id = 1
        source.name = "AV Source"
        mock_session.get = AsyncMock(return_value=source)

        mock_fetcher = AsyncMock()
        mock_fetcher.DAILY_REQUEST_LIMIT = 25

        with (
            patch(
                "app.collection.tasks.get_fetcher",
                return_value=mock_fetcher,
            ),
            patch(
                "app.collection.tasks.check_daily_quota",
                return_value=False,
            ) as mock_quota,
        ):
            result = await fetch_source_metadata(source_id=1, ctx=mock_ctx)

        assert result["status"] == "skipped"
        assert result["reason"] == "daily_quota"
        mock_quota.assert_called_once_with(1, 25)
        mock_fetcher.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_proceeds_when_daily_quota_available(self) -> None:
        """クォータに余裕がある場合は通常フローで fetch を実行する。"""
        from app.collection.tasks import fetch_source_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()
        _patch_session_factory(mock_ctx, mock_session)

        source = MagicMock(spec=NewsSource)
        source.id = 1
        source.name = "AV Source"
        mock_session.get = AsyncMock(return_value=source)

        fetch_result = SourceFetchResult(
            source_id=1, success=True, new_count=0, new_discovered=[]
        )
        mock_fetcher = AsyncMock()
        mock_fetcher.DAILY_REQUEST_LIMIT = 25
        mock_fetcher.fetch = AsyncMock(return_value=fetch_result)

        with (
            patch(
                "app.collection.tasks.get_fetcher",
                return_value=mock_fetcher,
            ),
            patch(
                "app.collection.tasks.check_daily_quota",
                return_value=True,
            ) as mock_quota,
        ):
            result = await fetch_source_metadata(source_id=1, ctx=mock_ctx)

        mock_quota.assert_called_once_with(1, 25)
        mock_fetcher.fetch.assert_called_once()
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_skips_quota_check_for_fetcher_without_limit(self) -> None:
        """DAILY_REQUEST_LIMIT を持たない Fetcher ではクォータチェックをスキップ。"""
        from app.collection.tasks import fetch_source_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()
        _patch_session_factory(mock_ctx, mock_session)

        source = MagicMock(spec=NewsSource)
        source.id = 1
        source.name = "RSS Source"
        mock_session.get = AsyncMock(return_value=source)

        fetch_result = SourceFetchResult(
            source_id=1, success=True, new_count=0, new_discovered=[]
        )
        mock_fetcher = AsyncMock()
        # DAILY_REQUEST_LIMIT を持たない
        del mock_fetcher.DAILY_REQUEST_LIMIT
        mock_fetcher.fetch = AsyncMock(return_value=fetch_result)

        with (
            patch(
                "app.collection.tasks.get_fetcher",
                return_value=mock_fetcher,
            ),
            patch(
                "app.collection.tasks.check_daily_quota",
            ) as mock_quota,
        ):
            result = await fetch_source_metadata(source_id=1, ctx=mock_ctx)

        mock_quota.assert_not_called()
        mock_fetcher.fetch.assert_called_once()
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_returns_not_found_for_missing_source(self) -> None:
        """存在しないソース ID の場合は not_found を返す。"""
        from app.collection.tasks import fetch_source_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()
        _patch_session_factory(mock_ctx, mock_session)

        mock_session.get = AsyncMock(return_value=None)

        result = await fetch_source_metadata(source_id=999, ctx=mock_ctx)

        assert result["status"] == "not_found"
