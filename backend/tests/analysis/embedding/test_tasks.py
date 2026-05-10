"""Embedding タスク (generate_embedding) のテスト。

Phase 2 リファクタ後 (typed-pipeline-preconditions.md): generate_embedding は
``ReadyForEmbedding`` を受け取り、embedder は ``ctx.state.embedder`` 経由で
Pure DI される。AlreadyEmbedded / Skipped Outcome は廃止 (Ready の
`try_advance_from` で代替)、残るのは ``EmbeddedOutcome | InvalidInputOutcome``。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.embedding.domain.embedding import Embedding
from app.analysis.embedding.domain.ready import ReadyForEmbedding
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


def _make_ready(analysis_id: int = 1) -> ReadyForEmbedding:
    return ReadyForEmbedding(
        analysis_id=analysis_id,
        text_for_embedding="title\nsummary",
    )


def _make_embedding(analysis_id: int = 1) -> Embedding:
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
        from app.analysis.embedding.tasks import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        outcome = EmbeddedOutcome(embedding=_make_embedding())
        ready = _make_ready(analysis_id=1)

        with (
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=outcome)
            await generate_embedding(ready=ready, ctx=mock_ctx)

        mock_svc_cls.return_value.execute.assert_called_once()
        # Ready が Service に渡されていること
        call_args = mock_svc_cls.return_value.execute.call_args
        assert call_args[0][0] is ready

    @pytest.mark.asyncio
    async def test_invalid_input_outcome_succeeds(self) -> None:
        """InvalidInputOutcome を Service が返したら task は静かに完了する。"""
        from app.analysis.embedding.tasks import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        outcome = InvalidInputOutcome()
        ready = _make_ready()

        with (
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=outcome)
            await generate_embedding(ready=ready, ctx=mock_ctx)

        mock_svc_cls.return_value.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limit_raises_for_retry(self) -> None:
        from app.analysis.embedding.tasks import generate_embedding

        mock_ctx = _make_ctx(
            embedder=_make_embedder_fake(), retry_count=0, max_retries=2
        )
        ready = _make_ready()

        with (
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
                await generate_embedding(ready=ready, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_rate_limit_last_attempt_returns(self) -> None:
        """最終試行では例外を送出せず return する。"""
        from app.analysis.embedding.tasks import generate_embedding

        mock_ctx = _make_ctx(
            embedder=_make_embedder_fake(), retry_count=2, max_retries=2
        )
        ready = _make_ready()

        with (
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            await generate_embedding(ready=ready, ctx=mock_ctx)
