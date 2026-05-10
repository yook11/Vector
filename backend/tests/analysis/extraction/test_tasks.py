"""``extract_content`` task のテスト (chain 経路 + rate limit 経路)。

Layer 1 marker dispatch ルーティングは ``test_extract_task_dispatch.py`` 側で
網羅する。本ファイルは:

- ExtractedOutcome → Ready 構築 → assess_content.kiq による chain
- NoiseOutcome / Ready None 時に chain しないこと
- legacy ``AIProviderRateLimitedError`` の audit 経路 (catch-all 経由)
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.assessment.domain.ready import ReadyForAssessment
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


def _make_ready_assess(extraction_id: int = 2) -> ReadyForAssessment:
    return ReadyForAssessment(
        extraction_id=extraction_id,
        translated_title="title",
        summary="summary",
    )


# ---------------------------------------------------------------------------
# extract_content
# ---------------------------------------------------------------------------


class TestExtractContent:
    @pytest.mark.asyncio
    async def test_chains_assess_with_ready_when_advance_succeeds(self) -> None:
        """ExtractedOutcome → Ready 構築 → assess_content.kiq(ready) で chain。"""
        from app.analysis.extraction.service import ExtractedOutcome
        from app.analysis.extraction.tasks import extract_content

        mock_ctx = _make_ctx(extractor=_make_provider_fake())
        mock_extraction = MagicMock()
        mock_extraction.id = 42
        mock_outcome = ExtractedOutcome(extraction=mock_extraction)
        ready_assess = _make_ready_assess(extraction_id=42)

        with (
            patch(
                "app.analysis.extraction.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.extraction.tasks.ExtractionService") as mock_svc_cls,
            patch(
                "app.analysis.extraction.tasks.ReadyForAssessment.try_advance_from",
                new=AsyncMock(return_value=ready_assess),
            ),
            patch("app.analysis.extraction.tasks.assess_content") as mock_assess,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=mock_outcome)
            mock_assess.kiq = AsyncMock()
            await extract_content(ready=_make_ready_extraction(), ctx=mock_ctx)

        mock_assess.kiq.assert_awaited_once_with(ready_assess)

    @pytest.mark.asyncio
    async def test_does_not_chain_when_advance_returns_none(self) -> None:
        """precondition 未充足 (try_advance_from が None) なら chain しない。"""
        from app.analysis.extraction.service import ExtractedOutcome
        from app.analysis.extraction.tasks import extract_content

        mock_ctx = _make_ctx(extractor=_make_provider_fake())
        mock_outcome = ExtractedOutcome(extraction=MagicMock())

        with (
            patch(
                "app.analysis.extraction.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.extraction.tasks.ExtractionService") as mock_svc_cls,
            patch(
                "app.analysis.extraction.tasks.ReadyForAssessment.try_advance_from",
                new=AsyncMock(return_value=None),
            ),
            patch("app.analysis.extraction.tasks.assess_content") as mock_assess,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=mock_outcome)
            mock_assess.kiq = AsyncMock()
            await extract_content(ready=_make_ready_extraction(), ctx=mock_ctx)

        mock_assess.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_noise_outcome_does_not_chain(self) -> None:
        """NoiseOutcome は chain しない (Service 側で extraction_noises に永続化済)。"""
        from app.analysis.extraction.service import NoiseOutcome
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
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=NoiseOutcome(),
            )
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
