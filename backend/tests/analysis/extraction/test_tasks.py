"""``extract_content`` task のテスト (chain 経路 + rate limit 経路 + skip 経路)。

PR3 案 3 化: task signature は ``trigger: ExtractionTrigger``。冒頭で
``ReadyForExtraction.try_advance_from`` を呼んで Ready 自構築する。

- signal 勝者 (``execute`` が ``int`` を返す) → ``assess_content.kiq`` で chain
- noise 勝者 / race 敗北 (``execute`` が ``None`` を返す) → chain しない
- precondition_not_met (``try_advance_from`` が ``None`` を返す) → skip log + return
- legacy ``AIProviderRateLimitedError`` の audit 経路 (catch-all 経由)

Layer 1 marker dispatch ルーティングは ``test_extract_task_dispatch.py`` 側で
網羅する。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.assessment.domain.ready import AssessmentTrigger
from app.analysis.errors import AIProviderRateLimitedError
from app.analysis.extraction.domain.ready import ExtractionTrigger, ReadyForExtraction


def _make_provider_fake() -> MagicMock:
    """extractor 用のスタブ。MODEL/PROMPT_VERSION/RPM/RPD を持つ。"""
    fake = MagicMock()
    fake.MODEL = "test-model"
    fake.PROMPT_VERSION = "test-prompt-v1"
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


def _trigger(article_id: int = 1) -> ExtractionTrigger:
    return ExtractionTrigger(article_id=article_id)


def _fixed_ready(article_id: int = 1) -> ReadyForExtraction:
    """task 冒頭の Ready 自構築が返す固定 Ready。"""
    return ReadyForExtraction(
        article_id=article_id,
        original_title="Title",
        original_content="content",
    )


def _patch_try_advance_from(ready: ReadyForExtraction | None) -> object:
    """``ReadyForExtraction.try_advance_from`` を固定値返却に patch するヘルパ。"""
    return patch.object(
        ReadyForExtraction,
        "try_advance_from",
        new=AsyncMock(return_value=ready),
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
            _patch_try_advance_from(_fixed_ready()),
            patch(
                "app.analysis.extraction.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.extraction.tasks.ExtractionService") as mock_svc_cls,
            patch("app.analysis.extraction.tasks.assess_content") as mock_assess,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=42)
            mock_assess.kiq = AsyncMock()
            await extract_content(trigger=_trigger(), ctx=mock_ctx)

        mock_assess.kiq.assert_awaited_once_with(
            AssessmentTrigger(extraction_id=42),
        )

    @pytest.mark.asyncio
    async def test_noise_or_race_loss_does_not_chain(self) -> None:
        """Service が None を返したら chain しない (noise 勝者 / race 敗北を吸収)。"""
        from app.analysis.extraction.tasks import extract_content

        mock_ctx = _make_ctx(extractor=_make_provider_fake())

        with (
            _patch_try_advance_from(_fixed_ready()),
            patch(
                "app.analysis.extraction.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.extraction.tasks.ExtractionService") as mock_svc_cls,
            patch("app.analysis.extraction.tasks.assess_content") as mock_assess,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=None)
            mock_assess.kiq = AsyncMock()
            await extract_content(trigger=_trigger(), ctx=mock_ctx)

        mock_assess.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_precondition_not_met_skips_and_does_not_call_service(self) -> None:
        """try_advance_from が None を返したら skip log + return、Service は呼ばない。

        案 3: precondition (article 既消滅 / 既処理 / 本文 oversized) の
        判定は Stage 3 task 冒頭で Ready 自構築時に行われ、未充足なら
        AI quota / Service を消費せず短絡する。
        """
        from app.analysis.extraction.tasks import extract_content

        mock_ctx = _make_ctx(extractor=_make_provider_fake())

        with (
            _patch_try_advance_from(None),
            patch(
                "app.analysis.extraction.tasks._build_limiters",
                return_value=(None, None),
            ) as mock_limiters,
            patch("app.analysis.extraction.tasks.ExtractionService") as mock_svc_cls,
            patch("app.analysis.extraction.tasks.assess_content") as mock_assess,
        ):
            mock_assess.kiq = AsyncMock()
            await extract_content(trigger=_trigger(), ctx=mock_ctx)

        # Service / rate limit / chain firing いずれも触らない
        mock_svc_cls.assert_not_called()
        mock_limiters.assert_not_called()
        mock_assess.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limited_records_audit_and_returns(self) -> None:
        """RateLimited は INLINE_RETRY=False、即 audit + return (PR3.5-c)。"""
        from app.analysis.extraction.tasks import extract_content

        mock_ctx = _make_ctx(
            extractor=_make_provider_fake(), retry_count=0, max_retries=1
        )

        with (
            _patch_try_advance_from(_fixed_ready()),
            patch(
                "app.analysis.extraction.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.extraction.tasks.ExtractionService") as mock_svc_cls,
            patch(
                "app.analysis.extraction.tasks._record_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=AIProviderRateLimitedError("429"),
            )
            await extract_content(trigger=_trigger(), ctx=mock_ctx)
        mock_audit.assert_awaited_once()
        # outcome_code 引数は廃止 (recording.py で内部導出)。exc が渡るのみ。
        assert isinstance(
            mock_audit.await_args.kwargs["exc"], AIProviderRateLimitedError
        )
