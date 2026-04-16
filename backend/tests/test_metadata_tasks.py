"""メタデータタスク (fetch_metadata) のテスト。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.collection.news_fetcher import FetchResult, SourceFetchResult
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
# fetch_metadata
# ---------------------------------------------------------------------------


class TestFetchMetadata:
    @pytest.mark.asyncio
    async def test_fetches_and_dispatches_content(self) -> None:
        from app.tasks.collection_tasks import fetch_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()
        _patch_session_factory(mock_ctx, mock_session)

        source = MagicMock(spec=NewsSource)
        source.id = 1
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [source]
        mock_session.execute = AsyncMock(return_value=mock_result)

        fetch_result = FetchResult(
            new_count=2,
            skipped_count=0,
            error_count=0,
            source_results=[
                SourceFetchResult(source_id=1, success=True, new_count=2),
            ],
            new_article_ids=[10, 11],
            content_ready_ids=[],
        )

        with (
            patch(
                "app.tasks.collection_tasks.fetch_news_for_sources",
                new_callable=AsyncMock,
                return_value=fetch_result,
            ) as mock_fetch,
            patch("app.tasks.collection_tasks.fetch_content") as mock_fc,
            patch("app.tasks.analysis_tasks.analyze_article") as mock_aa,
        ):
            mock_fc.kiq = AsyncMock()
            mock_aa.kiq = AsyncMock()
            result = await fetch_metadata(ctx=mock_ctx)

        assert result["sources_count"] == 1
        assert result["fetch_new"] == 2
        mock_fetch.assert_called_once()
        assert mock_fc.kiq.call_count == 2
        mock_aa.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatches_content_ready_to_analysis(self) -> None:
        """全文 RSS 記事は analyze_article に直接流す。"""
        from app.tasks.collection_tasks import fetch_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()
        _patch_session_factory(mock_ctx, mock_session)

        source = MagicMock(spec=NewsSource)
        source.id = 1
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [source]
        mock_session.execute = AsyncMock(return_value=mock_result)

        fetch_result = FetchResult(
            new_count=3,
            skipped_count=0,
            error_count=0,
            source_results=[
                SourceFetchResult(source_id=1, success=True, new_count=3),
            ],
            new_article_ids=[10, 11, 12],
            content_ready_ids=[10, 12],
        )

        with (
            patch(
                "app.tasks.collection_tasks.fetch_news_for_sources",
                new_callable=AsyncMock,
                return_value=fetch_result,
            ),
            patch("app.tasks.collection_tasks.fetch_content") as mock_fc,
            patch("app.tasks.analysis_tasks.analyze_article") as mock_aa,
        ):
            mock_fc.kiq = AsyncMock()
            mock_aa.kiq = AsyncMock()
            await fetch_metadata(ctx=mock_ctx)

        # article 11 は content fetch が必要
        mock_fc.kiq.assert_called_once_with(11)
        # article 10, 12 は content 準備済みなので analysis へ
        assert mock_aa.kiq.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_when_no_sources(self) -> None:
        from app.tasks.collection_tasks import fetch_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()
        _patch_session_factory(mock_ctx, mock_session)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await fetch_metadata(ctx=mock_ctx)

        assert result["sources_count"] == 0
