"""``generate_embedding`` task の分岐テスト。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from app.analysis.embedding.domain.ready import (
    EmbeddingReadyBuildBlockedCode,
    EmbeddingReadyBuildBlockedError,
    ReadyForEmbedding,
)
from app.analysis.rate_limit import AIModelRateLimitPolicy
from app.queue.messages.embedding import EmbeddingTrigger


def _make_embedder_fake() -> MagicMock:
    fake = MagicMock()
    fake.model_name = "gemini-embedding-001"
    fake.dimension = 768
    fake.rate_limit_policy = AIModelRateLimitPolicy(
        provider="gemini",
        model="gemini-embedding-001",
        rules=(),
    )
    fake.document_prefix = ""
    return fake


def _make_gate_fake(*, acquired: bool = True) -> MagicMock:
    gate = MagicMock()
    gate.acquire = AsyncMock(return_value=acquired)
    return gate


def _make_ctx(
    *,
    embedder: MagicMock | None = None,
    gate: MagicMock | None = None,
    retry_count: int = 0,
    max_retries: int = 0,
) -> MagicMock:
    ctx = MagicMock()
    ctx.state = SimpleNamespace(
        session_factory=MagicMock(),
        provider_rate_limit_gate=gate if gate is not None else _make_gate_fake(),
    )
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


def _patch_ready_construction(
    result: ReadyForEmbedding | EmbeddingReadyBuildBlockedError,
):
    mock = (
        AsyncMock(side_effect=result)
        if isinstance(result, EmbeddingReadyBuildBlockedError)
        else AsyncMock(return_value=result)
    )
    return patch(
        "app.queue.tasks.embedding.ReadyForEmbedding.try_advance_from",
        new=mock,
    )


# ---------------------------------------------------------------------------
# generate_embedding (Stage 5)
# ---------------------------------------------------------------------------


class TestGenerateEmbedding:
    @pytest.mark.asyncio
    async def test_task_completes_on_service_success(self) -> None:
        """Service.execute が None を返したら task は完了する。"""
        from app.queue.tasks.embedding import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        trigger = _make_trigger(analysis_id=1)
        ready = _make_ready(analysis_id=1)

        with (
            _patch_ready_construction(ready),
            patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=None)
            await generate_embedding(trigger=trigger, ctx=mock_ctx)

        mock_svc_cls.return_value.execute.assert_called_once()
        # 構築された Ready が Service に渡されていること
        call_args = mock_svc_cls.return_value.execute.call_args
        assert call_args[0][0] is ready
        # gate.acquire は embedder.rate_limit_policy で呼ばれる
        mock_ctx.state.provider_rate_limit_gate.acquire.assert_awaited_once_with(
            mock_ctx.state.embedder.rate_limit_policy
        )

    @pytest.mark.asyncio
    async def test_ready_build_blocked_audits_and_does_not_call_service(self) -> None:
        """Ready build blocked なら rejected audit + return、Service は呼ばない。

        rate limit acquire も試みない (Ready 構築が gatekeeper、案 3 順序)。
        """
        from app.queue.tasks.embedding import generate_embedding

        gate = _make_gate_fake()
        mock_ctx = _make_ctx(embedder=_make_embedder_fake(), gate=gate)
        trigger = _make_trigger(analysis_id=42)
        exc = EmbeddingReadyBuildBlockedError(
            EmbeddingReadyBuildBlockedCode.ANALYSIS_MISSING
        )

        with (
            _patch_ready_construction(exc),
            patch("app.queue.tasks.embedding.EmbeddingAuditRepository") as mock_audit,
            patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
        ):
            mock_audit.return_value.append_ready_build_blocked = AsyncMock()
            await generate_embedding(trigger=trigger, ctx=mock_ctx)

        mock_audit.return_value.append_ready_build_blocked.assert_awaited_once_with(
            analysis_id=42,
            exc=exc,
        )
        # rate limit acquire は試みず、Service も呼ばない
        gate.acquire.assert_not_called()
        mock_svc_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_ready_build_exception_audits_and_reraises(self) -> None:
        """Ready 判定中の例外は failed audit 後に元例外を raise する。"""
        from app.queue.tasks.embedding import generate_embedding

        gate = _make_gate_fake()
        mock_ctx = _make_ctx(embedder=_make_embedder_fake(), gate=gate)
        trigger = _make_trigger(analysis_id=42)
        exc = RuntimeError("ready build exploded")

        with (
            patch(
                "app.queue.tasks.embedding.ReadyForEmbedding.try_advance_from",
                new=AsyncMock(side_effect=exc),
            ),
            patch(
                "app.queue.tasks.embedding._append_ready_build_failed_audit",
                new=AsyncMock(),
            ) as audit_failed,
            patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
        ):
            with pytest.raises(RuntimeError):
                await generate_embedding(trigger=trigger, ctx=mock_ctx)

        audit_failed.assert_awaited_once_with(
            mock_ctx.state.session_factory,
            analysis_id=42,
            exc=exc,
        )
        gate.acquire.assert_not_called()
        mock_svc_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_gate_denies_quota(self) -> None:
        """gate.acquire が False なら svc を呼ばず gate skip の log + metric を出す。"""
        from app.queue.tasks.embedding import generate_embedding

        gate = _make_gate_fake(acquired=False)
        mock_ctx = _make_ctx(embedder=_make_embedder_fake(), gate=gate)
        trigger = _make_trigger(analysis_id=1)
        ready = _make_ready(analysis_id=1)

        with (
            _patch_ready_construction(ready),
            patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
            patch(
                "app.queue.tasks.embedding.record_rate_limit_gate_skipped"
            ) as mock_record,
            capture_logs() as cap,
        ):
            await generate_embedding(trigger=trigger, ctx=mock_ctx)

        gate.acquire.assert_awaited_once()
        mock_svc_cls.assert_not_called()
        mock_record.assert_called_once_with(
            stage="embedding", model="gemini-embedding-001"
        )
        skips = [
            e for e in cap if e.get("event") == "embedding_ai_rate_limit_gate_skipped"
        ]
        assert skips, "gate skip log が emit されていない"
        assert skips[-1]["analysis_id"] == 1
        assert skips[-1]["article_id"] == 7
        assert skips[-1]["embedding_model"] == "gemini-embedding-001"
