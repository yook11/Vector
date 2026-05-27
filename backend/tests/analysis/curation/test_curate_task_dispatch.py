"""``curate_content`` task の失敗 dispatch routing テスト。

Service の execute を mock して、tasks.py が **どんな exc** を受けたときに
``CurationFailureHandler.handle`` に正しく委譲し、戻り値の ``reraise`` を
正しく taskiq の raise/return semantics に変換するかを検証する。

実 DB / 実 Service / 実 Handler は呼ばない:
- ``CurationService.execute`` を patch (side_effect で exc を投げる)
- ``CurationFailureHandler`` を patch (handle の引数を assert、戻り値を制御)

marker ごとの後処理 (audit / DELETE / inline retry decision) の内部実装は
``CurationFailureHandler`` 側の責務で、本ファイルでは検証しない
(Handler の単体テストは ``test_failure_handler.py`` 参照)。

PR4 で rate limit 配線は ``ProviderRateLimitGate.acquire`` に置き換わったため、
ctx.state に gate mock (acquire=AsyncMock(return_value=True)) を bind する。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    AIProviderUsageLimitExhaustedError,
)
from app.analysis.curation.domain.ready import ReadyForCuration
from app.analysis.curation.errors import CurationResponseInvalidError
from app.analysis.rate_limit import AIModelRateLimitPolicy, RateLimitRule
from app.queue.messages.curation import CurationTrigger


def _make_provider_fake() -> MagicMock:
    fake = MagicMock()
    fake.model_name = "test-model"
    fake.prompt_version = "test-prompt-v1"
    fake.rate_limit_policy = AIModelRateLimitPolicy(
        provider="gemini",
        model="test-model",
        rules=(
            RateLimitRule(
                name="rpd", max_requests=1500, window_seconds=86400, block=False
            ),
            RateLimitRule(name="rpm", max_requests=50, window_seconds=60, block=True),
        ),
    )
    return fake


def _make_ctx(retry_count: int = 0, max_retries: int = 1) -> MagicMock:
    ctx = MagicMock()
    gate = MagicMock()
    gate.acquire = AsyncMock(return_value=True)
    ctx.state = SimpleNamespace(
        session_factory=MagicMock(),
        provider_rate_limit_gate=gate,
    )
    ctx.state.curator = _make_provider_fake()
    ctx.message.labels = {
        "retry_count": retry_count,
        "max_retries": max_retries,
    }
    return ctx


def _trigger() -> CurationTrigger:
    return CurationTrigger(article_id=42)


def _fixed_ready() -> ReadyForCuration:
    """task 冒頭の Ready 自構築が返す固定 Ready (Repository を mock するため)。"""
    return ReadyForCuration(
        article_id=42, original_title="t", original_content="content body"
    )


def _patch_try_advance_from(ready: ReadyForCuration | None = None) -> object:
    """``ReadyForCuration.try_advance_from`` を固定値返却に patch するヘルパ。"""
    return patch.object(
        ReadyForCuration,
        "try_advance_from",
        new=AsyncMock(return_value=ready if ready is not None else _fixed_ready()),
    )


# ---------------------------------------------------------------------------
# Drop 系 — Handler が False を返し、task は return (raise しない)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls",
    [
        AIProviderInputRejectedError,
        AIProviderOutputBlockedError,
    ],
)
@pytest.mark.asyncio
async def test_drop_article_delegates_to_handler(exc_cls: type[Exception]) -> None:
    """Drop 系例外は handler.handle に委譲され、reraise=False で return する。"""
    from app.queue.tasks.curation import curate_content

    ctx = _make_ctx()
    exc = exc_cls("boom")

    with (
        _patch_try_advance_from(),
        patch("app.queue.tasks.curation.CurationService") as mock_svc_cls,
        patch("app.queue.tasks.curation.CurationFailureHandler") as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
        mock_handler_cls.return_value.handle = AsyncMock(return_value=False)
        await curate_content(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    kwargs = handler_handle.await_args.kwargs
    assert kwargs["exc"] is exc
    assert kwargs["curator"] is ctx.state.curator
    assert kwargs["last_attempt"] is False


# ---------------------------------------------------------------------------
# Keep 系 — Handler が False を返し、task は return
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls",
    [
        AIProviderConfigurationError,
        AIProviderRequestInvalidError,
        AIProviderInsufficientBalanceError,
    ],
)
@pytest.mark.asyncio
async def test_keep_article_delegates_to_handler(exc_cls: type[Exception]) -> None:
    """Keep 系例外は handler に委譲され、reraise=False で return する。"""
    from app.queue.tasks.curation import curate_content

    ctx = _make_ctx()
    with (
        _patch_try_advance_from(),
        patch("app.queue.tasks.curation.CurationService") as mock_svc_cls,
        patch("app.queue.tasks.curation.CurationFailureHandler") as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc_cls("boom"))
        mock_handler_cls.return_value.handle = AsyncMock(return_value=False)
        await curate_content(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    assert handler_handle.await_args.kwargs["exc"].__class__ is exc_cls


# ---------------------------------------------------------------------------
# Recoverable 経路 — Handler の reraise 戻り値で raise/return が決まる
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_factory",
    [
        # Phase 4: Layer 2-B (CurationResponseInvalidError) は no-arg constructor
        # 必須、AIProvider*Error は accept-and-discard。factory で正常に
        # 構築できる呼び方を class ごとに分離する。
        AIProviderNetworkError,
        AIProviderServiceUnavailableError,
        CurationResponseInvalidError,
    ],
)
@pytest.mark.asyncio
async def test_retryable_reraise_true_raises(
    exc_factory: type[Exception],
) -> None:
    """Handler が ``reraise=True`` を返したら task は元の exc を raise する。"""
    from app.queue.tasks.curation import curate_content

    ctx = _make_ctx(retry_count=0, max_retries=1)  # retry 余地あり

    with (
        _patch_try_advance_from(),
        patch("app.queue.tasks.curation.CurationService") as mock_svc_cls,
        patch("app.queue.tasks.curation.CurationFailureHandler") as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc_factory())
        mock_handler_cls.return_value.handle = AsyncMock(return_value=True)
        with pytest.raises(exc_factory):
            await curate_content(trigger=_trigger(), ctx=ctx)

    mock_handler_cls.return_value.handle.assert_awaited_once()


@pytest.mark.asyncio
async def test_retryable_reraise_false_returns() -> None:
    """Handler が ``reraise=False`` を返したら task は return する (raise しない)。"""
    from app.queue.tasks.curation import curate_content

    ctx = _make_ctx(retry_count=1, max_retries=1)  # 最終試行

    with (
        _patch_try_advance_from(),
        patch("app.queue.tasks.curation.CurationService") as mock_svc_cls,
        patch("app.queue.tasks.curation.CurationFailureHandler") as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(
            side_effect=AIProviderNetworkError("connection reset")
        )
        mock_handler_cls.return_value.handle = AsyncMock(return_value=False)
        await curate_content(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    # last_attempt が Handler に正しく渡る
    assert handler_handle.await_args.kwargs["last_attempt"] is True


@pytest.mark.parametrize(
    "exc_cls",
    [
        AIProviderRateLimitedError,
        AIProviderUsageLimitExhaustedError,
    ],
)
@pytest.mark.asyncio
async def test_rate_limit_class_delegates_to_handler(
    exc_cls: type[Exception],
) -> None:
    """rate limit / usage limit 系 (Recoverable に詰め替えられる) も Handler に委譲。

    retry decision (taskiq retry に乗せるか) は Handler 内部の責務なので、本
    task テストでは Handler が呼ばれたことのみ確認する。
    """
    from app.queue.tasks.curation import curate_content

    ctx = _make_ctx(retry_count=0, max_retries=1)

    with (
        _patch_try_advance_from(),
        patch("app.queue.tasks.curation.CurationService") as mock_svc_cls,
        patch("app.queue.tasks.curation.CurationFailureHandler") as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc_cls("boom"))
        mock_handler_cls.return_value.handle = AsyncMock(return_value=False)
        await curate_content(trigger=_trigger(), ctx=ctx)

    mock_handler_cls.return_value.handle.assert_awaited_once()


# ---------------------------------------------------------------------------
# catch-all — Stage 3 marker いずれにも該当しない例外も Handler に委譲
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unexpected_exception_delegates_to_handler() -> None:
    """Stage 3 marker 外の exc も except Exception で Handler に届く。"""
    from app.queue.tasks.curation import curate_content

    ctx = _make_ctx()
    with (
        _patch_try_advance_from(),
        patch("app.queue.tasks.curation.CurationService") as mock_svc_cls,
        patch("app.queue.tasks.curation.CurationFailureHandler") as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(
            side_effect=ValueError("surprise"),
        )
        mock_handler_cls.return_value.handle = AsyncMock(return_value=False)
        await curate_content(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    assert isinstance(handler_handle.await_args.kwargs["exc"], ValueError)
