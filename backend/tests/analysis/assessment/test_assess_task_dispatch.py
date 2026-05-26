"""``assess_content`` task の失敗 dispatch routing テスト。

Service の execute を mock して、tasks.py が **どんな exc** を受けたときに
``AssessmentFailureHandler.handle`` に正しく委譲し、戻り値の ``reraise`` を
正しく taskiq の raise/return semantics に変換するかを検証する。

実 DB / 実 Service / 実 Handler は呼ばない:
- ``AssessmentService.execute`` を patch (side_effect で exc を投げる)
- ``AssessmentFailureHandler`` を patch (handle の引数を assert、戻り値を制御)
- ``ProviderRateLimitGate.acquire`` は AsyncMock で常に True を返す stub
  (rate limit gate 配線は ``test_tasks.py`` で網羅)

marker ごとの後処理 (audit / inline retry decision / 分類ログ) の内部実装は
``AssessmentFailureHandler`` 側の責務で、本ファイルでは検証しない
(Handler の単体テストは ``test_failure_handler.py`` 参照)。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.errors import (
    AssessmentCategoryMissingError,
    AssessmentRecoverableError,
    AssessmentResponseInvalidError,
    AssessmentTerminalSkipError,
)
from app.analysis.rate_limit import RatePolicy
from app.queue.messages.assessment import AssessmentTrigger


def _make_provider_fake() -> MagicMock:
    """assessor 用のスタブ。property 契約 (model_name / prompt_version /
    rate_policy) を持つ。"""
    fake = MagicMock()
    fake.model_name = "test-model"
    fake.prompt_version = "abc12345"
    fake.rate_policy = RatePolicy(
        provider="gemini", model="test-model", rpm=50, rpd=1500
    )
    return fake


def _make_ctx(retry_count: int = 0, max_retries: int = 2) -> MagicMock:
    ctx = MagicMock()
    gate = MagicMock()
    gate.acquire = AsyncMock(return_value=True)
    ctx.state = SimpleNamespace(
        session_factory=MagicMock(),
        provider_rate_limit_gate=gate,
    )
    ctx.state.assessor = _make_provider_fake()
    ctx.message.labels = {
        "retry_count": retry_count,
        "max_retries": max_retries,
    }
    return ctx


def _trigger() -> AssessmentTrigger:
    return AssessmentTrigger(curation_id=2)


def _fixed_ready() -> ReadyForAssessment:
    return ReadyForAssessment(
        curation_id=2,
        translated_title="title",
        summary="summary",
        article_id=7,
        source_name="Test Source",
    )


def _patch_ready_construction(ready: ReadyForAssessment | None = None) -> object:
    """``ReadyForAssessment.try_advance_from`` を固定値返却に patch するヘルパ。"""
    return patch(
        "app.queue.tasks.assessment.ReadyForAssessment.try_advance_from",
        new=AsyncMock(return_value=ready if ready is not None else _fixed_ready()),
    )


# ---------------------------------------------------------------------------
# TerminalSkip 系 — Handler が False を返し、task は return (raise しない)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_skip_delegates_to_handler() -> None:
    """``AssessmentTerminalSkipError`` は handler.handle に委譲され、
    reraise=False で return する。"""
    from app.queue.tasks.assessment import assess_content

    ctx = _make_ctx()
    exc = AssessmentTerminalSkipError("bad config", code="ai_error_configuration")

    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
        patch(
            "app.queue.tasks.assessment.AssessmentFailureHandler"
        ) as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
        mock_handler_cls.return_value.handle = AsyncMock(return_value=False)
        await assess_content(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    kwargs = handler_handle.await_args.kwargs
    assert kwargs["exc"] is exc
    assert kwargs["attempt"] == 1
    assert kwargs["last_attempt"] is False


@pytest.mark.asyncio
async def test_category_missing_dispatches_to_handler() -> None:
    """``AssessmentCategoryMissingError`` (Layer 2-B、TerminalSkip 継承) も
    Handler 経由で扱われる (kwargs["exc"] は AssessmentTerminalSkipError instance)。"""
    from app.queue.tasks.assessment import assess_content

    ctx = _make_ctx()
    exc = AssessmentCategoryMissingError("unknown slug 'foo'")

    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
        patch(
            "app.queue.tasks.assessment.AssessmentFailureHandler"
        ) as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
        mock_handler_cls.return_value.handle = AsyncMock(return_value=False)
        await assess_content(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    assert isinstance(
        handler_handle.await_args.kwargs["exc"], AssessmentTerminalSkipError
    )


# ---------------------------------------------------------------------------
# Recoverable 系 — Handler の reraise 戻り値で raise/return が決まる
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recoverable_reraise_true_raises() -> None:
    """Handler が ``reraise=True`` を返したら task は元の exc を raise する。"""
    from app.queue.tasks.assessment import assess_content

    ctx = _make_ctx(retry_count=0, max_retries=2)  # retry 余地あり
    exc = AssessmentRecoverableError("network", code="ai_error_network")

    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
        patch(
            "app.queue.tasks.assessment.AssessmentFailureHandler"
        ) as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
        mock_handler_cls.return_value.handle = AsyncMock(return_value=True)
        with pytest.raises(AssessmentRecoverableError):
            await assess_content(trigger=_trigger(), ctx=ctx)

    mock_handler_cls.return_value.handle.assert_awaited_once()


@pytest.mark.asyncio
async def test_recoverable_reraise_false_returns() -> None:
    """Handler が ``reraise=False`` を返したら task は return する (raise しない)。

    最終 attempt のとき Handler は False を返す想定 (Stage 4 仕様)。本 test は
    その経路で task が最後まで完走し、``last_attempt=True`` が Handler に渡る
    ことを確認する。
    """
    from app.queue.tasks.assessment import assess_content

    ctx = _make_ctx(retry_count=2, max_retries=2)  # 最終試行
    exc = AssessmentRecoverableError("network", code="ai_error_network")

    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
        patch(
            "app.queue.tasks.assessment.AssessmentFailureHandler"
        ) as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
        mock_handler_cls.return_value.handle = AsyncMock(return_value=False)
        await assess_content(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    assert handler_handle.await_args.kwargs["last_attempt"] is True


@pytest.mark.asyncio
async def test_response_invalid_dispatches_to_handler() -> None:
    """``AssessmentResponseInvalidError`` (Layer 2-B、Recoverable 継承) も
    Handler 経由で扱われる (kwargs["exc"] は AssessmentRecoverableError instance)。"""
    from app.queue.tasks.assessment import assess_content

    ctx = _make_ctx()
    exc = AssessmentResponseInvalidError("schema violation")

    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
        patch(
            "app.queue.tasks.assessment.AssessmentFailureHandler"
        ) as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
        mock_handler_cls.return_value.handle = AsyncMock(return_value=False)
        await assess_content(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    assert isinstance(
        handler_handle.await_args.kwargs["exc"], AssessmentRecoverableError
    )


# ---------------------------------------------------------------------------
# catch-all — Layer 1 marker いずれにも該当しない例外も Handler に委譲
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unexpected_exception_delegates_to_handler() -> None:
    """marker いずれにも該当しない exc も except Exception で Handler に渡る。"""
    from app.queue.tasks.assessment import assess_content

    ctx = _make_ctx()
    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
        patch(
            "app.queue.tasks.assessment.AssessmentFailureHandler"
        ) as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(
            side_effect=ValueError("surprise"),
        )
        mock_handler_cls.return_value.handle = AsyncMock(return_value=False)
        await assess_content(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    assert isinstance(handler_handle.await_args.kwargs["exc"], ValueError)
