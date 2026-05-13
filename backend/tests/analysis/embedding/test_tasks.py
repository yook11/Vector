"""``generate_embedding`` task のテスト (chain 経路 + skip 経路)。

案 3 (厚い Ready + 下流 Stage 自身が処理開始時に構築): generate_embedding は
``EmbeddingTrigger`` (analysis_id のみ) を受領し、task 自身が
``ReadyForEmbedding.try_advance_from`` で Ready を構築する。embedder は
``ctx.state.embedder`` 経由で Pure DI される。

Service.execute は副作用のみで戻り値 ``None`` 一本化 (2026-05-12 確定)。

- precondition 未充足 → svc.execute を呼ばずに return (rate limit も acquire しない)
- 成功 → task 完了 (Stage 5 は終端、chain firing なし)

Layer 1 marker dispatch ルーティングは ``test_embedding_task_dispatch.py`` 側で
網羅する。Handler 内部の audit 経路は ``test_failure_handler.py`` で integration
として検証する。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.embedding.domain.ready import (
    EmbeddingTrigger,
    ReadyForEmbedding,
)


def _make_embedder_fake() -> MagicMock:
    """ctx.state.embedder 用のスタブ。PROVIDER/MODEL/RPM/RPD を持つ。"""
    fake = MagicMock()
    fake.PROVIDER = "gemini"
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


def _make_ready(analysis_id: int = 1, article_id: int = 7) -> ReadyForEmbedding:
    return ReadyForEmbedding(
        analysis_id=analysis_id,
        text_for_embedding="分析タイトル\n分析要約",
        article_id=article_id,
    )


def _patch_ready_construction(ready: ReadyForEmbedding | None):
    """task 内 ``ReadyForEmbedding.try_advance_from`` を mock する patch。"""
    return patch(
        "app.analysis.embedding.tasks.ReadyForEmbedding.try_advance_from",
        new=AsyncMock(return_value=ready),
    )


# ---------------------------------------------------------------------------
# generate_embedding (Stage 5)
# ---------------------------------------------------------------------------


class TestGenerateEmbedding:
    @pytest.mark.asyncio
    async def test_task_completes_on_service_success(self) -> None:
        """Service.execute が None を返したら task は完了する。"""
        from app.analysis.embedding.tasks import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
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
            mock_svc_cls.return_value.execute = AsyncMock(return_value=None)
            await generate_embedding(trigger=trigger, ctx=mock_ctx)

        mock_svc_cls.return_value.execute.assert_called_once()
        # 構築された Ready が Service に渡されていること
        call_args = mock_svc_cls.return_value.execute.call_args
        assert call_args[0][0] is ready

    @pytest.mark.asyncio
    async def test_skips_when_precondition_not_met(self) -> None:
        """try_advance_from が None を返したら svc.execute を呼ばずに return。

        rate limit acquire も試みない (Ready 構築が gatekeeper、案 3 順序)。
        """
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
