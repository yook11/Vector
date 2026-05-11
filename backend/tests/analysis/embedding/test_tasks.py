"""Embedding タスク (generate_embedding) のテスト。

案 3 (厚い Ready + 下流 Stage 自身が処理開始時に構築): generate_embedding は
``EmbeddingTrigger`` (analysis_id のみ) を受領し、task 自身が
``ReadyForEmbedding.try_advance_from`` で Ready を構築する。embedder は
``ctx.state.embedder`` 経由で Pure DI される。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.embedding.domain.embedding import Embedding
from app.analysis.embedding.domain.ready import (
    EmbeddingTrigger,
    ReadyForEmbedding,
)
from app.analysis.embedding.domain.value_objects import EmbeddingVector
from app.analysis.embedding.service import (
    EmbeddedOutcome,
    InvalidInputOutcome,
)
from app.analysis.errors import RateLimitError


def _make_embedder_fake() -> MagicMock:
    """ctx.state.embedder 用のスタブ。MODEL/RPM/RPD を持つ。"""
    fake = MagicMock()
    fake.MODEL = "cl-nagoya/ruri-v3-310m"
    fake.RPM = None
    fake.RPD = None
    return fake


def _make_ctx(
    *,
    embedder: MagicMock | None = None,
    retry_count: int = 0,
    max_retries: int = 0,
) -> MagicMock:
    """taskiq Context モック (state.embedder Pure DI)。"""
    ctx = MagicMock()
    ctx.state = SimpleNamespace(session_factory=MagicMock())
    if embedder is not None:
        ctx.state.embedder = embedder
    ctx.message.labels = {
        "retry_count": retry_count,
        "max_retries": max_retries,
    }
    return ctx


def _make_trigger(analysis_id: int = 1) -> EmbeddingTrigger:
    return EmbeddingTrigger(analysis_id=analysis_id)


def _make_ready(analysis_id: int = 1) -> ReadyForEmbedding:
    return ReadyForEmbedding(
        analysis_id=analysis_id,
        text_for_embedding="分析タイトル\n分析要約",
    )


def _make_embedding(analysis_id: int = 1) -> Embedding:
    return Embedding(
        analysis_id=analysis_id,
        vector=EmbeddingVector(root=tuple([0.1] * 768)),
        model_name="cl-nagoya/ruri-v3-310m",
    )


def _patch_ready_construction(ready: ReadyForEmbedding | None):
    """task 内 ``ReadyForEmbedding.try_advance_from`` を mock する patch。"""
    return patch(
        "app.analysis.embedding.tasks.ReadyForEmbedding.try_advance_from",
        new=AsyncMock(return_value=ready),
    )


# ---------------------------------------------------------------------------
# generate_embedding
# ---------------------------------------------------------------------------


class TestGenerateEmbedding:
    @pytest.mark.asyncio
    async def test_embedded_outcome_succeeds(self) -> None:
        """EmbeddedOutcome を Service が返したら task は完了する。"""
        from app.analysis.embedding.tasks import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        outcome = EmbeddedOutcome(embedding=_make_embedding())
        trigger = _make_trigger(analysis_id=1)
        ready = _make_ready(analysis_id=1)

        with (
            _patch_ready_construction(ready),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=outcome)
            await generate_embedding(trigger=trigger, ctx=mock_ctx)

        mock_svc_cls.return_value.execute.assert_called_once()
        # 構築された Ready が Service に渡されていること
        call_args = mock_svc_cls.return_value.execute.call_args
        assert call_args[0][0] is ready

    @pytest.mark.asyncio
    async def test_invalid_input_outcome_succeeds(self) -> None:
        """InvalidInputOutcome を Service が返したら task は静かに完了する。"""
        from app.analysis.embedding.tasks import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        outcome = InvalidInputOutcome()
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=outcome)
            await generate_embedding(trigger=trigger, ctx=mock_ctx)

        mock_svc_cls.return_value.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_when_precondition_not_met(self) -> None:
        """try_advance_from が None を返したら svc.execute を呼ばずに return。"""
        from app.analysis.embedding.tasks import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        trigger = _make_trigger(analysis_id=42)

        with (
            _patch_ready_construction(None),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
            ) as mock_limiters,
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
        ):
            await generate_embedding(trigger=trigger, ctx=mock_ctx)

        # rate limit acquire は試みず、Service も呼ばない
        mock_limiters.assert_not_called()
        mock_svc_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_raises_for_retry(self) -> None:
        from app.analysis.embedding.tasks import generate_embedding

        mock_ctx = _make_ctx(
            embedder=_make_embedder_fake(), retry_count=0, max_retries=2
        )
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            with pytest.raises(RateLimitError):
                await generate_embedding(trigger=trigger, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_rate_limit_last_attempt_returns(self) -> None:
        """最終試行では例外を送出せず return する。"""
        from app.analysis.embedding.tasks import generate_embedding

        mock_ctx = _make_ctx(
            embedder=_make_embedder_fake(), retry_count=2, max_retries=2
        )
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            await generate_embedding(trigger=trigger, ctx=mock_ctx)
