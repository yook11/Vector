"""``extract_content`` task のテスト (chain 経路 + rate limit 経路)。

PR1-c で Outcome を廃止し ``ExtractionService.execute`` の戻り値を
``int | None`` に統一したため、本 file は:

- signal 勝者 (``execute`` が ``int`` を返す) → ``assess_content.kiq`` で chain
- noise 勝者 / race 敗北 (``execute`` が ``None`` を返す) → chain しない
- legacy ``AIProviderRateLimitedError`` の audit 経路 (catch-all 経由)

Layer 1 marker dispatch ルーティングは ``test_extract_task_dispatch.py`` 側で
網羅する。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.assessment.domain.ready import AssessmentTrigger
from app.analysis.errors import AIProviderRateLimitedError
from app.analysis.extraction.domain.ready import ReadyForExtraction


def _make_provider_fake() -> MagicMock:
    """extractor 用のスタブ。MODEL/RPM/RPD を持つ。"""
    fake = MagicMock()
    fake.MODEL = "test-model"
    fake.RPM = 50
    fake.RPD = 1500
    return fake


def _make_ctx(
    *,
    extractor: MagicMock | None = None,
    retry_count: int = 0,
    max_retries: int = 0,
) -> MagicMock:
    """taskiq Context モック。"""
    ctx = MagicMock()
    ctx.state = SimpleNamespace(session_factory=MagicMock())
    if extractor is not None:
        ctx.state.extractor = extractor
    ctx.message.labels = {
        "retry_count": retry_count,
        "max_retries": max_retries,
    }
    return ctx


def _make_ready_extraction(article_id: int = 1) -> ReadyForExtraction:
    return ReadyForExtraction(
        article_id=article_id,
        original_title="Title",
        original_content="content",
    )


# ---------------------------------------------------------------------------
# extract_content
# ---------------------------------------------------------------------------


class TestExtractContent:
    @pytest.mark.asyncio
    async def test_chains_assess_with_trigger_when_service_returns_extraction_id(
        self,
    ) -> None:
        """signal 勝者 (Service が int を返す) → ``assess_content.kiq`` で chain。

        案 3: 上流 Stage 3 task は Stage 4 Ready を構築せず、ID だけ運ぶ
        AssessmentTrigger を kiq に enqueue する。Ready 構築は下流 Stage 4
        task が処理開始時に行う。
        """
        from app.analysis.extraction.tasks import extract_content

        mock_ctx = _make_ctx(extractor=_make_provider_fake())

        with (
            patch(
                "app.analysis.extraction.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.extraction.tasks.ExtractionService") as mock_svc_cls,
            patch("app.analysis.extraction.tasks.assess_content") as mock_assess,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=42)
            mock_assess.kiq = AsyncMock()
            await extract_content(ready=_make_ready_extraction(), ctx=mock_ctx)

        mock_assess.kiq.assert_awaited_once_with(
            AssessmentTrigger(extraction_id=42),
        )

    @pytest.mark.asyncio
    async def test_noise_or_race_loss_does_not_chain(self) -> None:
        """Service が None を返したら chain しない (noise 勝者 / race 敗北を吸収)。"""
        from app.analysis.extraction.tasks import extract_content

        mock_ctx = _make_ctx(extractor=_make_provider_fake())

        with (
            patch(
                "app.analysis.extraction.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.extraction.tasks.ExtractionService") as mock_svc_cls,
            patch("app.analysis.extraction.tasks.assess_content") as mock_assess,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=None)
            mock_assess.kiq = AsyncMock()
            await extract_content(ready=_make_ready_extraction(), ctx=mock_ctx)

        mock_assess.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limited_records_audit_and_returns(self) -> None:
        """RateLimited は INLINE_RETRY=False、即 audit + return (PR3.5-c)。"""
        from app.analysis.extraction.tasks import extract_content

        mock_ctx = _make_ctx(
            extractor=_make_provider_fake(), retry_count=0, max_retries=1
        )

        with (
            patch(
                "app.analysis.extraction.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.extraction.tasks.ExtractionService") as mock_svc_cls,
            patch(
                "app.analysis.extraction.tasks.record_extraction_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=AIProviderRateLimitedError("429"),
            )
            await extract_content(ready=_make_ready_extraction(), ctx=mock_ctx)
        mock_audit.assert_awaited_once()
        # outcome_code 引数は廃止 (recording.py で内部導出)。exc が渡るのみ。
        assert isinstance(
            mock_audit.await_args.kwargs["exc"], AIProviderRateLimitedError
        )
