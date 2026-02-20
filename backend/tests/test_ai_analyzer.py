"""Tests for the AI analyzer service."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.analysis import AnalysisResult
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
    key_topics: list[str] | None = None,
    reasoning: str | None = "技術的に重要な進展であり市場に好影響",
) -> str:
    """Create a valid JSON string mimicking Gemini API response."""
    data = {
        "title_ja": title_ja,
        "summary_ja": summary_ja,
        "sentiment": sentiment,
        "impact_score": impact_score,
        "key_topics": key_topics or ["量子コンピューティング", "MIT", "超電導"],
        "reasoning": reasoning,
    }
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
    assert result.title_ja == "量子コンピューティングの新たなブレイクスルー"


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


# --- C. GeminiAnalyzer._call_with_retry tests ---


async def test_call_with_retry_succeeds_on_first_attempt() -> None:
    analyzer = _create_analyzer()

    mock_response = MagicMock()
    mock_response.text = _make_gemini_response()

    analyzer._client = MagicMock()
    analyzer._client.aio.models.generate_content = AsyncMock(
        return_value=mock_response
    )

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
            title_ja="量子ブレイクスルー",
            summary_ja="要約テスト",
            sentiment="positive",
            impact_score=8,
            key_topics=["量子"],
            reasoning="テスト理由",
        )
    )

    result = await analyze_article(db_session, article, mock_analyzer)
    await db_session.commit()

    assert result is not None
    assert result.news_article_id == article.id
    assert result.sentiment == "positive"
    assert result.ai_provider == "gemini"

    stmt = select(AnalysisResult).where(
        AnalysisResult.news_article_id == article.id
    )
    db_result = (await db_session.execute(stmt)).scalar_one_or_none()
    assert db_result is not None
    assert db_result.title_ja == "量子ブレイクスルー"


async def test_analyze_article_skips_already_analyzed(
    db_session: AsyncSession,
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
        title_ja="既存の分析",
        summary_ja="既存要約",
        sentiment="neutral",
        impact_score=5,
        ai_provider="gemini",
        ai_model="gemini-2.0-flash",
    )
    db_session.add(existing)
    await db_session.commit()

    mock_analyzer = MagicMock(spec=BaseAnalyzer)
    result = await analyze_article(db_session, article, mock_analyzer)

    assert result is None
    mock_analyzer.analyze.assert_not_called()


async def test_analyze_articles_batch(
    db_session: AsyncSession,
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
            title_ja="テスト",
            summary_ja="要約",
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
                title_ja="成功",
                summary_ja="要約",
                sentiment="positive",
                impact_score=7,
            ),
            AnalysisError("API failed"),
            AnalysisData(
                title_ja="成功2",
                summary_ja="要約2",
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
) -> None:
    article = NewsArticle(
        title_original="Test Article",
        url="https://example.com/integration-test",
        source="Google News",
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    analysis = AnalysisResult(
        news_article_id=article.id,
        title_ja="テスト記事",
        summary_ja="テスト要約",
        sentiment="positive",
        impact_score=8,
        key_topics=["テスト"],
        reasoning="テスト理由",
        ai_provider="gemini",
        ai_model="gemini-2.0-flash",
    )
    db_session.add(analysis)
    await db_session.commit()

    response = await client.get(f"/api/v1/news/{article.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["analysis"] is not None
    assert data["analysis"]["titleJa"] == "テスト記事"
    assert data["analysis"]["sentiment"] == "positive"
    assert data["analysis"]["impactScore"] == 8
    assert "aiModel" not in data["analysis"]
