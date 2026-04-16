"""コンテンツタスク (fetch_content) のテスト。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.collection.article_body_fetcher import TemporaryFetchError


def _make_ctx(
    retry_count: int = 0,
    max_retries: int = 0,
) -> MagicMock:
    """state.session_factory と labels を持つ taskiq Context のモックを作成する。"""
    ctx = MagicMock()
    ctx.state.session_factory = MagicMock()
    ctx.message.labels = {
        "retry_count": retry_count,
        "max_retries": max_retries,
    }
    return ctx


# ---------------------------------------------------------------------------
# fetch_content
# ---------------------------------------------------------------------------


class TestFetchContent:
    @pytest.mark.asyncio
    async def test_fetched_chains_analyze(self) -> None:
        from app.tasks.collection_tasks import fetch_content

        mock_ctx = _make_ctx()
        mock_result = MagicMock(status="fetched")

        with (
            patch("app.tasks.collection_tasks.ContentFetchService") as mock_svc_cls,
            patch("app.tasks.analysis_tasks.analyze_article") as mock_analyze,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=mock_result)
            mock_analyze.kiq = AsyncMock()
            await fetch_content(article_id=1, ctx=mock_ctx)

        mock_svc_cls.return_value.execute.assert_called_once_with(1)
        mock_analyze.kiq.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_already_exists_chains_analyze(self) -> None:
        from app.tasks.collection_tasks import fetch_content

        mock_ctx = _make_ctx()
        mock_result = MagicMock(status="already_exists")

        with (
            patch("app.tasks.collection_tasks.ContentFetchService") as mock_svc_cls,
            patch("app.tasks.analysis_tasks.analyze_article") as mock_analyze,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=mock_result)
            mock_analyze.kiq = AsyncMock()
            await fetch_content(article_id=1, ctx=mock_ctx)

        mock_analyze.kiq.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_skipped_does_not_chain(self) -> None:
        from app.tasks.collection_tasks import fetch_content

        mock_ctx = _make_ctx()
        mock_result = MagicMock(status="skipped")

        with (
            patch("app.tasks.collection_tasks.ContentFetchService") as mock_svc_cls,
            patch("app.tasks.analysis_tasks.analyze_article") as mock_analyze,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=mock_result)
            mock_analyze.kiq = AsyncMock()
            await fetch_content(article_id=1, ctx=mock_ctx)

        mock_analyze.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_temporary_error_raises_for_retry(self) -> None:
        from app.tasks.collection_tasks import fetch_content

        mock_ctx = _make_ctx(retry_count=0, max_retries=3)

        with patch(
            "app.tasks.collection_tasks.ContentFetchService",
        ) as mock_svc_cls:
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=TemporaryFetchError("HTTP 500"),
            )
            with pytest.raises(TemporaryFetchError):
                await fetch_content(article_id=1, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_temporary_error_last_attempt_marks_skip(self) -> None:
        from app.tasks.collection_tasks import fetch_content

        mock_ctx = _make_ctx(retry_count=3, max_retries=3)

        with (
            patch(
                "app.tasks.collection_tasks.ContentFetchService",
            ) as mock_svc_cls,
            patch(
                "app.tasks.collection_tasks.mark_article_skipped",
                new_callable=AsyncMock,
            ) as mock_mark,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=TemporaryFetchError("HTTP 500"),
            )
            # 最終試行では例外を送出しないこと
            await fetch_content(article_id=1, ctx=mock_ctx)

        mock_mark.assert_called_once_with(mock_ctx.state.session_factory, 1)
