"""コンテンツタスク (fetch_content) のテスト。

PR 2b: Outcome union 切替。``DiscoveredArticleMissing`` 例外捕捉が
``ContentFetchSkippedOutcome("discovered_not_found")`` の Service 戻り値で
置き換えられたことを検証する。
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.collection.errors import TemporaryFetchError
from app.collection.extraction.domain import Article, PublishedAt
from app.collection.extraction.service import (
    AlreadyFetchedOutcome,
    ContentFetchedOutcome,
    ContentFetchSkippedOutcome,
)


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


def _article(id: int = 42) -> Article:
    return Article(
        id=id,
        discovered_article_id=id * 10,
        title="Title",
        body="x" * 60,
        published_at=PublishedAt(datetime(2026, 4, 1, tzinfo=UTC)),
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# fetch_content
# ---------------------------------------------------------------------------


class TestFetchContent:
    @pytest.mark.asyncio
    async def test_fetched_outcome_chains_analyze(self) -> None:
        """ContentFetchedOutcome を受け取ったら article.id を下流にチェーンする。"""
        from app.collection.tasks import fetch_content

        mock_ctx = _make_ctx()

        with (
            patch("app.collection.tasks.ContentFetchService") as mock_svc_cls,
            patch("app.analysis.tasks.extract_content") as mock_analyze,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=ContentFetchedOutcome(article=_article(id=42))
            )
            mock_analyze.kiq = AsyncMock()
            await fetch_content(discovered_article_id=1, ctx=mock_ctx)

        mock_svc_cls.return_value.execute.assert_called_once_with(1)
        mock_analyze.kiq.assert_called_once_with(42)

    @pytest.mark.asyncio
    async def test_already_fetched_outcome_chains_analyze(self) -> None:
        """AlreadyFetchedOutcome (冪等ヒット) も article.id を下流にチェーンする。"""
        from app.collection.tasks import fetch_content

        mock_ctx = _make_ctx()

        with (
            patch("app.collection.tasks.ContentFetchService") as mock_svc_cls,
            patch("app.analysis.tasks.extract_content") as mock_analyze,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=AlreadyFetchedOutcome(article=_article(id=99))
            )
            mock_analyze.kiq = AsyncMock()
            await fetch_content(discovered_article_id=1, ctx=mock_ctx)

        mock_analyze.kiq.assert_called_once_with(99)

    @pytest.mark.asyncio
    async def test_skipped_outcome_does_not_chain(self) -> None:
        """ContentFetchSkippedOutcome は理由によらず下流へチェーンしない。"""
        from app.collection.tasks import fetch_content

        mock_ctx = _make_ctx()

        with (
            patch("app.collection.tasks.ContentFetchService") as mock_svc_cls,
            patch("app.analysis.tasks.extract_content") as mock_analyze,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=ContentFetchSkippedOutcome(
                    reason="quality_gate", discovered_article_id=1
                )
            )
            mock_analyze.kiq = AsyncMock()
            await fetch_content(discovered_article_id=1, ctx=mock_ctx)

        mock_analyze.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_discovered_not_found_does_not_chain(self) -> None:
        """discovered_not_found は Service が Skipped で返し、Task は分岐せず終わる。

        PR 2b で DiscoveredArticleMissing 例外が廃止されたことの回帰検証。
        """
        from app.collection.tasks import fetch_content

        mock_ctx = _make_ctx()

        with (
            patch("app.collection.tasks.ContentFetchService") as mock_svc_cls,
            patch("app.analysis.tasks.extract_content") as mock_analyze,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=ContentFetchSkippedOutcome(
                    reason="discovered_not_found", discovered_article_id=1
                )
            )
            mock_analyze.kiq = AsyncMock()
            # 例外は飛ばず、下流チェーンも発火しない
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
            await fetch_content(discovered_article_id=1, ctx=mock_ctx)
