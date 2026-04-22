"""コンテンツタスク (fetch_content) のテスト。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.collection.errors import TemporaryFetchError
from app.collection.extraction.candidate import DiscoveredNotFound
from app.collection.extraction.service import ArticleReady, ExtractionFailed


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
    async def test_article_ready_chains_analyze(self) -> None:
        """ArticleReady を受け取ったら article_id を下流にチェーンする。"""
        from app.collection.tasks import fetch_content

        mock_ctx = _make_ctx()

        with (
            patch("app.collection.tasks.ContentFetchService") as mock_svc_cls,
            patch("app.analysis.tasks.extract_content") as mock_analyze,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=ArticleReady(article_id=42)
            )
            mock_analyze.kiq = AsyncMock()
            await fetch_content(discovered_article_id=1, ctx=mock_ctx)

        mock_svc_cls.return_value.execute.assert_called_once_with(1)
        mock_analyze.kiq.assert_called_once_with(42)

    @pytest.mark.asyncio
    async def test_extraction_failed_does_not_chain(self) -> None:
        """ExtractionFailed（外部/品質の問題）は下流へチェーンしない。"""
        from app.collection.tasks import fetch_content

        mock_ctx = _make_ctx()

        with (
            patch("app.collection.tasks.ContentFetchService") as mock_svc_cls,
            patch("app.analysis.tasks.extract_content") as mock_analyze,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=ExtractionFailed(reason="quality_gate")
            )
            mock_analyze.kiq = AsyncMock()
            await fetch_content(discovered_article_id=1, ctx=mock_ctx)

        mock_analyze.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_discovered_not_found_does_not_chain(self) -> None:
        """DiscoveredNotFound（DB 不整合）は下流へチェーンしない。"""
        from app.collection.tasks import fetch_content

        mock_ctx = _make_ctx()

        with (
            patch("app.collection.tasks.ContentFetchService") as mock_svc_cls,
            patch("app.analysis.tasks.extract_content") as mock_analyze,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=DiscoveredNotFound()
            )
            mock_analyze.kiq = AsyncMock()
            await fetch_content(discovered_article_id=1, ctx=mock_ctx)

        mock_analyze.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_temporary_error_raises_for_retry(self) -> None:
        from app.collection.tasks import fetch_content

        mock_ctx = _make_ctx(retry_count=0, max_retries=3)

        with patch(
            "app.collection.tasks.ContentFetchService",
        ) as mock_svc_cls:
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=TemporaryFetchError("HTTP 500"),
            )
            with pytest.raises(TemporaryFetchError):
                await fetch_content(discovered_article_id=1, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_temporary_error_last_attempt_returns(self) -> None:
        """最終試行では例外を送出せず return する。"""
        from app.collection.tasks import fetch_content

        mock_ctx = _make_ctx(retry_count=3, max_retries=3)

        with patch(
            "app.collection.tasks.ContentFetchService",
        ) as mock_svc_cls:
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=TemporaryFetchError("HTTP 500"),
            )
            # 最終試行では例外を送出しないこと
            await fetch_content(discovered_article_id=1, ctx=mock_ctx)
