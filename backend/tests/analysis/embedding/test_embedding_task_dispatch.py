"""``generate_embedding`` task の失敗 dispatch routing テスト。

Service の execute を mock して、tasks.py が **どんな exc** を受けたときに
``EmbeddingFailureHandler.handle`` に正しく委譲し、戻り値の ``reraise`` を
正しく taskiq の raise/return semantics に変換するかを検証する。

実 DB / 実 Service / 実 Handler は呼ばない:
- ``EmbeddingService.execute`` を patch (side_effect で exc を投げる)
- ``EmbeddingFailureHandler`` を patch (handle の引数を assert、戻り値を制御)

marker ごとの後処理 (audit / inline retry decision / 分類ログ) の内部実装は
``EmbeddingFailureHandler`` 側の責務で、本ファイルでは検証しない
(Handler の単体テストは ``test_failure_handler.py`` 参照)。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire

from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.errors import (
    EmbeddingRecoverableError,
    EmbeddingResponseInvalidError,
    EmbeddingTerminalError,
)
from app.analysis.failure_handling import FailureHandlingDecision
from app.analysis.rate_limit import AIModelRateLimitPolicy
from app.queue.messages.embedding import EmbeddingTrigger
from tests.logfire._span_helpers import stage_attrs


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


def _make_gate_fake() -> MagicMock:
    gate = MagicMock()
    gate.acquire = AsyncMock(return_value=True)
    return gate


def _make_ctx(retries: int = 0, max_retries: int = 2) -> MagicMock:
    ctx = MagicMock()
    ctx.state = SimpleNamespace(
        session_factory=MagicMock(),
        provider_rate_limit_gate=_make_gate_fake(),
    )
    ctx.state.embedder = _make_embedder_fake()
    # taskiq SimpleRetryMiddleware が書く label は "_retries" (0..max_retries-1)
    ctx.message.labels = {
        "_retries": retries,
        "max_retries": max_retries,
    }
    return ctx


def _trigger() -> EmbeddingTrigger:
    return EmbeddingTrigger(analysis_id=1)


def _fixed_ready() -> ReadyForEmbedding:
    return ReadyForEmbedding(
        analysis_id=1,
        text_for_embedding="分析タイトル\n分析要約",
        article_id=7,
    )


def _patch_ready_construction(ready: ReadyForEmbedding | None = None) -> object:
    """``ReadyForEmbedding.try_advance_from`` を固定値返却に patch するヘルパ。"""
    return patch(
        "app.queue.tasks.embedding.ReadyForEmbedding.try_advance_from",
        new=AsyncMock(return_value=ready if ready is not None else _fixed_ready()),
    )


# Terminal 系 — Handler が False を返し、task は return (raise しない)


@pytest.mark.asyncio
async def test_terminal_delegates_to_handler() -> None:
    """``EmbeddingTerminalError`` は handler.handle に委譲され、
    reraise=False で return する。"""
    from app.queue.tasks.embedding import generate_embedding

    ctx = _make_ctx()
    exc = EmbeddingTerminalError(
        code="ai_error_configuration", failure_kind="operator_action_required"
    )

    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
        patch("app.queue.tasks.embedding.EmbeddingFailureHandler") as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
        mock_handler_cls.return_value.handle = AsyncMock(
            return_value=FailureHandlingDecision(
                reraise=False,
                stage_hold_reason="ai_error_configuration",
            )
        )
        with patch(
            "app.queue.tasks.embedding.set_embedding_hold", new=AsyncMock()
        ) as hold:
            await generate_embedding(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    kwargs = handler_handle.await_args.kwargs
    assert kwargs["exc"] is exc
    assert kwargs["last_attempt"] is False
    hold.assert_awaited_once()
    assert hold.await_args.kwargs["reason"] == "ai_error_configuration"


# Recoverable 系 — Handler の reraise 戻り値で raise/return が決まる


@pytest.mark.asyncio
async def test_recoverable_reraise_true_raises() -> None:
    """Handler が ``reraise=True`` を返したら task は元の exc を raise する。"""
    from app.queue.tasks.embedding import generate_embedding

    ctx = _make_ctx(
        retries=0, max_retries=2
    )  # retry 余地あり: _retries=0 < max_retries-1
    exc = EmbeddingRecoverableError(
        code="ai_error_network", failure_kind="attempt_scoped"
    )

    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
        patch("app.queue.tasks.embedding.EmbeddingFailureHandler") as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
        mock_handler_cls.return_value.handle = AsyncMock(
            return_value=FailureHandlingDecision(reraise=True)
        )
        with pytest.raises(EmbeddingRecoverableError):
            await generate_embedding(trigger=_trigger(), ctx=ctx)

    mock_handler_cls.return_value.handle.assert_awaited_once()


@pytest.mark.asyncio
async def test_recoverable_reraise_false_returns() -> None:
    """Handler が ``reraise=False`` を返したら task は return する (raise しない)。

    retry 上限到達 のとき Handler は False を返す想定 (Stage 5 仕様)。本 test は
    その経路で task が最後まで完走し、``last_attempt=True`` が Handler に渡る
    ことを確認する。
    """
    from app.queue.tasks.embedding import generate_embedding

    # 最終試行: _retries=max_retries-1=1 (旧 retry_count=2 は production が書かない値)
    ctx = _make_ctx(retries=1, max_retries=2)
    exc = EmbeddingRecoverableError(
        code="ai_error_network", failure_kind="attempt_scoped"
    )

    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
        patch("app.queue.tasks.embedding.EmbeddingFailureHandler") as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
        mock_handler_cls.return_value.handle = AsyncMock(
            return_value=FailureHandlingDecision(reraise=False)
        )
        await generate_embedding(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    assert handler_handle.await_args.kwargs["last_attempt"] is True


@pytest.mark.asyncio
async def test_response_invalid_dispatches_to_handler() -> None:
    """``EmbeddingResponseInvalidError`` (Layer 2-B、Recoverable 継承) も
    Handler 経由で扱われる (kwargs["exc"] は EmbeddingRecoverableError instance)。"""
    from app.queue.tasks.embedding import generate_embedding

    ctx = _make_ctx()
    exc = EmbeddingResponseInvalidError()

    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
        patch("app.queue.tasks.embedding.EmbeddingFailureHandler") as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
        mock_handler_cls.return_value.handle = AsyncMock(
            return_value=FailureHandlingDecision(reraise=False)
        )
        await generate_embedding(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    assert isinstance(
        handler_handle.await_args.kwargs["exc"], EmbeddingRecoverableError
    )


# catch-all — Layer 1 marker いずれにも該当しない例外も Handler に委譲


@pytest.mark.asyncio
async def test_unexpected_exception_delegates_to_handler() -> None:
    """marker いずれにも該当しない exc も except Exception で Handler に渡る。"""
    from app.queue.tasks.embedding import generate_embedding

    ctx = _make_ctx()
    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
        patch("app.queue.tasks.embedding.EmbeddingFailureHandler") as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(
            side_effect=ValueError("surprise"),
        )
        mock_handler_cls.return_value.handle = AsyncMock(
            return_value=FailureHandlingDecision(reraise=False)
        )
        await generate_embedding(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    assert isinstance(handler_handle.await_args.kwargs["exc"], ValueError)


@pytest.mark.parametrize("reraise", [True, False])
@pytest.mark.asyncio
async def test_service_exception_sets_failed_result(
    capfire: CaptureLogfire, reraise: bool
) -> None:
    """service 例外は handler の reraise 値に関わらず span result=failed を焼く。"""
    from app.queue.tasks.embedding import generate_embedding

    ctx = _make_ctx()
    exc = EmbeddingRecoverableError(
        code="ai_error_network", failure_kind="attempt_scoped"
    )
    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
        patch("app.queue.tasks.embedding.EmbeddingFailureHandler") as mock_handler_cls,
        patch("app.queue.tasks.embedding.set_embedding_hold", new=AsyncMock()),
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
        mock_handler_cls.return_value.handle = AsyncMock(
            return_value=FailureHandlingDecision(reraise=reraise)
        )
        if reraise:
            with pytest.raises(EmbeddingRecoverableError):
                await generate_embedding(trigger=_trigger(), ctx=ctx)
        else:
            await generate_embedding(trigger=_trigger(), ctx=ctx)

    assert stage_attrs(capfire)["result"] == "failed"
