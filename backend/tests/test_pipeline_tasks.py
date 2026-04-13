"""Tests for the pipeline chain tasks."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.embedding import EmbeddingError
from app.models.article_analysis import ArticleAnalysis
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource
from app.services.ai_analyzer import AnalysisError, RateLimitError
from app.services.content_extractor import PermanentFetchError, TemporaryFetchError
from app.services.news_fetcher import FetchResult, SourceFetchResult


def _mock_session_context(mock_session: AsyncMock) -> MagicMock:
    """Create a mock async context manager that yields mock_session."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _make_ctx(
    mock_engine: MagicMock | None = None,
    retry_count: int = 0,
    max_retries: int = 0,
) -> MagicMock:
    """Create a mock taskiq Context with state.engine and message labels."""
    ctx = MagicMock()
    ctx.state.engine = mock_engine or MagicMock()
    ctx.message.labels = {
        "retry_count": retry_count,
        "max_retries": max_retries,
    }
    return ctx


def _make_article(
    article_id: int = 1,
    original_content: str | None = None,
    skip_content_fetch: bool = False,
) -> MagicMock:
    """Create a mock NewsArticle."""
    article = MagicMock(spec=NewsArticle)
    article.id = article_id
    article.original_url = f"https://example.com/article-{article_id}"
    article.original_title = f"Article {article_id}"
    article.original_content = original_content
    article.skip_content_fetch = skip_content_fetch
    return article


# ---------------------------------------------------------------------------
# fetch_metadata
# ---------------------------------------------------------------------------


class TestFetchMetadata:
    @pytest.mark.asyncio
    async def test_fetches_and_dispatches(self) -> None:
        from app.tasks.pipeline_tasks import fetch_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        source = MagicMock(spec=NewsSource)
        source.id = 1
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [source]
        mock_session.execute = AsyncMock(return_value=mock_result)

        fetch_result = FetchResult(
            new_count=5,
            skipped_count=2,
            error_count=0,
            source_results=[
                SourceFetchResult(
                    source_id=1,
                    success=True,
                    new_count=5,
                    skipped_count=2,
                )
            ],
        )

        with (
            patch(
                "app.tasks.pipeline_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.pipeline_tasks.fetch_news_for_sources",
                new_callable=AsyncMock,
                return_value=fetch_result,
            ) as mock_fetch,
            patch(
                "app.tasks.pipeline_tasks.dispatch_pending",
            ) as mock_dispatch,
        ):
            mock_dispatch.kiq = AsyncMock()
            result = await fetch_metadata(ctx=mock_ctx)

        assert result["sources_count"] == 1
        assert result["fetch_new"] == 5
        mock_fetch.assert_called_once()
        mock_dispatch.kiq.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_when_no_sources(self) -> None:
        from app.tasks.pipeline_tasks import fetch_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch(
            "app.tasks.pipeline_tasks.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ):
            result = await fetch_metadata(ctx=mock_ctx)

        assert result["sources_count"] == 0


# ---------------------------------------------------------------------------
# dispatch_pending
# ---------------------------------------------------------------------------


class TestDispatchPending:
    @pytest.mark.asyncio
    async def test_dispatches_all_three_queries(self) -> None:
        from app.tasks.pipeline_tasks import dispatch_pending

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        # Q1: 2 need content, Q2: 1 needs analysis, Q3: 1 needs embedding
        q1 = MagicMock()
        q1.scalars.return_value.all.return_value = [10, 11]
        q2 = MagicMock()
        q2.scalars.return_value.all.return_value = [20]
        q3 = MagicMock()
        q3.scalars.return_value.all.return_value = [30]
        mock_session.execute = AsyncMock(side_effect=[q1, q2, q3])

        with (
            patch(
                "app.tasks.pipeline_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch("app.tasks.pipeline_tasks.fetch_content") as mock_fc,
            patch("app.tasks.pipeline_tasks.analyze_article") as mock_aa,
            patch("app.tasks.pipeline_tasks.generate_embedding") as mock_ge,
        ):
            mock_fc.kiq = AsyncMock()
            mock_aa.kiq = AsyncMock()
            mock_ge.kiq = AsyncMock()

            result = await dispatch_pending(ctx=mock_ctx)

        assert result["fetch_content"] == 2
        assert result["analyze_article"] == 1
        assert result["generate_embedding"] == 1
        assert mock_fc.kiq.call_count == 2
        assert mock_aa.kiq.call_count == 1
        assert mock_ge.kiq.call_count == 1


# ---------------------------------------------------------------------------
# fetch_content
# ---------------------------------------------------------------------------


class TestFetchContent:
    @pytest.mark.asyncio
    async def test_idempotency_guard(self) -> None:
        """Already-fetched article should return immediately."""
        from app.tasks.pipeline_tasks import fetch_content

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        article = _make_article(original_content="already fetched")
        mock_session.get = AsyncMock(return_value=article)

        with patch(
            "app.tasks.pipeline_tasks.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ):
            await fetch_content(article_id=1, ctx=mock_ctx)

        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_permanent_error_sets_skip(self) -> None:
        from app.tasks.pipeline_tasks import fetch_content

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        article = _make_article()
        mock_session.get = AsyncMock(return_value=article)

        with (
            patch(
                "app.tasks.pipeline_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.pipeline_tasks.extract_content",
                new_callable=AsyncMock,
                side_effect=PermanentFetchError("HTTP 403"),
            ),
            patch("app.tasks.pipeline_tasks.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            await fetch_content(article_id=1, ctx=mock_ctx)

        assert article.skip_content_fetch is True
        mock_session.commit.assert_called()

    @pytest.mark.asyncio
    async def test_temporary_error_raises_for_retry(self) -> None:
        from app.tasks.pipeline_tasks import fetch_content

        mock_session = AsyncMock()
        mock_ctx = _make_ctx(retry_count=0, max_retries=3)

        article = _make_article()
        mock_session.get = AsyncMock(return_value=article)

        with (
            patch(
                "app.tasks.pipeline_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.pipeline_tasks.extract_content",
                new_callable=AsyncMock,
                side_effect=TemporaryFetchError("HTTP 500"),
            ),
            patch("app.tasks.pipeline_tasks.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            with pytest.raises(TemporaryFetchError):
                await fetch_content(article_id=1, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_temporary_error_last_attempt_sets_skip(self) -> None:
        from app.tasks.pipeline_tasks import fetch_content

        mock_session = AsyncMock()
        mock_ctx = _make_ctx(retry_count=3, max_retries=3)

        article = _make_article()
        mock_session.get = AsyncMock(return_value=article)

        with (
            patch(
                "app.tasks.pipeline_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.pipeline_tasks.extract_content",
                new_callable=AsyncMock,
                side_effect=TemporaryFetchError("HTTP 500"),
            ),
            patch("app.tasks.pipeline_tasks.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            await fetch_content(article_id=1, ctx=mock_ctx)

        assert article.skip_content_fetch is True
        mock_session.commit.assert_called()

    @pytest.mark.asyncio
    async def test_quality_gate_none_sets_skip(self) -> None:
        from app.tasks.pipeline_tasks import fetch_content

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        article = _make_article()
        mock_session.get = AsyncMock(return_value=article)

        with (
            patch(
                "app.tasks.pipeline_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.pipeline_tasks.extract_content",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("app.tasks.pipeline_tasks.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            await fetch_content(article_id=1, ctx=mock_ctx)

        assert article.skip_content_fetch is True

    @pytest.mark.asyncio
    async def test_success_chains_analyze(self) -> None:
        from app.tasks.pipeline_tasks import fetch_content

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        article = _make_article()
        mock_session.get = AsyncMock(return_value=article)

        with (
            patch(
                "app.tasks.pipeline_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.pipeline_tasks.extract_content",
                new_callable=AsyncMock,
                return_value="Full article content here.",
            ),
            patch("app.tasks.pipeline_tasks.httpx.AsyncClient") as mock_client_cls,
            patch("app.tasks.pipeline_tasks.analyze_article") as mock_analyze,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_analyze.kiq = AsyncMock()
            await fetch_content(article_id=1, ctx=mock_ctx)

        assert article.original_content == "Full article content here."
        mock_analyze.kiq.assert_called_once_with(1)


# ---------------------------------------------------------------------------
# analyze_article
# ---------------------------------------------------------------------------


class TestAnalyzeArticle:
    @pytest.mark.asyncio
    async def test_idempotency_guard(self) -> None:
        from app.tasks.pipeline_tasks import analyze_article

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        existing_analysis = MagicMock(spec=ArticleAnalysis)
        mock_exec = MagicMock()
        mock_exec.scalar_one_or_none.return_value = existing_analysis
        mock_session.execute = AsyncMock(return_value=mock_exec)

        with patch(
            "app.tasks.pipeline_tasks.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ):
            await analyze_article(article_id=1, ctx=mock_ctx)

        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_safety_block_marks_article(self) -> None:
        from app.tasks.pipeline_tasks import analyze_article

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        mock_exec = MagicMock()
        mock_exec.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_exec)

        article = _make_article(original_content="some content")
        mock_session.get = AsyncMock(return_value=article)

        with (
            patch(
                "app.tasks.pipeline_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.pipeline_tasks._analyze_article_svc",
                new_callable=AsyncMock,
                side_effect=AnalysisError("Safety block"),
            ),
        ):
            await analyze_article(article_id=1, ctx=mock_ctx)

        assert article.original_content is None
        assert article.skip_content_fetch is True

    @pytest.mark.asyncio
    async def test_rate_limit_raises_for_retry(self) -> None:
        from app.tasks.pipeline_tasks import analyze_article

        mock_session = AsyncMock()
        mock_ctx = _make_ctx(retry_count=0, max_retries=2)

        mock_exec = MagicMock()
        mock_exec.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_exec)

        article = _make_article(original_content="some content")
        mock_session.get = AsyncMock(return_value=article)

        with (
            patch(
                "app.tasks.pipeline_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.pipeline_tasks._analyze_article_svc",
                new_callable=AsyncMock,
                side_effect=RateLimitError("429"),
            ),
        ):
            with pytest.raises(RateLimitError):
                await analyze_article(article_id=1, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_success_chains_embedding(self) -> None:
        from app.tasks.pipeline_tasks import analyze_article

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        mock_exec = MagicMock()
        mock_exec.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_exec)

        article = _make_article(original_content="some content")
        mock_session.get = AsyncMock(return_value=article)

        analysis = MagicMock(spec=ArticleAnalysis)

        with (
            patch(
                "app.tasks.pipeline_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.pipeline_tasks._analyze_article_svc",
                new_callable=AsyncMock,
                return_value=analysis,
            ),
            patch("app.tasks.pipeline_tasks.generate_embedding") as mock_embed,
        ):
            mock_embed.kiq = AsyncMock()
            await analyze_article(article_id=1, ctx=mock_ctx)

        mock_embed.kiq.assert_called_once_with(1)


# ---------------------------------------------------------------------------
# generate_embedding
# ---------------------------------------------------------------------------


class TestGenerateEmbedding:
    @pytest.mark.asyncio
    async def test_idempotency_guard(self) -> None:
        from app.tasks.pipeline_tasks import generate_embedding

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        analysis = MagicMock(spec=ArticleAnalysis)
        analysis.embedding = [0.1, 0.2]  # already has embedding
        mock_exec = MagicMock()
        mock_exec.scalar_one_or_none.return_value = analysis
        mock_session.execute = AsyncMock(return_value=mock_exec)

        with patch(
            "app.tasks.pipeline_tasks.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ):
            await generate_embedding(article_id=1, ctx=mock_ctx)

        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_embeds_and_commits(self) -> None:
        from app.tasks.pipeline_tasks import generate_embedding

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        analysis = MagicMock(spec=ArticleAnalysis)
        analysis.embedding = None
        analysis.news_article_id = 1
        mock_exec = MagicMock()
        mock_exec.scalar_one_or_none.return_value = analysis
        mock_session.execute = AsyncMock(return_value=mock_exec)

        article = _make_article(original_content="content")
        mock_session.get = AsyncMock(return_value=article)

        mock_embedder = AsyncMock()
        mock_embedder.MODEL = "gemini-embedding-001"
        mock_embedder.embed_document = AsyncMock(return_value=[0.1] * 768)

        with (
            patch(
                "app.tasks.pipeline_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.pipeline_tasks.get_embedder",
                return_value=mock_embedder,
            ),
        ):
            await generate_embedding(article_id=1, ctx=mock_ctx)

        assert analysis.embedding == [0.1] * 768
        assert analysis.embedding_model == "gemini-embedding-001"
        mock_session.commit.assert_called()

    @pytest.mark.asyncio
    async def test_embedding_error_raises(self) -> None:
        from app.tasks.pipeline_tasks import generate_embedding

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        analysis = MagicMock(spec=ArticleAnalysis)
        analysis.embedding = None
        analysis.news_article_id = 1
        mock_exec = MagicMock()
        mock_exec.scalar_one_or_none.return_value = analysis
        mock_session.execute = AsyncMock(return_value=mock_exec)

        article = _make_article(original_content="content")
        mock_session.get = AsyncMock(return_value=article)

        mock_embedder = AsyncMock()
        mock_embedder.embed_document = AsyncMock(side_effect=EmbeddingError("API down"))

        with (
            patch(
                "app.tasks.pipeline_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.pipeline_tasks.get_embedder",
                return_value=mock_embedder,
            ),
        ):
            with pytest.raises(EmbeddingError):
                await generate_embedding(article_id=1, ctx=mock_ctx)
