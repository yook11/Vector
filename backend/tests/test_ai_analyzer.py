"""AI アナライザーサービスのテスト。"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis import (
    AnalysisData,
    BaseAnalyzer,
    InvalidInputError,
    NetworkError,
    ProviderError,
    get_analyzer,
)
from app.analysis.analyzer.gemini import GeminiAnalyzer
from app.analysis.service import ArticleAnalysisService
from app.models.article_analysis import ArticleAnalysis, ImpactLevel
from app.models.category import Category
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource
from app.models.topic import Topic

# --- Helpers ---


def _make_gemini_response(
    category: str = "quantum",
    topic: str = "quantum computing breakthrough",
    title_ja: str = "量子コンピューティングの新たなブレイクスルー",
    summary_ja: str = (
        "MITが新手法を発表。\n半導体業界に大きな影響。\n投資家にとっては追い風。"
    ),
    impact_level: str = "high",
    reasoning: str | None = "技術的に重要な進展であり市場に好影響",
) -> str:
    """Gemini API レスポンスを模した有効な JSON 文字列を作成する。"""
    data: dict = {
        "category": category,
        "topic": topic,
        "title_ja": title_ja,
        "summary_ja": summary_ja,
        "impact_level": impact_level,
        "reasoning": reasoning,
    }
    return json.dumps(data, ensure_ascii=False)


def _create_analyzer() -> GeminiAnalyzer:
    """settings をモックして GeminiAnalyzer を生成する。"""
    with patch("app.analysis.analyzer.gemini.settings") as mock_gs:
        mock_gs.gemini_api_key = SecretStr("test-key")
        return GeminiAnalyzer()


def _create_article(source: NewsSource) -> NewsArticle:
    """テスト用の NewsArticle を作成する。"""
    return NewsArticle(
        original_title="Quantum Breakthrough",
        original_url="https://example.com/quantum",
        news_source_id=source.id,
        published_at=datetime.now(UTC),
    )


# --- A. Factory tests ---


def test_get_analyzer_returns_gemini_by_default() -> None:
    with patch("app.analysis.analyzer.factory.settings") as mock_settings:
        mock_settings.ai_provider = "gemini"
        with patch("app.analysis.analyzer.gemini.settings") as mock_gs:
            mock_gs.gemini_api_key = SecretStr("test-key")
            analyzer = get_analyzer()
    assert isinstance(analyzer, GeminiAnalyzer)
    assert analyzer.model_name == "gemini-2.5-flash-lite"


def test_get_analyzer_raises_for_unsupported_provider() -> None:
    with patch("app.analysis.analyzer.factory.settings") as mock_settings:
        mock_settings.ai_provider = "unknown"
        with pytest.raises(ValueError, match="Unsupported AI provider"):
            get_analyzer()


# --- A2. ClassVar enforcement tests ---


def test_base_analyzer_rejects_subclass_without_classvar() -> None:
    """MODEL/RPM/RPD を定義しない具象サブクラスは TypeError を送出する。"""
    with pytest.raises(TypeError, match="must define ClassVar"):

        class BadAnalyzer(BaseAnalyzer):
            MODEL = "test"
            RPM = 10
            # RPD は未定義

            async def analyze(
                self,
                title,
                description,
                content=None,
                existing_topics_by_category=None,
            ): ...

            async def _call_api(self, prompt): ...

            def _translate_error(self, exc): ...


# --- B. GeminiAnalyzer._parse_response tests ---


def test_parse_response_valid_json() -> None:
    analyzer = _create_analyzer()
    raw = _make_gemini_response()
    result = analyzer._parse_response(raw)

    assert isinstance(result, AnalysisData)
    assert result.impact_level == ImpactLevel.HIGH
    assert result.title == "量子コンピューティングの新たなブレイクスルー"
    assert result.category_slug == "quantum"
    assert result.topic_name == "quantum computing breakthrough"


def test_parse_response_strips_markdown_fences() -> None:
    analyzer = _create_analyzer()
    raw = "```json\n" + _make_gemini_response() + "\n```"
    result = analyzer._parse_response(raw)
    assert result.impact_level == ImpactLevel.HIGH


def test_parse_response_invalid_json_raises_error() -> None:
    analyzer = _create_analyzer()
    with pytest.raises(ProviderError, match="Failed to parse"):
        analyzer._parse_response("this is not json")


def test_parse_response_invalid_impact_level_raises_error() -> None:
    analyzer = _create_analyzer()
    raw = _make_gemini_response(impact_level="extreme")
    with pytest.raises(ProviderError, match="Invalid"):
        analyzer._parse_response(raw)


# --- C. BaseAnalyzer._call_once tests ---


async def test_call_once_succeeds() -> None:
    analyzer = _create_analyzer()
    analyzer._call_api = AsyncMock(return_value=_make_gemini_response())

    result = await analyzer._call_once("test prompt")
    assert result == _make_gemini_response()
    analyzer._call_api.assert_called_once()


async def test_call_once_translates_sdk_error() -> None:
    """SDK 例外は _translate_error で変換される。"""
    analyzer = _create_analyzer()
    analyzer._call_api = AsyncMock(side_effect=ConnectionError("timeout"))

    with pytest.raises(NetworkError):
        await analyzer._call_once("test prompt")


async def test_call_once_passes_through_domain_error() -> None:
    """_call_api からの AnalysisDomainError はそのまま再送出される。"""
    analyzer = _create_analyzer()
    analyzer._call_api = AsyncMock(side_effect=ProviderError("empty response"))

    with pytest.raises(ProviderError, match="empty response"):
        await analyzer._call_once("test prompt")


# --- D. Orchestration tests (with DB) ---


async def test_analyze_article_creates_analysis(
    db_session: AsyncSession,
    session_factory,
    sample_categories: list[Category],
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
    mock_analyzer.MODEL = "gemini-2.5-flash-lite"
    mock_analyzer.model_name = "gemini-2.5-flash-lite"
    mock_analyzer.analyze = AsyncMock(
        return_value=AnalysisData(
            title="量子ブレイクスルー",
            summary="要約テスト",
            impact_level=ImpactLevel.HIGH,
            reasoning="テスト理由",
            category_slug="quantum",
            topic_name="quantum breakthrough",
        )
    )

    article_id = article.id
    svc = ArticleAnalysisService(session_factory)
    result = await svc.execute(article_id, mock_analyzer)

    assert result.status == "created"
    assert result.analysis_id is not None

    db_session.expire_all()
    stmt = select(ArticleAnalysis).where(
        ArticleAnalysis.news_article_id == article_id,
    )
    analysis = (await db_session.execute(stmt)).scalar_one()
    assert analysis.impact_level == ImpactLevel.HIGH
    assert analysis.translated_title == "量子ブレイクスルー"
    assert analysis.ai_model == "gemini-2.5-flash-lite"
    assert analysis.topic_id is not None


async def test_analyze_article_skips_already_analyzed(
    db_session: AsyncSession,
    session_factory,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> None:
    topic = Topic(name="old topic", category_id=sample_categories[0].id)
    db_session.add(topic)
    await db_session.flush()

    article = NewsArticle(
        original_title="Old Article",
        original_url="https://example.com/old",
        news_source_id=sample_source.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()

    existing = ArticleAnalysis(
        news_article_id=article.id,
        translated_title="既存タイトル",
        summary="既存要約",
        impact_level=ImpactLevel.MEDIUM,
        reasoning="既存理由",
        ai_model="gemini-2.5-flash-lite",
        topic_id=topic.id,
    )
    db_session.add(existing)
    await db_session.commit()

    mock_analyzer = MagicMock(spec=BaseAnalyzer)
    mock_analyzer.MODEL = "gemini-2.5-flash-lite"
    mock_analyzer.model_name = "gemini-2.5-flash-lite"

    svc = ArticleAnalysisService(session_factory)
    result = await svc.execute(article.id, mock_analyzer)

    assert result.status == "already_exists"
    mock_analyzer.analyze.assert_not_called()


async def test_analyze_article_skips_on_invalid_input(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """InvalidInputError は記事を skipped としてマークすべき。"""
    article = NewsArticle(
        original_title="Bad Article",
        original_url="https://example.com/bad",
        news_source_id=sample_source.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    mock_analyzer = MagicMock(spec=BaseAnalyzer)
    mock_analyzer.MODEL = "gemini-2.5-flash-lite"
    mock_analyzer.model_name = "gemini-2.5-flash-lite"
    mock_analyzer.analyze = AsyncMock(
        side_effect=InvalidInputError("too long"),
    )

    article_id = article.id
    svc = ArticleAnalysisService(session_factory)
    result = await svc.execute(article_id, mock_analyzer)

    assert result.status == "skipped"

    # Service は独自セッションで commit しているのでキャッシュを expire
    db_session.expire_all()
    refreshed = await db_session.get(NewsArticle, article_id)
    assert refreshed.skip_content_fetch is True
    assert refreshed.original_content is None


# --- E. Integration test (API response) ---


async def test_news_endpoint_includes_analysis(
    client,
    db_session: AsyncSession,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> None:
    topic = Topic(name="integration test", category_id=sample_categories[0].id)
    db_session.add(topic)
    await db_session.flush()

    article = NewsArticle(
        original_title="Test Article",
        original_url="https://example.com/integration-test",
        news_source_id=sample_source.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()

    analysis = ArticleAnalysis(
        news_article_id=article.id,
        translated_title="テスト記事",
        summary="テスト要約",
        impact_level=ImpactLevel.HIGH,
        reasoning="テスト理由",
        ai_model="gemini-2.5-flash-lite",
        topic_id=topic.id,
    )
    db_session.add(analysis)
    await db_session.commit()
    await db_session.refresh(analysis)

    response = await client.get(f"/api/v1/articles/{analysis.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["translatedTitle"] == "テスト記事"
    assert data["impactLevel"] == "high"
    assert data["original"]["title"] == "Test Article"


# --- F. Topic tagging tests ---


def test_parse_response_extracts_category_and_topic() -> None:
    """category_slug と topic_name が正しくパースされる。"""
    analyzer = _create_analyzer()
    raw = _make_gemini_response(category="ai_ml", topic="large language models")
    result = analyzer._parse_response(raw)
    assert result.category_slug == "ai_ml"
    assert result.topic_name == "large language models"


def test_parse_response_normalizes_topic() -> None:
    """大文字入力が小文字に正規化される。"""
    analyzer = _create_analyzer()
    raw = _make_gemini_response(topic="Quantum Computing Breakthrough")
    result = analyzer._parse_response(raw)
    assert result.topic_name == "quantum computing breakthrough"


def test_parse_response_rejects_invalid_category() -> None:
    """不正なカテゴリで ProviderError を送出する。"""
    analyzer = _create_analyzer()
    raw = _make_gemini_response(category="invalid_category")
    with pytest.raises(ProviderError, match="Invalid category"):
        analyzer._parse_response(raw)


async def test_analyze_article_creates_topic_link(
    db_session: AsyncSession,
    session_factory,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> None:
    """AI 分析は Topic を find-or-create し ArticleAnalysis.topic_id を設定する。"""
    article = NewsArticle(
        original_title="Quantum Error Correction",
        original_url="https://example.com/topic-tag-test",
        news_source_id=sample_source.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    mock_analyzer = MagicMock(spec=BaseAnalyzer)
    mock_analyzer.MODEL = "gemini-2.5-flash-lite"
    mock_analyzer.model_name = "gemini-2.5-flash-lite"
    mock_analyzer.analyze = AsyncMock(
        return_value=AnalysisData(
            title="量子エラー訂正",
            summary="要約",
            impact_level=ImpactLevel.HIGH,
            reasoning="理由",
            category_slug="quantum",
            topic_name="quantum error correction",
        )
    )

    svc = ArticleAnalysisService(session_factory)
    result = await svc.execute(article.id, mock_analyzer)
    assert result.status == "created"

    # ArticleAnalysis.topic_id が設定されていることを確認
    db_session.expire_all()
    stmt = select(ArticleAnalysis).where(
        ArticleAnalysis.id == result.analysis_id,
    )
    analysis = (await db_session.execute(stmt)).scalar_one()
    assert analysis.topic_id is not None

    # Topic レコードが作成されていることを確認
    topic_stmt = select(Topic).where(Topic.id == analysis.topic_id)
    topic = (await db_session.execute(topic_stmt)).scalar_one()
    assert str(topic.name) == "quantum error correction"

    # existing_topics_by_category が analyzer に渡されていることを確認
    call_kwargs = mock_analyzer.analyze.call_args.kwargs
    assert "existing_topics_by_category" in call_kwargs
