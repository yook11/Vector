"""``assess_content`` task のテスト (chain 経路 + 3 marker dispatch)。

案 3 (厚い Ready + 下流 Stage 自身が処理開始時に構築): assess_content は
``AssessmentTrigger`` (extraction_id のみ) を受領し、task 自身が
``ReadyForAssessment.try_advance_from`` で Ready を構築する。

- precondition 未充足 → svc.execute を呼ばずに return (rate limit も acquire しない)
- in-scope 成功 (int 返却) → EmbeddingTrigger で embedding chain (ID のみ運ぶ)
- out-of-scope / race lost (``None`` 返却) は chain しないこと
- 3 marker dispatch: TerminalSkip / Recoverable / catch-all Exception
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.assessment.domain.ready import (
    AssessmentTrigger,
    ReadyForAssessment,
)
from app.analysis.assessment.errors import (
    AssessmentCategoryMissingError,
    AssessmentRecoverableError,
    AssessmentResponseInvalidError,
    AssessmentTerminalSkipError,
)
from app.analysis.embedding.domain.ready import EmbeddingTrigger
from app.analysis.errors import RateLimitError


def _make_provider_fake() -> MagicMock:
    """assessor 用のスタブ。MODEL/RPM/RPD を持つ。"""
    fake = MagicMock()
    fake.MODEL = "test-model"
    fake.RPM = 50
    fake.RPD = 1500
    return fake


def _make_ctx(
    *,
    assessor: MagicMock | None = None,
    retry_count: int = 0,
    max_retries: int = 0,
) -> MagicMock:
    """taskiq Context モック。"""
    ctx = MagicMock()
    ctx.state = SimpleNamespace(session_factory=MagicMock())
    if assessor is not None:
        ctx.state.assessor = assessor
    ctx.message.labels = {
        "retry_count": retry_count,
        "max_retries": max_retries,
    }
    return ctx


def _make_trigger(extraction_id: int = 2) -> AssessmentTrigger:
    return AssessmentTrigger(extraction_id=extraction_id)


def _make_ready(extraction_id: int = 2) -> ReadyForAssessment:
    return ReadyForAssessment(
        extraction_id=extraction_id,
        translated_title="title",
        summary="summary",
        article_id=7,
        source_name="Test Source",
    )


def _patch_ready_construction(ready: ReadyForAssessment | None):
    """task 内 ``ReadyForAssessment.try_advance_from`` を mock する patch。"""
    return patch(
        "app.analysis.assessment.tasks.ReadyForAssessment.try_advance_from",
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
        from app.analysis.assessment.tasks import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake())
        trigger = _make_trigger(extraction_id=42)

        with (
            _patch_ready_construction(None),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
            ) as mock_limiters,
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
        ):
            await assess_content(trigger=trigger, ctx=ctx)

        # rate limit acquire は試みず、Service も呼ばない
        mock_limiters.assert_not_called()
        mock_svc_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_in_scope_chains_embedding_with_trigger(self) -> None:
        """in-scope 成功 (assessment_id 返却) → EmbeddingTrigger で chain。

        案 3: 上流 Stage 4 task は Stage 5 Ready を構築せず、ID だけ運ぶ
        EmbeddingTrigger を kiq に enqueue する。Ready 構築は下流 Stage 5
        task が処理開始時に行う。
        """
        from app.analysis.assessment.tasks import assess_content

        mock_ctx = _make_ctx(assessor=_make_provider_fake())
        trigger = _make_trigger(extraction_id=2)
        ready = _make_ready(extraction_id=2)

        with (
            _patch_ready_construction(ready),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch("app.analysis.assessment.tasks.generate_embedding") as mock_embed,
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
        from app.analysis.assessment.tasks import assess_content

        mock_ctx = _make_ctx(assessor=_make_provider_fake())
        trigger = _make_trigger(extraction_id=2)

        with (
            _patch_ready_construction(_make_ready(extraction_id=2)),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch("app.analysis.assessment.tasks.generate_embedding") as mock_embed,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=None)
            mock_embed.kiq = AsyncMock()
            await assess_content(trigger=trigger, ctx=mock_ctx)

        mock_embed.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_raises_for_retry(self) -> None:
        """legacy RateLimitError は 3 marker いずれにも該当しないので
        catch-all 句に dispatch される。retry 余地ありで raise。"""
        from app.analysis.assessment.tasks import assess_content

        mock_ctx = _make_ctx(
            assessor=_make_provider_fake(), retry_count=0, max_retries=2
        )
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks.record_assessment_failure",
                new=AsyncMock(),
            ),
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            with pytest.raises(RateLimitError):
                await assess_content(trigger=trigger, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_rate_limit_last_attempt_returns(self) -> None:
        """assess_content は最終試行で例外を送出せず return する (catch-all 句)。"""
        from app.analysis.assessment.tasks import assess_content

        mock_ctx = _make_ctx(
            assessor=_make_provider_fake(), retry_count=2, max_retries=2
        )
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks.record_assessment_failure",
                new=AsyncMock(),
            ),
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            await assess_content(trigger=trigger, ctx=mock_ctx)


# ---------------------------------------------------------------------------
# assess_content: 3 marker dispatch (TerminalSkip / Recoverable / Exception)
# ---------------------------------------------------------------------------


class TestAssessContentMarkerDispatch:
    """``assess_content`` の except 句は 3 marker dispatch
    (TerminalSkip → Recoverable → Exception)。各 except で
    ``record_assessment_failure`` を呼び出す。"""

    @pytest.mark.asyncio
    async def test_terminal_skip_records_failure_and_returns(self) -> None:
        """``AssessmentTerminalSkipError`` → audit + return (taskiq retry なし)。"""
        from app.analysis.assessment.tasks import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()
        exc = AssessmentTerminalSkipError("bad config", code="ai_error_configuration")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks.record_assessment_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            await assess_content(trigger=trigger, ctx=ctx)

        mock_audit.assert_awaited_once()
        assert mock_audit.await_args.kwargs["exc"] is exc

    @pytest.mark.asyncio
    async def test_category_missing_dispatches_to_terminal_skip(self) -> None:
        """``AssessmentCategoryMissingError`` (Layer 2-B、TerminalSkip 継承) は
        TerminalSkip 句に dispatch される (Recoverable 句に誤って落ちない)。"""
        from app.analysis.assessment.tasks import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()
        exc = AssessmentCategoryMissingError("unknown slug 'foo'")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks.record_assessment_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            # TerminalSkip 句は raise しない → return 成立
            await assess_content(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()
        assert isinstance(
            mock_audit.await_args.kwargs["exc"], AssessmentTerminalSkipError
        )

    @pytest.mark.asyncio
    async def test_recoverable_records_failure_and_raises_when_not_last(self) -> None:
        """``AssessmentRecoverableError`` + retry 余地あり → audit + raise。"""
        from app.analysis.assessment.tasks import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()
        exc = AssessmentRecoverableError("network", code="ai_error_network")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks.record_assessment_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            with pytest.raises(AssessmentRecoverableError):
                await assess_content(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_recoverable_records_failure_and_returns_when_last(self) -> None:
        """``AssessmentRecoverableError`` + 最終 attempt → audit + return。"""
        from app.analysis.assessment.tasks import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake(), retry_count=2, max_retries=2)
        trigger = _make_trigger()
        exc = AssessmentRecoverableError("network", code="ai_error_network")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks.record_assessment_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            await assess_content(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_response_invalid_dispatches_to_recoverable(self) -> None:
        """``AssessmentResponseInvalidError`` (Layer 2-B、Recoverable 継承) は
        Recoverable 句に dispatch される (catch-all に落ちない)。"""
        from app.analysis.assessment.tasks import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()
        exc = AssessmentResponseInvalidError("schema violation")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks.record_assessment_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            with pytest.raises(AssessmentResponseInvalidError):
                await assess_content(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()
        assert isinstance(
            mock_audit.await_args.kwargs["exc"], AssessmentRecoverableError
        )

    @pytest.mark.asyncio
    async def test_unexpected_exception_records_and_returns_when_last(self) -> None:
        """任意 ``Exception`` + 最終 attempt → catch-all で audit + return。"""
        from app.analysis.assessment.tasks import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake(), retry_count=2, max_retries=2)
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks.record_assessment_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=ValueError("surprise")
            )
            await assess_content(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()
        assert isinstance(mock_audit.await_args.kwargs["exc"], ValueError)

    @pytest.mark.asyncio
    async def test_unexpected_exception_records_and_raises_when_not_last(self) -> None:
        """任意 ``Exception`` + retry 余地あり → catch-all で audit + raise。"""
        from app.analysis.assessment.tasks import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks.record_assessment_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=ValueError("surprise")
            )
            with pytest.raises(ValueError, match="surprise"):
                await assess_content(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()
