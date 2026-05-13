"""``generate_embedding`` task のテスト (2 marker dispatch + catch-all)。

案 3 (厚い Ready + 下流 Stage 自身が処理開始時に構築): generate_embedding は
``EmbeddingTrigger`` (analysis_id のみ) を受領し、task 自身が
``ReadyForEmbedding.try_advance_from`` で Ready を構築する。embedder は
``ctx.state.embedder`` 経由で Pure DI される。

Service.execute は副作用のみで戻り値 ``None`` 一本化 (2026-05-12 確定)。

dispatch 軸:
- precondition 未充足 → svc.execute を呼ばずに return (rate limit も acquire しない)
- ``EmbeddingTerminalSkipError`` → audit + return (taskiq retry なし)
- ``EmbeddingRecoverableError`` → audit + is_last_attempt で raise / return
- catch-all ``Exception`` → audit + is_last_attempt で raise / return
- audit 失敗時の log fallback (PR4: 末尾 inline audit 経由)
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from app.analysis.embedding.domain.ready import (
    EmbeddingTrigger,
    ReadyForEmbedding,
)
from app.analysis.embedding.errors import (
    EmbeddingRecoverableError,
    EmbeddingResponseInvalidError,
    EmbeddingTerminalSkipError,
)


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


def _patch_audit_repository() -> object:
    """task 末尾 inline audit の ``EmbeddingAuditRepository`` を mock する patch。

    PR4: ``_record_failure`` helper 廃止に伴い、Repository class を patch して
    ``return_value.append_failure`` の呼び出しを assert する形に切替。
    """
    return patch(
        "app.analysis.embedding.tasks.EmbeddingAuditRepository",
        autospec=False,
    )


# ---------------------------------------------------------------------------
# generate_embedding
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

    @pytest.mark.asyncio
    async def test_audit_failure_falls_back_to_log(self) -> None:
        """audit Repository が raise しても task は落ちず log fallback する。

        PR4 で ``_record_failure`` helper を廃止し task 末尾の inline audit に
        統一したため、helper 単体テストの代わりに「audit DB が落ちても業務
        task は完走し ``embedding_failure_audit_dropped`` 構造ログが出る」
        振る舞いを task 経由で検証する。同時に business / audit exception の
        message に混入した secret prefix が log field から除去されることも
        確認する (red-team chain γ-2 対称化)。
        """
        from app.analysis.embedding.tasks import generate_embedding

        ctx = _make_ctx(embedder=_make_embedder_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()
        business_exc = EmbeddingTerminalSkipError(
            "config Authorization: Bearer sk-live-BUSINESSSECRETabc",
            code="ai_error_configuration",
        )

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
            _patch_audit_repository() as mock_audit_cls,
            capture_logs() as cap,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=business_exc)
            mock_audit_cls.return_value.append_failure = AsyncMock(
                side_effect=RuntimeError(
                    "audit db down Authorization: Bearer sk-live-AUDITSECRETxyz"
                )
            )
            # task は落ちずに完走する
            await generate_embedding(trigger=trigger, ctx=ctx)

        drops = [e for e in cap if e.get("event") == "embedding_failure_audit_dropped"]
        assert drops, "fallback ログが emit されていない"
        drop = drops[-1]
        assert drop["analysis_id"] == 1
        assert drop["attempt"] == 1
        assert drop["business_error_class"].endswith(".EmbeddingTerminalSkipError")
        assert drop["audit_error_class"].endswith(".RuntimeError")
        # red-team chain γ-2: business / audit 両方の secret が redact される
        assert "sk-live-BUSINESSSECRETabc" not in drop["business_error_message"]
        assert "sk-live-AUDITSECRETxyz" not in drop["audit_error_message"]


# ---------------------------------------------------------------------------
# generate_embedding: 2 marker dispatch + catch-all
# ---------------------------------------------------------------------------


class TestGenerateEmbeddingMarkerDispatch:
    """``generate_embedding`` の except 句は 2 marker dispatch
    (TerminalSkip → Recoverable → Exception)。各 except で failure_exc /
    reraise flag を設定、task 末尾の inline audit で 1 行記録する
    (Stage 4 と同型)。"""

    @pytest.mark.asyncio
    async def test_terminal_skip_records_failure_and_returns(self) -> None:
        """``EmbeddingTerminalSkipError`` → audit + return (taskiq retry なし)。"""
        from app.analysis.embedding.tasks import generate_embedding

        ctx = _make_ctx(embedder=_make_embedder_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()
        exc = EmbeddingTerminalSkipError("bad config", code="ai_error_configuration")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
            _patch_audit_repository() as mock_audit_cls,
        ):
            mock_audit_cls.return_value.append_failure = AsyncMock()
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            await generate_embedding(trigger=trigger, ctx=ctx)

        append_failure = mock_audit_cls.return_value.append_failure
        append_failure.assert_awaited_once()
        assert append_failure.await_args.kwargs["exc"] is exc

    @pytest.mark.asyncio
    async def test_recoverable_records_failure_and_raises_when_not_last(self) -> None:
        """``EmbeddingRecoverableError`` + retry 余地あり → audit + raise。"""
        from app.analysis.embedding.tasks import generate_embedding

        ctx = _make_ctx(embedder=_make_embedder_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()
        exc = EmbeddingRecoverableError("network", code="ai_error_network")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
            _patch_audit_repository() as mock_audit_cls,
        ):
            mock_audit_cls.return_value.append_failure = AsyncMock()
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            with pytest.raises(EmbeddingRecoverableError):
                await generate_embedding(trigger=trigger, ctx=ctx)
        mock_audit_cls.return_value.append_failure.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_recoverable_records_failure_and_returns_when_last(self) -> None:
        """``EmbeddingRecoverableError`` + 最終 attempt → audit + return。"""
        from app.analysis.embedding.tasks import generate_embedding

        ctx = _make_ctx(embedder=_make_embedder_fake(), retry_count=2, max_retries=2)
        trigger = _make_trigger()
        exc = EmbeddingRecoverableError("network", code="ai_error_network")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
            _patch_audit_repository() as mock_audit_cls,
        ):
            mock_audit_cls.return_value.append_failure = AsyncMock()
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            await generate_embedding(trigger=trigger, ctx=ctx)
        mock_audit_cls.return_value.append_failure.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_response_invalid_dispatches_to_recoverable(self) -> None:
        """``EmbeddingResponseInvalidError`` (Layer 2-B、Recoverable 継承) は
        Recoverable 句に dispatch される (catch-all に落ちない)。"""
        from app.analysis.embedding.tasks import generate_embedding

        ctx = _make_ctx(embedder=_make_embedder_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()
        exc = EmbeddingResponseInvalidError("dimension mismatch")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
            _patch_audit_repository() as mock_audit_cls,
        ):
            mock_audit_cls.return_value.append_failure = AsyncMock()
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            with pytest.raises(EmbeddingResponseInvalidError):
                await generate_embedding(trigger=trigger, ctx=ctx)
        append_failure = mock_audit_cls.return_value.append_failure
        append_failure.assert_awaited_once()
        assert isinstance(
            append_failure.await_args.kwargs["exc"], EmbeddingRecoverableError
        )

    @pytest.mark.asyncio
    async def test_unexpected_exception_records_and_returns_when_last(self) -> None:
        """任意 ``Exception`` + 最終 attempt → catch-all で audit + return。"""
        from app.analysis.embedding.tasks import generate_embedding

        ctx = _make_ctx(embedder=_make_embedder_fake(), retry_count=2, max_retries=2)
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
            _patch_audit_repository() as mock_audit_cls,
        ):
            mock_audit_cls.return_value.append_failure = AsyncMock()
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=ValueError("surprise")
            )
            await generate_embedding(trigger=trigger, ctx=ctx)
        append_failure = mock_audit_cls.return_value.append_failure
        append_failure.assert_awaited_once()
        assert isinstance(append_failure.await_args.kwargs["exc"], ValueError)

    @pytest.mark.asyncio
    async def test_unexpected_exception_records_and_raises_when_not_last(self) -> None:
        """任意 ``Exception`` + retry 余地あり → catch-all で audit + raise。"""
        from app.analysis.embedding.tasks import generate_embedding

        ctx = _make_ctx(embedder=_make_embedder_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
            _patch_audit_repository() as mock_audit_cls,
        ):
            mock_audit_cls.return_value.append_failure = AsyncMock()
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=ValueError("surprise")
            )
            with pytest.raises(ValueError, match="surprise"):
                await generate_embedding(trigger=trigger, ctx=ctx)
        mock_audit_cls.return_value.append_failure.assert_awaited_once()
