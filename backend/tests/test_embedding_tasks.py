"""Embedding タスク (generate_embedding) のテスト。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis import RateLimitError


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


def _patch_embedder() -> MagicMock:
    """ClassVar 属性を持つモック embedder を返す。"""
    mock_embedder = MagicMock()
    mock_embedder.MODEL = "gemini-embedding-001"
    mock_embedder.RPM = 15
    mock_embedder.RPD = 1500
    return mock_embedder


# ---------------------------------------------------------------------------
# generate_embedding
# ---------------------------------------------------------------------------


class TestGenerateEmbedding:
    @pytest.mark.asyncio
    async def test_created_succeeds(self) -> None:
        from app.tasks.analysis_tasks import generate_embedding

        mock_ctx = _make_ctx()
        mock_result = MagicMock(status="created")

        with (
            patch(
                "app.tasks.analysis_tasks.get_embedder",
                return_value=_patch_embedder(),
            ),
            patch(
                "app.tasks.analysis_tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.tasks.analysis_tasks.EmbeddingService",
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=mock_result,
            )
            await generate_embedding(article_id=1, ctx=mock_ctx)

        mock_svc_cls.return_value.execute.assert_called_once()
        call_args = mock_svc_cls.return_value.execute.call_args
        assert call_args[0][0] == 1  # article_id であること

    @pytest.mark.asyncio
    async def test_already_exists_succeeds(self) -> None:
        from app.tasks.analysis_tasks import generate_embedding

        mock_ctx = _make_ctx()
        mock_result = MagicMock(status="already_exists")

        with (
            patch(
                "app.tasks.analysis_tasks.get_embedder",
                return_value=_patch_embedder(),
            ),
            patch(
                "app.tasks.analysis_tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.tasks.analysis_tasks.EmbeddingService",
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=mock_result,
            )
            await generate_embedding(article_id=1, ctx=mock_ctx)

        mock_svc_cls.return_value.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limit_raises_for_retry(self) -> None:
        from app.tasks.analysis_tasks import generate_embedding

        mock_ctx = _make_ctx(retry_count=0, max_retries=2)

        with (
            patch(
                "app.tasks.analysis_tasks.get_embedder",
                return_value=_patch_embedder(),
            ),
            patch(
                "app.tasks.analysis_tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.tasks.analysis_tasks.EmbeddingService",
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            with pytest.raises(RateLimitError):
                await generate_embedding(article_id=1, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_rate_limit_last_attempt_returns(self) -> None:
        from app.tasks.analysis_tasks import generate_embedding

        mock_ctx = _make_ctx(retry_count=2, max_retries=2)

        with (
            patch(
                "app.tasks.analysis_tasks.get_embedder",
                return_value=_patch_embedder(),
            ),
            patch(
                "app.tasks.analysis_tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.tasks.analysis_tasks.EmbeddingService",
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            # 最終試行では例外を送出しないこと
            await generate_embedding(article_id=1, ctx=mock_ctx)
