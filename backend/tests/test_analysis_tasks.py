"""分析タスク (extract_content / assess_content) のテスト。

Phase 1 / 2 / 3 リファクタ後 (typed-pipeline-preconditions.md):
- extract_content は ``ReadyForExtraction`` を受け取り、ExtractedOutcome なら
  ``ReadyForAssessment`` を構築して chain
- assess_content は ``ReadyForAssessment`` を受け取り、InScopeOutcome
  なら ``ReadyForEmbedding`` を構築して chain
- Skipped / AlreadyClassified / AlreadyEmbedded Outcome は廃止 (Ready の
  ``try_advance_from`` で代替)

注 (PR3.5-d.0): Stage 4 命名統一に伴い旧 ``classify_content`` task は
deprecated alias として残置。本 PR でも本ファイルでは新名 ``assess_content``
を中心に検証する。alias 経由の動作は ``test_assess_content_alias.py`` で別途
検証する。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.errors import AIProviderRateLimitedError, RateLimitError
from app.analysis.extraction.domain.ready import ReadyForExtraction


def _make_provider_fake() -> MagicMock:
    """extractor/classifier 用のスタブ。MODEL/RPM/RPD を持つ。"""
    fake = MagicMock()
    fake.MODEL = "test-model"
    fake.RPM = 50
    fake.RPD = 1500
    return fake


def _make_ctx(
    *,
    extractor: MagicMock | None = None,
    classifier: MagicMock | None = None,
    retry_count: int = 0,
    max_retries: int = 0,
) -> MagicMock:
    """taskiq Context モック。"""
    ctx = MagicMock()
    ctx.state = SimpleNamespace(session_factory=MagicMock())
    if extractor is not None:
        ctx.state.extractor = extractor
    if classifier is not None:
        ctx.state.classifier = classifier
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


def _make_ready(extraction_id: int = 2) -> ReadyForAssessment:
    return ReadyForAssessment(
        extraction_id=extraction_id,
        translated_title="title",
        summary="summary",
    )


def _make_ready_emb(analysis_id: int = 100) -> ReadyForEmbedding:
    return ReadyForEmbedding(
        analysis_id=analysis_id,
        text_for_embedding="title\nsummary",
    )


# ---------------------------------------------------------------------------
# extract_content
# ---------------------------------------------------------------------------


class TestExtractContent:
    @pytest.mark.asyncio
    async def test_chains_assess_with_ready_when_advance_succeeds(self) -> None:
        """ExtractedOutcome → Ready 構築 → assess_content.kiq(ready) で chain。"""
        from app.analysis.extraction.service import ExtractedOutcome
        from app.analysis.tasks import extract_content

        mock_ctx = _make_ctx(extractor=_make_provider_fake())
        mock_extraction = MagicMock()
        mock_extraction.id = 42
        mock_outcome = ExtractedOutcome(extraction=mock_extraction)
        ready_assess = _make_ready(extraction_id=42)

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.tasks.ExtractionService") as mock_svc_cls,
            patch(
                "app.analysis.tasks.ReadyForAssessment.try_advance_from",
                new=AsyncMock(return_value=ready_assess),
            ),
            patch("app.analysis.tasks.assess_content") as mock_assess,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=mock_outcome)
            mock_assess.kiq = AsyncMock()
            await extract_content(ready=_make_ready_extraction(), ctx=mock_ctx)

        mock_assess.kiq.assert_awaited_once_with(ready_assess)

    @pytest.mark.asyncio
    async def test_does_not_chain_when_advance_returns_none(self) -> None:
        """precondition 未充足 (try_advance_from が None) なら chain しない。"""
        from app.analysis.extraction.service import ExtractedOutcome
        from app.analysis.tasks import extract_content

        mock_ctx = _make_ctx(extractor=_make_provider_fake())
        mock_outcome = ExtractedOutcome(extraction=MagicMock())

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.tasks.ExtractionService") as mock_svc_cls,
            patch(
                "app.analysis.tasks.ReadyForAssessment.try_advance_from",
                new=AsyncMock(return_value=None),
            ),
            patch("app.analysis.tasks.assess_content") as mock_assess,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=mock_outcome)
            mock_assess.kiq = AsyncMock()
            await extract_content(ready=_make_ready_extraction(), ctx=mock_ctx)

        mock_assess.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_noise_outcome_does_not_chain(self) -> None:
        """NoiseOutcome は chain しない (Service 側で extraction_noises に永続化済)。"""
        from app.analysis.extraction.service import NoiseOutcome
        from app.analysis.tasks import extract_content

        mock_ctx = _make_ctx(extractor=_make_provider_fake())

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.tasks.ExtractionService") as mock_svc_cls,
            patch("app.analysis.tasks.assess_content") as mock_assess,
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
        from app.analysis.tasks import extract_content

        mock_ctx = _make_ctx(
            extractor=_make_provider_fake(), retry_count=0, max_retries=1
        )

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.tasks.ExtractionService") as mock_svc_cls,
            patch(
                "app.analysis.tasks.record_extraction_failure",
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


# ---------------------------------------------------------------------------
# assess_content (Stage 4)
# ---------------------------------------------------------------------------


class TestAssessContent:
    @pytest.mark.asyncio
    async def test_in_scope_chains_embedding_with_ready(self) -> None:
        """InScopeOutcome → ReadyForEmbedding を構築して embedding chain。"""
        from app.analysis.assessment.service import InScopeOutcome
        from app.analysis.tasks import assess_content

        mock_ctx = _make_ctx(classifier=_make_provider_fake())
        mock_result = InScopeOutcome(assessment=MagicMock())
        ready = _make_ready(extraction_id=2)
        ready_emb = _make_ready_emb(analysis_id=100)

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.tasks.ReadyForEmbedding.try_advance_from",
                new=AsyncMock(return_value=ready_emb),
            ),
            patch("app.analysis.tasks.generate_embedding") as mock_embed,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=mock_result)
            mock_embed.kiq = AsyncMock()
            await assess_content(ready=ready, ctx=mock_ctx)

        mock_embed.kiq.assert_awaited_once_with(ready_emb)

    @pytest.mark.asyncio
    async def test_in_scope_does_not_chain_when_advance_returns_none(self) -> None:
        """InScopeOutcome でも embedding precondition 未充足なら chain しない。"""
        from app.analysis.assessment.service import InScopeOutcome
        from app.analysis.tasks import assess_content

        mock_ctx = _make_ctx(classifier=_make_provider_fake())
        mock_result = InScopeOutcome(assessment=MagicMock())
        ready = _make_ready(extraction_id=2)

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.tasks.ReadyForEmbedding.try_advance_from",
                new=AsyncMock(return_value=None),
            ),
            patch("app.analysis.tasks.generate_embedding") as mock_embed,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=mock_result)
            mock_embed.kiq = AsyncMock()
            await assess_content(ready=ready, ctx=mock_ctx)

        mock_embed.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_out_of_scope_does_not_chain(self) -> None:
        """OutOfScopeOutcome は embedding に進まない。"""
        from app.analysis.assessment.service import OutOfScopeOutcome
        from app.analysis.tasks import assess_content

        mock_ctx = _make_ctx(classifier=_make_provider_fake())
        mock_result = OutOfScopeOutcome(assessment=MagicMock())
        ready = _make_ready(extraction_id=2)

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.tasks.AssessmentService") as mock_svc_cls,
            patch("app.analysis.tasks.generate_embedding") as mock_embed,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=mock_result)
            mock_embed.kiq = AsyncMock()
            await assess_content(ready=ready, ctx=mock_ctx)

        mock_embed.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_raises_for_retry(self) -> None:
        from app.analysis.tasks import assess_content

        mock_ctx = _make_ctx(
            classifier=_make_provider_fake(), retry_count=0, max_retries=2
        )
        ready = _make_ready()

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.tasks.AssessmentService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            with pytest.raises(RateLimitError):
                await assess_content(ready=ready, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_rate_limit_last_attempt_returns(self) -> None:
        """assess_content は最終試行で例外を送出せず return する。"""
        from app.analysis.tasks import assess_content

        mock_ctx = _make_ctx(
            classifier=_make_provider_fake(), retry_count=2, max_retries=2
        )
        ready = _make_ready()

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.tasks.AssessmentService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            await assess_content(ready=ready, ctx=mock_ctx)


# ---------------------------------------------------------------------------
# _build_limiters: 役割別キー独立性 (構造的保証)
# ---------------------------------------------------------------------------


class TestBuildLimitersKeyIsolation:
    """同一モデルを異なる役割で使ってもレート制御カウンターが共有されないこと。"""

    def test_keys_isolated_by_role(self) -> None:
        """extract と classify で同じモデルでも Redis キーが独立する。"""
        from app.analysis.tasks import _build_limiters

        with patch("app.redis.get_redis", return_value=MagicMock()):
            extract_rpm, extract_rpd = _build_limiters(
                "extract", "shared-model", 100, 1500
            )
            classify_rpm, classify_rpd = _build_limiters(
                "classify", "shared-model", 100, 1500
            )

        assert extract_rpm is not None
        assert extract_rpd is not None
        assert classify_rpm is not None
        assert classify_rpd is not None

        assert extract_rpm._key != classify_rpm._key
        assert extract_rpd._key != classify_rpd._key
        assert "extract" in extract_rpd._key
        assert "classify" in classify_rpd._key

    def test_embed_role_key_distinct(self) -> None:
        """embed 役割のキーも他と独立する。"""
        from app.analysis.tasks import _build_limiters

        with patch("app.redis.get_redis", return_value=MagicMock()):
            extract_rpm, _ = _build_limiters("extract", "m", 60, None)
            embed_rpm, _ = _build_limiters("embed", "m", 60, None)

        assert extract_rpm is not None
        assert embed_rpm is not None
        assert extract_rpm._key != embed_rpm._key
        assert "embed" in embed_rpm._key
