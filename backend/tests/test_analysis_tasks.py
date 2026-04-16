"""Tests for analysis tasks (analyze_article)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis import RateLimitError


def _make_ctx(
    retry_count: int = 0,
    max_retries: int = 0,
) -> MagicMock:
    """Create a mock taskiq Context with state.session_factory and labels."""
    ctx = MagicMock()
    ctx.state.session_factory = MagicMock()
    ctx.message.labels = {
        "retry_count": retry_count,
        "max_retries": max_retries,
    }
    return ctx


def _patch_analyzer() -> tuple:
    """Return patch context managers for get_analyzer + _build_limiters."""
    mock_analyzer = MagicMock()
    mock_analyzer.MODEL = "test-model"
    mock_analyzer.RPM = 50
    mock_analyzer.RPD = 1500
    return mock_analyzer


# ---------------------------------------------------------------------------
# analyze_article
# ---------------------------------------------------------------------------


class TestAnalyzeArticle:
    @pytest.mark.asyncio
    async def test_already_exists_chains_embedding(self) -> None:
        from app.tasks.analysis_tasks import analyze_article

        mock_ctx = _make_ctx()
        mock_result = MagicMock(status="already_exists")

        with (
            patch(
                "app.tasks.analysis_tasks.get_analyzer",
                return_value=_patch_analyzer(),
            ),
            patch(
                "app.tasks.analysis_tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.tasks.analysis_tasks.ArticleAnalysisService",
            ) as mock_svc_cls,
            patch(
                "app.tasks.analysis_tasks.generate_embedding",
            ) as mock_embed,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=mock_result,
            )
            mock_embed.kiq = AsyncMock()
            await analyze_article(article_id=1, ctx=mock_ctx)

        mock_embed.kiq.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_success_chains_embedding(self) -> None:
        from app.tasks.analysis_tasks import analyze_article

        mock_ctx = _make_ctx()
        mock_result = MagicMock(status="created")

        with (
            patch(
                "app.tasks.analysis_tasks.get_analyzer",
                return_value=_patch_analyzer(),
            ),
            patch(
                "app.tasks.analysis_tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.tasks.analysis_tasks.ArticleAnalysisService",
            ) as mock_svc_cls,
            patch(
                "app.tasks.analysis_tasks.generate_embedding",
            ) as mock_embed,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=mock_result,
            )
            mock_embed.kiq = AsyncMock()
            await analyze_article(article_id=1, ctx=mock_ctx)

        mock_embed.kiq.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_skipped_does_not_chain(self) -> None:
        from app.tasks.analysis_tasks import analyze_article

        mock_ctx = _make_ctx()
        mock_result = MagicMock(status="skipped")

        with (
            patch(
                "app.tasks.analysis_tasks.get_analyzer",
                return_value=_patch_analyzer(),
            ),
            patch(
                "app.tasks.analysis_tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.tasks.analysis_tasks.ArticleAnalysisService",
            ) as mock_svc_cls,
            patch(
                "app.tasks.analysis_tasks.generate_embedding",
            ) as mock_embed,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=mock_result,
            )
            mock_embed.kiq = AsyncMock()
            await analyze_article(article_id=1, ctx=mock_ctx)

        mock_embed.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_raises_for_retry(self) -> None:
        from app.tasks.analysis_tasks import analyze_article

        mock_ctx = _make_ctx(retry_count=0, max_retries=2)

        with (
            patch(
                "app.tasks.analysis_tasks.get_analyzer",
                return_value=_patch_analyzer(),
            ),
            patch(
                "app.tasks.analysis_tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.tasks.analysis_tasks.ArticleAnalysisService",
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            with pytest.raises(RateLimitError):
                await analyze_article(article_id=1, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_rate_limit_last_attempt_marks_skipped(self) -> None:
        from app.tasks.analysis_tasks import analyze_article

        mock_ctx = _make_ctx(retry_count=2, max_retries=2)

        with (
            patch(
                "app.tasks.analysis_tasks.get_analyzer",
                return_value=_patch_analyzer(),
            ),
            patch(
                "app.tasks.analysis_tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.tasks.analysis_tasks.ArticleAnalysisService",
            ) as mock_svc_cls,
            patch(
                "app.tasks.analysis_tasks.mark_article_skipped",
                new_callable=AsyncMock,
            ) as mock_skip,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            await analyze_article(article_id=1, ctx=mock_ctx)

        mock_skip.assert_called_once_with(
            mock_ctx.state.session_factory,
            1,
        )
