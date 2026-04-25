"""AI Extractor / Classifier / Service のテスト。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.classification.service import (
    AlreadyClassifiedOutcome,
    AlreadyRejectedOutcome,
    ClassificationService,
    ClassifiedOutcome,
    RejectedOutcome,
)
from app.analysis.classifier.base import BaseClassifier
from app.analysis.classifier.factory import get_classifier
from app.analysis.classifier.gemini import GeminiClassifier
from app.analysis.classifier.schema import (
    ClassificationResponse,
    Classified,
    OutOfScope,
    ValidCategory,
)
from app.analysis.domain.value_objects.entity import EntityName, EntityType
from app.analysis.domain.value_objects.topic import TopicName
from app.analysis.errors import InvalidInputError, NetworkError, ProviderError
from app.analysis.extraction.domain import Entity, ExtractionResult
from app.analysis.extraction.extractor.base import BaseExtractor
from app.analysis.extraction.extractor.factory import get_extractor
from app.analysis.extraction.extractor.gemini import GeminiExtractor
from app.analysis.extraction.service import ExtractionService
from app.models.article import Article
from app.models.article_analysis import ArticleAnalysis
from app.models.article_entity import ArticleEntity
from app.models.article_extraction import ArticleExtraction
from app.models.article_rejection import ArticleRejection
from app.models.category import Category
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource
from app.models.topic import Topic

# --- Helpers ---


def _make_extraction_result(
    title_ja: str = "量子コンピューティングの新たなブレイクスルー",
    summary_ja: str = "MITが新手法を発表。量子エラー訂正の分野で大きな進展。",
    entities: list[tuple[str, str]] | None = None,
) -> ExtractionResult:
    """ExtractionResult を生成するヘルパー。"""
    if entities is None:
        entities = [
            ("MIT", "company"),
            ("Quantum LDPC", "technology"),
        ]
    return ExtractionResult(
        title_ja=title_ja,
        summary_ja=summary_ja,
        entities=[Entity(name=EntityName(n), type=EntityType(t)) for n, t in entities],
    )


def _make_classified(
    category: ValidCategory = ValidCategory.COMPUTING,
    topic: str = "quantum computing breakthrough",
    topic_label_ja: str = "量子コンピューティング進展",
    reasoning: str = "技術的に重要な進展",
) -> Classified:
    """Classified を生成するヘルパー。"""
    return Classified(
        category=category,
        topic=TopicName(topic),
        topic_label_ja=topic_label_ja,
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


# --- B. ExtractionResult domain tests ---


def test_extraction_result_preserves_entity_name_case() -> None:
    resp = ExtractionResult(
        title_ja="t",
        summary_ja="s",
        entities=[
            Entity(name=EntityName("NVIDIA"), type=EntityType("Company")),
        ],
    )
    assert resp.entities[0].name.root == "NVIDIA"
    assert resp.entities[0].type.root == "company"


def test_extraction_result_deduplicates_entities_case_insensitive() -> None:
    resp = ExtractionResult(
        title_ja="t",
        summary_ja="s",
        entities=[
            Entity(name=EntityName("TSMC"), type=EntityType("company")),
            Entity(name=EntityName("tsmc"), type=EntityType("COMPANY")),
        ],
    )
    assert len(resp.entities) == 1
    assert resp.entities[0].name.root == "TSMC"


def test_extraction_result_accepts_any_entity_type() -> None:
    resp = ExtractionResult(
        title_ja="t",
        summary_ja="s",
        entities=[
            Entity(name=EntityName("MIT"), type=EntityType("company")),
            Entity(name=EntityName("Biden"), type=EntityType("person")),
        ],
    )
    assert len(resp.entities) == 2
    assert resp.entities[1].type.root == "person"


def test_extraction_result_sanitizes_html_in_title_and_summary() -> None:
    resp = ExtractionResult(
        title_ja="<b>タイトル</b>",
        summary_ja="<p>要約</p>",
        entities=[],
    )
    assert resp.title_ja == "タイトル"
    assert resp.summary_ja == "要約"


def test_extraction_result_rejects_empty_title() -> None:
    with pytest.raises(ValidationError):
        ExtractionResult(
            title_ja="",
            summary_ja="s",
            entities=[],
        )


def test_extraction_result_rejects_title_that_becomes_empty_after_sanitize() -> None:
    with pytest.raises(ValidationError):
        ExtractionResult(
            title_ja="<br/>",
            summary_ja="s",
            entities=[],
        )


def test_entity_name_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        EntityName("  ")


def test_entity_type_normalizes_lowercase() -> None:
    etype = EntityType("COMPANY")
    assert etype.root == "company"


# --- B3. TopicName VO tests ---


def test_topic_name_normalizes_hyphen_to_space() -> None:
    assert TopicName("ai-agents").root == "ai agents"
    assert TopicName("AI-Agents").root == "ai agents"
    assert TopicName("post-quantum cryptography").root == "post quantum cryptography"


def test_topic_name_normalizes_underscore_to_space() -> None:
    assert TopicName("generative_ai").root == "generative ai"


def test_topic_name_collapses_consecutive_separators() -> None:
    assert TopicName("ai  --  agents").root == "ai agents"
    assert TopicName("ai---agents").root == "ai agents"


def test_topic_name_accepts_single_word() -> None:
    assert TopicName("llm").root == "llm"
    assert TopicName("6g").root == "6g"


def test_topic_name_accepts_three_words_exactly() -> None:
    assert TopicName("small modular reactor").root == "small modular reactor"
    assert TopicName("post-quantum cryptography").root == "post quantum cryptography"


def test_topic_name_rejects_four_or_more_words() -> None:
    with pytest.raises(ValidationError, match="at most 3 words"):
        TopicName("ai driven business process automation")


def test_topic_name_rejects_four_words_from_hyphen_expansion() -> None:
    with pytest.raises(ValidationError, match="at most 3 words"):
        TopicName("ai-driven-business-process-automation")


def test_topic_name_rejects_stopword_article() -> None:
    with pytest.raises(ValidationError, match="stopwords"):
        TopicName("the llm")


def test_topic_name_rejects_stopword_preposition() -> None:
    with pytest.raises(ValidationError, match="stopwords"):
        TopicName("ai in finance")


# --- C. Classification schema tests ---


def test_classified_valid() -> None:
    resp = Classified(
        category=ValidCategory.COMPUTING,
        topic=TopicName("quantum computing breakthrough"),
        topic_label_ja="量子コンピューティング進展",
        reasoning="理由",
    )
    assert resp.category == ValidCategory.COMPUTING
    assert resp.topic.root == "quantum computing breakthrough"


def test_classified_normalizes_topic() -> None:
    resp = Classified(
        category=ValidCategory.COMPUTING,
        topic=TopicName("Quantum Computing Breakthrough"),
        topic_label_ja="量子コンピューティング進展",
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
    expected = _make_extraction_result()
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


# --- E2. Extraction domain factory tests (DB 不要) ---


def test_extraction_from_result_copies_fields() -> None:
    from app.analysis.extraction.domain import Extraction

    result = ExtractionResult(
        title_ja="タイトル",
        summary_ja="要約",
        entities=[
            Entity(name=EntityName("MIT"), type=EntityType("company")),
            Entity(name=EntityName("CRISPR"), type=EntityType("technology")),
        ],
    )
    extracted_at = datetime.now(UTC)
    extraction = Extraction.from_result(
        result,
        id=42,
        ai_model="test-model",
        extracted_at=extracted_at,
    )

    assert extraction.id == 42
    assert extraction.translated_title == "タイトル"
    assert extraction.summary == "要約"
    assert extraction.ai_model == "test-model"
    assert extraction.extracted_at == extracted_at
    assert len(extraction.entities) == 2
    assert extraction.entities[0].name.root == "MIT"
    assert extraction.entities[1].type.root == "technology"


def test_extraction_rejects_empty_translated_title() -> None:
    from app.analysis.extraction.domain import Extraction

    with pytest.raises(ValueError, match="translated_title"):
        Extraction(
            id=1,
            translated_title="",
            summary="s",
            entities=(),
            ai_model="m",
            extracted_at=datetime.now(UTC),
        )


def test_extraction_rejects_duplicated_entities() -> None:
    from app.analysis.extraction.domain import Extraction

    with pytest.raises(ValueError, match="deduplicated"):
        Extraction(
            id=1,
            translated_title="t",
            summary="s",
            entities=(
                Entity(name=EntityName("MIT"), type=EntityType("company")),
                Entity(name=EntityName("mit"), type=EntityType("company")),
            ),
            ai_model="m",
            extracted_at=datetime.now(UTC),
        )


def test_entity_dedup_key_is_case_insensitive_on_name() -> None:
    a = Entity(name=EntityName("NVIDIA"), type=EntityType("company"))
    b = Entity(name=EntityName("nvidia"), type=EntityType("company"))
    assert a.dedup_key() == b.dedup_key()


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
        return_value=_make_extraction_result(
            title_ja="量子ブレイクスルー",
            summary_ja="要約テスト",
            entities=[("MIT", "company"), ("CRISPR", "technology")],
        )
    )

    article_id = article.id
    svc = ExtractionService(session_factory)
    extraction = await svc.execute(article_id, mock_extractor)

    assert extraction is not None
    assert extraction.id > 0
    assert extraction.translated_title == "量子ブレイクスルー"
    assert len(extraction.entities) == 2

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
    extraction = await svc.execute(article.id, mock_extractor)

    assert extraction is not None  # 冪等ヒットでも chain 継続
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
    extraction = await svc.execute(article_id, mock_extractor)

    assert extraction is None


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
            reasoning="理由テスト",
        )
    )

    article_id = article.id
    extraction_id = extraction.id
    svc = ClassificationService(session_factory)
    result = await svc.execute(article_id, mock_classifier)
    assert isinstance(result, ClassifiedOutcome)

    db_session.expire_all()
    analysis = (
        await db_session.execute(
            select(ArticleAnalysis).where(
                ArticleAnalysis.extraction_id == extraction_id
            )
        )
    ).scalar_one()
    assert analysis.topic_id is not None
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
    assert isinstance(result, RejectedOutcome)

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
        reasoning="既存理由",
        ai_model="gemini-2.5-flash-lite",
        topic_id=topic.id,
    )
    db_session.add(analysis)
    await db_session.commit()

    mock_classifier = MagicMock(spec=BaseClassifier)
    svc = ClassificationService(session_factory)
    result = await svc.execute(article.id, mock_classifier)

    assert isinstance(result, AlreadyClassifiedOutcome)
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

    assert isinstance(result, AlreadyRejectedOutcome)
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
    assert data["original"]["title"] == "Test Article"
