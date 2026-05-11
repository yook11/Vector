"""``AssessmentRepository`` のユニットテスト。

in-scope / out-of-scope 永続化責務 + Ready 構築 (案 3 = 厚い Ready の 1 query
atomic ロード) を 1 class に統合した repository を検証する。
``AssessmentCall`` envelope (``call.model_name`` / ``call.result``) を 1 つ渡せば
業務 INSERT が完結することと、楽観ロック敗北時の戻り値が ``None`` であることを
業務観点で固定する (memory ``feedback_test_invariants_over_change_tracking``)。

検証する不変条件:
- ``try_load_for_assessment`` が precondition (両 assessment 未生成) を満たす場合
  のみ厚い Ready (5 fields) を返し、満たさない場合は ``None`` を返す
- ``save_out_of_scope`` 成功時、戻り値の id が DB 上の主キーと一致し、snapshot
  (``translated_title`` / ``summary`` / ``investor_take``) が DB row に保持される
- ``save_out_of_scope`` 既存 row 存在時 (race lost) には ``None`` を返し、勝者 row
  はそのまま残る
- ``save_in_scope`` 成功時、戻り値 ``id`` が DB 上の主キーと一致する
- ``save_in_scope`` race lost で ``None`` を返す
- ``save_in_scope`` で AI が未登録 slug を返したら ``AssessmentCategoryMissingError``
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.ai.schema import (
    InScope,
    InScopeCategory,
    OutOfScope,
)
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.errors import AssessmentCategoryMissingError
from app.analysis.assessment.repository import AssessmentRepository
from app.analysis.domain.value_objects.topic import TopicName
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
from app.models.category import Category
from app.models.in_scope_assessment import (
    InScopeAssessment as InScopeAssessmentORM,
)
from app.models.news_source import NewsSource
from app.models.out_of_scope_assessment import (
    OutOfScopeAssessment as OutOfScopeAssessmentORM,
)

_AI_MODEL = "gemini-2.5-flash-lite"


async def _make_extraction(
    db_session: AsyncSession, sample_source: NewsSource
) -> ArticleExtraction:
    article = Article(
        source_id=sample_source.id,
        source_url="https://e.com/repo-test",  # type: ignore[arg-type]
        original_title="Original",
        original_content="c" * 100,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    extraction = ArticleExtraction(
        article_id=article.id,
        translated_title="抽出タイトル",
        summary="抽出要約",
        ai_model=_AI_MODEL,
    )
    db_session.add(extraction)
    await db_session.commit()
    await db_session.refresh(extraction)
    return extraction


def _ready(
    extraction: ArticleExtraction, *, source_name: str | None = "Test Source"
) -> ReadyForAssessment:
    return ReadyForAssessment(
        extraction_id=extraction.id,
        translated_title=extraction.translated_title,
        summary=extraction.summary,
        article_id=extraction.article_id,
        source_name=source_name,
    )


def _in_scope_call(
    *, category: InScopeCategory = InScopeCategory.AI
) -> AssessmentCall[InScope]:
    return AssessmentCall(
        result=InScope(
            category=category,
            topic=TopicName("llm benchmark"),
            investor_take="bullish",
        ),
        raw_response='{"category":"ai"}',
        raw_category=category.value,
        raw_topic="llm benchmark",
        prompt_version="testver1",
        model_name=_AI_MODEL,
    )


def _out_of_scope_call(
    *, investor_take: str = "not relevant"
) -> AssessmentCall[OutOfScope]:
    return AssessmentCall(
        result=OutOfScope(investor_take=investor_take),
        raw_response='{"category":"out_of_scope"}',
        raw_category="out_of_scope",
        raw_topic="celebrity gossip",
        prompt_version="testver1",
        model_name=_AI_MODEL,
    )


# ---------------------------------------------------------------------------
# save_out_of_scope
# ---------------------------------------------------------------------------


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
            select(OutOfScopeAssessmentORM).where(
                OutOfScopeAssessmentORM.extraction_id == extraction.id
            )
        )
    ).scalar_one()
    assert row.id == saved_id
    assert row.translated_title == extraction.translated_title
    assert row.summary == extraction.summary
    assert row.investor_take == "not relevant"
    assert row.ai_model == _AI_MODEL


@pytest.mark.asyncio
async def test_save_out_of_scope_returns_none_on_race_lost(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """既存 row 存在時 ``save_out_of_scope`` は ``None`` を返し、勝者 row は残る。"""
    extraction = await _make_extraction(db_session, sample_source)
    # 勝者を先に焼く
    winner = OutOfScopeAssessmentORM(
        extraction_id=extraction.id,
        translated_title="勝者タイトル",
        summary="勝者要約",
        investor_take="winner take",
        ai_model=_AI_MODEL,
    )
    db_session.add(winner)
    await db_session.commit()

    repo = AssessmentRepository(db_session)
    saved_id = await repo.save_out_of_scope(
        _out_of_scope_call(investor_take="loser take"),
        ready=ReadyForAssessment(
            extraction_id=extraction.id,
            translated_title="敗者タイトル",
            summary="敗者要約",
            article_id=extraction.article_id,
            source_name=None,
        ),
    )
    await db_session.commit()

    assert saved_id is None
    # 勝者 row は影響を受けない
    row = (
        await db_session.execute(
            select(OutOfScopeAssessmentORM).where(
                OutOfScopeAssessmentORM.extraction_id == extraction.id
            )
        )
    ).scalar_one()
    assert row.translated_title == "勝者タイトル"
    assert row.summary == "勝者要約"
    assert row.investor_take == "winner take"


# ---------------------------------------------------------------------------
# save_in_scope
# ---------------------------------------------------------------------------


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
        _in_scope_call(category=InScopeCategory.AI),
        ready=_ready(extraction),
    )
    await db_session.commit()

    assert saved_id is not None
    assert saved_id > 0

    row = (
        await db_session.execute(
            select(InScopeAssessmentORM).where(
                InScopeAssessmentORM.extraction_id == extraction.id
            )
        )
    ).scalar_one()
    assert row.id == saved_id
    assert row.translated_title == extraction.translated_title
    assert row.summary == extraction.summary
    assert row.investor_take == "bullish"
    assert row.ai_model == _AI_MODEL


@pytest.mark.asyncio
async def test_save_in_scope_returns_none_on_race_lost(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """既存 row 存在時 ``save_in_scope`` は ``None`` を返し、勝者 row は残る。"""
    extraction = await _make_extraction(db_session, sample_source)
    ai_cat = next(c for c in sample_categories if str(c.slug) == "ai")
    winner = InScopeAssessmentORM(
        extraction_id=extraction.id,
        translated_title="勝者タイトル",
        summary="勝者要約",
        topic="llm benchmark",
        category_id=ai_cat.id,
        investor_take="winner take",
        ai_model=_AI_MODEL,
    )
    db_session.add(winner)
    await db_session.commit()

    repo = AssessmentRepository(db_session)
    saved = await repo.save_in_scope(
        _in_scope_call(category=InScopeCategory.AI),
        ready=_ready(extraction),
    )
    await db_session.commit()

    assert saved is None


@pytest.mark.asyncio
async def test_save_in_scope_raises_when_category_unknown(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """AI が catalog 未登録の slug を返したら fail-fast。

    ``sample_categories`` fixture を使わない (catalog 未登録状態を作る)。
    """
    extraction = await _make_extraction(db_session, sample_source)
    repo = AssessmentRepository(db_session)

    with pytest.raises(AssessmentCategoryMissingError):
        await repo.save_in_scope(
            _in_scope_call(category=InScopeCategory.AI),
            ready=_ready(extraction),
        )


# ---------------------------------------------------------------------------
# try_load_for_assessment (try_advance_from atomic loader)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_load_returns_ready_when_no_assessment(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """行存在 / 両 assessment 不在 → 5 field 揃った Ready を返す。"""
    extraction = await _make_extraction(db_session, sample_source)

    repo = AssessmentRepository(db_session)
    ready = await repo.try_load_for_assessment(extraction.id)

    assert ready is not None
    assert ready.extraction_id == extraction.id
    assert ready.translated_title == extraction.translated_title
    assert ready.summary == extraction.summary
    assert ready.article_id == extraction.article_id
    assert ready.source_name == str(sample_source.name)


@pytest.mark.asyncio
async def test_try_load_returns_none_when_in_scope_exists(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """in_scope_assessment あり → None。"""
    extraction = await _make_extraction(db_session, sample_source)
    ai_cat = next(c for c in sample_categories if str(c.slug) == "ai")
    db_session.add(
        InScopeAssessmentORM(
            extraction_id=extraction.id,
            translated_title="t",
            summary="s",
            topic="llm benchmark",
            category_id=ai_cat.id,
            investor_take="x",
            ai_model=_AI_MODEL,
        )
    )
    await db_session.commit()

    repo = AssessmentRepository(db_session)
    assert await repo.try_load_for_assessment(extraction.id) is None


@pytest.mark.asyncio
async def test_try_load_returns_none_when_out_of_scope_exists(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """out_of_scope_assessment あり → None。"""
    extraction = await _make_extraction(db_session, sample_source)
    db_session.add(
        OutOfScopeAssessmentORM(
            extraction_id=extraction.id,
            translated_title="t",
            summary="s",
            investor_take="x",
            ai_model=_AI_MODEL,
        )
    )
    await db_session.commit()

    repo = AssessmentRepository(db_session)
    assert await repo.try_load_for_assessment(extraction.id) is None


@pytest.mark.asyncio
async def test_try_load_returns_none_for_missing_extraction(
    db_session: AsyncSession,
) -> None:
    repo = AssessmentRepository(db_session)
    assert await repo.try_load_for_assessment(999_999) is None
