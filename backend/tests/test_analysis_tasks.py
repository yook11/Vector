"""分析タスク (extract_content / classify_content) のテスト。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.errors import RateLimitError


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
    """taskiq Context モック。

    composition root が attach する `state.extractor` / `state.classifier` を
    任意に注入できる。`session_factory` と `message.labels` も合わせて備える。
    """
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


# ---------------------------------------------------------------------------
# extract_content
# ---------------------------------------------------------------------------


class TestExtractContent:
    @pytest.mark.asyncio
    async def test_extraction_returned_chains_classify(self) -> None:
        """Extraction が返れば (新規 or 冪等ヒット) 後続 classify に chain する。"""
        from app.analysis.tasks import extract_content

        mock_ctx = _make_ctx(extractor=_make_provider_fake())
        mock_extraction = MagicMock()  # Extraction entity のスタンド

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.analysis.tasks.ExtractionService",
            ) as mock_svc_cls,
            patch(
                "app.analysis.tasks.classify_content",
            ) as mock_classify,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=mock_extraction,
            )
            mock_classify.kiq = AsyncMock()
            await extract_content(article_id=1, ctx=mock_ctx)

        mock_classify.kiq.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_none_does_not_chain(self) -> None:
        """None が返れば (記事不在 / InvalidInput) chain しない。"""
        from app.analysis.tasks import extract_content

        mock_ctx = _make_ctx(extractor=_make_provider_fake())

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.analysis.tasks.ExtractionService",
            ) as mock_svc_cls,
            patch(
                "app.analysis.tasks.classify_content",
            ) as mock_classify,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=None,
            )
            mock_classify.kiq = AsyncMock()
            await extract_content(article_id=1, ctx=mock_ctx)

        mock_classify.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_raises_for_retry(self) -> None:
        from app.analysis.tasks import extract_content

        mock_ctx = _make_ctx(
            extractor=_make_provider_fake(), retry_count=0, max_retries=2
        )

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.analysis.tasks.ExtractionService",
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            with pytest.raises(RateLimitError):
                await extract_content(article_id=1, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_rate_limit_last_attempt_returns(self) -> None:
        """最終試行では例外を送出せず return する。"""
        from app.analysis.tasks import extract_content

        mock_ctx = _make_ctx(
            extractor=_make_provider_fake(), retry_count=2, max_retries=2
        )

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.analysis.tasks.ExtractionService",
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            # 最終試行では例外を送出しない
            await extract_content(article_id=1, ctx=mock_ctx)


# ---------------------------------------------------------------------------
# classify_content
# ---------------------------------------------------------------------------


class TestClassifyContent:
    @pytest.mark.asyncio
    async def test_classified_chains_embedding(self) -> None:
        from app.analysis.classification.service import ClassifiedOutcome
        from app.analysis.tasks import classify_content

        mock_ctx = _make_ctx(classifier=_make_provider_fake())
        mock_result = ClassifiedOutcome(analysis=MagicMock())

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.analysis.tasks.ClassificationService",
            ) as mock_svc_cls,
            patch(
                "app.analysis.tasks.generate_embedding",
            ) as mock_embed,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=mock_result,
            )
            mock_embed.kiq = AsyncMock()
            await classify_content(article_id=1, ctx=mock_ctx)

        mock_embed.kiq.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_already_classified_chains_embedding(self) -> None:
        from app.analysis.classification.service import AlreadyClassifiedOutcome
        from app.analysis.tasks import classify_content

        mock_ctx = _make_ctx(classifier=_make_provider_fake())
        mock_result = AlreadyClassifiedOutcome(analysis=MagicMock())

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.analysis.tasks.ClassificationService",
            ) as mock_svc_cls,
            patch(
                "app.analysis.tasks.generate_embedding",
            ) as mock_embed,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=mock_result,
            )
            mock_embed.kiq = AsyncMock()
            await classify_content(article_id=1, ctx=mock_ctx)

        mock_embed.kiq.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_skipped_does_not_chain(self) -> None:
        from app.analysis.classification.service import SkippedOutcome
        from app.analysis.tasks import classify_content

        mock_ctx = _make_ctx(classifier=_make_provider_fake())
        mock_result = SkippedOutcome(reason="extraction_not_found")

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.analysis.tasks.ClassificationService",
            ) as mock_svc_cls,
            patch(
                "app.analysis.tasks.generate_embedding",
            ) as mock_embed,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                return_value=mock_result,
            )
            mock_embed.kiq = AsyncMock()
            await classify_content(article_id=1, ctx=mock_ctx)

        mock_embed.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_raises_for_retry(self) -> None:
        from app.analysis.tasks import classify_content

        mock_ctx = _make_ctx(
            classifier=_make_provider_fake(), retry_count=0, max_retries=2
        )

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.analysis.tasks.ClassificationService",
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            with pytest.raises(RateLimitError):
                await classify_content(article_id=1, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_rate_limit_last_attempt_returns(self) -> None:
        """classify_content は最終試行で例外を送出せず return する。"""
        from app.analysis.tasks import classify_content

        mock_ctx = _make_ctx(
            classifier=_make_provider_fake(), retry_count=2, max_retries=2
        )

        with (
            patch(
                "app.analysis.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.analysis.tasks.ClassificationService",
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            await classify_content(article_id=1, ctx=mock_ctx)


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
