"""AssessmentRepository の DB 境界テスト。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.domain.result import (
    InScope,
    InScopeCategory,
    KeyPoint,
    Mention,
    MentionType,
    OutOfScope,
)
from app.analysis.assessment.repository import (
    AssessmentRepository,
    CategoryEnumDatabaseMismatchError,
    missing_category_slugs,
)
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import (
    AnalyzedArticleRecord as AnalyzedArticleRecordORM,
)
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.news_source import NewsSource
from app.models.out_of_scope_article_record import (
    OutOfScopeArticleRecord as OutOfScopeArticleRecordORM,
)

_AI_MODEL = "gemini-2.5-flash-lite"


async def _make_extraction(
    db_session: AsyncSession,
    sample_source: NewsSource,
    *,
    url: str = "https://e.com/repo-test",
) -> ArticleCuration:
    article = AnalyzableArticleRecord(
        source_id=sample_source.id,
        source_url=url,  # type: ignore[arg-type]
        original_title="Original",
        original_content="c" * 100,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    extraction = ArticleCuration(
        analyzable_article_id=article.id,
        translated_title="抽出タイトル",
        summary="抽出要約",
    )
    db_session.add(extraction)
    await db_session.commit()
    await db_session.refresh(extraction)
    return extraction


def _ready(extraction: ArticleCuration) -> ReadyForAssessment:
    return ReadyForAssessment(
        curation_id=extraction.id,
        translated_title=extraction.translated_title,
        summary=extraction.summary,
        analyzable_article_id=extraction.analyzable_article_id,
    )


def _in_scope_call(
    *,
    category: InScopeCategory = InScopeCategory.AI,
    key_points: list[KeyPoint] | None = None,
) -> AssessmentCall[InScope]:
    return AssessmentCall(
        result=InScope(
            category=category,
            investor_take="bullish",
            key_points=key_points or [],
        ),
        raw_response='{"category":"ai"}',
        raw_category=category.value,
        prompt_version="testver1",
        model_name=_AI_MODEL,
    )


def _in_scope_article(
    extraction: ArticleCuration,
    *,
    title: str | None = None,
    summary: str | None = None,
    category: InScopeCategory = InScopeCategory.AI,
    key_points: list[KeyPoint] | None = None,
) -> InScopeAnalyzedArticle:
    return InScopeAnalyzedArticle(
        curation_id=extraction.id,
        title=title or extraction.translated_title,
        summary=summary or extraction.summary,
        assessment_result=InScope(
            category=category,
            investor_take="bullish",
            key_points=key_points or [],
        ),
    )


def _out_of_scope_call(
    *,
    investor_take: str = "not relevant",
    key_points: list[KeyPoint] | None = None,
) -> AssessmentCall[OutOfScope]:
    return AssessmentCall(
        result=OutOfScope(investor_take=investor_take, key_points=key_points or []),
        raw_response='{"category":"out_of_scope"}',
        raw_category="out_of_scope",
        prompt_version="testver1",
        model_name=_AI_MODEL,
    )


@pytest.mark.asyncio
async def test_save_out_of_scope_persists_snapshot_fields(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """``save_out_of_scope`` 成功時、snapshot が DB row に反映され戻り値は新規 id。"""
    extraction = await _make_extraction(db_session, sample_source)
    repo = AssessmentRepository(db_session)

    saved_id = await repo.save_out_of_scope(
        _out_of_scope_call(),
        ready=_ready(extraction),
    )
    await db_session.commit()

    assert isinstance(saved_id, int) and saved_id > 0

    row = (
        await db_session.execute(
            select(OutOfScopeArticleRecordORM).where(
                OutOfScopeArticleRecordORM.curation_id == extraction.id
            )
        )
    ).scalar_one()
    assert row.id == saved_id
    assert row.translated_title == extraction.translated_title
    assert row.summary == extraction.summary
    assert row.investor_take == "not relevant"


@pytest.mark.asyncio
async def test_save_out_of_scope_returns_none_on_race_lost(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """既存 row 存在時 ``save_out_of_scope`` は ``None`` を返し、勝者 row は残る。"""
    extraction = await _make_extraction(db_session, sample_source)
    # 勝者を先に焼く
    winner = OutOfScopeArticleRecordORM(
        curation_id=extraction.id,
        translated_title="勝者タイトル",
        summary="勝者要約",
        investor_take="winner take",
    )
    db_session.add(winner)
    await db_session.commit()

    repo = AssessmentRepository(db_session)
    saved_id = await repo.save_out_of_scope(
        _out_of_scope_call(investor_take="loser take"),
        ready=ReadyForAssessment(
            curation_id=extraction.id,
            translated_title="敗者タイトル",
            summary="敗者要約",
            analyzable_article_id=extraction.analyzable_article_id,
        ),
    )
    await db_session.commit()

    assert saved_id is None
    # 勝者 row は影響を受けない
    row = (
        await db_session.execute(
            select(OutOfScopeArticleRecordORM).where(
                OutOfScopeArticleRecordORM.curation_id == extraction.id
            )
        )
    ).scalar_one()
    assert row.translated_title == "勝者タイトル"
    assert row.summary == "勝者要約"
    assert row.investor_take == "winner take"


@pytest.mark.asyncio
async def test_save_in_scope_persists_snapshot_fields(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """``save_in_scope`` 成功時、戻り値 ``id`` と DB row が一致する。"""
    extraction = await _make_extraction(db_session, sample_source)
    repo = AssessmentRepository(db_session)

    saved_id = await repo.save_in_scope(
        _in_scope_article(
            extraction,
            title="保存用タイトル",
            summary="保存用要約",
            category=InScopeCategory.AI,
        ),
    )
    await db_session.commit()

    assert saved_id is not None
    assert saved_id > 0

    row = (
        await db_session.execute(
            select(AnalyzedArticleRecordORM).where(
                AnalyzedArticleRecordORM.curation_id == extraction.id
            )
        )
    ).scalar_one()
    assert row.id == saved_id
    assert row.translated_title == "保存用タイトル"
    assert row.summary == "保存用要約"
    assert row.investor_take == "bullish"


@pytest.mark.asyncio
async def test_save_in_scope_returns_none_on_race_lost(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """既存 row 存在時 ``save_in_scope`` は ``None`` を返し、勝者 row は残る。"""
    extraction = await _make_extraction(db_session, sample_source)
    ai_cat = next(c for c in sample_categories if str(c.slug) == "ai")
    winner = AnalyzedArticleRecordORM(
        curation_id=extraction.id,
        translated_title="勝者タイトル",
        summary="勝者要約",
        category_id=ai_cat.id,
        investor_take="winner take",
    )
    db_session.add(winner)
    await db_session.commit()

    repo = AssessmentRepository(db_session)
    saved = await repo.save_in_scope(
        _in_scope_article(extraction, category=InScopeCategory.AI),
    )
    await db_session.commit()

    assert saved is None


@pytest.mark.asyncio
async def test_save_in_scope_persists_key_points_jsonb(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """``save_in_scope`` で key_points が JSONB として保存される (dict のリスト)。"""
    extraction = await _make_extraction(db_session, sample_source)
    repo = AssessmentRepository(db_session)

    key_points = [
        KeyPoint(
            content="Anthropic launched Claude 5.",
            mentions=[
                Mention(surface="Anthropic", type=MentionType.COMPANY),
                Mention(surface="Claude 5", type=MentionType.PRODUCT),
            ],
        )
    ]
    await repo.save_in_scope(
        _in_scope_article(extraction, key_points=key_points),
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(AnalyzedArticleRecordORM).where(
                AnalyzedArticleRecordORM.curation_id == extraction.id
            )
        )
    ).scalar_one()
    assert row.key_points == [
        {
            "content": "Anthropic launched Claude 5.",
            "mentions": [
                {"surface": "Anthropic", "type": "company"},
                {"surface": "Claude 5", "type": "product"},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_save_in_scope_persists_empty_key_points_as_empty_list(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """key_points=[] は ``[]`` として保存される (NULL ではなく)。NULL は旧行のみ。"""
    extraction = await _make_extraction(db_session, sample_source)
    repo = AssessmentRepository(db_session)

    await repo.save_in_scope(_in_scope_article(extraction, key_points=[]))
    await db_session.commit()

    row = (
        await db_session.execute(
            select(AnalyzedArticleRecordORM).where(
                AnalyzedArticleRecordORM.curation_id == extraction.id
            )
        )
    ).scalar_one()
    assert row.key_points == []


@pytest.mark.asyncio
async def test_save_out_of_scope_persists_key_points_jsonb(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """``save_out_of_scope`` でも key_points が JSONB として保存される (対称化)。"""
    extraction = await _make_extraction(db_session, sample_source)
    repo = AssessmentRepository(db_session)

    key_points = [
        KeyPoint(
            content="Off-topic key point content.",
            mentions=[Mention(surface="Someone", type=MentionType.PERSON)],
        )
    ]
    await repo.save_out_of_scope(
        _out_of_scope_call(key_points=key_points),
        ready=_ready(extraction),
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(OutOfScopeArticleRecordORM).where(
                OutOfScopeArticleRecordORM.curation_id == extraction.id
            )
        )
    ).scalar_one()
    assert row.key_points == [
        {
            "content": "Off-topic key point content.",
            "mentions": [{"surface": "Someone", "type": "person"}],
        }
    ]


@pytest.mark.asyncio
async def test_save_out_of_scope_persists_empty_key_points_as_empty_list(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    extraction = await _make_extraction(db_session, sample_source)
    repo = AssessmentRepository(db_session)

    await repo.save_out_of_scope(
        _out_of_scope_call(key_points=[]), ready=_ready(extraction)
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(OutOfScopeArticleRecordORM).where(
                OutOfScopeArticleRecordORM.curation_id == extraction.id
            )
        )
    ).scalar_one()
    assert row.key_points == []


@pytest.mark.asyncio
async def test_save_in_scope_raises_when_category_unknown(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """AI が catalog 未登録の slug を返したら fail-fast (enum↔DB 不整合)。

    ``sample_categories`` fixture を使わない (catalog 未登録状態を作る)。
    """
    extraction = await _make_extraction(db_session, sample_source)
    repo = AssessmentRepository(db_session)

    with pytest.raises(CategoryEnumDatabaseMismatchError) as excinfo:
        await repo.save_in_scope(
            _in_scope_article(extraction, category=InScopeCategory.AI),
        )
    assert excinfo.value.missing == {"ai"}


@pytest.mark.asyncio
async def test_load_ready_build_facts_returns_values_for_unprocessed_curation(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    extraction = await _make_extraction(db_session, sample_source)

    repo = AssessmentRepository(db_session)
    facts = await repo.load_ready_build_facts(extraction.id)

    assert facts is not None
    assert facts.curation_id == extraction.id
    assert facts.analyzable_article_id == extraction.analyzable_article_id
    assert facts.translated_title == extraction.translated_title
    assert facts.summary == extraction.summary
    assert facts.has_analyzed_article is False
    assert facts.has_out_of_scope_article is False


@pytest.mark.asyncio
async def test_load_ready_build_facts_returns_none_when_missing(
    db_session: AsyncSession,
) -> None:
    repo = AssessmentRepository(db_session)
    assert await repo.load_ready_build_facts(999_999) is None


@pytest.mark.asyncio
async def test_load_ready_build_facts_marks_existing_analyzed_article(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    extraction = await _make_extraction(
        db_session,
        sample_source,
        url="https://example.com/assessment-facts-in",
    )
    ai_cat = next(c for c in sample_categories if str(c.slug) == "ai")
    db_session.add(
        AnalyzedArticleRecordORM(
            curation_id=extraction.id,
            translated_title="t",
            summary="s",
            category_id=ai_cat.id,
            investor_take="x",
        )
    )
    await db_session.commit()

    repo = AssessmentRepository(db_session)
    facts = await repo.load_ready_build_facts(extraction.id)

    assert facts is not None
    assert facts.has_analyzed_article is True
    assert facts.has_out_of_scope_article is False


@pytest.mark.asyncio
async def test_load_ready_build_facts_marks_existing_out_of_scope_article(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    extraction = await _make_extraction(
        db_session,
        sample_source,
        url="https://example.com/assessment-facts-out",
    )
    db_session.add(
        OutOfScopeArticleRecordORM(
            curation_id=extraction.id,
            translated_title="t",
            summary="s",
            investor_take="x",
        )
    )
    await db_session.commit()

    repo = AssessmentRepository(db_session)
    facts = await repo.load_ready_build_facts(extraction.id)

    assert facts is not None
    assert facts.has_analyzed_article is False
    assert facts.has_out_of_scope_article is True


# category catalog 整合チェック (enum↔DB)


def test_missing_category_slugs_empty_when_all_present() -> None:
    """全 InScopeCategory が DB slug 集合に在れば欠落なし (純粋関数、DB 不要)。"""
    db_slugs = {category.value for category in InScopeCategory}
    assert missing_category_slugs(db_slugs) == set()


def test_missing_category_slugs_reports_absent_enum_members() -> None:
    """DB 集合に無い enum slug だけが欠落として返る。"""
    db_slugs = {category.value for category in InScopeCategory} - {"ai", "bio"}
    assert missing_category_slugs(db_slugs) == {"ai", "bio"}


@pytest.mark.asyncio
async def test_assert_category_catalog_covers_enum_passes_when_all_seeded(
    db_session: AsyncSession,
) -> None:
    """全 InScopeCategory が categories に在れば raise しない。"""
    for category in InScopeCategory:
        db_session.add(Category(slug=category.value, name=category.value))
    await db_session.commit()

    await AssessmentRepository(db_session).assert_category_catalog_covers_enum()


@pytest.mark.asyncio
async def test_assert_category_catalog_covers_enum_raises_on_missing(
    db_session: AsyncSession,
) -> None:
    """enum の slug が 1 つでも DB に無ければ欠落 slug 付きで raise する。"""
    for category in InScopeCategory:
        if category is InScopeCategory.AI:
            continue
        db_session.add(Category(slug=category.value, name=category.value))
    await db_session.commit()

    repo = AssessmentRepository(db_session)
    with pytest.raises(CategoryEnumDatabaseMismatchError) as excinfo:
        await repo.assert_category_catalog_covers_enum()
    assert excinfo.value.missing == {"ai"}
