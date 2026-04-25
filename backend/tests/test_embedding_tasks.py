"""Embedding タスク (generate_embedding) のテスト。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.embedding.domain.embedding import Embedding
from app.analysis.embedding.domain.value_objects import EmbeddingVector
from app.analysis.embedding.service import (
    AlreadyEmbeddedOutcome,
    EmbeddedOutcome,
    SkippedOutcome,
)
from app.analysis.errors import RateLimitError


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
    mock_embedder.MODEL = "cl-nagoya/ruri-v3-310m"
    mock_embedder.RPM = None
    mock_embedder.RPD = None
    return mock_embedder


def _make_embedding(analysis_id: int = 1) -> Embedding:
    """テスト用の Embedding Entity を構築する。"""
    return Embedding(
        analysis_id=analysis_id,
        vector=EmbeddingVector(root=tuple([0.1] * 768)),
        model_name="cl-nagoya/ruri-v3-310m",
    )


# ---------------------------------------------------------------------------
# generate_embedding
# ---------------------------------------------------------------------------


class TestGenerateEmbedding:
    @pytest.mark.asyncio
    async def test_embedded_outcome_succeeds(self) -> None:
        """EmbeddedOutcome を Service が返したら task は完了する。"""
        from app.analysis.tasks import generate_embedding

        mock_ctx = _make_ctx()
        outcome = EmbeddedOutcome(embedding=_make_embedding())

        with (
            patch(
                "app.analysis.tasks.get_embedder",
                return_value=_patch_embedder(),
            ),
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.analysis.tasks.EmbeddingService",
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=outcome)
            await generate_embedding(article_id=1, ctx=mock_ctx)

        mock_svc_cls.return_value.execute.assert_called_once()
        call_args = mock_svc_cls.return_value.execute.call_args
        assert call_args[0][0] == 1  # article_id であること

    @pytest.mark.asyncio
    async def test_already_embedded_outcome_succeeds(self) -> None:
        """AlreadyEmbeddedOutcome を Service が返したら task は完了する。"""
        from app.analysis.tasks import generate_embedding

        mock_ctx = _make_ctx()
        outcome = AlreadyEmbeddedOutcome(embedding=_make_embedding())

        with (
            patch(
                "app.analysis.tasks.get_embedder",
                return_value=_patch_embedder(),
            ),
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.analysis.tasks.EmbeddingService",
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=outcome)
            await generate_embedding(article_id=1, ctx=mock_ctx)

        mock_svc_cls.return_value.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_skipped_outcome_succeeds(self) -> None:
        """SkippedOutcome を Service が返したら task は静かに完了する。"""
        from app.analysis.tasks import generate_embedding

        mock_ctx = _make_ctx()
        outcome = SkippedOutcome(reason="extraction_not_found")

        with (
            patch(
                "app.analysis.tasks.get_embedder",
                return_value=_patch_embedder(),
            ),
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.analysis.tasks.EmbeddingService",
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=outcome)
            await generate_embedding(article_id=1, ctx=mock_ctx)

        mock_svc_cls.return_value.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limit_raises_for_retry(self) -> None:
        from app.analysis.tasks import generate_embedding

        mock_ctx = _make_ctx(retry_count=0, max_retries=2)

        with (
            patch(
                "app.analysis.tasks.get_embedder",
                return_value=_patch_embedder(),
            ),
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.analysis.tasks.EmbeddingService",
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            with pytest.raises(RateLimitError):
                await generate_embedding(article_id=1, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_rate_limit_last_attempt_returns(self) -> None:
        from app.analysis.tasks import generate_embedding

        mock_ctx = _make_ctx(retry_count=2, max_retries=2)

        with (
            patch(
                "app.analysis.tasks.get_embedder",
                return_value=_patch_embedder(),
            ),
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.analysis.tasks.EmbeddingService",
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            # 最終試行では例外を送出しないこと
            await generate_embedding(article_id=1, ctx=mock_ctx)
