"""Tests for the AI analyzer service."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.analysis import ArticleAnalysis, ImpactLevel
from app.models.associations import ArticleKeyword
from app.models.category import Category
from app.models.keyword import Keyword
from app.models.news import NewsArticle
from app.models.news_source import NewsSource
from app.services.ai_analyzer import (
    AnalysisData,
    AnalysisError,
    BaseAnalyzer,
    analyze_article,
    analyze_articles,
    get_analyzer,
)
from app.services.gemini_analyzer import GeminiAnalyzer

# --- Helpers ---


def _make_gemini_response(
    title_ja: str = "量子コンピューティングの新たなブレイクスルー",
    summary_ja: str = (
        "MITが新手法を発表。\n半導体業界に大きな影響。\n投資家にとっては追い風。"
    ),
    impact_level: str = "high",
    reasoning: str | None = "技術的に重要な進展であり市場に好影響",
    keywords: list[str] | None = None,
) -> str:
    """Create a valid JSON string mimicking Gemini API response."""
    data: dict = {
        "title_ja": title_ja,
        "summary_ja": summary_ja,
        "impact_level": impact_level,
        "reasoning": reasoning,
    }
    if keywords is not None:
        data["keywords"] = keywords
    return json.dumps(data, ensure_ascii=False)


def _create_analyzer() -> GeminiAnalyzer:
    """Create a GeminiAnalyzer with mocked settings."""
    with patch("app.services.gemini_analyzer.settings") as mock_gs:
        mock_gs.gemini_api_key = "test-key"
        return GeminiAnalyzer()


def _create_article(source: NewsSource) -> NewsArticle:
    """Create a NewsArticle for testing."""
    return NewsArticle(
        original_title="Quantum Breakthrough",
        original_url="https://example.com/quantum",
        news_source_id=source.id,
        published_at=datetime.now(UTC),
    )


# --- A. Factory tests ---


def test_get_analyzer_returns_gemini_by_default() -> None:
    with patch("app.services.ai_analyzer.settings") as mock_settings:
        mock_settings.ai_provider = "gemini"
        with patch("app.services.gemini_analyzer.settings") as mock_gs:
            mock_gs.gemini_api_key = "test-key"
            analyzer = get_analyzer()
    assert isinstance(analyzer, GeminiAnalyzer)
    assert analyzer.provider_name == "gemini"


def test_get_analyzer_raises_for_unsupported_provider() -> None:
    with patch("app.services.ai_analyzer.settings") as mock_settings:
        mock_settings.ai_provider = "unknown"
        with pytest.raises(ValueError, match="Unsupported AI provider"):
            get_analyzer()


# --- B. GeminiAnalyzer._parse_response tests ---


def test_parse_response_valid_json() -> None:
    analyzer = _create_analyzer()
    raw = _make_gemini_response()
    result = analyzer._parse_response(raw)

    assert isinstance(result, AnalysisData)
    assert result.impact_level == ImpactLevel.HIGH
    assert result.title == "量子コンピューティングの新たなブレイクスルー"


def test_parse_response_strips_markdown_fences() -> None:
    analyzer = _create_analyzer()
    raw = "```json\n" + _make_gemini_response() + "\n```"
    result = analyzer._parse_response(raw)
    assert result.impact_level == ImpactLevel.HIGH


def test_parse_response_invalid_json_raises_error() -> None:
    analyzer = _create_analyzer()
    with pytest.raises(AnalysisError, match="Failed to parse"):
        analyzer._parse_response("this is not json")


def test_parse_response_invalid_impact_level_raises_error() -> None:
    analyzer = _create_analyzer()
    raw = _make_gemini_response(impact_level="extreme")
    with pytest.raises(AnalysisError, match="Invalid"):
        analyzer._parse_response(raw)


# --- C. GeminiAnalyzer._call_with_retry tests ---


async def test_call_with_retry_succeeds_on_first_attempt() -> None:
    analyzer = _create_analyzer()

    mock_response = MagicMock()
    mock_response.text = _make_gemini_response()

    analyzer._client = MagicMock()
    analyzer._client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    result = await analyzer._call_with_retry("test prompt")
    assert result == mock_response.text
    analyzer._client.aio.models.generate_content.assert_called_once()


async def test_call_with_retry_retries_on_failure() -> None:
    analyzer = _create_analyzer()

    mock_response = MagicMock()
    mock_response.text = _make_gemini_response()

    analyzer._client = MagicMock()
    analyzer._client.aio.models.generate_content = AsyncMock(
        side_effect=[Exception("Connection timeout"), mock_response]
    )

    with patch(
        "app.services.gemini_analyzer.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        result = await analyzer._call_with_retry("test prompt")

    assert result == mock_response.text
    assert analyzer._client.aio.models.generate_content.call_count == 2


async def test_call_with_retry_raises_after_max_retries() -> None:
    analyzer = _create_analyzer()

    analyzer._client = MagicMock()
    analyzer._client.aio.models.generate_content = AsyncMock(
        side_effect=Exception("API unavailable")
    )

    with patch(
        "app.services.gemini_analyzer.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        with pytest.raises(AnalysisError, match="failed after 3 attempts"):
            await analyzer._call_with_retry("test prompt")

    assert analyzer._client.aio.models.generate_content.call_count == 3


# --- D. Orchestration tests (with DB) ---


async def test_analyze_article_creates_analysis(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    article = NewsArticle(
        original_title="Quantum Breakthrough",
        original_url="https://example.com/quantum",
        news_source_id=sample_source.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    mock_analyzer = MagicMock(spec=BaseAnalyzer)
    mock_analyzer.provider_name = "gemini"
    mock_analyzer.model_name = "gemini-2.0-flash"
    mock_analyzer.analyze = AsyncMock(
        return_value=AnalysisData(
            title="量子ブレイクスルー",
            summary="要約テスト",
            impact_level=ImpactLevel.HIGH,
            reasoning="テスト理由",
        )
    )

    result = await analyze_article(db_session, article, mock_analyzer)
    await db_session.commit()

    assert result is not None
    assert result.news_article_id == article.id
    assert result.impact_level == ImpactLevel.HIGH
    assert result.translated_title == "量子ブレイクスルー"
    assert result.ai_model == "gemini-2.0-flash"


async def test_analyze_article_skips_already_analyzed(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    article = NewsArticle(
        original_title="Old Article",
        original_url="https://example.com/old",
        news_source_id=sample_source.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    existing = ArticleAnalysis(
        news_article_id=article.id,
        translated_title="既存タイトル",
        summary="既存要約",
        impact_level=ImpactLevel.MEDIUM,
        reasoning="既存理由",
        ai_model="gemini-2.0-flash",
    )
    db_session.add(existing)
    await db_session.commit()

    mock_analyzer = MagicMock(spec=BaseAnalyzer)
    mock_analyzer.provider_name = "gemini"
    mock_analyzer.model_name = "gemini-2.0-flash"
    result = await analyze_article(db_session, article, mock_analyzer)

    assert result is None
    mock_analyzer.analyze.assert_not_called()


async def test_analyze_articles_batch(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    articles = []
    for i in range(3):
        a = NewsArticle(
            original_title=f"Article {i}",
            original_url=f"https://example.com/batch-{i}",
            news_source_id=sample_source.id,
            published_at=datetime.now(UTC),
        )
        db_session.add(a)
        articles.append(a)
    await db_session.commit()
    for a in articles:
        await db_session.refresh(a)

    mock_analyzer = MagicMock(spec=BaseAnalyzer)
    mock_analyzer.provider_name = "gemini"
    mock_analyzer.model_name = "gemini-2.0-flash"
    mock_analyzer.analyze = AsyncMock(
        return_value=AnalysisData(
            title="テスト",
            summary="要約",
            impact_level=ImpactLevel.MEDIUM,
            reasoning="理由",
        )
    )

    with patch(
        "app.services.ai_analyzer.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        result = await analyze_articles(db_session, articles, mock_analyzer)

    assert result.analyzed_count == 3
    assert result.skipped_count == 0
    assert result.error_count == 0


async def test_analyze_articles_handles_errors(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    articles = []
    for i in range(3):
        a = NewsArticle(
            original_title=f"Article {i}",
            original_url=f"https://example.com/err-{i}",
            news_source_id=sample_source.id,
            published_at=datetime.now(UTC),
        )
        db_session.add(a)
        articles.append(a)
    await db_session.commit()
    for a in articles:
        await db_session.refresh(a)

    mock_analyzer = MagicMock(spec=BaseAnalyzer)
    mock_analyzer.provider_name = "gemini"
    mock_analyzer.model_name = "gemini-2.0-flash"
    mock_analyzer.analyze = AsyncMock(
        side_effect=[
            AnalysisData(
                title="成功",
                summary="要約",
                impact_level=ImpactLevel.HIGH,
                reasoning="理由1",
            ),
            AnalysisError("API failed"),
            AnalysisData(
                title="成功2",
                summary="要約2",
                impact_level=ImpactLevel.LOW,
                reasoning="理由2",
            ),
        ]
    )

    with patch(
        "app.services.ai_analyzer.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        result = await analyze_articles(db_session, articles, mock_analyzer)

    assert result.analyzed_count == 2
    assert result.error_count == 1
    assert len(result.errors) == 1


async def test_analyze_articles_with_empty_list(
    db_session: AsyncSession,
) -> None:
    result = await analyze_articles(db_session, [])
    assert result.analyzed_count == 0
    assert result.skipped_count == 0
    assert result.error_count == 0


# --- E. Integration test (API response) ---


async def test_news_endpoint_includes_analysis(
    client,
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    article = NewsArticle(
        original_title="Test Article",
        original_url="https://example.com/integration-test",
        news_source_id=sample_source.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    analysis = ArticleAnalysis(
        news_article_id=article.id,
        translated_title="テスト記事",
        summary="テスト要約",
        impact_level=ImpactLevel.HIGH,
        reasoning="テスト理由",
        ai_model="gemini-2.0-flash",
    )
    db_session.add(analysis)
    await db_session.commit()

    response = await client.get(f"/api/v1/news/{article.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["analysis"] is not None
    assert data["analysis"]["translatedTitle"] == "テスト記事"
    assert data["analysis"]["impactLevel"] == "high"
    assert "aiModel" in data["analysis"]


# --- F. Keyword tagging tests ---


def test_parse_response_with_keywords() -> None:
    analyzer = _create_analyzer()
    raw = _make_gemini_response(keywords=["Quantum Computing", "Error Correction"])
    kw_by_cat = {"quantum": ["Quantum Computing", "Error Correction", "Drug Discovery"]}
    result = analyzer._parse_response(raw, keywords_by_category=kw_by_cat)
    assert result.keywords == ["Quantum Computing", "Error Correction"]


def test_parse_response_filters_invalid_keywords() -> None:
    analyzer = _create_analyzer()
    raw = _make_gemini_response(keywords=["Quantum Computing", "Not A Candidate"])
    kw_by_cat = {"quantum": ["Quantum Computing", "Error Correction"]}
    result = analyzer._parse_response(raw, keywords_by_category=kw_by_cat)
    assert result.keywords == ["Quantum Computing"]


def test_parse_response_keywords_without_candidates() -> None:
    """Keywords in response are ignored when no candidates were provided."""
    analyzer = _create_analyzer()
    raw = _make_gemini_response(keywords=["Quantum Computing"])
    result = analyzer._parse_response(raw, keywords_by_category=None)
    assert result.keywords is None


def test_parse_response_limits_keywords_to_three() -> None:
    analyzer = _create_analyzer()
    raw = _make_gemini_response(keywords=["A", "B", "C", "D"])
    kw_by_cat = {"cat1": ["A", "B"], "cat2": ["C", "D"]}
    result = analyzer._parse_response(raw, keywords_by_category=kw_by_cat)
    assert len(result.keywords) == 3


async def test_analyze_article_saves_keyword_links(
    db_session: AsyncSession,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> None:
    """AI analysis should create article_keywords links for matched keywords."""
    # Create keywords with category_id (1:N)
    cat_id = sample_categories[0].id
    kw1 = Keyword(name="Quantum Computing", category_id=cat_id)
    kw2 = Keyword(name="Error Correction", category_id=cat_id)
    kw3 = Keyword(name="Drug Discovery", category_id=cat_id)
    db_session.add_all([kw1, kw2, kw3])
    await db_session.flush()

    article = NewsArticle(
        original_title="Quantum Error Correction",
        original_url="https://example.com/kw-tag-test",
        news_source_id=sample_source.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    mock_analyzer = MagicMock(spec=BaseAnalyzer)
    mock_analyzer.provider_name = "gemini"
    mock_analyzer.model_name = "gemini-2.0-flash"
    mock_analyzer.analyze = AsyncMock(
        return_value=AnalysisData(
            title="量子エラー訂正",
            summary="要約",
            impact_level=ImpactLevel.HIGH,
            keywords=["Quantum Computing", "Error Correction"],
            reasoning="理由",
        )
    )

    result = await analyze_article(db_session, article, mock_analyzer)
    await db_session.commit()

    assert result is not None

    # Verify keyword links were created
    stmt = select(ArticleKeyword).where(ArticleKeyword.news_article_id == article.id)
    links = (await db_session.execute(stmt)).scalars().all()
    assert len(links) == 2

    linked_kws = set()
    for link in links:
        kw_stmt = select(Keyword).where(Keyword.id == link.keyword_id)
        kw = (await db_session.execute(kw_stmt)).scalar_one()
        linked_kws.add(str(kw.name))
    assert linked_kws == {"Quantum Computing", "Error Correction"}

    # Verify keywords_by_category were passed to analyzer
    call_kwargs = mock_analyzer.analyze.call_args.kwargs
    assert "keywords_by_category" in call_kwargs
    kw_by_cat = call_kwargs["keywords_by_category"]
    all_kws = set()
    for kws in kw_by_cat.values():
        all_kws.update(kws)
    assert all_kws == {
        "Quantum Computing",
        "Error Correction",
        "Drug Discovery",
    }
