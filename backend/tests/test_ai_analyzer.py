"""Tests for the AI analyzer service."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.ai_model import AIModel
from app.models.analysis import AnalysisResult
from app.models.associations import NewsKeyword
from app.models.investment_category import (
    AnalysisInvestmentCategory,
    InvestmentCategory,
)
from app.models.keyword import Keyword
from app.models.keyword_category import KeywordCategory, KeywordCategoryLink
from app.models.news import NewsArticle
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
    sentiment: str = "positive",
    impact_score: int = 8,
    reasoning: str | None = "技術的に重要な進展であり市場に好影響",
    investment_categories: list[str] | None = None,
    keywords: list[str] | None = None,
) -> str:
    """Create a valid JSON string mimicking Gemini API response."""
    data: dict = {
        "title_ja": title_ja,
        "summary_ja": summary_ja,
        "sentiment": sentiment,
        "impact_score": impact_score,
        "reasoning": reasoning,
    }
    if investment_categories is not None:
        data["investment_categories"] = investment_categories
    if keywords is not None:
        data["keywords"] = keywords
    return json.dumps(data, ensure_ascii=False)


def _create_analyzer() -> GeminiAnalyzer:
    """Create a GeminiAnalyzer with mocked settings."""
    with patch("app.services.gemini_analyzer.settings") as mock_gs:
        mock_gs.gemini_api_key = "test-key"
        return GeminiAnalyzer()


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
    assert result.sentiment == "positive"
    assert result.impact_score == 8
    assert result.title == "量子コンピューティングの新たなブレイクスルー"


def test_parse_response_strips_markdown_fences() -> None:
    analyzer = _create_analyzer()
    raw = "```json\n" + _make_gemini_response() + "\n```"
    result = analyzer._parse_response(raw)
    assert result.sentiment == "positive"


def test_parse_response_invalid_json_raises_error() -> None:
    analyzer = _create_analyzer()
    with pytest.raises(AnalysisError, match="Failed to parse"):
        analyzer._parse_response("this is not json")


def test_parse_response_invalid_sentiment_raises_error() -> None:
    analyzer = _create_analyzer()
    raw = _make_gemini_response(sentiment="very_positive")
    with pytest.raises(AnalysisError, match="Invalid"):
        analyzer._parse_response(raw)


def test_parse_response_impact_score_out_of_range() -> None:
    analyzer = _create_analyzer()
    raw = _make_gemini_response(impact_score=15)
    with pytest.raises(AnalysisError, match="out of range"):
        analyzer._parse_response(raw)


def test_parse_response_with_investment_categories() -> None:
    analyzer = _create_analyzer()
    raw = _make_gemini_response(
        investment_categories=["growth_catalyst", "financial_signal"]
    )
    result = analyzer._parse_response(raw)
    assert result.investment_categories == ["growth_catalyst", "financial_signal"]


def test_parse_response_filters_invalid_categories() -> None:
    analyzer = _create_analyzer()
    raw = _make_gemini_response(
        investment_categories=["growth_catalyst", "invalid_slug", "financial_signal"]
    )
    result = analyzer._parse_response(raw)
    assert result.investment_categories == ["growth_catalyst", "financial_signal"]


def test_parse_response_limits_to_three_categories() -> None:
    analyzer = _create_analyzer()
    raw = _make_gemini_response(
        investment_categories=[
            "growth_catalyst",
            "financial_signal",
            "competitive_edge",
            "risk_mitigation",
        ]
    )
    result = analyzer._parse_response(raw)
    assert len(result.investment_categories) == 3


def test_parse_response_no_categories_field() -> None:
    analyzer = _create_analyzer()
    raw = _make_gemini_response()
    result = analyzer._parse_response(raw)
    assert result.investment_categories is None


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
    sample_ai_model: AIModel,
) -> None:
    article = NewsArticle(
        title_original="Quantum Breakthrough",
        url="https://example.com/quantum",
        source="Google News",
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
            sentiment="positive",
            impact_score=8,
            reasoning="テスト理由",
        )
    )

    result = await analyze_article(db_session, article, mock_analyzer)
    await db_session.commit()

    assert result is not None
    assert result.news_article_id == article.id
    assert result.sentiment == "positive"
    assert result.ai_model_id == sample_ai_model.id

    # Verify translation was created
    from app.models.analysis import AnalysisTranslation

    t_stmt = select(AnalysisTranslation).where(
        AnalysisTranslation.analysis_id == result.id
    )
    translation = (await db_session.execute(t_stmt)).scalar_one_or_none()
    assert translation is not None
    assert translation.title == "量子ブレイクスルー"
    assert translation.locale == "ja"


async def test_analyze_article_saves_categories(
    db_session: AsyncSession,
    sample_categories: list[InvestmentCategory],
    sample_ai_model: AIModel,
) -> None:
    article = NewsArticle(
        title_original="AI Chip Launch",
        url="https://example.com/ai-chip",
        source="Google News",
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    mock_analyzer = MagicMock(spec=BaseAnalyzer)
    mock_analyzer.provider_name = "gemini"
    mock_analyzer.model_name = "gemini-2.0-flash"
    mock_analyzer.analyze = AsyncMock(
        return_value=AnalysisData(
            title="AIチップ発売",
            summary="要約テスト",
            sentiment="positive",
            impact_score=9,
            reasoning="テスト理由",
            investment_categories=["growth_catalyst", "competitive_edge"],
        )
    )

    result = await analyze_article(db_session, article, mock_analyzer)
    await db_session.commit()

    assert result is not None

    # Verify category links were created
    stmt = select(AnalysisInvestmentCategory).where(
        AnalysisInvestmentCategory.analysis_id == result.id
    )
    links = (await db_session.execute(stmt)).scalars().all()
    assert len(links) == 2

    linked_slugs = set()
    for link in links:
        cat_stmt = select(InvestmentCategory).where(
            InvestmentCategory.id == link.category_id
        )
        cat = (await db_session.execute(cat_stmt)).scalar_one()
        linked_slugs.add(cat.slug)
    assert linked_slugs == {"growth_catalyst", "competitive_edge"}


async def test_analyze_article_ignores_unknown_category(
    db_session: AsyncSession,
    sample_categories: list[InvestmentCategory],
    sample_ai_model: AIModel,
) -> None:
    article = NewsArticle(
        title_original="Unknown Category Test",
        url="https://example.com/unknown-cat",
        source="Google News",
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    mock_analyzer = MagicMock(spec=BaseAnalyzer)
    mock_analyzer.provider_name = "gemini"
    mock_analyzer.model_name = "gemini-2.0-flash"
    mock_analyzer.analyze = AsyncMock(
        return_value=AnalysisData(
            title="テスト",
            summary="要約",
            sentiment="neutral",
            impact_score=5,
            investment_categories=["nonexistent_slug"],
        )
    )

    result = await analyze_article(db_session, article, mock_analyzer)
    await db_session.commit()

    assert result is not None

    stmt = select(AnalysisInvestmentCategory).where(
        AnalysisInvestmentCategory.analysis_id == result.id
    )
    links = (await db_session.execute(stmt)).scalars().all()
    assert len(links) == 0


async def test_analyze_article_skips_already_analyzed(
    db_session: AsyncSession,
    sample_ai_model: AIModel,
) -> None:
    article = NewsArticle(
        title_original="Old Article",
        url="https://example.com/old",
        source="Google News",
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    existing = AnalysisResult(
        news_article_id=article.id,
        ai_model_id=sample_ai_model.id,
        sentiment="neutral",
        impact_score=5,
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
    sample_ai_model: AIModel,
) -> None:
    articles = []
    for i in range(3):
        a = NewsArticle(
            title_original=f"Article {i}",
            url=f"https://example.com/batch-{i}",
            source="Google News",
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
            sentiment="neutral",
            impact_score=5,
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
    sample_ai_model: AIModel,
) -> None:
    articles = []
    for i in range(3):
        a = NewsArticle(
            title_original=f"Article {i}",
            url=f"https://example.com/err-{i}",
            source="Google News",
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
                sentiment="positive",
                impact_score=7,
            ),
            AnalysisError("API failed"),
            AnalysisData(
                title="成功2",
                summary="要約2",
                sentiment="negative",
                impact_score=3,
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
    sample_ai_model: AIModel,
) -> None:
    article = NewsArticle(
        title_original="Test Article",
        url="https://example.com/integration-test",
        source="Google News",
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    from app.models.analysis import AnalysisTranslation

    analysis = AnalysisResult(
        news_article_id=article.id,
        ai_model_id=sample_ai_model.id,
        sentiment="positive",
        impact_score=8,
        reasoning="テスト理由",
    )
    db_session.add(analysis)
    await db_session.flush()

    translation = AnalysisTranslation(
        analysis_id=analysis.id,
        locale="ja",
        title="テスト記事",
        summary="テスト要約",
    )
    db_session.add(translation)
    await db_session.commit()

    response = await client.get(f"/api/v1/news/{article.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["analysis"] is not None
    assert data["analysis"]["title"] == "テスト記事"
    assert data["analysis"]["sentiment"] == "positive"
    assert data["analysis"]["impactScore"] == 8
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
    raw = _make_gemini_response(
        keywords=["A", "B", "C", "D"]
    )
    kw_by_cat = {"cat1": ["A", "B"], "cat2": ["C", "D"]}
    result = analyzer._parse_response(raw, keywords_by_category=kw_by_cat)
    assert len(result.keywords) == 3


async def test_analyze_article_saves_keyword_links(
    db_session: AsyncSession,
    sample_keyword_categories: list[KeywordCategory],
    sample_ai_model: AIModel,
) -> None:
    """AI analysis should create news_keywords links for matched keywords."""
    # Create keywords in category
    kw1 = Keyword(keyword="Quantum Computing")
    kw2 = Keyword(keyword="Error Correction")
    kw3 = Keyword(keyword="Drug Discovery")
    db_session.add_all([kw1, kw2, kw3])
    await db_session.flush()

    # Link keywords to categories
    for kw in [kw1, kw2, kw3]:
        db_session.add(
            KeywordCategoryLink(
                keyword_id=kw.id,
                category_id=sample_keyword_categories[0].id,
            )
        )
    await db_session.flush()

    article = NewsArticle(
        title_original="Quantum Error Correction",
        url="https://example.com/kw-tag-test",
        source="Test Source",
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
            sentiment="positive",
            impact_score=8,
            keywords=["Quantum Computing", "Error Correction"],
        )
    )

    result = await analyze_article(db_session, article, mock_analyzer)
    await db_session.commit()

    assert result is not None

    # Verify keyword links were created
    stmt = select(NewsKeyword).where(NewsKeyword.news_article_id == article.id)
    links = (await db_session.execute(stmt)).scalars().all()
    assert len(links) == 2

    linked_kws = set()
    for link in links:
        kw_stmt = select(Keyword).where(Keyword.id == link.keyword_id)
        kw = (await db_session.execute(kw_stmt)).scalar_one()
        linked_kws.add(kw.keyword)
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
