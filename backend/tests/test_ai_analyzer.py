"""AI Extractor / Classifier / Service のテスト。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr, ValidationError
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
from app.analysis.classifier.base import BaseClassifier
from app.analysis.classifier.gemini import GeminiClassifier
from app.analysis.classifier.schema import (
    ClassificationResponse,
    Classified,
    OutOfScope,
    ValidCategory,
)
from app.analysis.extraction.extractor.base import BaseExtractor
from app.analysis.extraction.extractor.gemini import GeminiExtractor
from app.analysis.extraction.schema import EntityResponse, ExtractionResponse
from app.analysis.extraction.service import ExtractionService
from app.domain.entity import EntityName, EntityType
from app.domain.topic import TopicName
from app.models.article import Article
from app.models.article_analysis import ArticleAnalysis, ImpactLevel
from app.models.article_entity import ArticleEntity
from app.models.article_extraction import ArticleExtraction
from app.models.article_rejection import ArticleRejection
from app.models.category import Category
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource
from app.models.topic import Topic

# --- Helpers ---


def _make_extraction_response(
    title_ja: str = "量子コンピューティングの新たなブレイクスルー",
    summary_ja: str = "MITが新手法を発表。量子エラー訂正の分野で大きな進展。",
    entities: list[tuple[str, str]] | None = None,
) -> ExtractionResponse:
    """ExtractionResponse を生成するヘルパー。"""
    if entities is None:
        entities = [
            ("MIT", "company"),
            ("Quantum LDPC", "technology"),
        ]
    return ExtractionResponse(
        title_ja=title_ja,
        summary_ja=summary_ja,
        entities=[
            EntityResponse(name=EntityName(n), type=EntityType(t)) for n, t in entities
        ],
    )


def _make_classified(
    category: ValidCategory = ValidCategory.COMPUTING,
    topic: str = "quantum computing breakthrough",
    topic_label_ja: str = "量子コンピューティング進展",
    impact_level: ImpactLevel = ImpactLevel.HIGH,
    reasoning: str = "技術的に重要な進展",
) -> Classified:
    """Classified を生成するヘルパー。"""
    return Classified(
        category=category,
        topic=TopicName(topic),
        topic_label_ja=topic_label_ja,
        impact_level=impact_level,
        reasoning=reasoning,
    )


def _create_extractor() -> GeminiExtractor:
    """settings をモックして GeminiExtractor を生成する。"""
    with patch("app.analysis.extraction.extractor.gemini.settings") as mock_gs:
        mock_gs.gemini_api_key = SecretStr("test-key")
        return GeminiExtractor()


def _create_classifier() -> GeminiClassifier:
    """settings をモックして GeminiClassifier を生成する。"""
    with patch("app.analysis.classifier.gemini.settings") as mock_gs:
        mock_gs.gemini_api_key = SecretStr("test-key")
        return GeminiClassifier()


async def _create_article_with_extraction(
    db_session: AsyncSession,
    source: NewsSource,
    *,
    url: str,
    title: str = "Test Article",
    translated_title: str = "テスト記事",
    summary: str = "要約テスト",
) -> tuple[Article, ArticleExtraction]:
    """Stage 1 完了済みの記事（article + extraction）を作成するヘルパー。"""
    discovered = DiscoveredArticle(
        original_title=title,
        original_url=url,
        news_source_id=source.id,
    )
    db_session.add(discovered)
    await db_session.flush()
    article = Article(
        discovered_article_id=discovered.id,
        original_title=title,
        original_content="Content.",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()
    extraction = ArticleExtraction(
        article_id=article.id,
        translated_title=translated_title,
        summary=summary,
        ai_model="gemini-2.5-flash-lite",
    )
    db_session.add(extraction)
    await db_session.flush()
    return article, extraction


# --- A. Factory tests ---


def test_get_extractor_returns_gemini_by_default() -> None:
    with patch("app.analysis.extraction.extractor.factory.settings") as mock_settings:
        mock_settings.ai_provider = "gemini"
        with patch("app.analysis.extraction.extractor.gemini.settings") as mock_gs:
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
    with patch("app.analysis.extraction.extractor.factory.settings") as mock_settings:
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

            async def extract(self, title, content): ...

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


# --- B. ExtractionResponse schema tests ---


def test_extraction_response_preserves_entity_name_case() -> None:
    resp = ExtractionResponse(
        title_ja="t",
        summary_ja="s",
        entities=[
            EntityResponse(name=EntityName("NVIDIA"), type=EntityType("Company")),
        ],
    )
    assert resp.entities[0].name.root == "NVIDIA"
    assert resp.entities[0].type.root == "company"


def test_extraction_response_deduplicates_entities_case_insensitive() -> None:
    resp = ExtractionResponse(
        title_ja="t",
        summary_ja="s",
        entities=[
            EntityResponse(name=EntityName("TSMC"), type=EntityType("company")),
            EntityResponse(name=EntityName("tsmc"), type=EntityType("COMPANY")),
        ],
    )
    assert len(resp.entities) == 1
    assert resp.entities[0].name.root == "TSMC"


def test_extraction_response_accepts_any_entity_type() -> None:
    resp = ExtractionResponse(
        title_ja="t",
        summary_ja="s",
        entities=[
            EntityResponse(name=EntityName("MIT"), type=EntityType("company")),
            EntityResponse(name=EntityName("Biden"), type=EntityType("person")),
        ],
    )
    assert len(resp.entities) == 2
    assert resp.entities[1].type.root == "person"


def test_extraction_response_rejects_empty_title() -> None:
    with pytest.raises(ValidationError):
        ExtractionResponse(
            title_ja="",
            summary_ja="s",
            entities=[],
        )


def test_entity_name_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        EntityName("  ")


def test_entity_type_normalizes_lowercase() -> None:
    etype = EntityType("COMPANY")
    assert etype.root == "company"


# --- C. Classification schema tests ---


def test_classified_valid() -> None:
    resp = Classified(
        category=ValidCategory.COMPUTING,
        topic=TopicName("quantum computing breakthrough"),
        topic_label_ja="量子コンピューティング進展",
        impact_level=ImpactLevel.HIGH,
        reasoning="理由",
    )
    assert resp.category == ValidCategory.COMPUTING
    assert resp.topic.root == "quantum computing breakthrough"
    assert resp.impact_level == ImpactLevel.HIGH


def test_classified_normalizes_topic() -> None:
    resp = Classified(
        category=ValidCategory.COMPUTING,
        topic=TopicName("Quantum Computing Breakthrough"),
        topic_label_ja="量子コンピューティング進展",
        impact_level=ImpactLevel.HIGH,
        reasoning="理由",
    )
    assert resp.topic.root == "quantum computing breakthrough"


def test_classified_rejects_invalid_category() -> None:
    with pytest.raises(ValidationError):
        Classified.model_validate(
            {
                "category": "invalid_category",
                "topic": "foo bar",
                "topic_label_ja": "ラベル",
                "impact_level": "high",
                "reasoning": "r",
            }
        )


def test_classified_rejects_invalid_impact_level() -> None:
    with pytest.raises(ValidationError):
        Classified.model_validate(
            {
                "category": "computing",
                "topic": "foo bar",
                "topic_label_ja": "ラベル",
                "impact_level": "extreme",
                "reasoning": "r",
            }
        )


def test_out_of_scope_valid() -> None:
    resp = OutOfScope(reasoning="技術的な先端要素を含まない")
    assert resp.reasoning == "技術的な先端要素を含まない"


def test_out_of_scope_rejects_empty_reasoning() -> None:
    with pytest.raises(ValidationError):
        OutOfScope(reasoning="")


# --- D. BaseExtractor._call_once tests ---


async def test_extractor_call_once_succeeds() -> None:
    extractor = _create_extractor()
    expected = _make_extraction_response()
    extractor._call_api = AsyncMock(return_value=expected)

    result = await extractor._call_once("test prompt")
    assert result is expected


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
    expected: ClassificationResponse = _make_classified()
    classifier._call_api = AsyncMock(return_value=expected)

    result = await classifier._call_once("test prompt")
    assert result is expected


async def test_classifier_call_once_translates_sdk_error() -> None:
    classifier = _create_classifier()
    classifier._call_api = AsyncMock(side_effect=ConnectionError("timeout"))

    with pytest.raises(NetworkError):
        await classifier._call_once("test prompt")


# --- E2. Domain model unit tests (DB 不要) ---


def test_article_extraction_from_response_sanitizes_html() -> None:
    extraction = ArticleExtraction.from_extraction_response(
        article_id=1,
        response=ExtractionResponse(
            title_ja="<b>タイトル</b>",
            summary_ja="<p>要約</p>",
            entities=[
                EntityResponse(name=EntityName("MIT"), type=EntityType("company"))
            ],
        ),
        model_name="test-model",
    )
    assert extraction.translated_title == "タイトル"
    assert extraction.summary == "要約"
    assert extraction.ai_model == "test-model"
    assert extraction.article_id == 1


def test_article_extraction_from_response_builds_entities() -> None:
    extraction = ArticleExtraction.from_extraction_response(
        article_id=1,
        response=ExtractionResponse(
            title_ja="タイトル",
            summary_ja="要約",
            entities=[
                EntityResponse(name=EntityName("MIT"), type=EntityType("company")),
                EntityResponse(
                    name=EntityName("CRISPR"), type=EntityType("technology")
                ),
            ],
        ),
        model_name="test-model",
    )
    assert len(extraction.entities) == 2
    assert extraction.entities[0].name == "MIT"
    assert extraction.entities[0].type == "company"
    assert extraction.entities[1].name == "CRISPR"
    assert extraction.entities[1].type == "technology"


def test_article_extraction_from_response_empty_string_guard() -> None:
    extraction = ArticleExtraction.from_extraction_response(
        article_id=1,
        response=ExtractionResponse(
            title_ja="<br/>",
            summary_ja="<br/>",
            entities=[],
        ),
        model_name="test-model",
    )
    assert extraction.translated_title == ""
    assert extraction.summary == ""
    assert extraction.entities == []


# --- F. ExtractionService orchestration tests ---


async def test_extraction_creates_extraction_and_entities(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    discovered = DiscoveredArticle(
        original_title="Quantum Breakthrough",
        original_url="https://example.com/quantum",
        news_source_id=sample_source.id,
    )
    db_session.add(discovered)
    await db_session.flush()
    article = Article(
        discovered_article_id=discovered.id,
        original_title="Quantum Breakthrough",
        original_content="Full content here.",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    mock_extractor = MagicMock(spec=BaseExtractor)
    mock_extractor.MODEL = "gemini-2.5-flash-lite"
    mock_extractor.model_name = "gemini-2.5-flash-lite"
    mock_extractor.extract = AsyncMock(
        return_value=_make_extraction_response(
            title_ja="量子ブレイクスルー",
            summary_ja="要約テスト",
            entities=[("MIT", "company"), ("CRISPR", "technology")],
        )
    )

    article_id = article.id
    svc = ExtractionService(session_factory)
    result = await svc.execute(article_id, mock_extractor)

    assert result.status == "created"
    assert result.extraction_id is not None

    db_session.expire_all()
    extraction = (
        await db_session.execute(
            select(ArticleExtraction).where(
                ArticleExtraction.article_id == article_id,
            )
        )
    ).scalar_one()
    assert extraction.translated_title == "量子ブレイクスルー"

    entities = list(
        (
            await db_session.execute(
                select(ArticleEntity).where(
                    ArticleEntity.article_extraction_id == extraction.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(entities) == 2


async def test_extraction_skips_already_extracted(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    _, _ = await _create_article_with_extraction(
        db_session, sample_source, url="https://example.com/old", title="Old Article"
    )
    article = (await db_session.execute(select(Article).limit(1))).scalar_one()
    await db_session.commit()

    mock_extractor = MagicMock(spec=BaseExtractor)
    svc = ExtractionService(session_factory)
    result = await svc.execute(article.id, mock_extractor)

    assert result.status == "already_exists"
    mock_extractor.extract.assert_not_called()


async def test_extraction_returns_skipped_on_invalid_input(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    discovered = DiscoveredArticle(
        original_title="Bad Article",
        original_url="https://example.com/bad",
        news_source_id=sample_source.id,
    )
    db_session.add(discovered)
    await db_session.flush()
    article = Article(
        discovered_article_id=discovered.id,
        original_title="Bad Article",
        original_content="Bad content.",
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


# --- G. ClassificationService orchestration tests ---


async def test_classification_creates_topic(
    db_session: AsyncSession,
    session_factory,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> None:
    """Stage 1 完了後の記事に対して Stage 2 が Topic を作成し analysis を生成する。"""
    article, extraction = await _create_article_with_extraction(
        db_session,
        sample_source,
        url="https://example.com/classify-test",
        title="Quantum Breakthrough",
        translated_title="量子ブレイクスルー",
    )
    entity = ArticleEntity(
        article_extraction_id=extraction.id, name="MIT", type="company"
    )
    db_session.add(entity)
    await db_session.commit()

    mock_classifier = MagicMock(spec=BaseClassifier)
    mock_classifier.MODEL = "gemini-2.5-flash-lite"
    mock_classifier.model_name = "gemini-2.5-flash-lite"
    mock_classifier.classify = AsyncMock(
        return_value=_make_classified(
            category=ValidCategory.COMPUTING,
            topic="quantum computing breakthrough",
            impact_level=ImpactLevel.HIGH,
            reasoning="理由テスト",
        )
    )

    article_id = article.id
    extraction_id = extraction.id
    svc = ClassificationService(session_factory)
    result = await svc.execute(article_id, mock_classifier)
    assert result.status == "classified"

    db_session.expire_all()
    analysis = (
        await db_session.execute(
            select(ArticleAnalysis).where(
                ArticleAnalysis.extraction_id == extraction_id
            )
        )
    ).scalar_one()
    assert analysis.topic_id is not None
    assert analysis.impact_level == ImpactLevel.HIGH
    assert analysis.reasoning == "理由テスト"

    topic = (
        await db_session.execute(select(Topic).where(Topic.id == analysis.topic_id))
    ).scalar_one()
    assert str(topic.name) == "quantum computing breakthrough"


async def test_classification_persists_rejection_when_out_of_scope(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    """AI が OutOfScope を返したときに Rejection が永続化されチェーンが止まる。"""
    article, extraction = await _create_article_with_extraction(
        db_session,
        sample_source,
        url="https://example.com/out-of-scope",
        title="Sports News",
        translated_title="スポーツニュース",
    )
    await db_session.commit()

    mock_classifier = MagicMock(spec=BaseClassifier)
    mock_classifier.MODEL = "gemini-2.5-flash-lite"
    mock_classifier.model_name = "gemini-2.5-flash-lite"
    mock_classifier.classify = AsyncMock(
        return_value=OutOfScope(reasoning="先端技術の話題ではない")
    )

    extraction_id = extraction.id
    svc = ClassificationService(session_factory)
    result = await svc.execute(article.id, mock_classifier)
    assert result.status == "rejected"

    db_session.expire_all()
    rejection = (
        await db_session.execute(
            select(ArticleRejection).where(
                ArticleRejection.extraction_id == extraction_id
            )
        )
    ).scalar_one()
    assert rejection.reasoning == "先端技術の話題ではない"
    analysis = (
        await db_session.execute(
            select(ArticleAnalysis).where(
                ArticleAnalysis.extraction_id == extraction_id
            )
        )
    ).scalar_one_or_none()
    assert analysis is None


async def test_classification_skips_already_classified(
    db_session: AsyncSession,
    session_factory,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> None:
    topic = Topic(
        name="existing topic",
        label_ja="既存トピック",
        category_id=sample_categories[0].id,
    )
    db_session.add(topic)
    await db_session.flush()

    article, extraction = await _create_article_with_extraction(
        db_session,
        sample_source,
        url="https://example.com/already-classified",
        title="Classified Article",
        translated_title="分類済みタイトル",
        summary="分類済み要約",
    )
    analysis = ArticleAnalysis(
        extraction_id=extraction.id,
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


async def test_classification_skips_already_rejected(
    db_session: AsyncSession,
    session_factory,
    sample_source: NewsSource,
) -> None:
    article, extraction = await _create_article_with_extraction(
        db_session,
        sample_source,
        url="https://example.com/already-rejected",
        title="Rejected Article",
    )
    rejection = ArticleRejection(
        extraction_id=extraction.id,
        reasoning="対象外",
        ai_model="gemini-2.5-flash-lite",
    )
    db_session.add(rejection)
    await db_session.commit()

    mock_classifier = MagicMock(spec=BaseClassifier)
    svc = ClassificationService(session_factory)
    result = await svc.execute(article.id, mock_classifier)

    assert result.status == "already_rejected"
    mock_classifier.classify.assert_not_called()


# --- H. Integration test (API response) ---


async def test_news_endpoint_includes_analysis(
    client,
    db_session: AsyncSession,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> None:
    topic = Topic(
        name="integration test",
        label_ja="統合テスト",
        category_id=sample_categories[0].id,
    )
    db_session.add(topic)
    await db_session.flush()

    _, extraction = await _create_article_with_extraction(
        db_session,
        sample_source,
        url="https://example.com/integration-test",
        title="Test Article",
        translated_title="テスト記事",
        summary="テスト要約",
    )
    analysis = ArticleAnalysis(
        extraction_id=extraction.id,
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
