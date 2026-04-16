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
from app.models.article_keyword import ArticleKeyword
from app.models.category import Category
from app.models.keyword import Keyword
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource

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
    """Gemini API レスポンスを模した有効な JSON 文字列を作成する。"""
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
                keywords_by_category=None,
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
        )
    )

    svc = ArticleAnalysisService(session_factory)
    result = await svc.execute(article.id, mock_analyzer)

    assert result.status == "created"
    assert result.analysis_id is not None

    stmt = select(ArticleAnalysis).where(
        ArticleAnalysis.news_article_id == article.id,
    )
    analysis = (await db_session.execute(stmt)).scalar_one()
    assert analysis.impact_level == ImpactLevel.HIGH
    assert analysis.translated_title == "量子ブレイクスルー"
    assert analysis.ai_model == "gemini-2.5-flash-lite"


async def test_analyze_article_skips_already_analyzed(
    db_session: AsyncSession,
    session_factory,
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
        ai_model="gemini-2.5-flash-lite",
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
        ai_model="gemini-2.5-flash-lite",
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
    """候補が渡されない場合、レスポンス内の keywords は無視される。"""
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
    session_factory,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> None:
    """AI 分析はマッチしたキーワードに対し article_keywords リンクを生成する。"""
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
    mock_analyzer.MODEL = "gemini-2.5-flash-lite"
    mock_analyzer.model_name = "gemini-2.5-flash-lite"
    mock_analyzer.analyze = AsyncMock(
        return_value=AnalysisData(
            title="量子エラー訂正",
            summary="要約",
            impact_level=ImpactLevel.HIGH,
            keywords=["Quantum Computing", "Error Correction"],
            reasoning="理由",
        )
    )

    svc = ArticleAnalysisService(session_factory)
    result = await svc.execute(article.id, mock_analyzer)
    assert result.status == "created"

    # キーワードリンクが作成されていることを確認
    stmt = select(ArticleKeyword).where(
        ArticleKeyword.article_analysis_id == result.analysis_id,
    )
    links = (await db_session.execute(stmt)).scalars().all()
    assert len(links) == 2

    linked_kws = set()
    for link in links:
        kw_stmt = select(Keyword).where(Keyword.id == link.keyword_id)
        kw = (await db_session.execute(kw_stmt)).scalar_one()
        linked_kws.add(str(kw.name))
    assert linked_kws == {"Quantum Computing", "Error Correction"}

    # keywords_by_category が analyzer に渡されていることを確認
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
