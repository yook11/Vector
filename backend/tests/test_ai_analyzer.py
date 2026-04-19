"""AI Extractor / Classifier / Service のテスト。"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis import (
    InvalidInputError,
    NetworkError,
    ProviderError,
    get_classifier,
    get_extractor,
)
from app.analysis.classification_service import ClassificationService
from app.analysis.classifier.base import BaseClassifier, ClassificationData
from app.analysis.classifier.gemini import GeminiClassifier
from app.analysis.extraction_service import ExtractionService
from app.analysis.extractor.base import BaseExtractor, EntityData, ExtractionData
from app.analysis.extractor.gemini import GeminiExtractor
from app.models.article_analysis import ArticleAnalysis, ImpactLevel
from app.models.article_entity import ArticleEntity, EntityType
from app.models.category import Category
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource
from app.models.topic import Topic

# --- Helpers ---


def _make_extraction_response(
    title_ja: str = "量子コンピューティングの新たなブレイクスルー",
    summary_ja: str = "MITが新手法を発表。量子エラー訂正の分野で大きな進展。",
    entities: list[dict] | None = None,
) -> str:
    """GeminiExtractor のレスポンスを模した JSON 文字列を作成する。"""
    if entities is None:
        entities = [
            {"name": "MIT", "type": "company"},
            {"name": "Quantum LDPC", "type": "technology"},
        ]
    return json.dumps(
        {"title_ja": title_ja, "summary_ja": summary_ja, "entities": entities},
        ensure_ascii=False,
    )


def _make_classification_response(
    category: str = "computing",
    topic: str = "quantum computing breakthrough",
    impact_level: str = "high",
    reasoning: str = "技術的に重要な進展",
) -> str:
    """GeminiClassifier のレスポンスを模した JSON 文字列を作成する。"""
    return json.dumps(
        {
            "category": category,
            "topic": topic,
            "impact_level": impact_level,
            "reasoning": reasoning,
        },
        ensure_ascii=False,
    )


def _create_extractor() -> GeminiExtractor:
    """settings をモックして GeminiExtractor を生成する。"""
    with patch("app.analysis.extractor.gemini.settings") as mock_gs:
        mock_gs.gemini_api_key = SecretStr("test-key")
        return GeminiExtractor()


def _create_classifier() -> GeminiClassifier:
    """settings をモックして GeminiClassifier を生成する。"""
    with patch("app.analysis.classifier.gemini.settings") as mock_gs:
        mock_gs.gemini_api_key = SecretStr("test-key")
        return GeminiClassifier()


# --- A. Factory tests ---


def test_get_extractor_returns_gemini_by_default() -> None:
    with patch("app.analysis.extractor.factory.settings") as mock_settings:
        mock_settings.ai_provider = "gemini"
        with patch("app.analysis.extractor.gemini.settings") as mock_gs:
            mock_gs.gemini_api_key = SecretStr("test-key")
            extractor = get_extractor()
    assert isinstance(extractor, GeminiExtractor)
    assert extractor.model_name == "gemini-2.5-flash-lite"


def test_get_classifier_returns_gemini_by_default() -> None:
    with patch("app.analysis.classifier.factory.settings") as mock_settings:
        mock_settings.ai_provider = "gemini"
        with patch("app.analysis.classifier.gemini.settings") as mock_gs:
            mock_gs.gemini_api_key = SecretStr("test-key")
            classifier = get_classifier()
    assert isinstance(classifier, GeminiClassifier)
    assert classifier.model_name == "gemini-2.5-flash-lite"


def test_get_extractor_raises_for_unsupported_provider() -> None:
    with patch("app.analysis.extractor.factory.settings") as mock_settings:
        mock_settings.ai_provider = "unknown"
        with pytest.raises(ValueError, match="Unsupported AI provider"):
            get_extractor()


# --- A2. ClassVar enforcement tests ---


def test_base_extractor_rejects_subclass_without_classvar() -> None:
    with pytest.raises(TypeError, match="must define ClassVar"):

        class BadExtractor(BaseExtractor):
            MODEL = "test"
            RPM = 10
            # RPD は未定義

            async def extract(self, title, description, content=None): ...

            async def _call_api(self, prompt): ...

            def _translate_error(self, exc): ...


def test_base_classifier_rejects_subclass_without_classvar() -> None:
    with pytest.raises(TypeError, match="must define ClassVar"):

        class BadClassifier(BaseClassifier):
            MODEL = "test"
            RPM = 10
            # RPD は未定義

            async def classify(
                self, title_ja, summary_ja, entities, existing_topics_by_category=None
            ): ...

            async def _call_api(self, prompt): ...

            def _translate_error(self, exc): ...


# --- B. GeminiExtractor._parse_response tests ---


def test_extractor_parse_valid_json() -> None:
    extractor = _create_extractor()
    raw = _make_extraction_response()
    result = extractor._parse_response(raw)

    assert isinstance(result, ExtractionData)
    assert result.title_ja == "量子コンピューティングの新たなブレイクスルー"
    assert len(result.entities) == 2
    assert result.entities[0].type == EntityType.COMPANY


def test_extractor_parse_strips_markdown_fences() -> None:
    extractor = _create_extractor()
    raw = "```json\n" + _make_extraction_response() + "\n```"
    result = extractor._parse_response(raw)
    assert result.title_ja == "量子コンピューティングの新たなブレイクスルー"


def test_extractor_parse_invalid_json_raises_error() -> None:
    extractor = _create_extractor()
    with pytest.raises(ProviderError, match="Failed to parse"):
        extractor._parse_response("this is not json")


def test_extractor_parse_deduplicates_entities() -> None:
    extractor = _create_extractor()
    raw = _make_extraction_response(
        entities=[
            {"name": "TSMC", "type": "company"},
            {"name": "tsmc", "type": "company"},
        ]
    )
    result = extractor._parse_response(raw)
    assert len(result.entities) == 1


def test_extractor_parse_skips_invalid_entity_type() -> None:
    extractor = _create_extractor()
    raw = _make_extraction_response(
        entities=[
            {"name": "MIT", "type": "company"},
            {"name": "foo", "type": "invalid_type"},
        ]
    )
    result = extractor._parse_response(raw)
    assert len(result.entities) == 1


# --- C. GeminiClassifier._parse_response tests ---


def test_classifier_parse_valid_json() -> None:
    classifier = _create_classifier()
    raw = _make_classification_response()
    result = classifier._parse_response(raw)

    assert isinstance(result, ClassificationData)
    assert result.category_slug == "computing"
    assert result.topic_name == "quantum computing breakthrough"
    assert result.impact_level == ImpactLevel.HIGH


def test_classifier_parse_normalizes_topic() -> None:
    classifier = _create_classifier()
    raw = _make_classification_response(topic="Quantum Computing Breakthrough")
    result = classifier._parse_response(raw)
    assert result.topic_name == "quantum computing breakthrough"


def test_classifier_parse_rejects_invalid_category() -> None:
    classifier = _create_classifier()
    raw = _make_classification_response(category="invalid_category")
    with pytest.raises(ProviderError, match="Invalid category"):
        classifier._parse_response(raw)


def test_classifier_parse_rejects_invalid_impact_level() -> None:
    classifier = _create_classifier()
    raw = _make_classification_response(impact_level="extreme")
    with pytest.raises(ProviderError, match="Invalid"):
        classifier._parse_response(raw)


# --- D. BaseExtractor._call_once tests ---


async def test_extractor_call_once_succeeds() -> None:
    extractor = _create_extractor()
    extractor._call_api = AsyncMock(return_value=_make_extraction_response())

    result = await extractor._call_once("test prompt")
    assert result == _make_extraction_response()


async def test_extractor_call_once_translates_sdk_error() -> None:
    extractor = _create_extractor()
    extractor._call_api = AsyncMock(side_effect=ConnectionError("timeout"))

    with pytest.raises(NetworkError):
        await extractor._call_once("test prompt")


async def test_extractor_call_once_passes_through_domain_error() -> None:
    extractor = _create_extractor()
    extractor._call_api = AsyncMock(side_effect=ProviderError("empty response"))

    with pytest.raises(ProviderError, match="empty response"):
        await extractor._call_once("test prompt")


# --- E. BaseClassifier._call_once tests ---


async def test_classifier_call_once_succeeds() -> None:
    classifier = _create_classifier()
    classifier._call_api = AsyncMock(return_value=_make_classification_response())

    result = await classifier._call_once("test prompt")
    assert result == _make_classification_response()


async def test_classifier_call_once_translates_sdk_error() -> None:
    classifier = _create_classifier()
    classifier._call_api = AsyncMock(side_effect=ConnectionError("timeout"))

    with pytest.raises(NetworkError):
        await classifier._call_once("test prompt")


# --- F. ExtractionService orchestration tests ---


async def test_extraction_creates_analysis_and_entities(
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

    mock_extractor = MagicMock(spec=BaseExtractor)
    mock_extractor.MODEL = "gemini-2.5-flash-lite"
    mock_extractor.model_name = "gemini-2.5-flash-lite"
    mock_extractor.extract = AsyncMock(
        return_value=ExtractionData(
            title_ja="量子ブレイクスルー",
            summary_ja="要約テスト",
            entities=[
                EntityData(name="MIT", type=EntityType.COMPANY),
                EntityData(name="CRISPR", type=EntityType.TECHNOLOGY),
            ],
        )
    )

    article_id = article.id
    svc = ExtractionService(session_factory)
    result = await svc.execute(article_id, mock_extractor)

    assert result.status == "created"
    assert result.analysis_id is not None

    db_session.expire_all()
    analysis = (
        await db_session.execute(
            select(ArticleAnalysis).where(
                ArticleAnalysis.news_article_id == article_id,
            )
        )
    ).scalar_one()
    assert analysis.translated_title == "量子ブレイクスルー"
    assert analysis.topic_id is None  # Stage 2 未実行

    entities = list(
        (
            await db_session.execute(
                select(ArticleEntity).where(
                    ArticleEntity.article_analysis_id == analysis.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(entities) == 2


async def test_extraction_skips_already_analyzed(
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

    mock_extractor = MagicMock(spec=BaseExtractor)
    svc = ExtractionService(session_factory)
    result = await svc.execute(article.id, mock_extractor)

    assert result.status == "already_exists"
    mock_extractor.extract.assert_not_called()


async def test_extraction_marks_skipped_on_invalid_input(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    article = NewsArticle(
        original_title="Bad Article",
        original_url="https://example.com/bad",
        news_source_id=sample_source.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    mock_extractor = MagicMock(spec=BaseExtractor)
    mock_extractor.extract = AsyncMock(
        side_effect=InvalidInputError("too long"),
    )

    article_id = article.id
    svc = ExtractionService(session_factory)
    result = await svc.execute(article_id, mock_extractor)

    assert result.status == "skipped"

    db_session.expire_all()
    refreshed = await db_session.get(NewsArticle, article_id)
    assert refreshed.skip_content_fetch is True


# --- G. ClassificationService orchestration tests ---


async def test_classification_creates_topic(
    db_session: AsyncSession,
    session_factory,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> None:
    """Stage 1 完了後の記事に対して Stage 2 が Topic を作成し分類を完了する。"""
    article = NewsArticle(
        original_title="Quantum Breakthrough",
        original_url="https://example.com/classify-test",
        news_source_id=sample_source.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()

    # Stage 1 の結果（topic_id なし）
    analysis = ArticleAnalysis(
        news_article_id=article.id,
        translated_title="量子ブレイクスルー",
        summary="要約テスト",
        ai_model="gemini-2.5-flash-lite",
    )
    db_session.add(analysis)
    await db_session.flush()

    entity = ArticleEntity(
        article_analysis_id=analysis.id,
        name="MIT",
        type=EntityType.COMPANY,
    )
    db_session.add(entity)
    await db_session.commit()

    mock_classifier = MagicMock(spec=BaseClassifier)
    mock_classifier.classify = AsyncMock(
        return_value=ClassificationData(
            category_slug="computing",
            topic_name="quantum computing breakthrough",
            impact_level=ImpactLevel.HIGH,
            reasoning="理由テスト",
        )
    )

    article_id = article.id
    analysis_id = analysis.id
    svc = ClassificationService(session_factory)
    result = await svc.execute(article_id, mock_classifier)
    assert result.status == "classified"

    db_session.expire_all()
    updated = (
        await db_session.execute(
            select(ArticleAnalysis).where(ArticleAnalysis.id == analysis_id)
        )
    ).scalar_one()
    assert updated.topic_id is not None
    assert updated.impact_level == ImpactLevel.HIGH
    assert updated.reasoning == "理由テスト"

    topic = (
        await db_session.execute(select(Topic).where(Topic.id == updated.topic_id))
    ).scalar_one()
    assert str(topic.name) == "quantum computing breakthrough"


async def test_classification_skips_already_classified(
    db_session: AsyncSession,
    session_factory,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> None:
    topic = Topic(name="existing topic", category_id=sample_categories[0].id)
    db_session.add(topic)
    await db_session.flush()

    article = NewsArticle(
        original_title="Classified Article",
        original_url="https://example.com/already-classified",
        news_source_id=sample_source.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()

    analysis = ArticleAnalysis(
        news_article_id=article.id,
        translated_title="分類済みタイトル",
        summary="分類済み要約",
        impact_level=ImpactLevel.MEDIUM,
        reasoning="既存理由",
        ai_model="gemini-2.5-flash-lite",
        topic_id=topic.id,
    )
    db_session.add(analysis)
    await db_session.commit()

    mock_classifier = MagicMock(spec=BaseClassifier)
    svc = ClassificationService(session_factory)
    result = await svc.execute(article.id, mock_classifier)

    assert result.status == "already_classified"
    mock_classifier.classify.assert_not_called()


# --- H. Integration test (API response) ---


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
