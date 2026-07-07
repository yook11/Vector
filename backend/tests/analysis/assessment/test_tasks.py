"""``assess_content`` task の分岐テスト。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire
from structlog.testing import capture_logs

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
)
from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildBlockedCode,
    AssessmentReadyBuildBlockedError,
    ReadyForAssessment,
)
from app.analysis.failure_handling import FailureHandlingDecision
from app.analysis.rate_limit import AIModelRateLimitPolicy, RateLimitRule
from app.audit.domain.event import Stage
from app.queue.messages.assessment import AssessmentTrigger
from app.queue.messages.embedding import EmbeddingTrigger
from tests.logfire._span_helpers import one_article_stage_span, stage_attrs


def _make_provider_fake() -> MagicMock:
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


def _make_ctx(
    *,
    assessor: MagicMock | None = None,
    retries: int = 0,
    max_retries: int = 0,
    gate_acquire: bool = True,
) -> MagicMock:
    ctx = MagicMock()
    gate = MagicMock()
    gate.acquire = AsyncMock(return_value=gate_acquire)
    ctx.state = SimpleNamespace(
        session_factory=MagicMock(),
        provider_rate_limit_gate=gate,
    )
    if assessor is not None:
        ctx.state.assessor = assessor
    # taskiq SimpleRetryMiddleware が書く label は "_retries" (0..max_retries-1)
    ctx.message.labels = {
        "_retries": retries,
        "max_retries": max_retries,
    }
    return ctx


def _make_trigger(curation_id: int = 2) -> AssessmentTrigger:
    return AssessmentTrigger(curation_id=curation_id)


def _make_ready(curation_id: int = 2) -> ReadyForAssessment:
    return ReadyForAssessment(
        curation_id=curation_id,
        translated_title="title",
        summary="summary",
        analyzable_article_id=7,
    )


def _patch_ready_construction(
    result: ReadyForAssessment | AssessmentReadyBuildBlockedError,
):
    mock = (
        AsyncMock(side_effect=result)
        if isinstance(result, AssessmentReadyBuildBlockedError)
        else AsyncMock(return_value=result)
    )
    return patch(
        "app.queue.tasks.assessment.ReadyForAssessment.try_advance_from",
        new=mock,
    )


class TestAssessContent:
    @pytest.mark.asyncio
    async def test_ready_build_blocked_audits_and_does_not_call_service(self) -> None:
        """Ready build blocked なら rejected audit + return、Service は呼ばない。

        rate limit acquire も試みない (Ready 構築が gatekeeper)。
        """
        from app.queue.tasks.assessment import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake())
        trigger = _make_trigger(curation_id=42)
        exc = AssessmentReadyBuildBlockedError(
            AssessmentReadyBuildBlockedCode.CURATION_MISSING
        )

        with (
            _patch_ready_construction(exc),
            patch("app.queue.tasks.assessment.AssessmentAuditRepository") as mock_audit,
            patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
        ):
            mock_audit.return_value.append_ready_build_blocked = AsyncMock()
            await assess_content(trigger=trigger, ctx=ctx)

        mock_audit.return_value.append_ready_build_blocked.assert_awaited_once_with(
            curation_id=42,
            exc=exc,
        )
        # rate limit acquire は試みず、Service も呼ばない
        ctx.state.provider_rate_limit_gate.acquire.assert_not_called()
        mock_svc_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_ready_build_exception_audits_and_reraises(self) -> None:
        """Ready 判定中の例外は failed audit 後に元例外を raise する。"""
        from app.queue.tasks.assessment import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake())
        trigger = _make_trigger(curation_id=42)
        exc = RuntimeError("ready build exploded")

        with (
            patch(
                "app.queue.tasks.assessment.ReadyForAssessment.try_advance_from",
                new=AsyncMock(side_effect=exc),
            ),
            patch(
                "app.queue.tasks.assessment._append_ready_build_failed_audit",
                new=AsyncMock(),
            ) as audit_failed,
            patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
        ):
            with pytest.raises(RuntimeError):
                await assess_content(trigger=trigger, ctx=ctx)

        audit_failed.assert_awaited_once_with(
            ctx.state.session_factory,
            curation_id=42,
            exc=exc,
        )
        ctx.state.provider_rate_limit_gate.acquire.assert_not_called()
        mock_svc_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_in_scope_chains_embedding_with_trigger(self) -> None:
        """in-scope 成功 (analyzed_article_id 返却) → EmbeddingTrigger で chain。

        assessment task は embedding 用 Ready を構築せず、ID だけを運ぶ
        EmbeddingTrigger を kiq に enqueue する。
        """
        from app.queue.tasks.assessment import assess_content

        mock_ctx = _make_ctx(assessor=_make_provider_fake())
        trigger = _make_trigger(curation_id=2)
        ready = _make_ready(curation_id=2)

        with (
            _patch_ready_construction(ready),
            patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
            patch("app.queue.tasks.assessment.generate_embedding") as mock_embed,
        ):
            # Service は in-scope 成功時 assessment id を返す
            mock_svc_cls.return_value.execute = AsyncMock(return_value=100)
            mock_embed.kiq = AsyncMock()
            await assess_content(trigger=trigger, ctx=mock_ctx)

        # 構築された Ready が Service に渡されていること
        call_args = mock_svc_cls.return_value.execute.call_args
        assert call_args[0][0] is ready
        # 相関 id (元記事 id) も trigger に載せて chain する。
        mock_embed.kiq.assert_awaited_once_with(
            EmbeddingTrigger(analyzed_article_id=100, analyzable_article_id=7)
        )

    @pytest.mark.asyncio
    async def test_none_result_does_not_chain(self) -> None:
        """``execute`` が None (out-of-scope / race lost) → embedding chain しない。

        in-scope 経路だけが embedding chain の対象。out-of-scope はパイプライン終了で、
        race lost は勝者 task が自身で chain を起動する責務。
        """
        from app.queue.tasks.assessment import assess_content

        mock_ctx = _make_ctx(assessor=_make_provider_fake())
        trigger = _make_trigger(curation_id=2)

        with (
            _patch_ready_construction(_make_ready(curation_id=2)),
            patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
            patch("app.queue.tasks.assessment.generate_embedding") as mock_embed,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=None)
            mock_embed.kiq = AsyncMock()
            await assess_content(trigger=trigger, ctx=mock_ctx)

        mock_embed.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_quota_skip_returns_without_invoking_service(self) -> None:
        """gate.acquire=False の場合 gate skip の log + metric を出して return する。"""
        from app.queue.tasks.assessment import assess_content

        mock_ctx = _make_ctx(assessor=_make_provider_fake(), gate_acquire=False)
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
            patch("app.queue.tasks.assessment.generate_embedding") as mock_embed,
            patch(
                "app.queue.tasks.assessment.record_rate_limit_gate_skipped"
            ) as mock_record,
            capture_logs() as cap,
        ):
            mock_embed.kiq = AsyncMock()
            await assess_content(trigger=trigger, ctx=mock_ctx)

        mock_svc_cls.assert_not_called()
        mock_embed.kiq.assert_not_called()
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["stage"] is Stage.ASSESSMENT
        assert mock_record.call_args.kwargs["model"] == "test-model"
        skips = [
            e for e in cap if e.get("event") == "assessment_ai_rate_limit_gate_skipped"
        ]
        assert skips, "gate skip log が emit されていない"
        assert skips[-1]["analyzable_article_id"] == 7
        assert skips[-1]["ai_model"] == "test-model"

    @pytest.mark.asyncio
    async def test_rate_limit_raises_for_retry(self) -> None:
        """Handler が ``reraise=True`` を返したら task は元の exc を raise する。

        ``AIProviderRateLimitedError`` は assessment marker のいずれにも該当しない
        ので catch-all 経路で Handler に委譲される。retry 余地ありで Handler
        が True を返した想定で task が raise することを確認する。
        """
        from app.queue.tasks.assessment import assess_content

        mock_ctx = _make_ctx(
            assessor=_make_provider_fake(),
            retries=0,
            max_retries=2,  # retry 余地あり
        )
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
            patch(
                "app.queue.tasks.assessment.AssessmentFailureHandler"
            ) as mock_handler_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=AIProviderRateLimitedError("429"),
            )
            mock_handler_cls.return_value.handle = AsyncMock(
                return_value=FailureHandlingDecision(reraise=True)
            )
            with pytest.raises(AIProviderRateLimitedError):
                await assess_content(trigger=trigger, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_rate_limit_last_attempt_returns(self) -> None:
        """Handler が ``reraise=False`` を返したら task は return する。"""
        from app.queue.tasks.assessment import assess_content

        # 最終試行: _retries=max_retries-1=1
        mock_ctx = _make_ctx(assessor=_make_provider_fake(), retries=1, max_retries=2)
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
            patch(
                "app.queue.tasks.assessment.AssessmentFailureHandler"
            ) as mock_handler_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=AIProviderRateLimitedError("429"),
            )
            mock_handler_cls.return_value.handle = AsyncMock(
                return_value=FailureHandlingDecision(reraise=False)
            )
            await assess_content(trigger=trigger, ctx=mock_ctx)


class TestAssessContentStageSpan:
    """``article_stage`` span の assessment task 配線 (capfire oracle)。

    Service は mock するため in_scope / out_of_scope の result は service テストが
    正本。ここでは task が設定する skipped / rate_limited / failed、kiq 成功後の
    mark、ready 構築後の article_id late-binding を固定する。
    """

    @pytest.mark.asyncio
    async def test_in_scope_chain_marks_next_task_and_binds_article_id(
        self, capfire: CaptureLogfire
    ) -> None:
        """in-scope 成功 → mark (name=generate_embedding) + article_id late-bind。"""
        from app.queue.tasks.assessment import assess_content

        mock_ctx = _make_ctx(assessor=_make_provider_fake())
        ready = _make_ready(curation_id=2)  # article_id=7
        with (
            _patch_ready_construction(ready),
            patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
            patch("app.queue.tasks.assessment.generate_embedding") as mock_embed,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=100)
            mock_embed.kiq = AsyncMock()
            await assess_content(trigger=_make_trigger(curation_id=2), ctx=mock_ctx)

        attrs = stage_attrs(capfire)
        assert attrs["next_task_enqueued"] is True
        assert attrs["next_task_name"] == "generate_embedding"
        assert attrs["article_id"] == 7
        # result は service (mock) の責務。task は success 経路で result を設定しない。
        assert "result" not in attrs

    @pytest.mark.asyncio
    async def test_none_result_does_not_mark_next_task(
        self, capfire: CaptureLogfire
    ) -> None:
        """Service が None (out_of_scope / race) → mark せず enqueued は False。"""
        from app.queue.tasks.assessment import assess_content

        mock_ctx = _make_ctx(assessor=_make_provider_fake())
        with (
            _patch_ready_construction(_make_ready(curation_id=2)),
            patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
            patch("app.queue.tasks.assessment.generate_embedding") as mock_embed,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=None)
            mock_embed.kiq = AsyncMock()
            await assess_content(trigger=_make_trigger(curation_id=2), ctx=mock_ctx)

        attrs = stage_attrs(capfire)
        assert attrs["next_task_enqueued"] is False
        assert "next_task_name" not in attrs

    @pytest.mark.asyncio
    async def test_ready_build_blocked_sets_skipped(
        self, capfire: CaptureLogfire
    ) -> None:
        """Ready build blocked 経路で task が skipped を焼く (article_id 無し)。"""
        from app.queue.tasks.assessment import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake())
        exc = AssessmentReadyBuildBlockedError(
            AssessmentReadyBuildBlockedCode.CURATION_MISSING
        )
        with (
            _patch_ready_construction(exc),
            patch("app.queue.tasks.assessment.AssessmentAuditRepository") as mock_audit,
            patch("app.queue.tasks.assessment.AssessmentService"),
        ):
            mock_audit.return_value.append_ready_build_blocked = AsyncMock()
            await assess_content(trigger=_make_trigger(curation_id=42), ctx=ctx)

        attrs = stage_attrs(capfire)
        assert attrs["result"] == "skipped"
        # late-binding は ready 構築後。blocked では article_id は載らない。
        assert "article_id" not in attrs

    @pytest.mark.asyncio
    async def test_ready_build_exception_sets_failed_via_backstop(
        self, capfire: CaptureLogfire
    ) -> None:
        """Ready 構築例外 (task は result 不設定) → backstop が failed を焼く。"""
        from app.queue.tasks.assessment import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake())
        with (
            patch(
                "app.queue.tasks.assessment.ReadyForAssessment.try_advance_from",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch(
                "app.queue.tasks.assessment._append_ready_build_failed_audit",
                new=AsyncMock(),
            ),
            patch("app.queue.tasks.assessment.AssessmentService"),
        ):
            with pytest.raises(RuntimeError):
                await assess_content(trigger=_make_trigger(curation_id=42), ctx=ctx)

        assert stage_attrs(capfire)["result"] == "failed"

    @pytest.mark.asyncio
    async def test_gate_skip_sets_rate_limited(self, capfire: CaptureLogfire) -> None:
        """gate.acquire=False 経路で task が result=rate_limited を焼く。"""
        from app.queue.tasks.assessment import assess_content

        mock_ctx = _make_ctx(assessor=_make_provider_fake(), gate_acquire=False)
        with (
            _patch_ready_construction(_make_ready()),
            patch("app.queue.tasks.assessment.AssessmentService"),
            patch("app.queue.tasks.assessment.generate_embedding") as mock_embed,
            patch("app.queue.tasks.assessment.record_rate_limit_gate_skipped"),
        ):
            mock_embed.kiq = AsyncMock()
            await assess_content(trigger=_make_trigger(), ctx=mock_ctx)

        assert stage_attrs(capfire)["result"] == "rate_limited"

    @pytest.mark.asyncio
    async def test_terminal_sets_failure_attrs_without_drop_article(
        self, capfire: CaptureLogfire
    ) -> None:
        """Service が terminal marker を raise したとき span に failure 属性が焼かれる。

        期待値の根拠:
        - AIProviderConfigurationError: CODE="ai_error_configuration",
          FAILURE_MODE=OPERATOR_ACTION_REQUIRED (ai_provider_errors.py)
        - map_provider_to_assessment: not retryable → AssessmentTerminalError,
          failure_kind=mode.value="operator_action_required", code=exc.CODE
          (assessment/errors.py)
        - AssessmentTerminalError: RETRYABILITY=NON_RETRYABLE, FAILURE_ACTION=None
          → failure_action は span に載らない (failure_attrs.py: None なら set しない)
        """
        from app.analysis.assessment.errors import map_provider_to_assessment
        from app.queue.tasks.assessment import assess_content

        mock_ctx = _make_ctx(assessor=_make_provider_fake())
        raw = AIProviderConfigurationError()
        marker = map_provider_to_assessment(raw)

        with (
            _patch_ready_construction(_make_ready()),
            patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
            patch(
                "app.queue.tasks.assessment.AssessmentFailureHandler"
            ) as mock_handler_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=marker)
            mock_handler_cls.return_value.handle = AsyncMock(
                return_value=FailureHandlingDecision(reraise=False)
            )
            await assess_content(trigger=_make_trigger(), ctx=mock_ctx)

        attrs = stage_attrs(capfire)
        assert attrs["result"] == "failed"
        # failure_kind: OPERATOR_ACTION_REQUIRED.value (assessment/errors.py)
        assert attrs["failure_kind"] == "operator_action_required"
        # code: AIProviderConfigurationError.CODE (ai_provider_errors.py)
        assert attrs["code"] == "ai_error_configuration"
        # retryability: AssessmentTerminalError.RETRYABILITY (assessment/errors.py)
        assert attrs["retryability"] == "non_retryable"
        # error_class: exception_fqn of the marker instance (AssessmentTerminalError)
        assert attrs["error_class"].endswith(".AssessmentTerminalError")
        # failure_action: FAILURE_ACTION=None → attribute not set (failure_attrs.py)
        assert "failure_action" not in attrs

    @pytest.mark.asyncio
    async def test_recoverable_reraise_sets_failure_attrs_and_exception_event(
        self, capfire: CaptureLogfire
    ) -> None:
        """Service が recoverable marker を raise し reraise=True のとき、span に
        failure 属性と OTel exception event が両方残る (二層記録の統合確認)。

        期待値の根拠:
        - AIProviderNetworkError: CODE="ai_error_network",
          FAILURE_MODE=ATTEMPT_SCOPED (ai_provider_errors.py)
        - map_provider_to_assessment: retryable → AssessmentRecoverableError,
          failure_kind="attempt_scoped", code="ai_error_network" (assessment/errors.py)
        - AssessmentRecoverableError: RETRYABILITY=RETRYABLE (assessment/errors.py)
        - backstop: reraise=True → task raises → backstop catches → record_failure
          (no-override = no-op) + OTel exception event by logfire.span backstop
        """
        from app.analysis.assessment.errors import map_provider_to_assessment
        from app.queue.tasks.assessment import assess_content
        from tests.logfire._span_helpers import exception_event

        mock_ctx = _make_ctx(assessor=_make_provider_fake())
        raw = AIProviderNetworkError()
        marker = map_provider_to_assessment(raw)

        with (
            _patch_ready_construction(_make_ready()),
            patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
            patch(
                "app.queue.tasks.assessment.AssessmentFailureHandler"
            ) as mock_handler_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=marker)
            mock_handler_cls.return_value.handle = AsyncMock(
                return_value=FailureHandlingDecision(reraise=True)
            )
            with pytest.raises(type(marker)):
                await assess_content(trigger=_make_trigger(), ctx=mock_ctx)

        attrs = stage_attrs(capfire)
        # failure_kind: ATTEMPT_SCOPED.value (assessment/errors.py)
        assert attrs["failure_kind"] == "attempt_scoped"
        # code: AIProviderNetworkError.CODE (ai_provider_errors.py)
        assert attrs["code"] == "ai_error_network"
        # retryability: AssessmentRecoverableError.RETRYABILITY (assessment/errors.py)
        assert attrs["retryability"] == "retryable"
        # error_class は early record_failure が焼いたもの (AssessmentRecoverableError)
        assert attrs["error_class"].endswith(".AssessmentRecoverableError")
        # OTel exception event: logfire records the propagating exception on the span
        evt = exception_event(one_article_stage_span(capfire))
        assert evt is not None, "OTel exception event が span に記録されていない"
        # exception event の type も同じ marker class であること
        assert evt["attributes"]["exception.type"].endswith(
            ".AssessmentRecoverableError"
        )

    @pytest.mark.asyncio
    async def test_no_override_preserves_original_failure_class(
        self, capfire: CaptureLogfire
    ) -> None:
        """stage.record_failure の no-override: handler が二次例外を発生させても
        span の error_class / failure_kind は最初の業務例外 (recoverable) のまま。

        handler.handle が RuntimeError を raise → task の except から伝播 →
        backstop の record_failure(RuntimeError) は _failure_set=True のため no-op。
        span 属性は元の marker クラスで固定される (failure_attrs.py)。
        """
        from app.analysis.assessment.errors import map_provider_to_assessment
        from app.queue.tasks.assessment import assess_content

        mock_ctx = _make_ctx(assessor=_make_provider_fake())
        raw = AIProviderNetworkError()
        original_marker = map_provider_to_assessment(raw)  # AssessmentRecoverableError

        with (
            _patch_ready_construction(_make_ready()),
            patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
            patch(
                "app.queue.tasks.assessment.AssessmentFailureHandler"
            ) as mock_handler_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=original_marker)
            # handler 自体が二次例外を raise → no-override の検証対象
            mock_handler_cls.return_value.handle = AsyncMock(
                side_effect=RuntimeError("secondary audit/hold down")
            )
            with pytest.raises(RuntimeError):
                await assess_content(trigger=_make_trigger(), ctx=mock_ctx)

        attrs = stage_attrs(capfire)
        # error_class は最初の業務例外 (AssessmentRecoverableError) のまま
        # RuntimeError で上書きされていないことを確認 (no-override 保証)
        assert attrs["error_class"].endswith(".AssessmentRecoverableError")
        # failure_kind も元の marker のまま (attempt_scoped, ai_provider_errors.py)
        assert attrs["failure_kind"] == "attempt_scoped"

    @pytest.mark.parametrize("reraise", [True, False])
    @pytest.mark.asyncio
    async def test_service_exception_sets_failed_result(
        self, capfire: CaptureLogfire, reraise: bool
    ) -> None:
        """service 例外は handler の reraise 値に関わらず span result=failed を焼く。"""
        from app.queue.tasks.assessment import assess_content

        mock_ctx = _make_ctx(assessor=_make_provider_fake())
        with (
            _patch_ready_construction(_make_ready()),
            patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
            patch(
                "app.queue.tasks.assessment.AssessmentFailureHandler"
            ) as mock_handler_cls,
            patch("app.queue.tasks.assessment.set_assessment_hold", new=AsyncMock()),
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=AIProviderRateLimitedError("429")
            )
            mock_handler_cls.return_value.handle = AsyncMock(
                return_value=FailureHandlingDecision(reraise=reraise)
            )
            if reraise:
                with pytest.raises(AIProviderRateLimitedError):
                    await assess_content(trigger=_make_trigger(), ctx=mock_ctx)
            else:
                await assess_content(trigger=_make_trigger(), ctx=mock_ctx)

        assert stage_attrs(capfire)["result"] == "failed"
