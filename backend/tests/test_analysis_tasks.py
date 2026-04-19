"""分析タスク (extract_content / classify_content) のテスト。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis import RateLimitError


def _make_ctx(
    retry_count: int = 0,
    max_retries: int = 0,
) -> MagicMock:
    """state.session_factory と labels を持つ taskiq Context のモックを作成する。"""
    ctx = MagicMock()
    ctx.state.session_factory = MagicMock()
    ctx.message.labels = {
        "retry_count": retry_count,
        "max_retries": max_retries,
    }
    return ctx


def _patch_provider() -> MagicMock:
    """extractor/classifier 用のモックを返す。"""
    mock = MagicMock()
    mock.MODEL = "test-model"
    mock.RPM = 50
    mock.RPD = 1500
    return mock


# ---------------------------------------------------------------------------
# extract_content
# ---------------------------------------------------------------------------


class TestExtractContent:
    @pytest.mark.asyncio
    async def test_already_exists_chains_classify(self) -> None:
        from app.analysis.tasks import extract_content

        mock_ctx = _make_ctx()
        mock_result = MagicMock(status="already_exists")

        with (
            patch(
                "app.analysis.tasks.get_extractor",
                return_value=_patch_provider(),
            ),
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
                return_value=mock_result,
            )
            mock_classify.kiq = AsyncMock()
            await extract_content(article_id=1, ctx=mock_ctx)

        mock_classify.kiq.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_created_chains_classify(self) -> None:
        from app.analysis.tasks import extract_content

        mock_ctx = _make_ctx()
        mock_result = MagicMock(status="created")

        with (
            patch(
                "app.analysis.tasks.get_extractor",
                return_value=_patch_provider(),
            ),
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
                return_value=mock_result,
            )
            mock_classify.kiq = AsyncMock()
            await extract_content(article_id=1, ctx=mock_ctx)

        mock_classify.kiq.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_skipped_does_not_chain(self) -> None:
        from app.analysis.tasks import extract_content

        mock_ctx = _make_ctx()
        mock_result = MagicMock(status="skipped")

        with (
            patch(
                "app.analysis.tasks.get_extractor",
                return_value=_patch_provider(),
            ),
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
                return_value=mock_result,
            )
            mock_classify.kiq = AsyncMock()
            await extract_content(article_id=1, ctx=mock_ctx)

        mock_classify.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_raises_for_retry(self) -> None:
        from app.analysis.tasks import extract_content

        mock_ctx = _make_ctx(retry_count=0, max_retries=2)

        with (
            patch(
                "app.analysis.tasks.get_extractor",
                return_value=_patch_provider(),
            ),
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

        mock_ctx = _make_ctx(retry_count=2, max_retries=2)

        with (
            patch(
                "app.analysis.tasks.get_extractor",
                return_value=_patch_provider(),
            ),
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
        from app.analysis.tasks import classify_content

        mock_ctx = _make_ctx()
        mock_result = MagicMock(status="classified")

        with (
            patch(
                "app.analysis.tasks.get_classifier",
                return_value=_patch_provider(),
            ),
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
        from app.analysis.tasks import classify_content

        mock_ctx = _make_ctx()
        mock_result = MagicMock(status="already_classified")

        with (
            patch(
                "app.analysis.tasks.get_classifier",
                return_value=_patch_provider(),
            ),
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
        from app.analysis.tasks import classify_content

        mock_ctx = _make_ctx()
        mock_result = MagicMock(status="skipped")

        with (
            patch(
                "app.analysis.tasks.get_classifier",
                return_value=_patch_provider(),
            ),
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

        mock_ctx = _make_ctx(retry_count=0, max_retries=2)

        with (
            patch(
                "app.analysis.tasks.get_classifier",
                return_value=_patch_provider(),
            ),
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

        mock_ctx = _make_ctx(retry_count=2, max_retries=2)

        with (
            patch(
                "app.analysis.tasks.get_classifier",
                return_value=_patch_provider(),
            ),
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
