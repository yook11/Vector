"""``assess_content`` task のテスト (chain 経路 + rate limit 経路 + skip 経路)。

案 3 (厚い Ready + 下流 Stage 自身が処理開始時に構築): assess_content は
``AssessmentTrigger`` (curation_id のみ) を受領し、task 自身が
``ReadyForAssessment.try_advance_from`` で Ready を構築する。

- precondition 未充足 → svc.execute を呼ばずに return (rate limit も acquire しない)
- in-scope 成功 (int 返却) → EmbeddingTrigger で embedding chain (ID のみ運ぶ)
- out-of-scope / race lost (``None`` 返却) は chain しないこと
- rate limit (``AIProviderRateLimitedError``) 経路で Handler に委譲され、
  ``reraise`` 戻り値で raise/return が決まること
- gate.acquire=False (quota skip) → svc.execute も Handler も呼ばずに return

Layer 1 marker dispatch ルーティングは ``test_assess_task_dispatch.py`` 側で
網羅する。Handler 内部の audit 経路は ``test_failure_handler.py`` で integration
として検証する。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from app.analysis.ai_provider_errors import AIProviderRateLimitedError
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.rate_limit import RatePolicy
from app.queue.messages.assessment import AssessmentTrigger
from app.queue.messages.embedding import EmbeddingTrigger


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


def _make_ctx(
    *,
    assessor: MagicMock | None = None,
    retry_count: int = 0,
    max_retries: int = 0,
    gate_acquire: bool = True,
) -> MagicMock:
    """taskiq Context モック。``provider_rate_limit_gate.acquire`` は
    ``gate_acquire`` で True / False を選ぶ。"""
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


def _patch_ready_construction(ready: ReadyForAssessment | None):
    """task 内 ``ReadyForAssessment.try_advance_from`` を mock する patch。"""
    return patch(
        "app.queue.tasks.assessment.ReadyForAssessment.try_advance_from",
        new=AsyncMock(return_value=ready),
    )


# ---------------------------------------------------------------------------
# assess_content (Stage 4)
# ---------------------------------------------------------------------------


class TestAssessContent:
    @pytest.mark.asyncio
    async def test_skips_when_precondition_not_met(self) -> None:
        """try_advance_from が None を返したら svc.execute を呼ばずに return。

        rate limit acquire も試みない (Ready 構築が gatekeeper、案 3 順序)。
        """
        from app.queue.tasks.assessment import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake())
        trigger = _make_trigger(curation_id=42)

        with (
            _patch_ready_construction(None),
            patch("app.queue.tasks.assessment.AssessmentService") as mock_svc_cls,
        ):
            await assess_content(trigger=trigger, ctx=ctx)

        # rate limit acquire は試みず、Service も呼ばない
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
            mock_handler_cls.return_value.handle = AsyncMock(return_value=True)
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
            mock_handler_cls.return_value.handle = AsyncMock(return_value=False)
            await assess_content(trigger=trigger, ctx=mock_ctx)
