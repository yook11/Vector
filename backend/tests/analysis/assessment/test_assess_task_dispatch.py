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
from logfire.testing import CaptureLogfire
from sqlalchemy.exc import OperationalError

from app.analysis.assessment.ai.parse import AssessmentResponseDefect
from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildBlockedCode,
    AssessmentReadyBuildBlockedError,
    ReadyForAssessment,
)
from app.analysis.assessment.errors import (
    AssessmentRecoverableError,
    AssessmentResponseInvalidError,
    AssessmentTerminalError,
)
from app.analysis.assessment.repository import CategoryEnumDatabaseMismatchError
from app.analysis.failure_handling import FailureHandlingDecision
from app.analysis.rate_limit import AIModelRateLimitPolicy, RateLimitRule
from app.queue.messages.assessment import AssessmentTrigger
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result

_PROCESSING_OUTCOME_METRIC = "vector.assessment.processing_outcome"


def _make_provider_fake() -> MagicMock:
    """assessor 用のスタブ。property 契約 (model_name / prompt_version /
    rate_limit_policy) を持つ。"""
    fake = MagicMock()
    fake.model_name = "test-model"
    fake.prompt_version = "abc12345"
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


def _make_ctx(retries: int = 0, max_retries: int = 2) -> MagicMock:
    ctx = MagicMock()
    gate = MagicMock()
    gate.acquire = AsyncMock(return_value=True)
    ctx.state = SimpleNamespace(
        session_factory=MagicMock(),
        provider_rate_limit_gate=gate,
    )
    ctx.state.assessor = _make_provider_fake()
    # taskiq SimpleRetryMiddleware が書く label は "_retries" (0..max_retries-1)
    ctx.message.labels = {
        "_retries": retries,
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
        analyzable_article_id=7,
    )


def _patch_ready_construction(ready: ReadyForAssessment | None = None) -> object:
    """``ReadyForAssessment.try_advance_from`` を固定値返却に patch するヘルパ。"""
    return patch(
        "app.queue.tasks.assessment.ReadyForAssessment.try_advance_from",
        new=AsyncMock(return_value=ready if ready is not None else _fixed_ready()),
    )


# Terminal 系 — Handler が False を返し、task は return (raise しない)


@pytest.mark.asyncio
async def test_terminal_delegates_to_handler() -> None:
    """``AssessmentTerminalError`` は handler.handle に委譲され、
    reraise=False で return する。"""
    from app.queue.tasks.assessment import assess_content

    ctx = _make_ctx()
    exc = AssessmentTerminalError(
        code="ai_error_configuration", failure_kind="operator_action_required"
    )

    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
        patch(
            "app.queue.tasks.assessment.AssessmentFailureHandler"
        ) as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
        mock_handler_cls.return_value.handle = AsyncMock(
            return_value=FailureHandlingDecision(
                reraise=False,
                stage_hold_reason="ai_error_configuration",
            )
        )
        with patch(
            "app.queue.tasks.assessment.set_assessment_hold", new=AsyncMock()
        ) as hold:
            await assess_content(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    kwargs = handler_handle.await_args.kwargs
    assert kwargs["exc"] is exc
    assert kwargs["last_attempt"] is False
    hold.assert_awaited_once()
    assert hold.await_args.kwargs["reason"] == "ai_error_configuration"


@pytest.mark.asyncio
async def test_category_enum_db_mismatch_dispatches_to_handler() -> None:
    """marker でない ``CategoryEnumDatabaseMismatchError`` も task の catch-all
    (``except Exception``) で拾われ Handler に渡る (想定外 = case _: 経路)。"""
    from app.queue.tasks.assessment import assess_content

    ctx = _make_ctx()
    exc = CategoryEnumDatabaseMismatchError({"ai"})

    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
        patch(
            "app.queue.tasks.assessment.AssessmentFailureHandler"
        ) as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
        mock_handler_cls.return_value.handle = AsyncMock(
            return_value=FailureHandlingDecision(reraise=False)
        )
        await assess_content(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    assert handler_handle.await_args.kwargs["exc"] is exc


# Recoverable 系 — Handler の reraise 戻り値で raise/return が決まる


@pytest.mark.asyncio
async def test_recoverable_reraise_true_raises() -> None:
    """Handler が ``reraise=True`` を返したら task は元の exc を raise する。"""
    from app.queue.tasks.assessment import assess_content

    ctx = _make_ctx(
        retries=0, max_retries=2
    )  # retry 余地あり: _retries=0 < max_retries-1
    exc = AssessmentRecoverableError(
        code="ai_error_network", failure_kind="attempt_scoped"
    )

    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
        patch(
            "app.queue.tasks.assessment.AssessmentFailureHandler"
        ) as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
        mock_handler_cls.return_value.handle = AsyncMock(
            return_value=FailureHandlingDecision(reraise=True)
        )
        with pytest.raises(AssessmentRecoverableError):
            await assess_content(trigger=_trigger(), ctx=ctx)

    mock_handler_cls.return_value.handle.assert_awaited_once()


@pytest.mark.asyncio
async def test_recoverable_reraise_false_returns() -> None:
    """Handler が ``reraise=False`` を返したら task は return する (raise しない)。

    retry 上限到達 のとき Handler は False を返す想定 (Stage 4 仕様)。本 test は
    その経路で task が最後まで完走し、``last_attempt=True`` が Handler に渡る
    ことを確認する。
    """
    from app.queue.tasks.assessment import assess_content

    # 最終試行: _retries=max_retries-1=1 (旧 retry_count=2 は production が書かない値)
    ctx = _make_ctx(retries=1, max_retries=2)
    exc = AssessmentRecoverableError(
        code="ai_error_network", failure_kind="attempt_scoped"
    )

    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
        patch(
            "app.queue.tasks.assessment.AssessmentFailureHandler"
        ) as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
        mock_handler_cls.return_value.handle = AsyncMock(
            return_value=FailureHandlingDecision(reraise=False)
        )
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
    exc = AssessmentResponseInvalidError(AssessmentResponseDefect.CATEGORY_KEY_MISSING)

    with (
        _patch_ready_construction(),
        patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
        patch(
            "app.queue.tasks.assessment.AssessmentFailureHandler"
        ) as mock_handler_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
        mock_handler_cls.return_value.handle = AsyncMock(
            return_value=FailureHandlingDecision(reraise=False)
        )
        await assess_content(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    assert isinstance(
        handler_handle.await_args.kwargs["exc"], AssessmentRecoverableError
    )


# catch-all — Layer 1 marker いずれにも該当しない例外も Handler に委譲


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
        mock_handler_cls.return_value.handle = AsyncMock(
            return_value=FailureHandlingDecision(reraise=False)
        )
        await assess_content(trigger=_trigger(), ctx=ctx)

    handler_handle = mock_handler_cls.return_value.handle
    handler_handle.assert_awaited_once()
    assert isinstance(handler_handle.await_args.kwargs["exc"], ValueError)


# processing_outcome emit — ready-build failed / blocked / rate limit gate 経路


@pytest.mark.parametrize(
    ("exc", "expected_result"),
    [
        (OperationalError("SELECT 1", {}, Exception("db down")), "infra_error"),
        (ValueError("boom"), "failed"),
    ],
)
@pytest.mark.asyncio
async def test_ready_build_failed_emits_classified_outcome(
    capfire: CaptureLogfire, exc: Exception, expected_result: str
) -> None:
    """ready-build の blocked 以外の例外は projection 分類で emit し再送出する。

    DB 障害は infra_error (成功率の分母外)、その他は failed。例外は task を貫通する。
    """
    from app.queue.tasks.assessment import assess_content

    ctx = _make_ctx()
    session = ctx.state.session_factory.return_value.__aenter__.return_value
    session.commit = AsyncMock()
    with (
        patch(
            "app.queue.tasks.assessment.ReadyForAssessment.try_advance_from",
            new=AsyncMock(side_effect=exc),
        ),
        patch("app.queue.tasks.assessment.AssessmentAuditRepository") as mock_audit_cls,
    ):
        mock_audit_cls.return_value.append_ready_build_failed = AsyncMock()
        with pytest.raises(type(exc)):
            await assess_content(trigger=_trigger(), ctx=ctx)

    metrics = collected_metrics(capfire)
    assert (
        sum_counter_for_result(metrics, _PROCESSING_OUTCOME_METRIC, expected_result)
        == 1
    )
    other = "failed" if expected_result == "infra_error" else "infra_error"
    assert sum_counter_for_result(metrics, _PROCESSING_OUTCOME_METRIC, other) == 0


@pytest.mark.parametrize(
    "code",
    [
        AssessmentReadyBuildBlockedCode.CURATION_MISSING,
        AssessmentReadyBuildBlockedCode.ALREADY_IN_SCOPE,
        AssessmentReadyBuildBlockedCode.ALREADY_OUT_OF_SCOPE,
    ],
)
@pytest.mark.asyncio
async def test_ready_build_blocked_emits_nothing(
    capfire: CaptureLogfire, code: AssessmentReadyBuildBlockedCode
) -> None:
    """ready-build blocked は全コード stale/冪等で emit しない (rejected 無し)。"""
    from app.queue.tasks.assessment import assess_content

    ctx = _make_ctx()
    session = ctx.state.session_factory.return_value.__aenter__.return_value
    session.commit = AsyncMock()
    blocked = AssessmentReadyBuildBlockedError(code, analyzable_article_id=7)
    with (
        patch(
            "app.queue.tasks.assessment.ReadyForAssessment.try_advance_from",
            new=AsyncMock(side_effect=blocked),
        ),
        patch("app.queue.tasks.assessment.AssessmentAuditRepository") as mock_audit_cls,
    ):
        mock_audit_cls.return_value.append_ready_build_blocked = AsyncMock()
        await assess_content(trigger=_trigger(), ctx=ctx)

    metrics = collected_metrics(capfire)
    for result in ("in_scope", "out_of_scope", "failed", "infra_error"):
        assert sum_counter_for_result(metrics, _PROCESSING_OUTCOME_METRIC, result) == 0


@pytest.mark.asyncio
async def test_rate_limit_gate_skip_does_not_emit_processing_outcome(
    capfire: CaptureLogfire,
) -> None:
    """rate limit gate skip では processing_outcome を emit しない (capacity 制御)。"""
    from app.queue.tasks.assessment import assess_content

    ctx = _make_ctx()
    ctx.state.provider_rate_limit_gate.acquire = AsyncMock(return_value=False)
    with _patch_ready_construction():
        await assess_content(trigger=_trigger(), ctx=ctx)

    metrics = collected_metrics(capfire)
    for result in ("in_scope", "out_of_scope", "failed", "infra_error"):
        assert sum_counter_for_result(metrics, _PROCESSING_OUTCOME_METRIC, result) == 0
