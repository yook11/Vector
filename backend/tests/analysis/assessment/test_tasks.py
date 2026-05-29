"""``assess_content`` task の分岐テスト。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from app.analysis.ai_provider_errors import AIProviderRateLimitedError
from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildBlockedCode,
    AssessmentReadyBuildBlockedError,
    ReadyForAssessment,
)
from app.analysis.failure_handling import FailureHandlingDecision
from app.analysis.rate_limit import AIModelRateLimitPolicy, RateLimitRule
from app.queue.messages.assessment import AssessmentTrigger
from app.queue.messages.embedding import EmbeddingTrigger


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
    retry_count: int = 0,
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
    ctx.message.labels = {
        "retry_count": retry_count,
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
        article_id=7,
        source_name="Test Source",
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


# ---------------------------------------------------------------------------
# assess_content (Stage 4)
# ---------------------------------------------------------------------------


class TestAssessContent:
    @pytest.mark.asyncio
    async def test_ready_build_blocked_audits_and_does_not_call_service(self) -> None:
        """Ready build blocked なら rejected audit + return、Service は呼ばない。

        rate limit acquire も試みない (Ready 構築が gatekeeper、案 3 順序)。
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
        """in-scope 成功 (assessment_id 返却) → EmbeddingTrigger で chain。

        案 3: 上流 Stage 4 task は Stage 5 Ready を構築せず、ID だけ運ぶ
        EmbeddingTrigger を kiq に enqueue する。Ready 構築は下流 Stage 5
        task が処理開始時に行う。
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
        mock_embed.kiq.assert_awaited_once_with(EmbeddingTrigger(analysis_id=100))

    @pytest.mark.asyncio
    async def test_none_result_does_not_chain(self) -> None:
        """``execute`` が None (out-of-scope / race lost) → embedding chain しない。

        in-scope 経路だけが Stage 5 chain の対象。out-of-scope はパイプライン終了で、
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
        """gate.acquire=False の場合 quota log + return、Service は呼ばない。"""
        from app.queue.tasks.assessment import assess_content

        mock_ctx = _make_ctx(assessor=_make_provider_fake(), gate_acquire=False)
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
            patch("app.queue.tasks.assessment.generate_embedding") as mock_embed,
            capture_logs() as cap,
        ):
            mock_embed.kiq = AsyncMock()
            await assess_content(trigger=trigger, ctx=mock_ctx)

        mock_svc_cls.assert_not_called()
        mock_embed.kiq.assert_not_called()
        warnings = [e for e in cap if e.get("event") == "assess_content_daily_quota"]
        assert warnings, "quota skip 時の warning log が emit されていない"

    @pytest.mark.asyncio
    async def test_rate_limit_raises_for_retry(self) -> None:
        """Handler が ``reraise=True`` を返したら task は元の exc を raise する。

        ``AIProviderRateLimitedError`` は Stage 4 marker のいずれにも該当しない
        ので catch-all 経路で Handler に委譲される。retry 余地ありで Handler
        が True を返した想定で task が raise することを確認する。
        """
        from app.queue.tasks.assessment import assess_content

        mock_ctx = _make_ctx(
            assessor=_make_provider_fake(), retry_count=0, max_retries=2
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

        mock_ctx = _make_ctx(
            assessor=_make_provider_fake(), retry_count=2, max_retries=2
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
                return_value=FailureHandlingDecision(reraise=False)
            )
            await assess_content(trigger=trigger, ctx=mock_ctx)
