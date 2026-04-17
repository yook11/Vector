"""メタデータタスク (dispatch_sources / fetch_source_metadata) のテスト。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.collection.article_persister import SourceFetchResult
from app.models.news_article import NewsArticle
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
        from app.tasks.collection_tasks import dispatch_sources

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

        with patch("app.tasks.collection_tasks.fetch_source_metadata") as mock_fsm:
            mock_fsm.kiq = AsyncMock()
            result = await dispatch_sources(ctx=mock_ctx)

        assert result["dispatched_count"] == 2
        assert mock_fsm.kiq.call_count == 2
        mock_fsm.kiq.assert_any_call(1)
        mock_fsm.kiq.assert_any_call(2)

    @pytest.mark.asyncio
    async def test_skips_when_no_sources(self) -> None:
        """アクティブソースが無い場合は dispatch しない。"""
        from app.tasks.collection_tasks import dispatch_sources

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
        """新規記事を fetch_content に dispatch する。"""
        from app.tasks.collection_tasks import fetch_source_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()
        _patch_session_factory(mock_ctx, mock_session)

        source = MagicMock(spec=NewsSource)
        source.id = 1
        source.name = "Test Source"
        mock_session.get = AsyncMock(return_value=source)

        article_a = MagicMock(spec=NewsArticle)
        article_a.id = 10
        article_a.original_content = None
        article_a.published_at = None

        article_b = MagicMock(spec=NewsArticle)
        article_b.id = 11
        article_b.original_content = None
        article_b.published_at = None

        fetch_result = SourceFetchResult(
            source_id=1,
            success=True,
            new_count=2,
            new_articles=[article_a, article_b],
        )

        mock_fetcher = AsyncMock()
        mock_fetcher.fetch = AsyncMock(return_value=fetch_result)

        with (
            patch(
                "app.tasks.collection_tasks.get_fetcher",
                return_value=mock_fetcher,
            ),
            patch("app.tasks.collection_tasks.fetch_content") as mock_fc,
            patch("app.tasks.analysis_tasks.analyze_article") as mock_aa,
        ):
            mock_fc.kiq = AsyncMock()
            mock_aa.kiq = AsyncMock()
            result = await fetch_source_metadata(source_id=1, ctx=mock_ctx)

        assert result["new_count"] == 2
        assert result["success"] is True
        assert mock_fc.kiq.call_count == 2
        mock_aa.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatches_content_ready_to_analysis(self) -> None:
        """全文 RSS 記事は analyze_article に直接流す。"""
        from app.tasks.collection_tasks import fetch_source_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()
        _patch_session_factory(mock_ctx, mock_session)

        source = MagicMock(spec=NewsSource)
        source.id = 1
        source.name = "Test Source"
        mock_session.get = AsyncMock(return_value=source)

        from datetime import UTC, datetime

        article_ready = MagicMock(spec=NewsArticle)
        article_ready.id = 10
        article_ready.original_content = "Full content here..."
        article_ready.published_at = datetime(2025, 1, 1, tzinfo=UTC)

        article_need_content = MagicMock(spec=NewsArticle)
        article_need_content.id = 11
        article_need_content.original_content = None
        article_need_content.published_at = None

        fetch_result = SourceFetchResult(
            source_id=1,
            success=True,
            new_count=2,
            new_articles=[article_ready, article_need_content],
        )

        mock_fetcher = AsyncMock()
        mock_fetcher.fetch = AsyncMock(return_value=fetch_result)

        with (
            patch(
                "app.tasks.collection_tasks.get_fetcher",
                return_value=mock_fetcher,
            ),
            patch("app.tasks.collection_tasks.fetch_content") as mock_fc,
            patch("app.tasks.analysis_tasks.analyze_article") as mock_aa,
        ):
            mock_fc.kiq = AsyncMock()
            mock_aa.kiq = AsyncMock()
            await fetch_source_metadata(source_id=1, ctx=mock_ctx)

        # article 10 は content 準備済みなので analysis へ
        mock_aa.kiq.assert_called_once_with(10)
        # article 11 は content fetch が必要
        mock_fc.kiq.assert_called_once_with(11)

    @pytest.mark.asyncio
    async def test_returns_not_found_for_missing_source(self) -> None:
        """存在しないソース ID の場合は not_found を返す。"""
        from app.tasks.collection_tasks import fetch_source_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()
        _patch_session_factory(mock_ctx, mock_session)

        mock_session.get = AsyncMock(return_value=None)

        result = await fetch_source_metadata(source_id=999, ctx=mock_ctx)

        assert result["status"] == "not_found"
